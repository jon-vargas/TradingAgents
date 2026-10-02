"""Vendor unavailable vs empty market (#1386)."""
import unittest
from unittest.mock import MagicMock, patch

from tradingagents.dataflows.stockstats_utils import StockstatsUtils
from tradingagents.dataflows.vendor_errors import (
    annotate_vendor_payload,
    format_vendor_unavailable,
    is_vendor_unavailable_payload,
    should_persist_ticker_health_failure,
)
from tradingagents.dataflows.y_finance import get_YFin_data_online
from tradingagents.screening.engine import ScreeningEngine


class TestVendorUnavailableSemantics(unittest.TestCase):
    def test_format_and_detect(self):
        msg = format_vendor_unavailable("get_stock_data", "yfinance", "rate limit")
        self.assertTrue(is_vendor_unavailable_payload(msg))
        self.assertFalse(is_vendor_unavailable_payload("No data found for symbol 'X'"))

    @patch("tradingagents.dataflows.y_finance.get_yfinance_limiter")
    def test_yfinance_breaker_open_returns_unavailable(self, mock_get_limiter):
        limiter = mock_get_limiter.return_value
        limiter.is_open.return_value = True
        out = get_YFin_data_online("AAPL", "2026-01-01", "2026-01-10")
        self.assertTrue(is_vendor_unavailable_payload(out))

    def test_retry_later_note_and_health_skip(self):
        msg = annotate_vendor_payload(
            format_vendor_unavailable("get_stock_data", "yfinance", "rate limit")
        )
        self.assertIn("Retry later", msg)
        self.assertIn("not evidence the symbol is delisted", msg)
        self.assertFalse(should_persist_ticker_health_failure(msg))
        self.assertFalse(should_persist_ticker_health_failure("vendor_unavailable: breaker"))
        self.assertTrue(should_persist_ticker_health_failure("insufficient_ohlcv_history"))

    @patch("tradingagents.dataflows.yfinance_limiter.get_yfinance_limiter")
    @patch("tradingagents.dataflows.stockstats_utils.get_config")
    def test_stockstats_breaker_returns_unavailable_string(self, mock_cfg, mock_limiter):
        mock_cfg.return_value = {
            "data_vendors": {"technical_indicators": "yfinance"},
            "data_cache_dir": "/tmp",
        }
        mock_limiter.return_value.is_open.return_value = True
        out = StockstatsUtils.get_stock_stats("AAPL", "rsi", "2026-01-02")
        self.assertTrue(is_vendor_unavailable_payload(out))
        self.assertIn("Retry later", str(out))

    def test_screening_unavailable_ohlcv_skips_health_write(self):
        db = MagicMock()
        engine = ScreeningEngine(
            config={"screening": {"ticker_health": {"track": True}}},
            db=db,
        )
        with patch.object(engine.cache, "get", return_value=None), patch(
            "tradingagents.dataflows.screening_ohlcv_cache.load_ticker_bars",
            return_value=({}, ["AAPL"]),
        ), patch.object(engine, "_download_ohlcv_with_retry", return_value=None):
            batch = engine._fetch_batch_ohlcv(["AAPL"], "2026-01-15")
        self.assertEqual(batch, {})
        db.record_ticker_failure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
