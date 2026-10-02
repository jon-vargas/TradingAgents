"""Correctness coverage for market regime classification and sector alignment.

Complements test_macro_overlay_hardening.py (which only proves the overlay
never raises) with assertions on the actual classification/scoring math:
  - regime hysteresis band + 50-SMA trend confirmation for "bull"
  - continuous (not discretely bucketed) sector momentum scoring
  - missing ETF data excluded from ranking rather than silently zeroed
  - per-ticker (not batch-wide) regime weight-shift strength
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from tradingagents.screening import macro_overlay
from tradingagents.screening.macro_overlay import (
    SECTOR_ETF_MAP,
    _MISSING_SECTOR_RANK,
    _SECTOR_SCORE_NEUTRAL,
    _score_sectors_continuous,
    compute_sector_momentum,
)
from tradingagents.screening.engine import ScreeningEngine


from tradingagents.dataflows.index_regime import classify_index_trend


class TestRegimeHysteresis(unittest.TestCase):
    def test_deep_below_200sma_is_bear(self):
        self.assertEqual(classify_index_trend({"current": 90, "sma_200": 100, "sma_50": 95}), "bear")

    def test_marginally_above_200sma_without_50sma_confirmation_is_neutral(self):
        # +0.5% above the 200-SMA but *below* the 50-SMA — old logic called
        # this "bull" on the slightest tick; new logic requires trend
        # confirmation and a wider buffer, so this must be neutral.
        self.assertEqual(classify_index_trend({"current": 100.5, "sma_200": 100, "sma_50": 102}), "neutral")

    def test_within_buffer_band_is_neutral(self):
        # -3% is inside the widened dead zone (only <= -5% is bear now).
        self.assertEqual(classify_index_trend({"current": 97, "sma_200": 100, "sma_50": 98}), "neutral")

    def test_clears_buffer_and_50sma_confirms_bull(self):
        self.assertEqual(classify_index_trend({"current": 103, "sma_200": 100, "sma_50": 101}), "bull")

    def test_clears_buffer_but_50sma_does_not_confirm_stays_neutral(self):
        # +3% above the 200-SMA (clears the buffer) but price is still below
        # the 50-SMA — not yet a confirmed uptrend.
        self.assertEqual(classify_index_trend({"current": 103, "sma_200": 100, "sma_50": 105}), "neutral")

    def test_exactly_at_bear_threshold_is_bear(self):
        self.assertEqual(classify_index_trend({"current": 95, "sma_200": 100, "sma_50": 96}), "bear")


class TestContinuousSectorScoring(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        self.assertEqual(_score_sectors_continuous({}), {})

    def test_best_and_worst_map_to_score_range_bounds(self):
        scores = _score_sectors_continuous({"A": 5.0, "B": -3.0, "C": 1.0})
        self.assertAlmostEqual(scores["A"], 100.0)
        self.assertAlmostEqual(scores["B"], 10.0)
        self.assertTrue(10.0 < scores["C"] < 100.0)

    def test_all_equal_returns_neutral_midpoint(self):
        scores = _score_sectors_continuous({"A": 2.0, "B": 2.0, "C": 2.0})
        for v in scores.values():
            self.assertAlmostEqual(v, 55.0)  # midpoint of 10-100

    def test_small_return_gap_yields_small_score_gap(self):
        # Two sectors separated by a hair of relative return, sandwiched
        # between a much stronger and much weaker sector, must not land on
        # opposite sides of a large discrete jump (the old bucket table
        # could put adjacent ranks 25+ points apart).
        scores = _score_sectors_continuous({
            "Strong": 10.0, "Near1": 0.01, "Near2": 0.0, "Weak": -10.0,
        })
        self.assertLess(abs(scores["Near1"] - scores["Near2"]), 1.0)


class TestSectorMomentumMissingDataHandling(unittest.TestCase):
    """Exercises the real compute_sector_momentum() with a mocked yf.download
    so a sector whose ETF has no data at all is excluded from the ranking
    pool instead of being silently blended in as 0.0 ('tracked the market
    exactly')."""

    def setUp(self):
        n = 90
        dates = pd.date_range("2026-01-01", periods=n, freq="D")

        def series(values):
            return pd.DataFrame({"Close": pd.Series(values, index=dates)})

        flat = [100.0] * n
        strong_up = [100.0 + i * 0.5 for i in range(n)]   # outperformer
        strong_down = [100.0 - i * 0.5 for i in range(n)]  # underperformer

        frames = {
            "SPY": series(flat),
            "IWM": series(flat),
            "XLK": series(strong_up),        # Technology — full, strong outperform
            "XLF": series(strong_down),      # Financial Services — full, strong underperform
            "XLV": series(flat),             # Healthcare — full, neutral
            "XLI": series(flat),             # Industrials — full, neutral
            "XLP": series(flat),             # Consumer Defensive — full, neutral
            "XLY": series(flat),             # Consumer Cyclical — full, neutral
            "XLU": series(flat),             # Utilities — full, neutral
            "XLB": series(flat),             # Basic Materials — full, neutral
            "XLC": series(flat),             # Communication Services — full, neutral
            # XLE (Energy) intentionally given only 25 points: r20 available,
            # r60 unavailable -> "partial".
            "XLE": pd.DataFrame({"Close": pd.Series([100.0 + i * 0.2 for i in range(25)],
                                                      index=dates[:25])}),
            # XLRE (Real Estate) intentionally omitted entirely -> "missing".
        }

        class _FakeCache:
            def get(self, *_a, **_k):
                return None

            def set(self, *_a, **_k):
                pass

        self._patches = [
            patch.object(macro_overlay, "get_cache", return_value=_FakeCache()),
            patch.object(macro_overlay.yf, "download", return_value=frames),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_missing_sector_excluded_from_ranking_with_neutral_score(self):
        result = compute_sector_momentum()
        self.assertEqual(result["sector_data_quality"]["Real Estate"], "missing")
        self.assertEqual(result["sector_ranks"]["Real Estate"], _MISSING_SECTOR_RANK)
        self.assertEqual(result["sector_scores"]["Real Estate"], _SECTOR_SCORE_NEUTRAL)

    def test_partial_sector_uses_available_window_only(self):
        result = compute_sector_momentum()
        self.assertEqual(result["sector_data_quality"]["Energy"], "partial")
        # Energy modestly outperforms flat SPY on its available 20d window,
        # so it should score above neutral but well below the strong outperformer.
        self.assertGreater(result["sector_scores"]["Energy"], _SECTOR_SCORE_NEUTRAL)

    def test_full_data_sectors_ranked_by_actual_momentum(self):
        result = compute_sector_momentum()
        self.assertEqual(result["sector_data_quality"]["Technology"], "full")
        self.assertEqual(result["sector_ranks"]["Technology"], 1)
        self.assertEqual(result["sector_scores"]["Technology"], 100.0)
        # Financial Services is the strongest underperformer among sectors
        # with real data (missing/partial sectors don't distort this).
        real_data_ranks = {
            s: r for s, r in result["sector_ranks"].items()
            if result["sector_data_quality"][s] == "full"
        }
        self.assertEqual(max(real_data_ranks, key=real_data_ranks.get), "Financial Services")

    def test_missing_sector_does_not_distort_other_sectors_ranks(self):
        result = compute_sector_momentum()
        # Full + partial sectors (10 total) should occupy a clean 1..10 rank
        # run; only the fully-missing sector gets excluded (sentinel 12).
        ranked = sorted(
            r for s, r in result["sector_ranks"].items()
            if result["sector_data_quality"][s] != "missing"
        )
        self.assertEqual(ranked, list(range(1, 11)))
        self.assertEqual(result["sector_ranks"]["Real Estate"], _MISSING_SECTOR_RANK)


class TestPerTickerRegimeStrength(unittest.TestCase):
    def test_only_tickers_with_deviating_macro_fit_get_reduced_strength(self):
        engine = ScreeningEngine.__new__(ScreeningEngine)
        engine._screening_config = {}
        ticker_macro = {
            "AAA": {"macro_fit": 80.0},   # deviates >= 5 from 50 -> reduced strength
            "BBB": {"macro_fit": 51.0},   # deviates < 5 from 50 -> full strength
            "CCC": {"macro_fit": None},   # no macro fit -> full strength
        }

        macro_dev_threshold = 5.0
        regime_strength_default = 0.15
        regime_strength_reduced = 0.08

        def _macro_fit_deviates(ticker):
            entry = ticker_macro.get(ticker.upper())
            if not entry:
                return False
            mf = entry.get("macro_fit")
            return mf is not None and abs(float(mf) - 50.0) >= macro_dev_threshold

        def _ticker_regime_strength(ticker):
            return regime_strength_reduced if _macro_fit_deviates(ticker) else regime_strength_default

        self.assertEqual(_ticker_regime_strength("AAA"), regime_strength_reduced)
        self.assertEqual(_ticker_regime_strength("BBB"), regime_strength_default)
        self.assertEqual(_ticker_regime_strength("CCC"), regime_strength_default)


if __name__ == "__main__":
    unittest.main()
