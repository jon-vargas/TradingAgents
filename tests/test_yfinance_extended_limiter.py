"""Regression coverage for the yfinance_limiter <-> _call_with_timeout wiring.

Every Tier-2/enhanced yfinance helper (options, ownership, valuation,
earnings, insider, estimate/rating revisions, weekly technicals) funnels
through ``_call_with_timeout``. This is the single choke point where the
shared circuit breaker must see successes/failures, otherwise a sustained
401 ("Invalid Crumb")/429 storm silently retries with no backoff.
"""
import unittest
from unittest.mock import patch

from tradingagents.dataflows import yfinance_extended as ye
from tradingagents.dataflows.yfinance_limiter import YFinanceLimiter


class TestCallWithTimeoutLimiterIntegration(unittest.TestCase):
    def _fresh_limiter(self, **kwargs):
        kwargs.setdefault("min_interval_seconds", 0.0)
        return YFinanceLimiter(**kwargs)

    def test_success_records_success_and_returns_value(self):
        limiter = self._fresh_limiter()
        with patch.object(ye, "get_yfinance_limiter", return_value=limiter):
            result = ye._call_with_timeout(lambda: 42, default=None, op="test")
        self.assertEqual(result, 42)
        self.assertFalse(limiter.is_open())

    def test_rate_limit_exception_trips_breaker(self):
        limiter = self._fresh_limiter(initial_cooldown_seconds=60.0)

        def _boom():
            raise Exception("HTTP Error 401: Invalid Crumb")

        with patch.object(ye, "get_yfinance_limiter", return_value=limiter):
            result = ye._call_with_timeout(_boom, default="fallback", op="test")
        self.assertEqual(result, "fallback")
        self.assertTrue(limiter.is_open())

    def test_open_breaker_short_circuits_without_calling_fn(self):
        limiter = self._fresh_limiter(initial_cooldown_seconds=60.0)
        limiter.record_rate_limit("429")
        calls = []

        def _fn():
            calls.append(1)
            return "should not run"

        with patch.object(ye, "get_yfinance_limiter", return_value=limiter):
            result = ye._call_with_timeout(_fn, default="skipped", op="test")
        self.assertEqual(result, "skipped")
        self.assertEqual(calls, [])

    def test_unrelated_exception_does_not_trip_breaker(self):
        limiter = self._fresh_limiter()

        def _boom():
            raise ValueError("symbol not found")

        with patch.object(ye, "get_yfinance_limiter", return_value=limiter):
            result = ye._call_with_timeout(_boom, default=None, op="test")
        self.assertIsNone(result)
        self.assertFalse(limiter.is_open())


if __name__ == "__main__":
    unittest.main()
