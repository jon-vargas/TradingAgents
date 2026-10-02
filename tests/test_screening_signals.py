"""Unit tests for screening engine signal computations."""
import unittest
import pandas as pd
import numpy as np


class TestSignalRanges(unittest.TestCase):
    """Verify all signals return values in [0, 1]."""

    def _get_engine(self):
        from tradingagents.screening.engine import ScreeningEngine
        return ScreeningEngine.__new__(ScreeningEngine)

    def test_volume_surge_range(self):
        engine = self._get_engine()
        vol = pd.Series(np.random.randint(100000, 500000, size=25))
        score = engine._signal_volume_surge(vol)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_volume_surge_log_scaling(self):
        engine = self._get_engine()
        avg = 100000
        vol_2x = pd.Series([avg] * 20 + [avg * 2])
        vol_5x = pd.Series([avg] * 20 + [avg * 5])
        score_2x = engine._signal_volume_surge(vol_2x)
        score_5x = engine._signal_volume_surge(vol_5x)
        self.assertGreater(score_5x, score_2x, "5x volume should score higher than 2x")
        self.assertLess(score_2x, 0.5, "2x volume should score below 0.5 with log scaling")

    def test_relative_strength_log_scaling(self):
        engine = self._get_engine()
        close_5pct = pd.Series([100] + [100] * 19 + [105])
        close_25pct = pd.Series([100] + [100] * 19 + [125])
        spy = pd.Series([100] * 21)
        score_5 = engine._signal_relative_strength(close_5pct, spy)
        score_25 = engine._signal_relative_strength(close_25pct, spy)
        self.assertGreater(score_25, score_5, "25% excess should score higher than 5%")
        self.assertLess(score_5, 0.6, "5% excess should not max out with log scaling")

    def test_price_vs_target_raised_cap(self):
        engine = self._get_engine()
        close = pd.Series([100.0])
        info_20 = {"targetMeanPrice": 120}
        info_40 = {"targetMeanPrice": 140}
        score_20 = engine._signal_price_vs_target(close, info_20)
        score_40 = engine._signal_price_vs_target(close, info_40)
        self.assertLess(score_20, 0.6, "20% upside should score around 0.5 with 40% cap")
        self.assertAlmostEqual(score_40, 1.0, places=1, msg="40% upside should score ~1.0")


