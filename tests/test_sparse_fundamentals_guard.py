"""Tests for sparse-fundamentals guardrails in screening enrichment.

Goals:
- Ensure symbols with valid quotes but sparse fundamentals payloads can be
  detected conservatively.
- Ensure estimate/rating fetchers honor the temporary cooldown marker and do
  not hit yfinance endpoints repeatedly.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch


class TestSparseFundamentalsDetection(unittest.TestCase):
    def test_has_sparse_fundamentals_true_when_no_pe_and_no_analyst_fields(self):
        from tradingagents.screening.engine import ScreeningEngine

        eng = ScreeningEngine.__new__(ScreeningEngine)
        info = {
            "marketCap": 1_500_000_000,
            "trailingPE": None,
            "forwardPE": None,
            "numberOfAnalystOpinions": 0,
            "recommendationMean": None,
            "targetMeanPrice": None,
        }
        self.assertTrue(eng._has_sparse_fundamentals(info))

    def test_has_sparse_fundamentals_false_when_valuation_present(self):
        from tradingagents.screening.engine import ScreeningEngine

        eng = ScreeningEngine.__new__(ScreeningEngine)
        info = {
            "marketCap": 1_500_000_000,
            "forwardPE": 18.2,
            "numberOfAnalystOpinions": 0,
            "recommendationMean": None,
            "targetMeanPrice": None,
        }
        self.assertFalse(eng._has_sparse_fundamentals(info))

    def test_has_sparse_fundamentals_false_when_analyst_fields_present(self):
        from tradingagents.screening.engine import ScreeningEngine

        eng = ScreeningEngine.__new__(ScreeningEngine)
        info = {
            "marketCap": 1_500_000_000,
            "trailingPE": None,
            "forwardPE": None,
            "numberOfAnalystOpinions": 12,
            "recommendationMean": 2.3,
            "targetMeanPrice": 127.0,
        }
        self.assertFalse(eng._has_sparse_fundamentals(info))


class TestFundamentalsCooldown(unittest.TestCase):
    def setUp(self):
        from tradingagents.dataflows.cache import get_cache

        self.cache = get_cache()
        self.cache.clear()

    def test_estimate_revisions_skips_yf_when_cooldown_marker_set(self):
        from tradingagents.dataflows import yfinance_extended as yfx

        ticker = "ZZZZ"
        yfx._mark_fundamentals_temporarily_unavailable(ticker, "unit_test_marker")

        with patch.object(yfx.yf, "Ticker", side_effect=AssertionError("Ticker() should not be called under cooldown")):
            data = yfx.get_estimate_revisions(ticker)

        self.assertEqual(data.get("ticker"), ticker)
        self.assertEqual(data.get("note"), "sparse_fundamentals_cooldown")

    def test_rating_changes_skips_yf_when_cooldown_marker_set(self):
        from tradingagents.dataflows import yfinance_extended as yfx

        ticker = "ZZZY"
        yfx._mark_fundamentals_temporarily_unavailable(ticker, "unit_test_marker")

        with patch.object(yfx.yf, "Ticker", side_effect=AssertionError("Ticker() should not be called under cooldown")):
            data = yfx.get_rating_changes(ticker)

        self.assertEqual(data.get("ticker"), ticker)
        self.assertEqual(data.get("note"), "sparse_fundamentals_cooldown")
        self.assertEqual(data.get("actions"), [])


if __name__ == "__main__":
    unittest.main()

