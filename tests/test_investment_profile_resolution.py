"""Tests for source-aware investment-profile resolution."""

from datetime import datetime, timezone
from unittest.mock import patch

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.utils.investment_profile_resolution import (
    resolve_investment_profile_for_ticker,
)


class FakeDb:
    def __init__(self, metadata=None, watchlist=None):
        self.metadata = metadata
        self.watchlist = watchlist
        self.saved = []

    def get_watchlist(self, _watchlist_id):
        return self.watchlist

    def get_ticker_metadata(self, _ticker):
        return self.metadata

    def save_ticker_metadata(self, **kwargs):
        self.saved.append(kwargs)


def test_explicit_profile_wins_over_watchlist_and_metadata():
    db = FakeDb(
        metadata={"resolved_profile": "large_cap_core", "last_updated": datetime.now(timezone.utc).isoformat()},
        watchlist={"default_investment_profile": "dividend_income"},
    )
    key, profile, source = resolve_investment_profile_for_ticker(
        "TEST",
        explicit_key="high_growth",
        watchlist_id=1,
        db=db,
        config=DEFAULT_CONFIG,
    )
    assert key == "high_growth"
    assert profile["profile_key"] == "high_growth"
    assert source == "explicit"


def test_watchlist_profile_wins_over_metadata():
    db = FakeDb(
        metadata={"resolved_profile": "large_cap_core", "last_updated": datetime.now(timezone.utc).isoformat()},
        watchlist={"default_investment_profile": "commodity_cyclical"},
    )
    key, profile, source = resolve_investment_profile_for_ticker(
        "TEST", watchlist_id=1, db=db, config=DEFAULT_CONFIG
    )
    assert key == "commodity_cyclical"
    assert profile["resolved_from"] == "watchlist"
    assert source == "watchlist"


def test_fresh_metadata_is_used_without_provider_refresh():
    db = FakeDb(
        metadata={"resolved_profile": "large_cap_core", "last_updated": datetime.now(timezone.utc).isoformat()}
    )
    with patch(
        "tradingagents.screening.ticker_resolver.resolve_ticker_metadata"
    ) as refresh:
        key, profile, source = resolve_investment_profile_for_ticker(
            "TEST", db=db, config=DEFAULT_CONFIG
        )
    assert key == "large_cap_core"
    assert profile["resolved_from"] == "metadata_cache"
    assert source == "metadata_cache"
    refresh.assert_not_called()


def test_stale_metadata_refreshes_and_persists():
    db = FakeDb(
        metadata={"resolved_profile": "large_cap_core", "last_updated": "2000-01-01T00:00:00+00:00"}
    )
    refreshed = {
        "ticker": "TEST",
        "resolved_profile": "high_growth",
        "sector": "Technology",
        "industry": "Software",
        "market_cap": 10_000_000_000,
        "market_cap_tier": "large",
        "is_profitable": True,
        "has_dividend": False,
        "beta": 1.4,
        "beta_tier": "high",
        "resolved_preset": "momentum_hunter",
        "asset_class": "equity",
        "country": "US",
    }
    with patch(
        "tradingagents.screening.ticker_resolver.resolve_ticker_metadata",
        return_value=refreshed,
    ):
        key, profile, source = resolve_investment_profile_for_ticker(
            "TEST", db=db, config=DEFAULT_CONFIG
        )
    assert key == "high_growth"
    assert profile["resolved_from"] == "metadata_refresh"
    assert source == "metadata_refresh"
    assert db.saved and db.saved[0]["resolved_profile"] == "high_growth"


def test_resolution_falls_back_to_none_when_refresh_fails():
    db = FakeDb(metadata={})
    with patch(
        "tradingagents.screening.ticker_resolver.resolve_ticker_metadata",
        side_effect=RuntimeError("provider unavailable"),
    ):
        key, profile, source = resolve_investment_profile_for_ticker(
            "TEST", db=db, config=DEFAULT_CONFIG
        )
    assert key is None
    assert profile is None
    assert source == "none"
