"""Approve-bar tests for Signal Weights, Compare, Signal Performance, Auto-Discovery."""
import json
import unittest
from unittest.mock import MagicMock, patch

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.screening.discovery import get_discovery_template
from tradingagents.screening.engine import ScreeningEngine
from tradingagents.screening.theme_detector import (
    THEME_CATALOG_IDS,
    ThemeDetector,
    ThemeSpec,
    _THEME_TEMPLATES,
)


class TestSignalWeights(unittest.TestCase):
    def test_custom_weights_reorder_composite(self):
        engine = ScreeningEngine.__new__(ScreeningEngine)
        signals_a = {"valuation_gap": 0.9, "relative_strength": 0.1, "volume_surge": 0.1}
        signals_b = {"valuation_gap": 0.1, "relative_strength": 0.9, "volume_surge": 0.1}
        value_w = {"valuation_gap": 0.8, "relative_strength": 0.1, "volume_surge": 0.1}
        mom_w = {"valuation_gap": 0.1, "relative_strength": 0.8, "volume_surge": 0.1}
        score_a_value = engine._compute_composite_score(signals_a, value_w)
        score_b_value = engine._compute_composite_score(signals_b, value_w)
        score_a_mom = engine._compute_composite_score(signals_a, mom_w)
        score_b_mom = engine._compute_composite_score(signals_b, mom_w)
        self.assertGreater(score_a_value, score_b_value)
        self.assertGreater(score_b_mom, score_a_mom)

    def test_default_weights_sum_to_one(self):
        weights = DEFAULT_CONFIG["screening"]["signal_weights"]
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)
        self.assertFalse(DEFAULT_CONFIG["screening"]["adaptive_blend"]["enabled"])


class TestCompareGrouping(unittest.TestCase):
    def test_skips_empty_qa_and_caps_per_list(self):
        from webapp.app import _group_compare_runs

        runs = [
            {"id": 1, "watchlist_name": "NASDAQ 100", "results_count": 99, "strategy": None},
            {"id": 2, "watchlist_name": "NASDAQ 100", "results_count": 98, "strategy": "screen_all_union"},
            {"id": 3, "watchlist_name": "NASDAQ 100", "results_count": 98, "strategy": "qa_matched_preset_validation"},
            {"id": 4, "watchlist_name": "NASDAQ 100", "results_count": 0, "strategy": "screen_all_union"},
            {"id": 5, "watchlist_name": "Movers: x", "results_count": 82, "strategy": "movers"},
            {"id": 6, "watchlist_name": "Russell 2000", "results_count": 1982, "strategy": "screen_all_union"},
        ]
        grouped = _group_compare_runs(runs, per_list=2)
        self.assertEqual([r["id"] for r in grouped["NASDAQ 100"]], [1, 2])
        self.assertNotIn(3, [r["id"] for r in grouped["NASDAQ 100"]])
        self.assertEqual([r["id"] for r in grouped["Movers: x"]], [5])
        self.assertEqual([r["id"] for r in grouped["Russell 2000"]], [6])


class _PerfDB:
    def __init__(self, rows):
        self.rows = rows
        self.saved = []

    def get_backtested_screening_results(self, limit=2000):
        return list(self.rows)[:limit]

    def save_signal_performance(self, **kwargs):
        self.saved.append(kwargs)


class TestSignalPerformanceCompute(unittest.TestCase):
    def _rows(self, n=40, percent_scale=False):
        rows = []
        for i in range(n):
            fired = i % 2 == 0
            ret = (0.03 if fired else -0.02)
            if percent_scale:
                ret *= 100
            rows.append({
                "signals": {"volume_surge": 0.8 if fired else 0.1, "valuation_gap": 0.2},
                "return_7d": ret,
                "return_14d": ret,
                "return_30d": ret,
            })
        return rows

    def test_computes_hit_rate_from_backtested_rows(self):
        db = _PerfDB(self._rows())
        engine = ScreeningEngine(config=DEFAULT_CONFIG, db=db)
        perf = engine.compute_signal_performance(min_samples=30)
        self.assertTrue(perf)
        vol = next(v for v in perf.values() if v["signal_name"] == "volume_surge" and v["period"] == "7d")
        self.assertGreater(vol["hit_rate"], 80)
        self.assertGreater(vol["sample_count"], 10)
        self.assertTrue(db.saved)

    def test_normalizes_legacy_percent_returns(self):
        records = [{"return_7d": 2.5, "volume_surge": 0.8}]
        ScreeningEngine._normalize_forward_return_records(records)
        self.assertAlmostEqual(records[0]["return_7d"], 0.025, places=6)

    def test_leaves_decimal_returns_alone(self):
        records = [{"return_7d": 0.025, "volume_surge": 0.8}]
        ScreeningEngine._normalize_forward_return_records(records)
        self.assertAlmostEqual(records[0]["return_7d"], 0.025, places=6)


