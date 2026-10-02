"""
Process-wide yfinance rate-limit breaker.

Yahoo Finance has no documented rate limits but aggressively throttles clients
that burst (returning ``Too Many Requests`` / ``YFRateLimitError``). This module
gives every yfinance caller a single shared view of throttle state:

* a minimum inter-call spacing (token bucket) to smooth bursts
* a circuit breaker that opens on 429 / ``Too Many Requests`` responses,
  with exponential backoff (5m -> 10m -> 20m -> 30m), and resets on success
* a ``cooldown_remaining()`` helper so loops (alert monitor, screening
  scheduler) can sleep the breaker out rather than hammering it

Usage::

    from tradingagents.dataflows.yfinance_limiter import get_yfinance_limiter

    limiter = get_yfinance_limiter()
    if limiter.is_open():
        return None  # breaker is open, skip fetch
    limiter.acquire()
    try:
        data = yf.Ticker(sym).history(...)
    except Exception as exc:
        if limiter.record_if_rate_limited(exc):
            return None
        raise
    limiter.record_success()

The module is intentionally small, dependency-free, and defensive (never
raises from the hot path).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("tradingagents.dataflows.yfinance_limiter")


_RATE_LIMIT_MARKERS = (
    "too many requests",
    "rate limit",
    "rate-limit",
    "rate limited",
    "yfratelimiterror",
    "429",
    # Yahoo invalidates the session crumb under concurrent/bursty load and
    # returns 401s instead of a 429. Functionally the same throttle signal —
    # without treating it as one, these calls retry with zero backoff and
    # can compound into a sustained failure storm as universe size grows.
    "invalid crumb",
    "error 401",
)


def _looks_rate_limited(exc: BaseException) -> bool:
    """Heuristic: does this exception look like a Yahoo throttle response?"""
    if exc is None:
        return False
    msg = str(exc).lower()
    if not msg:
        return False
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


class YFinanceLimiter:
    """Thread-safe circuit breaker + token bucket for yfinance calls."""

    def __init__(
        self,
        min_interval_seconds: float = 0.1,
        initial_cooldown_seconds: float = 300.0,   # 5 min
        max_cooldown_seconds: float = 1800.0,      # 30 min
        backoff_multiplier: float = 2.0,
    ) -> None:
        self._min_interval = max(0.0, float(min_interval_seconds))
        self._initial_cooldown = max(1.0, float(initial_cooldown_seconds))
        self._max_cooldown = max(self._initial_cooldown, float(max_cooldown_seconds))
        self._backoff_multiplier = max(1.0, float(backoff_multiplier))

        self._lock = threading.Lock()
        self._last_call_ts = 0.0
        self._open_until_ts = 0.0
        self._current_cooldown = self._initial_cooldown
        self._consecutive_trips = 0
        self._total_trips = 0
        self._last_trip_reason = ""
        self._last_trip_at = 0.0

    # ------------------------------------------------------------------
    # Breaker state
    # ------------------------------------------------------------------

    def is_open(self) -> bool:
        """Return True while the breaker is tripped."""
        with self._lock:
            return self._open_until_ts > time.monotonic()

    def cooldown_remaining(self) -> float:
        """Seconds until the breaker closes (0 if closed)."""
        with self._lock:
            remaining = self._open_until_ts - time.monotonic()
            return max(0.0, remaining)

    def status(self) -> dict:
        """Snapshot of breaker state for diagnostics / HTTP endpoints."""
        with self._lock:
            now = time.monotonic()
            return {
                "open": self._open_until_ts > now,
                "cooldown_remaining_seconds": max(0.0, self._open_until_ts - now),
                "current_cooldown_seconds": self._current_cooldown,
                "consecutive_trips": self._consecutive_trips,
                "total_trips": self._total_trips,
                "last_trip_reason": self._last_trip_reason,
                "last_trip_at_monotonic": self._last_trip_at,
            }

    # ------------------------------------------------------------------
    # Hot path
    # ------------------------------------------------------------------

    def acquire(self, block: bool = True) -> bool:
        """Wait for the inter-call spacing budget.

        Returns False if ``block=False`` and the spacing budget would require
        waiting. Returns False if the breaker is currently open.
        """
        with self._lock:
            now = time.monotonic()
            if self._open_until_ts > now:
                return False
            deficit = (self._last_call_ts + self._min_interval) - now
            if deficit > 0:
                if not block:
                    return False
            else:
                deficit = 0.0
            wait = deficit
            self._last_call_ts = now + wait
        if wait > 0:
            time.sleep(wait)
        return True

    def record_success(self) -> None:
        """Signal that a call completed successfully; resets backoff."""
        with self._lock:
            if self._consecutive_trips:
                logger.info(
                    "yfinance limiter: success after %d consecutive throttles; "
                    "cooldown reset to %.0fs",
                    self._consecutive_trips,
                    self._initial_cooldown,
                )
            self._consecutive_trips = 0
            self._current_cooldown = self._initial_cooldown
            self._open_until_ts = 0.0

    def record_rate_limit(self, reason: str = "") -> float:
        """Trip the breaker unconditionally. Returns new cooldown seconds."""
        with self._lock:
            now = time.monotonic()
            if self._open_until_ts > now:
                # Already open; don't escalate backoff on piled-up callers.
                return max(0.0, self._open_until_ts - now)
            self._consecutive_trips += 1
            self._total_trips += 1
            cooldown = self._current_cooldown
            self._open_until_ts = now + cooldown
            # Escalate for next trip, capped.
            self._current_cooldown = min(
                self._max_cooldown,
                self._current_cooldown * self._backoff_multiplier,
            )
            self._last_trip_reason = (reason or "rate_limited")[:200]
            self._last_trip_at = now
        logger.warning(
            "yfinance limiter: breaker OPEN for %.0fs (reason=%s, consecutive=%d, total=%d)",
            cooldown,
            reason or "rate_limited",
            self._consecutive_trips,
            self._total_trips,
        )
        return cooldown

    def record_if_rate_limited(self, exc: BaseException) -> bool:
        """Trip the breaker if the exception looks like a rate-limit response.

        Returns True if the breaker was tripped (caller should treat the call
        as a soft failure and move on), False otherwise.
        """
        if not _looks_rate_limited(exc):
            return False
        self.record_rate_limit(reason=str(exc)[:200])
        return True

    def reset(self) -> None:
        """Force-close the breaker (admin/debug only)."""
        with self._lock:
            self._open_until_ts = 0.0
            self._consecutive_trips = 0
            self._current_cooldown = self._initial_cooldown


_GLOBAL_LIMITER: Optional[YFinanceLimiter] = None
_GLOBAL_LOCK = threading.Lock()


def get_yfinance_limiter() -> YFinanceLimiter:
    """Return (lazily constructing) the process-wide yfinance limiter."""
    global _GLOBAL_LIMITER
    if _GLOBAL_LIMITER is not None:
        return _GLOBAL_LIMITER
    with _GLOBAL_LOCK:
        if _GLOBAL_LIMITER is None:
            _GLOBAL_LIMITER = YFinanceLimiter()
    return _GLOBAL_LIMITER
