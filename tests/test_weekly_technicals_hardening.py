"""Regression coverage for weekly technicals shape/conversion hardening."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from tradingagents.dataflows.yfinance_extended import (
    _extract_close_series,
    _safe_float,
    get_weekly_technicals,
)


class TestSafeFloatSeriesHandling(unittest.TestCase):
    def test_scalar_series_last_value(self):
        self.assertAlmostEqual(_safe_float(pd.Series([1.0, 2.5]).iloc[-1]), 2.5)

    def test_single_column_dataframe(self):
        df = pd.DataFrame({"Close": [10.0, 11.0, 12.0]})
        self.assertAlmostEqual(_safe_float(df.iloc[-1]), 12.0)

    def test_multi_column_dataframe_does_not_raise(self):
        df = pd.DataFrame({"A": [1.0, 2.0], "B": [3.0, 4.0]})
        self.assertAlmostEqual(_safe_float(df.iloc[-1]), 4.0)


class TestExtractCloseSeries(unittest.TestCase):
    def test_multiindex_single_ticker(self):
        cols = pd.MultiIndex.from_tuples(
            [("Close", "CTNM"), ("High", "CTNM"), ("Low", "CTNM")],
            names=["Price", "Ticker"],
        )
        df = pd.DataFrame(
            {"Close": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23],
             "High": [0] * 14,
             "Low": [0] * 14},
        )
        df.columns = cols
        close = _extract_close_series(df, "CTNM")
        self.assertEqual(len(close), 14)
        self.assertAlmostEqual(float(close.iloc[-1]), 23.0)

    def test_two_column_close_frame_squeezes_to_series(self):
        cols = pd.MultiIndex.from_tuples(
            [("Close", "AAA"), ("Close", "BBB")],
            names=["Price", "Ticker"],
        )
        df = pd.DataFrame([[1.0, 2.0], [3.0, 4.0]], columns=cols)
        close = _extract_close_series(df, "AAA")
        self.assertIsInstance(close, pd.Series)
        self.assertEqual(len(close), 2)


class TestGetWeeklyTechnicalsHardening(unittest.TestCase):
    @patch("tradingagents.dataflows.yfinance_extended.yf.download")
    def test_two_column_close_download_computes_without_warning(self, mock_download):
        cols = pd.MultiIndex.from_tuples(
            [("Close", "CTNM"), ("Close", "DUP")],
            names=["Price", "Ticker"],
        )
        n = 30
        df = pd.DataFrame(
            {("Close", "CTNM"): [10 + i * 0.5 for i in range(n)],
             ("Close", "DUP"): [20 + i * 0.5 for i in range(n)]},
        )
        df.columns = cols
        mock_download.return_value = df

        class _FakeCache:
            def get(self, *_a, **_k):
                return None

            def set(self, *_a, **_k):
                pass

        with patch("tradingagents.dataflows.yfinance_extended.get_cache", return_value=_FakeCache()):
            result = get_weekly_technicals("CTNM")

        self.assertIsNotNone(result.get("weekly_close"))
        self.assertIsNotNone(result.get("confluence_score"))

    @patch("tradingagents.dataflows.yfinance_extended.yf.download")
    def test_partial_cache_is_not_served(self, mock_download):
        cols = pd.MultiIndex.from_tuples([("Close", "BAD")], names=["Price", "Ticker"])
        df = pd.DataFrame({("Close", "BAD"): [10 + i for i in range(30)]})
        df.columns = cols
        mock_download.return_value = df

        class _FakeCache:
            def __init__(self):
                self.store = {"weekly_technicals:BAD:latest": {"ticker": "BAD", "weekly_rsi": 50.0}}

            def get(self, data_type, *args):
                return self.store.get(":".join([data_type, *args]))

            def set(self, data_type, *args, data=None, ttl=None):
                self.store[":".join([data_type, *args])] = data

        with patch("tradingagents.dataflows.yfinance_extended.get_cache", return_value=_FakeCache()):
            result = get_weekly_technicals("BAD")

        self.assertIsNotNone(result.get("weekly_close"))
        self.assertIsNotNone(result.get("confluence_score"))

    @patch("tradingagents.dataflows.yfinance_extended.yf.download")
    def test_exception_path_is_not_cached(self, mock_download):
        mock_download.side_effect = RuntimeError("network down")

        class _FakeCache:
            def __init__(self):
                self.sets = 0

            def get(self, *_a, **_k):
                return None

            def set(self, *_a, **_k):
                self.sets += 1

        fake = _FakeCache()
        with patch("tradingagents.dataflows.yfinance_extended.get_cache", return_value=fake):
            result = get_weekly_technicals("FAIL")

        self.assertNotIn("weekly_close", result)
        self.assertEqual(fake.sets, 0)


if __name__ == "__main__":
    unittest.main()
