import copy
import unittest
from datetime import datetime
from unittest.mock import patch


class _FakeDB:
    def __init__(self):
        self.watchlists = []
        self.snapshots_saved = 0
        self.movers_details_saved = {}

    def get_watchlists(self):
        return list(self.watchlists)

    def create_watchlist(self, name, description="", tickers="", default_preset=None, default_investment_profile=None):
        watchlist_id = len(self.watchlists) + 1
        self.watchlists.append(
            {
                "id": watchlist_id,
                "name": name,
                "description": description,
                "tickers": tickers,
                "default_preset": default_preset,
            }
        )
        return watchlist_id

    def update_watchlist(self, watchlist_id, **kwargs):
        for row in self.watchlists:
            if int(row["id"]) == int(watchlist_id):
                row.update(kwargs)
                return True
        return False

    def save_movers_snapshots(self, asof_date, fetched_at, rows):
        self.snapshots_saved += len(rows)
        return len(rows)

    def save_movers_run_details(self, run_id, details):
        self.movers_details_saved[run_id] = details
        return len(details)


class _FakeEngine:
    seq = 1000

    def __init__(self, config=None, db=None):
        self.config = config or {}
        self.db = db
        self.last_run_id = None
        self.calls = []

    def scan(self, tickers, date=None, preset=None, watchlist_id=None, criteria_meta=None, **kwargs):
        self.last_run_id = _FakeEngine.seq
        _FakeEngine.seq += 1
        self.calls.append(
            {
                "tickers": list(tickers),
                "preset": preset,
                "watchlist_id": watchlist_id,
                "criteria_meta": criteria_meta or {},
            }
        )
        from tradingagents.screening.engine import ScreeningResult

        results = []
        for idx, ticker in enumerate(tickers):
            results.append(
                ScreeningResult(
                    ticker=ticker,
                    composite_score=80.0 - idx,
                    direction="bullish",
                    signals={
                        "volume_surge": 0.8,
                        "relative_strength": 0.75,
                        "ma_crossover": 0.7,
                        "trend_strength": 0.65,
                        "rating_momentum": 0.6,
                    },
                    rank=idx + 1,
                )
            )
        return results


class TestMoversIntelligenceService(unittest.TestCase):
    def _config(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["screening"]["movers"]["post_close_guard_enabled"] = False
        cfg["screening"]["movers"]["max_universe_size"] = 10
        cfg["screening"]["movers"]["default_top_n"] = 5
        return cfg

    def test_normalize_filter_dedupe_policy(self):
        from tradingagents.screening.movers import MoversIntelligenceService

        db = _FakeDB()
        svc = MoversIntelligenceService(db=db, config=self._config())
        now_et = datetime(2026, 3, 4, 16, 30)
        rows = [
            {
                "symbol": "ABC",
                "regularMarketPrice": 20,
                "regularMarketVolume": 900000,
                "marketCap": 1_000_000_000,
                "regularMarketChangePercent": 5.0,
                "_source_list": "day_gainers",
            },
            {
                "symbol": "ABC",
                "regularMarketPrice": 20,
                "regularMarketVolume": 950000,
                "marketCap": 1_100_000_000,
                "regularMarketChangePercent": -7.0,
                "_source_list": "day_losers",
            },
            {
                "symbol": "ZEEMEDIA.NS",
                "regularMarketPrice": 12,
                "regularMarketVolume": 2_000_000,
                "marketCap": 2_000_000_000,
                "regularMarketChangePercent": 9.0,
                "_source_list": "small_mid_caps",
            },
        ]

        out = svc._normalize_filter_dedupe(rows, now_et=now_et, asof_date="2026-03-04")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ticker"], "ABC")
        self.assertEqual(out[0]["primary_bucket"], "day_losers")
        self.assertCountEqual(out[0]["source_tags"], ["day_gainers", "day_losers"])

    def test_preclose_guard_skips_scan(self):
        from tradingagents.screening.movers import MoversIntelligenceService

        cfg = self._config()
        cfg["screening"]["movers"]["post_close_guard_enabled"] = True
        svc = MoversIntelligenceService(db=_FakeDB(), config=cfg)

        with patch.object(MoversIntelligenceService, "_is_post_close", return_value=False):
            result = svc.run_scan(allow_preclose_skip=True)
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("skip_reason"), "preclose_guard")

    @patch("tradingagents.screening.movers.ScreeningEngine", _FakeEngine)
    def test_manual_scan_bypasses_preclose_guard(self):
        from tradingagents.screening.movers import MoversIntelligenceService

        cfg = self._config()
        cfg["screening"]["movers"]["post_close_guard_enabled"] = True
        db = _FakeDB()
        svc = MoversIntelligenceService(db=db, config=cfg)

        with patch.object(MoversIntelligenceService, "_is_post_close", return_value=False):
            with patch.object(MoversIntelligenceService, "_fetch_small_mid_caps_equity_query", return_value=[]):
                with patch("tradingagents.screening.movers.yf.screen") as mock_screen:
                    mock_screen.side_effect = lambda source: [
                        {
                            "symbol": "AAA",
                            "regularMarketPrice": 15.0,
                            "regularMarketVolume": 2_000_000,
                            "averageDailyVolume3Month": 1_800_000,
                            "marketCap": 5_000_000_000,
                            "regularMarketChangePercent": 6.3,
                        }
                    ]
                    result = svc.run_scan(include_losers=True, top_n=1, allow_preclose_skip=False)

        self.assertFalse(result.get("skipped", False))
        self.assertIn("runs", result)
        self.assertEqual(len(result["runs"]["short"]["top"]), 1)

    @patch("tradingagents.screening.movers.ScreeningEngine", _FakeEngine)
    def test_run_scan_returns_locked_shape(self):
        from tradingagents.screening.movers import MoversIntelligenceService

        db = _FakeDB()
        svc = MoversIntelligenceService(db=db, config=self._config())

        with patch.object(MoversIntelligenceService, "_fetch_small_mid_caps_equity_query", return_value=[]):
            with patch("tradingagents.screening.movers.yf.screen") as mock_screen:
                mock_screen.side_effect = lambda source: [
                    {
                        "symbol": "AAA",
                        "regularMarketPrice": 15.0,
                        "regularMarketVolume": 2_000_000,
                        "averageDailyVolume3Month": 1_800_000,
                        "marketCap": 5_000_000_000,
                        "regularMarketChangePercent": 6.3,
                    },
                    {
                        "symbol": "BBB",
                        "regularMarketPrice": 22.0,
                        "regularMarketVolume": 1_500_000,
                        "averageDailyVolume3Month": 1_200_000,
                        "marketCap": 3_500_000_000,
                        "regularMarketChangePercent": 4.1,
                    },
                ]
                payload = svc.run_scan(include_losers=True, top_n=2)

        self.assertIn("snapshot_id", payload)
        self.assertIn("watchlist_id", payload)
        self.assertIn("as_of_et", payload)
        self.assertIn("runs", payload)
        self.assertIn("short", payload["runs"])
        self.assertIn("medium", payload["runs"])
        self.assertEqual(len(payload["runs"]["short"]["top"]), 2)
        self.assertGreaterEqual(db.snapshots_saved, 2)
        self.assertTrue(payload["runs"]["short"]["run_id"] in db.movers_details_saved)
        first_row = payload["runs"]["short"]["top"][0]
        self.assertIn("ticker", first_row)
        self.assertIn("composite_score", first_row)
        self.assertIn("catalyst_type", first_row)
        self.assertIn("reason_codes", first_row)
        self.assertIn("primary_bucket", first_row)
        self.assertIn("source_tags", first_row)


