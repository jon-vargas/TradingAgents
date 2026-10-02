"""Board-contract helpers for refined presets (book filter, sort-last, aliases)."""
import unittest


class TestPresetBoardContract(unittest.TestCase):
    def test_aliases_and_book_match(self):
        from webapp.app import _canonical_preset_name, _row_matches_book

        self.assertEqual(_canonical_preset_name("small_cap_growth"), "momentum_hunter")
        self.assertEqual(_canonical_preset_name("long_horizon_6to12m"), "long_horizon_12to36m")
        self.assertTrue(_row_matches_book(
            {"resolved_preset": "value_fisher", "preset": "momentum_hunter"},
            "value_fisher",
        ))
        self.assertTrue(_row_matches_book(
            {"resolved_preset": "small_cap_growth", "preset": "nasdaq"},
            "momentum_hunter",
        ))
        self.assertTrue(_row_matches_book({"resolved_preset": "default"}, "default"))
        self.assertTrue(_row_matches_book({"resolved_preset": "value_fisher"}, "all"))

    def test_hide_filters_and_summary_merge(self):
        from webapp.app import _merge_filtered_summaries, _row_passes_hide_filters

        illiquid = {"liquidity_pass": False, "event_risk_flags": [], "signal_coverage_pct": 90}
        blackout = {"liquidity_pass": True, "event_risk_flags": ["earnings_blackout"], "signal_coverage_pct": 90}
        low_cov = {"liquidity_pass": True, "event_risk_flags": [], "signal_coverage_pct": 40}
        counter = {"liquidity_pass": True, "event_risk_flags": [], "weekly_counter_trend": True}
        ok = {"liquidity_pass": True, "event_risk_flags": [], "signal_coverage_pct": 80}
        self.assertFalse(_row_passes_hide_filters(illiquid, hide_illiquid=True))
        self.assertFalse(_row_passes_hide_filters(blackout, hide_blackout=True))
        self.assertFalse(_row_passes_hide_filters(low_cov, hide_coverage=True))
        self.assertFalse(_row_passes_hide_filters(counter, hide_counter_trend=True))
        self.assertTrue(_row_passes_hide_filters(ok, hide_illiquid=True, hide_blackout=True, hide_coverage=True))

        original = [
            {"watchlist_id": 1, "watchlist_name": "A", "run_id": 10, "ticker_count": 5},
            {"watchlist_id": 2, "watchlist_name": "B", "run_id": 11, "ticker_count": 5},
        ]
        rows = [{
            "watchlist_id": 1,
            "watchlist_name": "A",
            "run_id": 10,
            "composite_score": 70,
            "opportunity_score": 80,
            "direction": "bullish",
        }]
        merged = _merge_filtered_summaries(original, rows)
        by_id = {s["watchlist_id"]: s for s in merged}
        self.assertEqual(by_id[1]["visible_count"], 1)
        self.assertEqual(by_id[1]["ticker_count"], 5)
        self.assertEqual(by_id[2]["visible_count"], 0)
        self.assertEqual(by_id[2]["ticker_count"], 5)

    def test_headline_rotates_cap_bands(self):
        from tradingagents.screening.scan_all_overlays import headline_top_opportunities

        rows = [
            {"ticker": "SMALL1", "market_cap_tier": "small", "opportunity_score": 90},
            {"ticker": "SMALL2", "market_cap_tier": "micro", "opportunity_score": 89},
            {"ticker": "MID1", "market_cap_tier": "mid", "opportunity_score": 80},
            {"ticker": "LARGE1", "market_cap_tier": "large", "opportunity_score": 70},
            {"ticker": "MEGA1", "market_cap_tier": "mega", "opportunity_score": 69},
        ]
        headline = headline_top_opportunities(
            rows,
            3,
            headline_cfg={"min_coverage_pct": 0, "min_dollar_adv_usd": 0, "include_etf_slot": False},
        )
        self.assertEqual([r["ticker"] for r in headline], ["LARGE1", "MID1", "SMALL1"])

    def test_headline_prefers_coverage_and_includes_etf_slot(self):
        from tradingagents.screening.scan_all_overlays import headline_top_opportunities

        rows = [
            {"ticker": "THIN", "market_cap_tier": "small", "signal_coverage_pct": 32,
             "avg_dollar_volume_usd": 8_000_000, "opportunity_score": 90},
            {"ticker": "SOLID", "market_cap_tier": "small", "signal_coverage_pct": 72,
             "avg_dollar_volume_usd": 8_000_000, "opportunity_score": 70},
            {"ticker": "MID1", "market_cap_tier": "mid", "signal_coverage_pct": 70,
             "avg_dollar_volume_usd": 20_000_000, "opportunity_score": 80},
            {"ticker": "LARGE1", "market_cap_tier": "large", "signal_coverage_pct": 65,
             "avg_dollar_volume_usd": 80_000_000, "opportunity_score": 75},
            {"ticker": "PSI", "asset_class": "etf", "market_cap_tier": "large",
             "signal_coverage_pct": 20, "avg_dollar_volume_usd": 40_000_000, "opportunity_score": 54},
        ]
        cfg = {
            "min_coverage_pct": 50,
            "min_dollar_adv_usd": 5_000_000,
            "include_etf_slot": True,
            "etf_max": 2,
        }
        headline = headline_top_opportunities(rows, 4, headline_cfg=cfg)
        self.assertEqual([r["ticker"] for r in headline], ["LARGE1", "MID1", "SOLID", "PSI"])

        dry = dict(rows[-1])
        dry["ticker"] = "DRYETF"
        dry["avg_dollar_volume_usd"] = 0
        dry["opportunity_score"] = 90
        capped = headline_top_opportunities(rows + [dry], 8, headline_cfg=cfg)
        self.assertEqual([r["ticker"] for r in capped if r.get("asset_class") == "etf"], ["PSI"])

    def test_quality_sort_last_on_missing(self):
        from webapp.app import _sort_scan_all_rows

        rows = [
            {"opportunity_score": 10, "factor_scorecard": {"Quality": None}},
            {"opportunity_score": 5, "factor_scorecard": {"Quality": 80}},
            {"opportunity_score": 8, "factor_scorecard": {}},
        ]
        ordered = _sort_scan_all_rows(rows, "quality")
        self.assertEqual(ordered[0]["factor_scorecard"].get("Quality"), 80)
        self.assertIsNone(ordered[1]["factor_scorecard"].get("Quality"))
        self.assertIsNone(ordered[2]["factor_scorecard"].get("Quality"))


if __name__ == "__main__":
    unittest.main()
