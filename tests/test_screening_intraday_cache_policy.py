"""Scheduler intraday screening cache policy tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tradingagents.default_config import DEFAULT_CONFIG


def test_intraday_ttl_only_skips_invalidation():
    from webapp.screening_scheduler import ScreeningScheduler

    cfg = dict(DEFAULT_CONFIG)
    cfg["screening"] = dict(cfg.get("screening") or {})
    cfg["screening"]["intraday_cache_policy"] = "ttl_only"
    scheduler = ScreeningScheduler(db_path=":memory:", config=cfg)

    with patch("webapp.screening_scheduler.get_cache") as mock_get:
        scheduler._run_intraday_refresh()
        mock_get.assert_not_called()


def test_intraday_full_invalidate_clears_all_screening_types():
    from webapp.screening_scheduler import ScreeningScheduler

    cfg = dict(DEFAULT_CONFIG)
    cfg["screening"] = dict(cfg.get("screening") or {})
    cfg["screening"]["intraday_cache_policy"] = "full_invalidate"
    scheduler = ScreeningScheduler(db_path=":memory:", config=cfg)

    cache = MagicMock()
    cache.invalidate_type.side_effect = [2, 3, 1]

    with patch("webapp.screening_scheduler.get_cache", return_value=cache):
        scheduler._run_intraday_refresh()

    assert cache.invalidate_type.call_count == 3
    types = [c.args[0] for c in cache.invalidate_type.call_args_list]
    assert types == ["screening_prices", "screening_ohlcv_ticker", "screening_spy"]