class TestPresetWeights(unittest.TestCase):
    def test_all_presets_sum_to_one(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        presets = DEFAULT_CONFIG["screening"]["presets"]
        for name, preset in presets.items():
            total = sum(preset["weights"].values())
            self.assertAlmostEqual(total, 1.0, places=2, msg=f"Preset {name} weights sum to {total}")

    def test_all_presets_include_weekly_trend_alignment(self):
        """IMP-5: every named preset must carry a non-zero weekly_trend_alignment
        weight so the funnel-gated Phase 1 signal actually influences ranking
        under presets (not just the bare default signal_weights block)."""
        from tradingagents.default_config import DEFAULT_CONFIG
        presets = DEFAULT_CONFIG["screening"]["presets"]
        self.assertGreater(len(presets), 0, "expected named presets to exist")
        for name, preset in presets.items():
            weight = preset["weights"].get("weekly_trend_alignment")
            self.assertIsNotNone(weight, f"Preset {name} missing weekly_trend_alignment weight")
            self.assertGreater(weight, 0.0, f"Preset {name} has zero weekly_trend_alignment weight")

    def test_default_weights_sum_to_one_and_keep_overbought(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        weights = DEFAULT_CONFIG["screening"]["signal_weights"]
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=2)
        self.assertGreater(weights["rsi_overbought"], 0.0)
        for name, preset in DEFAULT_CONFIG["screening"]["presets"].items():
            self.assertGreater(
                preset["weights"].get("rsi_overbought", 0),
                0.0,
                f"Preset {name} zeroed rsi_overbought",
            )

    def test_operator_visible_flag_present(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        presets = DEFAULT_CONFIG["screening"]["presets"]
        self.assertTrue(presets["momentum_hunter"].get("operator_visible"))
        self.assertFalse(presets["small_cap_growth"].get("operator_visible"))
        self.assertFalse(presets["movers_swing_1to5d"].get("operator_visible"))
        self.assertTrue(presets["quality_compounder"].get("operator_visible"))
        self.assertTrue(presets["short_squeeze"].get("operator_visible"))
        self.assertTrue(presets["dividend_income"].get("operator_visible"))


class TestSignedTechnicalsAndHygiene(unittest.TestCase):
    def _engine(self):
        from tradingagents.screening.engine import ScreeningEngine
        return ScreeningEngine.__new__(ScreeningEngine)

    def test_ma_below_sma_is_zero(self):
        engine = self._engine()
        close = pd.Series([100 - i * 0.4 for i in range(60)])
        self.assertEqual(engine._signal_ma_crossover(close), 0.0)

    def test_adx_downtrend_is_zero(self):
        engine = self._engine()
        n = 40
        close = pd.Series([80 - i * 0.8 for i in range(n)])
        high = close + 0.5
        low = close - 0.5
        self.assertEqual(engine._signal_trend_strength(close, high, low), 0.0)

    def test_pead_missing_is_none(self):
        engine = self._engine()
        self.assertIsNone(engine._signal_pead_drift("X", {}, {}, None))
        self.assertIsNone(engine._signal_pead_drift("X", {}, {"earnings_profile": {}}, None))

    def test_composite_skips_overbought(self):
        from tradingagents.screening.engine import ScreeningEngine
        engine = ScreeningEngine.__new__(ScreeningEngine)
        weights = {"rsi_overbought": 1.0, "relative_strength": 0.0}
        signals = {"rsi_overbought": 1.0, "relative_strength": 0.0}
        self.assertEqual(engine._compute_composite_score(signals, weights), 0.0)

    def test_coverage_counts_signed_zero_ma(self):
        from tradingagents.screening.engine import ScreeningEngine
        engine = ScreeningEngine.__new__(ScreeningEngine)
        signals = {"ma_crossover": 0.0, "trend_strength": 0.0, "relative_strength": None}
        weights = {"ma_crossover": 0.5, "trend_strength": 0.5, "relative_strength": 0.5}
        cov = engine._compute_signal_coverage(
            signals, weights, presence_signals={"ma_crossover", "trend_strength"}
        )
        self.assertGreater(cov, 0.0)

    def test_quality_income_none_without_fundamentals(self):
        engine = self._engine()
        self.assertIsNone(engine._signal_quality_factor({}))
        self.assertIsNone(engine._signal_income_factor({}))
        self.assertIsNone(engine._signal_income_factor({"dividendYield": 0}))
        self.assertIsNotNone(engine._signal_income_factor({"trailingAnnualDividendYield": 0.03}))

    def test_residual_momentum_none_on_short_history(self):
        engine = self._engine()
        close = pd.Series(range(50), dtype=float)
        spy = pd.Series(range(50), dtype=float)
        self.assertIsNone(engine._signal_residual_momentum_12_1(close, spy))

    def test_funnel_none_is_numeric_safe(self):
        from tradingagents.screening.engine import _numeric_or_zero
        self.assertEqual(_numeric_or_zero(None), 0.0)
        self.assertEqual(_numeric_or_zero(0.4), 0.4)

    def test_scorecard_missing_family_is_none(self):
        from tradingagents.screening.engine import compute_factor_scorecard
        card = compute_factor_scorecard({"volume_surge": 0.5})
        self.assertIsNone(card["Quality"])
        self.assertIsNone(card["Income"])

    def test_zscore_skips_below_min_peers(self):
        from tradingagents.screening.engine import ScreeningEngine
        engine = ScreeningEngine.__new__(ScreeningEngine)
        engine._screening_config = {
            "cross_sectional_zscore": {
                "enabled": True,
                "min_peers": 8,
                "clip": 3.0,
                "eligible_keys": ["quality_factor"],
            }
        }
        scores = {
            f"T{i}": {"signals": {"quality_factor": 0.2 + i * 0.05}}
            for i in range(3)
        }
        meta = {f"T{i}": {"sector": "Technology"} for i in range(3)}
        before = {k: v["signals"]["quality_factor"] for k, v in scores.items()}
        engine._apply_cross_sectional_zscore(scores, meta)
        after = {k: v["signals"]["quality_factor"] for k, v in scores.items()}
        self.assertEqual(before, after)

    def test_zscore_maps_to_unit_interval(self):
        from tradingagents.screening.engine import ScreeningEngine
        engine = ScreeningEngine.__new__(ScreeningEngine)
        engine._screening_config = {
            "cross_sectional_zscore": {
                "enabled": True,
                "min_peers": 8,
                "clip": 3.0,
                "eligible_keys": ["quality_factor"],
            }
        }
        scores = {
            f"T{i}": {"signals": {"quality_factor": float(i) / 10.0}}
            for i in range(10)
        }
        meta = {f"T{i}": {"sector": "Technology"} for i in range(10)}
        engine._apply_cross_sectional_zscore(scores, meta)
        vals = [scores[f"T{i}"]["signals"]["quality_factor"] for i in range(10)]
        self.assertTrue(all(0.0 <= v <= 1.0 for v in vals))
        self.assertNotIn("opportunity_z", scores["T0"])

    def test_options_and_smart_money_missing_are_none(self):
        engine = self._engine()
        self.assertIsNone(engine._signal_options_sentiment({}))
        self.assertIsNone(engine._signal_options_sentiment({"unusual_activity": []}))
        self.assertIsNone(engine._signal_smart_money({}, {}))
        self.assertGreater(engine._signal_options_sentiment({"put_call_volume_ratio": 0.4}), 0.0)
        self.assertGreater(engine._signal_smart_money({"held_by_institutions_pct": 85}, {}), 0.0)

    def test_valuation_gap_multi_none_without_metrics(self):
        engine = self._engine()
        engine._screening_config = {}
        score, detail = engine._signal_valuation_gap_multi(None, None, None, "Technology", {})
        self.assertIsNone(score)
        self.assertEqual(detail, {})

    def test_primary_sleeve_gate_sorts_missing_last(self):
        from tradingagents.screening.engine import primary_sleeve_present

        self.assertTrue(primary_sleeve_present("momentum_hunter", {}))
        self.assertFalse(primary_sleeve_present(
            "dividend_income", {"quality_factor": 1.0}, tier_reached="enhanced"
        ))
        self.assertFalse(primary_sleeve_present(
            "dividend_income", {"quality_factor": 1.0}, tier_reached="tier1_only"
        ))
        self.assertTrue(primary_sleeve_present("dividend_income", {"income_factor": 0.0}))
        self.assertTrue(primary_sleeve_present("long_horizon_12to36m", {"quality_factor": 0.8}))
        self.assertFalse(primary_sleeve_present(
            "long_horizon_6to12m", {"valuation_gap": 0.0, "estimate_momentum": 1.0},
        ))
        self.assertFalse(primary_sleeve_present(
            "long_horizon_6to12m", {"quality_factor": 0.05, "valuation_gap": 1.0},
        ))
        self.assertTrue(primary_sleeve_present(
            "long_horizon_6to12m", {"quality_factor": 0.20, "valuation_gap": 0.0},
        ))
        self.assertFalse(primary_sleeve_present(
            "long_horizon_12to36m", {"quality_factor": 0.15, "valuation_gap": 1.0},
        ))
        self.assertFalse(primary_sleeve_present(
            "value_fisher", {"quality_factor": 1.0}, tier_reached="enhanced"
        ))
        self.assertFalse(primary_sleeve_present(
            "smart_money_tracker", {"insider_buying": 1.0},
        ))
        self.assertTrue(primary_sleeve_present(
            "smart_money_tracker", {"smart_money": 0.4},
        ))
        self.assertFalse(primary_sleeve_present(
            "earnings_play", {"volume_surge": 1.0, "relative_strength": 0.9},
        ))
        self.assertTrue(primary_sleeve_present(
            "earnings_play", {"pead_drift": 0.2},
        ))
        self.assertFalse(primary_sleeve_present(
            "earnings_play", {"estimate_momentum": 1.0, "volume_surge": 1.0},
        ))
        self.assertTrue(primary_sleeve_present(
            "earnings_play", {"earnings_proximity": 0.4},
        ))
        self.assertFalse(primary_sleeve_present(
            "short_squeeze", {"volume_surge": 1.0, "options_sentiment": 0.8},
        ))
        self.assertFalse(primary_sleeve_present(
            "short_squeeze", {"short_pressure": 0.10},
        ))
        self.assertTrue(primary_sleeve_present(
            "short_squeeze", {"short_pressure": 0.30},
        ))
        self.assertFalse(primary_sleeve_present(
            "value_fisher",
            {
                "valuation_gap": 0.8,
                "_screening_meta": {"market_cap_tier": "mega", "sector": "Technology"},
            },
        ))
        self.assertTrue(primary_sleeve_present(
            "value_fisher",
            {
                "valuation_gap": 0.8,
                "_screening_meta": {"market_cap_tier": "large", "sector": "Technology"},
            },
        ))
        self.assertFalse(primary_sleeve_present(
            "value_fisher",
            {
                "valuation_gap": 0.8,
                "_screening_meta": {"ticker": "NVDA", "sector": "Technology"},
            },
        ))
        self.assertTrue(primary_sleeve_present(
            "value_fisher",
            {
                "valuation_gap": 0.8,
                "_screening_meta": {"ticker": "UNH", "sector": "Healthcare"},
            },
        ))

    def test_live_medians_require_eight_peers(self):
        from tradingagents.screening.engine import ScreeningEngine

        engine = ScreeningEngine.__new__(ScreeningEngine)
        engine._screening_config = {}
        enhanced = {
            f"T{i}": {"valuation": {"forward_pe": 20.0 + i, "sector": "Technology"}}
            for i in range(7)
        }
        medians = engine._compute_live_sector_medians(enhanced)
        self.assertNotIn("Technology", medians.get("forward_pe", {}))
        enhanced["T7"] = {"valuation": {"forward_pe": 21.0, "sector": "Technology"}}
        medians = engine._compute_live_sector_medians(enhanced)
        self.assertIn("Technology", medians.get("forward_pe", {}))

    def test_ohlcv_lookback_covers_twelve_one(self):
        from tradingagents.screening.engine import _OHLCV_LOOKBACK_DAYS
        from tradingagents.default_config import DEFAULT_CONFIG

        self.assertGreaterEqual(_OHLCV_LOOKBACK_DAYS, 400)
        self.assertGreaterEqual(DEFAULT_CONFIG["screening"]["ohlcv_lookback_days"], 400)


class TestYahooPassScoring(unittest.TestCase):
    def _engine(self):
        from tradingagents.screening.engine import ScreeningEngine
        engine = ScreeningEngine.__new__(ScreeningEngine)
        engine._screening_config = {}
        engine.db = None
        return engine

    def test_tier2_includes_valuation_gap_from_info(self):
        from unittest.mock import patch
        from tradingagents.screening.engine import ScreeningEngine

        engine = self._engine()
        self.assertIn("valuation_gap", ScreeningEngine.TIER2_SIGNALS)
        self.assertNotIn("valuation_gap", ScreeningEngine.ENHANCED_SIGNALS)
        data = {
            "close": pd.Series([100.0]),
            "info": {
                "forwardPE": 10.0,
                "priceToSalesTrailing12Months": 2.0,
                "enterpriseToEbitda": 8.0,
                "sector": "Technology",
            },
        }
        with patch("tradingagents.screening.engine.get_earnings_profile", return_value={}):
            signals = engine._compute_tier2_signals("AAPL", data)
        self.assertIsNotNone(signals["valuation_gap"])
        self.assertGreater(signals["valuation_gap"], 0.4)

    def test_identity_funnel_bypass_and_raised_cutoff(self):
        from tradingagents.screening.engine import apply_identity_funnel_policy

        enabled, cut = apply_identity_funnel_policy("dividend_income", 50, True, 0.60)
        self.assertFalse(enabled)
        self.assertEqual(cut, 0.60)

        enabled, cut = apply_identity_funnel_policy("value_fisher", 200, True, 0.60)
        self.assertTrue(enabled)
        self.assertAlmostEqual(cut, 0.85)

        enabled, cut = apply_identity_funnel_policy("momentum_hunter", 50, True, 0.60)
        self.assertTrue(enabled)
        self.assertEqual(cut, 0.60)

        enabled, cut = apply_identity_funnel_policy("quality_compounder", 80, False, 0.60)
        self.assertFalse(enabled)

        # Flow/event books must keep the funnel so enhanced smart_money /
        # options / PEAD can populate. They are sort-last gated, not identity-T2.
        enabled, cut = apply_identity_funnel_policy("smart_money_tracker", 50, True, 0.60)
        self.assertTrue(enabled)
        self.assertEqual(cut, 0.60)
        enabled, cut = apply_identity_funnel_policy("earnings_play", 80, True, 0.60)
        self.assertTrue(enabled)
        self.assertEqual(cut, 0.60)

    def test_primary_sleeve_none_contributes_zero(self):
        engine = self._engine()
        weights = {"income_factor": 0.5, "relative_strength": 0.5}
        self.assertAlmostEqual(
            engine._compute_composite_score(
                {"income_factor": None, "relative_strength": 1.0}, weights
            ),
            50.0,
        )
        self.assertAlmostEqual(
            engine._compute_composite_score({"relative_strength": 1.0}, weights),
            50.0,
        )
        other = {"options_sentiment": 0.5, "relative_strength": 0.5}
        self.assertAlmostEqual(
            engine._compute_composite_score(
                {"options_sentiment": None, "relative_strength": 1.0}, other
            ),
            100.0,
        )

    def test_quality_log_scale_and_fcf(self):
        engine = self._engine()
        mid = engine._signal_quality_factor({"returnOnEquity": 0.20})
        high = engine._signal_quality_factor({"returnOnEquity": 0.40})
        self.assertIsNotNone(mid)
        self.assertLess(mid, 1.0)
        self.assertGreater(high, mid)
        with_fcf = engine._signal_quality_factor({
            "returnOnEquity": 0.20,
            "freeCashflow": 8e9,
            "marketCap": 100e9,
        })
        self.assertNotEqual(with_fcf, mid)

    def test_income_dividend_rate_over_price(self):
        engine = self._engine()
        score = engine._signal_income_factor({
            "dividendRate": 3.0,
            "currentPrice": 100.0,
        })
        self.assertIsNotNone(score)
        trailing = engine._signal_income_factor({"trailingAnnualDividendYield": 0.03})
        self.assertAlmostEqual(score, trailing, places=4)

    def test_options_pc_bucket_is_soft(self):
        engine = self._engine()
        score = engine._signal_options_sentiment({"put_call_volume_ratio": 0.4})
        self.assertAlmostEqual(score, 0.20, places=2)
        mid = engine._signal_options_sentiment({"put_call_volume_ratio": 0.7})
        self.assertAlmostEqual(mid, 0.10, places=2)

    def test_value_fisher_not_cloned_long_horizon(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        vf = DEFAULT_CONFIG["screening"]["presets"]["value_fisher"]["weights"]
        lh = DEFAULT_CONFIG["screening"]["presets"]["long_horizon_12to36m"]["weights"]
        self.assertGreater(vf["valuation_gap"], lh["valuation_gap"])
        self.assertGreater(lh["quality_factor"], vf["quality_factor"])
        self.assertGreater(lh["residual_momentum_12_1"], vf["residual_momentum_12_1"])
        self.assertTrue(DEFAULT_CONFIG["screening"]["cross_sectional_zscore"]["enabled"])

    def test_zscore_leaves_none_untouched(self):
        from tradingagents.screening.engine import ScreeningEngine

        engine = ScreeningEngine.__new__(ScreeningEngine)
        engine._screening_config = {
            "cross_sectional_zscore": {
                "enabled": True,
                "min_peers": 8,
                "clip": 3.0,
                "eligible_keys": ["quality_factor"],
            }
        }
        scores = {
            f"T{i}": {"signals": {"quality_factor": (None if i == 0 else float(i) / 10.0)}}
            for i in range(10)
        }
        meta = {f"T{i}": {"sector": "Technology"} for i in range(10)}
        engine._apply_cross_sectional_zscore(scores, meta)
        self.assertIsNone(scores["T0"]["signals"]["quality_factor"])
        self.assertTrue(0.0 <= scores["T9"]["signals"]["quality_factor"] <= 1.0)

    def test_sector_medians_persist_roundtrip(self):
        import os
        import tempfile
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.engine import ScreeningEngine

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(path)
            medians = {"forward_pe": {"Technology": 27.5, "Energy": 11.0}}
            rid = db.save_sector_medians(medians, universe_size=100)
            self.assertGreater(rid, 0)
            loaded = db.get_latest_sector_medians()
            self.assertAlmostEqual(loaded["forward_pe"]["Technology"], 27.5)
            engine = ScreeningEngine.__new__(ScreeningEngine)
            engine.db = db
            engine._screening_config = {}
            stored = engine._stored_sector_medians()
            self.assertAlmostEqual(stored["forward_pe"]["Energy"], 11.0)
            merged = engine._merged_static_medians(
                {"Technology": 28, "Healthcare": 22}, "forward_pe"
            )
            self.assertAlmostEqual(merged["Technology"], 27.5)
            self.assertEqual(merged["Healthcare"], 22)
        finally:
            os.unlink(path)

    def test_earnings_proximity_none_outside_window(self):
        from unittest.mock import patch

        engine = self._engine()
        with patch(
            "tradingagents.screening.engine.get_earnings_profile",
            return_value={"days_until_earnings": 45},
        ):
            self.assertIsNone(engine._signal_earnings_proximity("AAPL"))
        with patch(
            "tradingagents.screening.engine.get_earnings_profile",
            return_value={"days_until_earnings": None},
        ):
            self.assertIsNone(engine._signal_earnings_proximity("AAPL"))
        with patch(
            "tradingagents.screening.engine.get_earnings_profile",
            return_value={"days_until_earnings": 7, "earnings_beat_rate_pct": 80},
        ):
            score = engine._signal_earnings_proximity("AAPL")
            self.assertIsNotNone(score)
            self.assertGreater(score, 0.5)

    def test_short_pressure_from_info_and_ownership(self):
        engine = self._engine()
        self.assertIsNone(engine._signal_short_pressure({}))
        mid = engine._signal_short_pressure({"shortPercentOfFloat": 0.10, "shortRatio": 5.0})
        high = engine._signal_short_pressure({"shortPercentOfFloat": 0.25, "shortRatio": 12.0})
        self.assertIsNotNone(mid)
        self.assertGreater(high, mid)
        from_own = engine._signal_short_pressure({}, {"short_pct_of_float": 20.0, "days_to_cover": 8})
        self.assertIsNotNone(from_own)
        self.assertGreaterEqual(from_own, 0.7)


class TestEarningsSurpriseParser(unittest.TestCase):
    def test_parse_legacy_and_new_column_names(self):
        from tradingagents.dataflows.yfinance_extended import _parse_surprise_history

        legacy = pd.DataFrame([
            {"surprisePercent": 8.0, "epsEstimate": 1.0, "epsActual": 1.08, "reportDate": "2026-05-01"},
        ])
        modern = pd.DataFrame([
            {"Surprise(%)": 4.0, "EPS Estimate": 2.0, "Reported EPS": 2.08},
        ], index=pd.Index(["2026-02-01"], name="Earnings Date"))
        derived = pd.DataFrame([
            {"epsEstimate": 1.0, "epsActual": 1.2},
        ])
        self.assertEqual(_parse_surprise_history(legacy)[0]["surprise_pct"], 8.0)
        self.assertEqual(_parse_surprise_history(modern)[0]["surprise_pct"], 4.0)
        self.assertAlmostEqual(_parse_surprise_history(derived)[0]["surprise_pct"], 20.0)


if __name__ == "__main__":
    unittest.main()
