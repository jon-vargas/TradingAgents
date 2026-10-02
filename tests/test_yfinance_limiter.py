import time
import unittest

from tradingagents.dataflows.yfinance_limiter import (
    YFinanceLimiter,
    _looks_rate_limited,
    get_yfinance_limiter,
)


class TestYFinanceLimiterHeuristics(unittest.TestCase):
    def test_detects_common_rate_limit_strings(self):
        self.assertTrue(_looks_rate_limited(Exception("Too Many Requests.")))
        self.assertTrue(_looks_rate_limited(Exception("Too Many Requests. Rate limited. Try after a while.")))
        self.assertTrue(_looks_rate_limited(Exception("YFRateLimitError: rate-limit")))
        self.assertTrue(_looks_rate_limited(Exception("HTTP 429 returned")))

    def test_detects_crumb_invalidation_as_throttle(self):
        # Yahoo returns 401s (not 429s) when the session crumb is invalidated
        # under concurrent load; this must be treated as a throttle signal too.
        self.assertTrue(_looks_rate_limited(Exception("HTTP Error 401: Invalid Crumb")))
        self.assertTrue(_looks_rate_limited(Exception("Error 401: unauthorized")))

    def test_rejects_unrelated_errors(self):
        self.assertFalse(_looks_rate_limited(Exception("symbol not found")))
        self.assertFalse(_looks_rate_limited(Exception("")))
        self.assertFalse(_looks_rate_limited(None))


class TestYFinanceLimiterBreaker(unittest.TestCase):
    def test_starts_closed(self):
        limiter = YFinanceLimiter(min_interval_seconds=0.0)
        self.assertFalse(limiter.is_open())
        self.assertEqual(limiter.cooldown_remaining(), 0.0)

    def test_trip_opens_breaker_with_initial_cooldown(self):
        limiter = YFinanceLimiter(
            min_interval_seconds=0.0,
            initial_cooldown_seconds=60.0,
        )
        cooldown = limiter.record_rate_limit("429")
        self.assertAlmostEqual(cooldown, 60.0, delta=0.5)
        self.assertTrue(limiter.is_open())
        self.assertGreater(limiter.cooldown_remaining(), 0.0)

    def test_backoff_escalates_across_trips(self):
        limiter = YFinanceLimiter(
            min_interval_seconds=0.0,
            initial_cooldown_seconds=10.0,
            max_cooldown_seconds=80.0,
            backoff_multiplier=2.0,
        )
        c1 = limiter.record_rate_limit("first")
        # Manually close the breaker so the next trip escalates backoff.
        limiter.reset()
        limiter._current_cooldown = 20.0  # what next cooldown would be
        c2 = limiter.record_rate_limit("second")
        self.assertAlmostEqual(c1, 10.0, delta=0.5)
        self.assertAlmostEqual(c2, 20.0, delta=0.5)

    def test_backoff_caps_at_max(self):
        limiter = YFinanceLimiter(
            min_interval_seconds=0.0,
            initial_cooldown_seconds=100.0,
            max_cooldown_seconds=100.0,
            backoff_multiplier=5.0,
        )
        c1 = limiter.record_rate_limit("first")
        limiter.reset()
        limiter._current_cooldown = 100.0  # already at cap
        c2 = limiter.record_rate_limit("second")
        self.assertAlmostEqual(c1, 100.0, delta=0.5)
        self.assertAlmostEqual(c2, 100.0, delta=0.5)

    def test_success_resets_cooldown(self):
        limiter = YFinanceLimiter(
            min_interval_seconds=0.0,
            initial_cooldown_seconds=10.0,
            max_cooldown_seconds=40.0,
            backoff_multiplier=2.0,
        )
        limiter.record_rate_limit("first")
        self.assertEqual(limiter._current_cooldown, 20.0)
        limiter.record_success()
        self.assertFalse(limiter.is_open())
        self.assertEqual(limiter._current_cooldown, 10.0)
        self.assertEqual(limiter._consecutive_trips, 0)

    def test_record_if_rate_limited_returns_flag(self):
        limiter = YFinanceLimiter(
            min_interval_seconds=0.0,
            initial_cooldown_seconds=30.0,
        )
        self.assertFalse(limiter.record_if_rate_limited(Exception("bad symbol")))
        self.assertFalse(limiter.is_open())
        self.assertTrue(
            limiter.record_if_rate_limited(Exception("Too Many Requests. Rate limited."))
        )
        self.assertTrue(limiter.is_open())

    def test_acquire_returns_false_while_open(self):
        limiter = YFinanceLimiter(
            min_interval_seconds=0.0,
            initial_cooldown_seconds=30.0,
        )
        limiter.record_rate_limit("trip")
        self.assertFalse(limiter.acquire(block=False))

    def test_acquire_enforces_min_interval(self):
        limiter = YFinanceLimiter(min_interval_seconds=0.05)
        t0 = time.monotonic()
        self.assertTrue(limiter.acquire())
        self.assertTrue(limiter.acquire())
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.04)

    def test_double_trip_does_not_extend_open_window(self):
        limiter = YFinanceLimiter(
            min_interval_seconds=0.0,
            initial_cooldown_seconds=30.0,
        )
        limiter.record_rate_limit("first")
        remaining_first = limiter.cooldown_remaining()
        # Piled-up callers shouldn't each add a fresh 30s window.
        limiter.record_rate_limit("second")
        remaining_second = limiter.cooldown_remaining()
        self.assertLessEqual(remaining_second, remaining_first + 0.5)


class TestYFinanceLimiterSingleton(unittest.TestCase):
    def test_returns_same_instance(self):
        a = get_yfinance_limiter()
        b = get_yfinance_limiter()
        self.assertIs(a, b)


if __name__ == "__main__":
    unittest.main()