class TestAutoDiscoveryCatalog(unittest.TestCase):
    def test_every_theme_maps_to_a_real_catalog_chip(self):
        theme_ids = {t.id for t in _THEME_TEMPLATES}
        self.assertEqual(theme_ids, set(THEME_CATALOG_IDS))
        for theme_id, catalog_id in THEME_CATALOG_IDS.items():
            tmpl = get_discovery_template(catalog_id)
            self.assertIsNotNone(tmpl, f"{theme_id} → {catalog_id}")

    def test_detect_themes_attaches_catalog_id(self):
        detector = ThemeDetector(max_themes=5)
        sector = {
            "sector_ranks": {"Technology": 1, "Healthcare": 2, "Energy": 3, "Industrials": 4},
            "iwm_vs_spy_20d": 1.2,
        }
        macro = {"market_regime": "bull", "vix_value": 14.0}
        themes = detector.detect_themes(sector, macro)
        self.assertTrue(themes)
        for spec in themes:
            self.assertTrue(spec.catalog_id, spec.name)
            self.assertIsNotNone(get_discovery_template(spec.catalog_id))

    def test_auto_discovery_kwargs_pass_template_id(self):
        from webapp.screening_scheduler import _auto_discovery_kwargs

        spec = ThemeSpec(
            name="Auto: AI Infrastructure",
            theme="t",
            criteria="c",
            market_cap_filter="under $10B",
            preset="momentum_hunter",
            profile="high_growth",
            rationale="r",
            primary_sector="Technology",
            catalog_id="ai_infrastructure",
        )
        kwargs = _auto_discovery_kwargs(spec, "sonar")
        self.assertEqual(kwargs["template_id"], "ai_infrastructure")
        self.assertTrue(kwargs["skip_cache"])
        self.assertEqual(kwargs["model"], "sonar")

    def test_scan_all_still_excludes_auto(self):
        from webapp.app import _is_scan_all_eligible_watchlist

        self.assertFalse(_is_scan_all_eligible_watchlist({"source": "auto", "name": "Auto: AI"}))


class TestDashboardIndexRegime(unittest.TestCase):
    """Dashboard stats must surface live index regime, not last analysis signal_summary."""

    @patch("webapp.app.get_macro_snapshot")
    @patch("webapp.app.get_db")
    def test_empty_and_populated_both_use_live_index_regime(self, mock_get_db, mock_macro):
        from webapp.app import dashboard_stats

        mock_macro.return_value = {
            "index_trend": "bull",
            "index_stress": "quiet",
            "index_regime": "bull_quiet",
            "index_regime_label": "Bull / Quiet",
            "market_regime": "bull",
        }

        empty = dashboard_stats()
        self.assertEqual(empty["index_regime"]["index_regime_label"], "Bull / Quiet")
        self.assertNotIn("signal_summary", empty)

        analysis = MagicMock()
        analysis.duration_seconds = 12
        analysis.confidence = 65
        analysis.data_quality_score = 75
        analysis.decision = "SELL"
        analysis.risk_profile = "growth"
        analysis.analysis_mode = "standard"
        analysis.has_sec_snapshot = False
        analysis.has_transcript_snapshot = False
        analysis.report_warnings = "[]"
        analysis.was_correct = None
        analysis.actual_return_7d = None
        analysis.signal_summary = json.dumps({"market_regime": "BEAR"})
        mock_get_db.return_value.get_recent_analyses.return_value = [analysis]

        populated = dashboard_stats()
        self.assertEqual(populated["index_regime"]["index_regime_label"], "Bull / Quiet")
        self.assertEqual(populated["market_regime"], "BULL")


if __name__ == "__main__":
    unittest.main()
