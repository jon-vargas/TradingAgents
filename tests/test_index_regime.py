"""Tests for index trend × stress regime classification and integration."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

from tradingagents.dataflows.index_regime import (
    build_index_regime_fields,
    classify_index_stress,
    classify_index_trend,
    unknown_macro_snapshot,
)
from tradingagents.dataflows.yfinance_extended import format_macro_context, get_macro_snapshot
from tradingagents.graph.signal_aggregator import compute_signal_summary
from tradingagents.reporting.pdf_generator import extract_confidence_score


class TestIndexTrendClassifier(unittest.TestCase):
    def test_deep_below_200sma_is_bear(self):
        self.assertEqual(
            classify_index_trend({"current": 90, "sma_200": 100, "sma_50": 95}),
            "bear",
        )

    def test_marginally_above_without_50sma_confirmation_is_neutral(self):
        self.assertEqual(
            classify_index_trend({"current": 100.5, "sma_200": 100, "sma_50": 102}),
            "neutral",
        )

    def test_clears_buffer_and_50sma_confirms_bull(self):
        self.assertEqual(
            classify_index_trend({"current": 103, "sma_200": 100, "sma_50": 101}),
            "bull",
        )


class TestIndexStressClassifier(unittest.TestCase):
    def test_vix_elevated_is_stressed(self):
        label, evidence = classify_index_stress(vix_level="elevated", vix_value=28.0)
        self.assertEqual(label, "stressed")
        self.assertEqual(evidence["rule"], "vix_elevated")

    def test_credit_high_is_stressed(self):
        label, evidence = classify_index_stress(credit_stress="high")
        self.assertEqual(label, "stressed")
        self.assertEqual(evidence["rule"], "credit_elevated")

    def test_quiet_when_vix_and_credit_usable(self):
        label, evidence = classify_index_stress(
            vix_level="normal",
            vix_value=18.0,
            credit_stress="normal",
            sector_breadth="healthy",
        )
        self.assertEqual(label, "quiet")
        self.assertEqual(evidence["rule"], "vix_credit_quiet")

    def test_weak_breadth_with_missing_credit_is_stressed(self):
        label, evidence = classify_index_stress(
            vix_level="normal",
            vix_value=18.0,
            credit_stress="unknown",
            sector_breadth="weak",
        )
        self.assertEqual(label, "stressed")
        self.assertEqual(evidence["rule"], "breadth_weak_with_missing_vix_or_credit")

    def test_weak_breadth_with_usable_vix_credit_is_unknown(self):
        label, _ = classify_index_stress(
            vix_level="normal",
            vix_value=18.0,
            credit_stress="normal",
            sector_breadth="very_weak",
        )
        self.assertEqual(label, "unknown")

    def test_all_unknown_inputs(self):
        label, evidence = classify_index_stress()
        self.assertEqual(label, "unknown")
        self.assertEqual(evidence["rule"], "partial_data")


class TestCompoundLabel(unittest.TestCase):
    def test_bull_quiet_label(self):
        fields = build_index_regime_fields("bull", "quiet", {"rule": "vix_credit_quiet"})
        self.assertEqual(fields["index_regime"], "bull_quiet")
        self.assertEqual(fields["index_regime_label"], "Bull / Quiet")
        self.assertEqual(fields["market_regime"], "bull")


class TestMacroSnapshotAsOf(unittest.TestCase):
    def test_invalid_as_of_returns_unknown_without_live_fetch(self):
        with patch("tradingagents.dataflows.yfinance_extended.yf.download") as mock_dl:
            snap = get_macro_snapshot(as_of_date="not-a-date")
            mock_dl.assert_not_called()
        self.assertEqual(snap.get("index_trend"), "unknown")
        self.assertEqual(snap.get("error"), "invalid_as_of_date")

    def test_future_as_of_returns_unknown(self):
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        with patch("tradingagents.dataflows.yfinance_extended.yf.download") as mock_dl:
            snap = get_macro_snapshot(as_of_date=future)
            mock_dl.assert_not_called()
        self.assertEqual(snap.get("error"), "future_as_of_date")

    @patch("tradingagents.dataflows.yfinance_extended.get_cache")
    @patch("tradingagents.dataflows.yfinance_extended.yf.download")
    def test_as_of_uses_date_specific_cache_key(self, mock_dl, mock_get_cache):
        cache = MagicMock()
        cache.get.return_value = None
        mock_get_cache.return_value = cache

        n = 220
        dates = pd.date_range("2025-01-01", periods=n, freq="B")
        close = pd.Series([100.0 + i * 0.1 for i in range(n)], index=dates)
        frame = pd.DataFrame({"Close": close})
        mock_dl.return_value = {
            "^GSPC": frame,
            "^VIX": frame,
            "^TNX": frame,
            "^IRX": frame,
            "DX-Y.NYB": frame,
            "GC=F": frame,
            "HYG": frame,
            "LQD": frame,
            "XLK": frame,
            "XLF": frame,
            "XLV": frame,
            "XLE": frame,
            "XLI": frame,
            "XLC": frame,
            "XLP": frame,
            "XLY": frame,
            "XLU": frame,
            "XLB": frame,
            "XLRE": frame,
        }

        as_of = dates[-10].strftime("%Y-%m-%d")
        snap = get_macro_snapshot(as_of_date=as_of)
        cache.get.assert_called_with("macro_snapshot", as_of)
        self.assertEqual(snap.get("as_of_date"), as_of)
        self.assertIn(snap.get("index_trend"), ("bull", "bear", "neutral"))
        mock_dl.assert_called_once()
        dl_kwargs = mock_dl.call_args.kwargs
        expected_end = (pd.to_datetime(as_of) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertEqual(dl_kwargs.get("end"), expected_end)
        self.assertIn(dl_kwargs.get("start"), (pd.to_datetime(as_of) - pd.Timedelta(days=365)).strftime("%Y-%m-%d"))


class TestSignalAggregatorIndexRegime(unittest.TestCase):
    def _base_state(self):
        return {
            "company_of_interest": "AAPL",
            "trade_date": "2026-08-20",
            "macro_snapshot": {
                "index_trend": "bull",
                "index_stress": "stressed",
                "index_regime_label": "Bull / Stressed",
                "market_regime": "bull",
            },
            "market_report": 'SIGNAL_JSON: {"section":"Market","stance":"bullish","confidence":0.7}',
            "fundamentals_report": 'SIGNAL_JSON: {"section":"Fundamentals","stance":"neutral","confidence":0.5}',
            "news_report": 'SIGNAL_JSON: {"section":"News","stance":"neutral","confidence":0.5}',
            "sentiment_report": 'SIGNAL_JSON: {"section":"Sentiment","stance":"neutral","confidence":0.5}',
        }

    @patch("tradingagents.dataflows.yfinance_extended.get_macro_snapshot")
    def test_uses_state_snapshot_without_refetch(self, mock_get):
        result = compute_signal_summary(self._base_state(), "AAPL")
        mock_get.assert_not_called()
        self.assertEqual(result["index_regime_label"], "Bull / Stressed")
        self.assertIn("Index Regime:", result["text_block"])
        self.assertNotIn("Market Regime: BULL", result["text_block"])

    @patch("tradingagents.dataflows.yfinance_extended.get_macro_snapshot")
    def test_unknown_regime_stays_unknown(self, mock_get):
        state = self._base_state()
        state["macro_snapshot"] = {"index_trend": "unknown", "index_stress": "unknown"}
        result = compute_signal_summary(state, "AAPL")
        mock_get.assert_not_called()
        self.assertEqual(result["market_regime"], "UNKNOWN")


class TestConfidenceHaircut(unittest.TestCase):
    def _buy_state(self, macro_snapshot):
        return {
            "company_of_interest": "AVGO",
            "macro_snapshot": macro_snapshot,
            "market_report": 'SIGNAL_JSON: {"section":"Market","stance":"bullish","confidence":0.8}',
            "fundamentals_report": 'SIGNAL_JSON: {"section":"Fundamentals","stance":"bullish","confidence":0.8}',
            "news_report": 'SIGNAL_JSON: {"section":"News","stance":"bullish","confidence":0.8}',
            "sentiment_report": 'SIGNAL_JSON: {"section":"Sentiment","stance":"bullish","confidence":0.8}',
            "investment_plan": 'SIGNAL_JSON: {"section":"Research","stance":"bullish","confidence":0.8}',
            "trader_investment_plan": 'SIGNAL_JSON: {"section":"Trading Plan","stance":"bullish","confidence":0.8}',
            "final_trade_decision": 'DECISION_JSON: {"decision":"BUY","conviction":"high"}',
            "data_quality_score": 80,
        }

    def test_buy_stressed_haircut_vs_quiet(self):
        quiet = self._buy_state({"index_trend": "bull", "index_stress": "quiet"})
        stressed = self._buy_state({"index_trend": "bull", "index_stress": "stressed"})
        self.assertEqual(
            extract_confidence_score(stressed, "BUY"),
            extract_confidence_score(quiet, "BUY") - 5,
        )

    def test_sell_stressed_matches_sell_quiet(self):
        stressed = self._buy_state({"index_trend": "bull", "index_stress": "stressed"})
        quiet = self._buy_state({"index_trend": "bull", "index_stress": "quiet"})
        self.assertEqual(
            extract_confidence_score(stressed, "SELL"),
            extract_confidence_score(quiet, "SELL"),
        )

    def test_sell_unaffected_by_stress(self):
        state = self._buy_state({"index_trend": "bull", "index_stress": "stressed"})
        buy = extract_confidence_score(state, "BUY")
        sell = extract_confidence_score(state, "SELL")
        self.assertNotEqual(buy, sell)


class TestFormatMacroContext(unittest.TestCase):
    def test_uses_prefetched_snapshot(self):
        snap = unknown_macro_snapshot()
        snap.update(build_index_regime_fields("bull", "stressed", {"rule": "vix_elevated"}))
        snap["indices"] = {"vix": {"current": 30.0}}
        snap["credit_stress"] = "elevated"
        text = format_macro_context(macro_snapshot=snap)
        self.assertIn("Index Regime: Bull / Stressed", text)
        self.assertIn("Index stress elevated", text)


class TestScreeningRegimeCompat(unittest.TestCase):
    def test_get_regime_forwards_scan_date(self):
        engine = __import__(
            "tradingagents.screening.engine", fromlist=["ScreeningEngine"]
        ).ScreeningEngine.__new__(
            __import__(
                "tradingagents.screening.engine", fromlist=["ScreeningEngine"]
            ).ScreeningEngine
        )
        with patch("tradingagents.screening.engine.get_macro_snapshot") as mock_macro:
            mock_macro.return_value = {"market_regime": "bull", "index_trend": "bull"}
            self.assertEqual(engine._get_regime("2026-08-20"), "bull")
            mock_macro.assert_called_once_with(as_of_date="2026-08-20")

    def test_default_regime_strength_unchanged(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        regime_cfg = DEFAULT_CONFIG["screening"]["regime_adjustments"]
        self.assertEqual(regime_cfg["strength"], 0.15)
        self.assertEqual(regime_cfg["strength_when_macro_present"], 0.08)


class TestMacroStatusAPI(unittest.TestCase):
    def test_as_of_invalid_returns_explicit_error(self):
        from fastapi.testclient import TestClient
        from webapp.app import app

        with patch("webapp.app.get_macro_snapshot") as mock_macro:
            mock_macro.return_value = {
                "index_trend": "unknown",
                "index_stress": "unknown",
                "index_regime": "unknown",
                "index_regime_label": "Unknown",
                "market_regime": "unknown",
                "error": "invalid_as_of_date",
            }
            resp = TestClient(app).get("/api/macro/status?as_of_date=not-a-date")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["error"], "invalid_as_of_date")
        self.assertEqual(body["index_trend"], "unknown")
        mock_macro.assert_called_once_with(as_of_date="not-a-date")


class TestGraphMacroSnapshot(unittest.TestCase):
    @patch("tradingagents.reporting.position_action.enforce_position_action")
    @patch("tradingagents.graph.signal_processing.validate_decision")
    @patch("tradingagents.graph.signal_aggregator.compute_signal_summary")
    @patch("tradingagents.dataflows.provenance.get_events")
    @patch("tradingagents.dataflows.yfinance_extended.get_macro_snapshot")
    def test_propagate_freezes_macro_snapshot(
        self,
        mock_get_macro,
        mock_provenance,
        mock_signal_summary,
        mock_validate,
        mock_enforce,
    ):
        from tradingagents.graph.propagation import Propagator
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        frozen = {
            "index_trend": "bull",
            "index_stress": "quiet",
            "index_regime_label": "Bull / Quiet",
            "market_regime": "bull",
            "as_of_date": "2026-08-20",
        }
        mock_get_macro.return_value = frozen
        mock_provenance.return_value = []
        mock_signal_summary.return_value = {"composite": 0.1}
        mock_validate.return_value = {
            "guardrail_triggered": False,
            "final_decision": "HOLD",
        }
        mock_enforce.return_value = {"adjusted": False}

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.debug = False
        graph.ticker = "AAPL"
        graph.risk_profile = "growth"
        graph._llm_usage_tracker = None
        graph.propagator = Propagator(max_recur_limit=50)
        graph.process_signal = lambda raw: raw
        graph._log_state = lambda *args, **kwargs: None

        captured = {}

        def _invoke(state, **kwargs):
            captured.update(state)
            return {**state, "final_trade_decision": "HOLD"}

        graph.graph = MagicMock()
        graph.graph.invoke.side_effect = _invoke

        graph.propagate("AAPL", "2026-08-20")

        mock_get_macro.assert_called_once_with(as_of_date="2026-08-20")
        self.assertEqual(captured.get("macro_snapshot"), frozen)
        self.assertEqual(captured.get("sec_filings_snapshot"), "")


class TestDatabaseSignalSummary(unittest.TestCase):
    def test_persists_index_regime_fields(self):
        from tradingagents.reporting.database import _compute_signal_summary_for_db

        state = {
            "trade_date": "2026-08-20",
            "macro_snapshot": {
                "index_trend": "bull",
                "index_stress": "stressed",
                "index_regime_label": "Bull / Stressed",
                "market_regime": "bull",
            },
            "market_report": 'SIGNAL_JSON: {"section":"Market","stance":"bullish","confidence":0.7}',
            "fundamentals_report": 'SIGNAL_JSON: {"section":"Fundamentals","stance":"neutral","confidence":0.5}',
            "news_report": 'SIGNAL_JSON: {"section":"News","stance":"neutral","confidence":0.5}',
            "sentiment_report": 'SIGNAL_JSON: {"section":"Sentiment","stance":"neutral","confidence":0.5}',
        }
        payload = json.loads(_compute_signal_summary_for_db(state, "AAPL"))
        self.assertEqual(payload["index_regime_label"], "Bull / Stressed")
        self.assertEqual(payload["index_stress"], "stressed")
        self.assertEqual(payload["index_trend"], "bull")


class TestNormalizeIndexRegime(unittest.TestCase):
    def test_legacy_market_regime_only_gets_trend_label(self):
        from tradingagents.dataflows.index_regime import normalize_index_regime

        normalized = normalize_index_regime({"market_regime": "bull"})
        self.assertEqual(normalized["index_trend"], "bull")
        self.assertEqual(normalized["index_regime_label"], "Bull")

    def test_full_snapshot_round_trips(self):
        from tradingagents.dataflows.index_regime import normalize_index_regime

        normalized = normalize_index_regime({
            "index_trend": "bull",
            "index_stress": "stressed",
            "index_regime": "bull_stressed",
            "index_regime_label": "Bull / Stressed",
            "market_regime": "bull",
        })
        self.assertEqual(normalized["index_regime_label"], "Bull / Stressed")


class TestDashboardLiveIndexRegime(unittest.TestCase):
    @patch("webapp.app.get_macro_snapshot")
    @patch("webapp.app.get_db")
    def test_empty_dashboard_returns_live_index_regime(self, mock_get_db, mock_macro):
        mock_macro.return_value = {
            "index_trend": "bull",
            "index_stress": "quiet",
            "index_regime": "bull_quiet",
            "index_regime_label": "Bull / Quiet",
            "market_regime": "bull",
        }
        mock_get_db.return_value.get_recent_analyses.return_value = []

        from webapp.app import dashboard_stats

        payload = dashboard_stats()
        self.assertEqual(payload["index_regime"]["index_regime_label"], "Bull / Quiet")
        self.assertEqual(payload["market_regime"], "BULL")

    @patch("webapp.app.get_macro_snapshot")
    @patch("webapp.app.get_db")
    def test_populated_dashboard_returns_live_index_regime_not_last_report(self, mock_get_db, mock_macro):
        mock_macro.return_value = {
            "index_trend": "neutral",
            "index_stress": "stressed",
            "index_regime": "neutral_stressed",
            "index_regime_label": "Neutral / Stressed",
            "market_regime": "neutral",
        }
        analysis = MagicMock()
        analysis.duration_seconds = 10
        analysis.confidence = 70
        analysis.data_quality_score = 80
        analysis.decision = "BUY"
        analysis.risk_profile = "growth"
        analysis.analysis_mode = "deep"
        analysis.has_sec_snapshot = False
        analysis.has_transcript_snapshot = False
        analysis.report_warnings = "[]"
        analysis.was_correct = None
        analysis.actual_return_7d = None
        analysis.signal_summary = json.dumps({"market_regime": "BULL", "index_regime_label": "Bull / Quiet"})

        mock_get_db.return_value.get_recent_analyses.return_value = [analysis]

        from webapp.app import dashboard_stats

        payload = dashboard_stats()
        self.assertEqual(payload["index_regime"]["index_regime_label"], "Neutral / Stressed")
        self.assertEqual(payload["market_regime"], "NEUTRAL")


if __name__ == "__main__":
    unittest.main()
