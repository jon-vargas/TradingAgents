"""Tests for Deep Perplexity call-budget optimizations."""

from unittest.mock import MagicMock, patch

import pytest

from tradingagents.dataflows.cache import DataCache
from tradingagents.dataflows import interface as iface
from tradingagents.dataflows.perplexity_budget import (
    is_valid_snapshot_payload,
    mark_snapshot_ownership,
    reset_run_budget,
    get_live_count,
)
from tradingagents.dataflows.usage_tracker import (
    UsageTracker,
    get_deep_ceiling,
    get_discovery_reserve,
    get_perplexity_monthly_limit,
)
from tradingagents.reporting.pdf_generator import compute_data_quality_score


@pytest.fixture
def tmp_tracker(tmp_path):
    return UsageTracker(data_dir=str(tmp_path))


def test_cache_key_ignores_empty_optional_args():
    cache = DataCache(enable_disk=False)
    cache.set("perplexity_sec_snapshot", "AAPL", "", data="one")
    assert cache.get("perplexity_sec_snapshot", "AAPL") == "one"
    assert cache.get("perplexity_sec_snapshot", "AAPL", None) == "one"
    assert cache.get("perplexity_sec_snapshot", "AAPL", "   ") == "one"


def test_sec_snapshot_prefetch_and_agent_share_cache_key(tmp_path):
    cache = DataCache(cache_dir=str(tmp_path / "cache"), enable_disk=True)
    tracker = UsageTracker(data_dir=str(tmp_path / "usage"))
    live_calls = []

    def fake_sec(ticker, company_name=None, after_date=None, filing_url=None):
        live_calls.append({"after_date": after_date, "filing_url": filing_url})
        return '{"filings": [{"form": "10-Q"}]}'

    with patch.object(iface, "get_cache", return_value=cache), patch.object(
        iface, "get_tracker", return_value=tracker
    ), patch.object(iface, "get_perplexity_sec_snapshot", side_effect=fake_sec), patch.object(
        iface, "get_config", return_value={"enable_cache": True, "enable_usage_tracking": True, "enable_provenance": False}
    ):
        reset_run_budget(max_live_calls=3)
        first = iface.get_sec_filings_snapshot("AAPL", "Apple", after_date="2025-01-01")
        second = iface.get_sec_filings_snapshot("AAPL", "Apple")
        third = iface.get_sec_filings_snapshot("AAPL", "Apple", after_date="")

    assert len(live_calls) == 1
    assert first == second == third
    assert tracker.get_perplexity_monthly_used() == 1


def test_interface_skips_catalyst_live_when_research_slot_needed(tmp_path):
    cache = DataCache(enable_disk=False)
    tracker = UsageTracker(data_dir=str(tmp_path))
    calls = {"n": 0}

    def fake_catalyst(ticker, company_name=""):
        calls["n"] += 1
        return "should not run"

    with patch.object(iface, "get_cache", return_value=cache), patch.object(
        iface, "get_tracker", return_value=tracker
    ), patch.object(iface, "get_perplexity_catalyst_pipeline", side_effect=fake_catalyst), patch.object(
        iface, "get_config", return_value={"enable_cache": True, "enable_usage_tracking": True}
    ):
        reset_run_budget(max_live_calls=3)
        from tradingagents.dataflows.perplexity_budget import register_live_call, has_live_budget

        register_live_call("get_sec_filings_snapshot")
        register_live_call("get_earnings_transcript_snapshot")
        result = iface.get_catalyst_pipeline("AAPL", "Apple")

    assert calls["n"] == 0
    assert result == ""
    assert tracker.get_perplexity_monthly_used() == 0
    assert has_live_budget("get_deep_research") is True


def test_catalyst_single_cache_layer_no_double_record(tmp_path):
    cache = DataCache(cache_dir=str(tmp_path / "cache"), enable_disk=True)
    tracker = UsageTracker(data_dir=str(tmp_path / "usage"))
    calls = {"n": 0}

    def fake_catalyst(ticker, company_name=""):
        calls["n"] += 1
        return "earnings next week"

    with patch.object(iface, "get_cache", return_value=cache), patch.object(
        iface, "get_tracker", return_value=tracker
    ), patch.object(iface, "get_perplexity_catalyst_pipeline", side_effect=fake_catalyst), patch.object(
        iface, "get_config", return_value={"enable_cache": True, "enable_usage_tracking": True}
    ), patch.object(iface, "_should_fetch_catalyst_live", return_value=True):
        reset_run_budget(max_live_calls=3)
        first = iface.get_catalyst_pipeline("AAPL", "Apple")
        second = iface.get_catalyst_pipeline("AAPL", "Apple")

    assert calls["n"] == 1
    assert first == second == "earnings next week"
    assert tracker.get_perplexity_monthly_used() == 1


