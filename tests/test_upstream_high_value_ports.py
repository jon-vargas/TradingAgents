"""Acceptance tests for the upstream high-value ports (path maps, prompts, stale OHLCV, CSV TTL)."""
import inspect
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from tradingagents.agents.analysts import news_analyst, social_media_analyst
from tradingagents.dataflows.stockstats_utils import (
    MAX_OHLCV_STALE_DAYS,
    OHLCV_CACHE_TTL_SECONDS,
    _assert_ohlcv_not_stale,
    _needs_same_day_refresh,
    load_or_refresh_ohlcv_csv,
)
from tradingagents.dataflows.y_finance import get_YFin_data_online
from tradingagents.graph.setup import (
    DEBATE_PATH_MAP,
    RISK_ANALYSIS_PATH_MAP,
    GraphSetup,
)


class TestPathMaps(unittest.TestCase):
    def test_debate_path_map_is_complete(self):
        expected = {
            "Bull Researcher": "Bull Researcher",
            "Bear Researcher": "Bear Researcher",
            "Research Manager": "Research Manager",
        }
        self.assertEqual(DEBATE_PATH_MAP, expected)

    def test_risk_path_map_is_complete(self):
        expected = {
            "Risky Analyst": "Risky Analyst",
            "Safe Analyst": "Safe Analyst",
            "Neutral Analyst": "Neutral Analyst",
            "Risk Judge": "Risk Judge",
        }
        self.assertEqual(RISK_ANALYSIS_PATH_MAP, expected)

    def test_setup_graph_uses_module_level_maps_on_all_five_edges(self):
        src = inspect.getsource(GraphSetup.setup_graph)
        # Trailing comma marks the path-map argument on add_conditional_edges.
        self.assertEqual(src.count("DEBATE_PATH_MAP,"), 2)
        self.assertEqual(src.count("RISK_ANALYSIS_PATH_MAP,"), 3)
        self.assertNotIn('"Bear Researcher": "Bear Researcher"', src)
        self.assertNotIn('"Safe Analyst": "Safe Analyst"', src)
        self.assertNotIn('"Risk Judge": "Risk Judge"', src)


class TestNewsPromptSignatures(unittest.TestCase):
    def test_news_analyst_uses_ticker_not_query(self):
        src = inspect.getsource(news_analyst)
        self.assertIn("get_news(ticker", src)
        self.assertNotIn("get_news(query", src)

    def test_social_analyst_uses_ticker_not_query(self):
        src = inspect.getsource(social_media_analyst)
        self.assertIn("get_news(ticker", src)
        self.assertNotIn("get_news(query", src)


def _history_frame(last_date: str):
    idx = pd.DatetimeIndex([last_date])
    return pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Adj Close": [10.5],
            "Volume": [1000],
        },
        index=idx,
    )


class TestStaleOhlcv(unittest.TestCase):
    def test_stale_window_is_ten_calendar_days(self):
        self.assertEqual(MAX_OHLCV_STALE_DAYS, 10)

    def test_assert_compares_naive_calendar_dates(self):
        end = "2026-08-17"
        eleven = _history_frame("2026-08-06")
        three = _history_frame("2026-08-14")
        self.assertIsNotNone(_assert_ohlcv_not_stale(eleven, end))
        self.assertIsNone(_assert_ohlcv_not_stale(three, end))

    def _mock_history(self, last_date):
        inst = MagicMock()
        inst.history.return_value = _history_frame(last_date)
        return inst

    @patch("tradingagents.dataflows.y_finance.yf.Ticker")
    def test_year_old_frame_returns_no_data_found(self, mock_ticker):
        mock_ticker.return_value = self._mock_history("2025-08-17")
        result = get_YFin_data_online("AAPL", "2026-08-01", "2026-08-17")
        self.assertTrue(result.startswith("No data found"))
        self.assertIn("stale", result.lower())

    @patch("tradingagents.dataflows.y_finance.yf.Ticker")
    def test_eleven_calendar_days_before_end_is_stale(self, mock_ticker):
        mock_ticker.return_value = self._mock_history("2026-08-06")
        result = get_YFin_data_online("AAPL", "2026-07-01", "2026-08-17")
        self.assertTrue(result.startswith("No data found"))
        self.assertIn("stale", result.lower())

    @patch("tradingagents.dataflows.y_finance.yf.Ticker")
    def test_three_calendar_days_before_end_returns_csv(self, mock_ticker):
        mock_ticker.return_value = self._mock_history("2026-08-14")
        result = get_YFin_data_online("AAPL", "2026-08-01", "2026-08-17")
        self.assertFalse(result.startswith("No data found"))
        self.assertIn("Close", result)

    @patch("tradingagents.dataflows.y_finance.yf.Ticker")
    def test_same_window_frame_returns_csv(self, mock_ticker):
        mock_ticker.return_value = self._mock_history("2026-08-17")
        result = get_YFin_data_online("AAPL", "2026-08-01", "2026-08-17")
        self.assertFalse(result.startswith("No data found"))
        self.assertIn("Close", result)