class TestMoversClassifyResult(unittest.TestCase):
    def test_none_signal_values_do_not_raise(self):
        from tradingagents.screening.engine import ScreeningResult
        from tradingagents.screening.movers import MoversIntelligenceService

        service = MoversIntelligenceService(db=_FakeDB(), config={})
        result = ScreeningResult(
            ticker="TEST",
            composite_score=50.0,
            direction="bullish",
            signals={
                "earnings_proximity": None,
                "pead_drift": None,
                "volume_surge": 0.85,
                "smart_money": None,
                "estimate_momentum": None,
            },
        )
        catalyst, reasons = service._classify_result(result)
        self.assertEqual(catalyst, "low_quality_spike")
        self.assertIn("volume_surge", reasons)
        self.assertNotIn("pead_drift_support", reasons)

    def test_downside_print_is_not_a_breakout(self):
        from tradingagents.screening.engine import ScreeningResult
        from tradingagents.screening.movers import MoversIntelligenceService

        service = MoversIntelligenceService(db=_FakeDB(), config={})
        result = ScreeningResult(
            ticker="LOSS",
            composite_score=70.0,
            direction="bearish",
            signals={
                "relative_strength": 0.8,
                "ma_crossover": 0.7,
                "trend_strength": 0.7,
                "volume_surge": 0.85,
                "rsi_oversold": 0.7,
            },
        )
        catalyst, reasons = service._classify_result(
            result, {"price_change_pct": -8.2}
        )
        self.assertEqual(catalyst, "capitulation_washout")
        self.assertIn("downside_print", reasons)

    def test_select_board_reserves_loser_pocket(self):
        from tradingagents.screening.movers import MoversIntelligenceService

        rows = [
            {"ticker": "UP1", "catalyst_type": "upside_move", "composite_score": 40, "price_change_pct": 12},
            {"ticker": "UP2", "catalyst_type": "upside_move", "composite_score": 39, "price_change_pct": 10},
            {"ticker": "UP3", "catalyst_type": "momentum_breakout", "composite_score": 50, "price_change_pct": 3},
            {"ticker": "UP4", "catalyst_type": "unclassified", "composite_score": 48, "price_change_pct": 2},
            {"ticker": "DN1", "catalyst_type": "downside_move", "composite_score": 55, "price_change_pct": -7},
            {"ticker": "DN2", "catalyst_type": "downside_move", "composite_score": 50, "price_change_pct": -4},
            {"ticker": "DN3", "catalyst_type": "downside_move", "composite_score": 48, "price_change_pct": -3.5},
            {"ticker": "TINY", "catalyst_type": "unclassified", "composite_score": 90, "price_change_pct": -0.2},
        ]
        board = MoversIntelligenceService.select_board(rows, top_n=5, include_losers=True)
        tickers = [r["ticker"] for r in board]
        self.assertIn("DN1", tickers)
        self.assertNotIn("TINY", tickers)
        self.assertTrue(any(t.startswith("UP") for t in tickers))
        self.assertEqual(len(board), 5)
        no_losers = MoversIntelligenceService.select_board(rows, top_n=5, include_losers=False)
        self.assertTrue(all(r["price_change_pct"] >= 0 for r in no_losers))

    def test_rank_key_sorts_junk_last(self):
        from tradingagents.screening.movers import MoversIntelligenceService

        rows = [
            {"ticker": "JUNK", "catalyst_type": "low_quality_spike", "composite_score": 90, "price_change_pct": 12},
            {"ticker": "GOOD", "catalyst_type": "momentum_breakout", "composite_score": 60, "price_change_pct": 5},
            {"ticker": "EXT", "catalyst_type": "overextended_rally", "composite_score": 80, "price_change_pct": 9},
            {"ticker": "UNC", "catalyst_type": "unclassified", "composite_score": 70, "price_change_pct": 11},
        ]
        ordered = sorted(rows, key=MoversIntelligenceService.rank_key)
        self.assertEqual([r["ticker"] for r in ordered], ["UNC", "GOOD", "EXT", "JUNK"])

    def test_quiet_print_is_not_a_breakout(self):
        from tradingagents.screening.engine import ScreeningResult
        from tradingagents.screening.movers import MoversIntelligenceService

        service = MoversIntelligenceService(db=_FakeDB(), config={})
        result = ScreeningResult(
            ticker="FLAT",
            composite_score=51.0,
            direction="bullish",
            signals={
                "relative_strength": 0.8,
                "ma_crossover": 0.7,
                "trend_strength": 0.7,
            },
        )
        catalyst, _reasons = service._classify_result(result, {"price_change_pct": -0.4})
        self.assertNotEqual(catalyst, "momentum_breakout")

    def test_large_up_print_is_upside_or_estimate(self):
        from tradingagents.screening.engine import ScreeningResult
        from tradingagents.screening.movers import MoversIntelligenceService

        service = MoversIntelligenceService(db=_FakeDB(), config={})
        figr = ScreeningResult(
            ticker="FIGR",
            composite_score=44.0,
            direction="bullish",
            signals={"rating_momentum": 0.5, "estimate_momentum": 0.2},
        )
        cbrs = ScreeningResult(
            ticker="CBRS",
            composite_score=51.0,
            direction="bullish",
            signals={"estimate_momentum": 1.0},
        )
        cat_figr, _ = service._classify_result(figr, {"price_change_pct": 13.8})
        cat_cbrs, reasons = service._classify_result(cbrs, {"price_change_pct": 15.1})
        self.assertEqual(cat_figr, "upside_move")
        self.assertEqual(cat_cbrs, "estimate_revision")
        self.assertIn("estimate_momentum", reasons)

    def test_implausible_mega_cap_is_skipped(self):
        from tradingagents.screening.engine import ScreeningResult
        from tradingagents.screening.movers import MoversIntelligenceService

        service = MoversIntelligenceService(db=_FakeDB(), config={})
        result = ScreeningResult(
            ticker="SPCX",
            composite_score=56.0,
            direction="bullish",
            signals={"rating_momentum": 0.8},
        )
        top, _details = service._decorate_results(
            [result],
            {"SPCX": {"price_change_pct": 4.4, "primary_bucket": "day_gainers", "market_cap": 1.9e12}},
            25,
        )
        self.assertEqual(top, [])

    def test_price_vs_target_alone_is_not_analyst(self):
        from tradingagents.screening.engine import ScreeningResult
        from tradingagents.screening.movers import MoversIntelligenceService

        service = MoversIntelligenceService(db=_FakeDB(), config={})
        result = ScreeningResult(
            ticker="TGT",
            composite_score=44.0,
            direction="bullish",
            signals={"price_vs_target": 0.9, "rating_momentum": 0.2},
        )
        catalyst, _reasons = service._classify_result(result, {"price_change_pct": 2.0})
        self.assertNotEqual(catalyst, "analyst_catalyst")

    def test_weekend_counts_as_post_close(self):
        from tradingagents.screening.movers import MoversIntelligenceService

        saturday = datetime(2026, 8, 15, 10, 0)
        self.assertTrue(MoversIntelligenceService._is_post_close(saturday))


if __name__ == "__main__":
    unittest.main()