def test_deep_mode_tool_selection_omits_prefetched_snapshots():
    from tradingagents.agents.analysts.news_analyst import select_deep_news_tools

    names = [t.name for t in select_deep_news_tools(
        has_sec_snapshot=True,
        has_transcript_snapshot=True,
    )]
    assert names == ["get_deep_research"]
    assert "get_earnings_analysis" not in names
    assert "get_sec_analysis" not in names

    fallback = [t.name for t in select_deep_news_tools(
        has_sec_snapshot=False,
        has_transcript_snapshot=False,
    )]
    assert fallback == [
        "get_deep_research",
        "get_sec_filings_snapshot",
        "get_earnings_transcript_snapshot",
    ]


def test_catalyst_skips_to_reserve_research_slot():
    from tradingagents.dataflows.perplexity_budget import (
        has_live_budget,
        register_live_call,
        reset_run_budget,
        get_live_count,
    )

    reset_run_budget(max_live_calls=3)
    register_live_call("get_sec_filings_snapshot")
    register_live_call("get_earnings_transcript_snapshot")
    assert get_live_count() == 2
    assert has_live_budget("catalyst") is False
    assert has_live_budget("get_deep_research") is True

    register_live_call("get_deep_research")
    assert has_live_budget("get_deep_research") is False
    # Research already spent; still no room for optional catalyst
    assert has_live_budget("catalyst", research_available=True) is False


def test_etf_cap_two_slots_prefetch_and_research():
    from tradingagents.dataflows.perplexity_budget import (
        has_live_budget,
        register_live_call,
        reset_run_budget,
    )

    reset_run_budget(max_live_calls=2)
    register_live_call("get_sec_filings_snapshot")
    assert has_live_budget("catalyst") is False
    assert has_live_budget("get_deep_research") is True


def test_catalyst_allowed_when_research_already_cached():
    from tradingagents.dataflows.perplexity_budget import (
        has_live_budget,
        reset_run_budget,
    )

    reset_run_budget(max_live_calls=3)
    assert has_live_budget("catalyst", research_available=True) is True


def test_news_toolnode_omits_demoted_prose_tools():
    import inspect
    from tradingagents.graph import trading_graph as tg

    source = inspect.getsource(tg.TradingAgentsGraph._create_tool_nodes)
    assert "get_earnings_analysis" not in source
    assert "get_sec_analysis" not in source
    assert "get_deep_research" in source
    assert "get_sec_filings_snapshot" in source



def test_quality_score_skips_transcript_penalty_when_snapshot_present():
    entries = [
        {"vendor": "perplexity", "method": "get_deep_research", "status": "success"},
        {"vendor": "cache", "method": "get_sec_filings_snapshot", "status": "success", "cache_hit": True},
    ]
    without = compute_data_quality_score(
        entries,
        analysis_mode="deep",
        has_transcript_snapshot=False,
        is_commodity_etf=False,
    )
    with_snapshot = compute_data_quality_score(
        entries,
        analysis_mode="deep",
        has_transcript_snapshot=True,
        is_commodity_etf=False,
    )
    assert with_snapshot >= without + 10


def test_usage_limit_reads_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tradingagents.dataflows.usage_tracker._get_perplexity_config",
        lambda: {"monthly_limit": 250, "discovery_reserve": 25},
    )
    assert get_perplexity_monthly_limit() == 250
    assert get_discovery_reserve() == 25
    assert get_deep_ceiling() == 225


def test_discover_refuses_at_reserve(tmp_path):
    tracker = UsageTracker(data_dir=str(tmp_path))
    with patch(
        "tradingagents.dataflows.usage_tracker._get_perplexity_config",
        lambda: {"monthly_limit": 100, "discovery_reserve": 10},
    ):
        tracker.record_use("perplexity", method="discover")
        tracker.record_discover_use(10)
        assert tracker.can_use_discover() is False


def test_prefetched_snapshot_blocks_agent_live_call(tmp_path):
    cache = DataCache(enable_disk=False)
    tracker = UsageTracker(data_dir=str(tmp_path))
    calls = {"n": 0}

    def fake_sec(*args, **kwargs):
        calls["n"] += 1
        return '{"filings": []}'

    with patch.object(iface, "get_cache", return_value=cache), patch.object(
        iface, "get_tracker", return_value=tracker
    ), patch.object(iface, "get_perplexity_sec_snapshot", side_effect=fake_sec), patch.object(
        iface, "get_config", return_value={"enable_cache": True, "enable_usage_tracking": True, "enable_provenance": False}
    ):
        reset_run_budget(max_live_calls=3)
        mark_snapshot_ownership(sec_prefetched=True, block_overlap_prose=True)
        result = iface.get_sec_filings_snapshot("AAPL", "Apple")

    assert calls["n"] == 0
    assert "prefetched" in result.lower()
    assert tracker.get_perplexity_monthly_used() == 0


def test_is_valid_snapshot_payload():
    assert is_valid_snapshot_payload('{"ok": true}')
    assert not is_valid_snapshot_payload("[unavailable]")
    assert not is_valid_snapshot_payload("")