class TestSameDayCacheTtl(unittest.TestCase):
    TODAY = pd.Timestamp("2026-08-17")
    FRIDAY = pd.Timestamp("2026-08-14")

    def _write(self, tmp_path, name="cache.csv", age_seconds=0.0, last_date="2026-08-16"):
        path = os.path.join(tmp_path, name)
        pd.DataFrame({"Date": [last_date], "Close": [100.0]}).to_csv(path, index=False)
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(path, (old, old))
        return path

    def test_today_named_old_mtime_refreshes_even_if_curr_date_is_friday(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                name="AAPL-YFin-data-2011-08-17-2026-08-17.csv",
                age_seconds=OHLCV_CACHE_TTL_SECONDS + 60,
            )
            self.assertTrue(
                _needs_same_day_refresh(
                    path,
                    "2026-08-17",
                    today_date=self.TODAY,
                    curr_date=self.FRIDAY,
                )
            )

    def test_fresh_current_day_file_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, last_date="2026-08-17")
            self.assertFalse(
                _needs_same_day_refresh(
                    path, "2026-08-17", today_date=self.TODAY
                )
            )

    def test_non_today_end_date_never_refreshes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                name="AAPL-YFin-data-2011-05-01-2026-05-01.csv",
                age_seconds=OHLCV_CACHE_TTL_SECONDS + 60,
                last_date="2026-04-30",
            )
            self.assertFalse(
                _needs_same_day_refresh(
                    path, "2026-05-01", today_date=self.TODAY
                )
            )

    @patch("tradingagents.dataflows.stockstats_utils.yf.download")
    def test_load_or_refresh_overwrites_stale_today_file(self, mock_download):
        mock_download.return_value = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-08-16", "2026-08-17"]),
                "Close": [100.0, 222.0],
            }
        ).set_index("Date")

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                name="AAPL-YFin-data-2011-08-17-2026-08-17.csv",
                age_seconds=OHLCV_CACHE_TTL_SECONDS + 60,
                last_date="2026-08-16",
            )
            out = load_or_refresh_ohlcv_csv(
                "AAPL",
                "2011-08-17",
                "2026-08-17",
                path,
                today_date=self.TODAY,
                curr_date=self.FRIDAY,
            )
            self.assertTrue(mock_download.called)
            self.assertIn(222.0, set(out["Close"].tolist()))
            rewritten = pd.read_csv(path)
            self.assertIn(222.0, set(rewritten["Close"].tolist()))

    @patch("tradingagents.dataflows.stockstats_utils.yf.download")
    def test_load_or_refresh_reuses_fresh_file(self, mock_download):
        mock_download.side_effect = AssertionError("fresh cache must not refetch")
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, last_date="2026-08-17")
            out = load_or_refresh_ohlcv_csv(
                "AAPL",
                "2011-08-17",
                "2026-08-17",
                path,
                today_date=self.TODAY,
            )
            self.assertIn(100.0, set(out["Close"].tolist()))


class TestBothCacheSitesUseSharedHelper(unittest.TestCase):
    def test_stockstats_and_bulk_call_shared_helper(self):
        from tradingagents.dataflows import stockstats_utils, y_finance

        stats_src = inspect.getsource(stockstats_utils.StockstatsUtils.get_stock_stats)
        bulk_src = inspect.getsource(y_finance._get_stock_stats_bulk)
        self.assertIn("load_or_refresh_ohlcv_csv", stats_src)
        self.assertIn("load_or_refresh_ohlcv_csv", bulk_src)
        self.assertNotIn("yf.download", stats_src)
        self.assertNotIn("yf.download", bulk_src)


if __name__ == "__main__":
    unittest.main()
