import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


class _OpenLimiter:
    def is_open(self):
        return True

    def cooldown_remaining(self):
        return 123.0

    def acquire(self, block=True):
        return False

    def record_if_rate_limited(self, exc):
        return False

    def record_success(self):
        return None


class TestEngineBreakerIntegration(unittest.TestCase):
    def test_ohlcv_download_returns_none_when_breaker_open(self):
        from tradingagents.screening.engine import ScreeningEngine

        engine = ScreeningEngine(config={"screening": {}}, db=None)
        start = datetime.now() - timedelta(days=10)
        end = datetime.now()

        with patch(
            "tradingagents.screening.engine.get_yfinance_limiter",
            return_value=_OpenLimiter(),
        ), patch("tradingagents.screening.engine.yf.download") as mock_download:
            out = engine._download_ohlcv_with_retry(["AAPL"], start, end, is_single=True)

        self.assertIsNone(out)
        mock_download.assert_not_called()

    def test_spy_download_returns_none_when_breaker_open(self):
        from tradingagents.screening.engine import ScreeningEngine

        engine = ScreeningEngine(config={"screening": {}}, db=None)
        start = datetime.now() - timedelta(days=10)
        end = datetime.now()

        with patch(
            "tradingagents.screening.engine.get_yfinance_limiter",
            return_value=_OpenLimiter(),
        ), patch("tradingagents.screening.engine.yf.download") as mock_download:
            out = engine._download_spy_with_retry(start, end)

        self.assertIsNone(out)
        mock_download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
