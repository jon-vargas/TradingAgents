"""
AlertMonitor — Background thread that polls alert rules and fires alerts.

Adapts polling frequency automatically:
  - During US market hours (9:30 AM – 4:00 PM ET, weekdays): fast interval
  - Outside market hours: slow interval (reduces API usage / noise)

Groups rules by ticker to minimize API calls.
Integrates with FastAPI lifecycle (startup/shutdown).
"""

import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from tradingagents.reporting.database import ResearchDatabase, get_db
from tradingagents.screening.alert_evaluator import AlertEvaluator

logger = logging.getLogger("tradingagents.alerts")

# US Eastern timezone offset helpers (no pytz dependency)
_ET_OFFSET_STANDARD = timedelta(hours=-5)  # EST
_ET_OFFSET_DST = timedelta(hours=-4)       # EDT


def _is_us_dst(dt_utc: datetime) -> bool:
    """Rough US DST check: second Sunday in March to first Sunday in November."""
    year = dt_utc.year
    # Second Sunday of March
    mar1 = datetime(year, 3, 1)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    # First Sunday of November
    nov1 = datetime(year, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return dst_start <= dt_utc.replace(tzinfo=None) < dst_end


def _now_et() -> datetime:
    """Return current datetime in US Eastern time (naive)."""
    utc_now = datetime.now(timezone.utc)
    offset = _ET_OFFSET_DST if _is_us_dst(utc_now) else _ET_OFFSET_STANDARD
    return (utc_now + offset).replace(tzinfo=None)


def _is_market_hours() -> bool:
    """True if right now is during US equity market hours (weekday 9:30-16:00 ET)."""
    et = _now_et()
    if et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= et < market_close


class AlertMonitor:
    """
    Background alert polling service with adaptive intervals.

    Usage:
        monitor = AlertMonitor(db_path="research.db")
        monitor.start()  # On app startup
        monitor.stop()   # On app shutdown
    """

    def __init__(
        self,
        db_path: str = "research.db",
        check_interval: int = 300,
        market_hours_interval: int = 120,
        off_hours_interval: int = 900,
    ):
        """
        Args:
            db_path: Path to SQLite database
            check_interval: Legacy fallback interval (kept for API compat)
            market_hours_interval: Seconds between polls during market hours (default: 2 min)
            off_hours_interval: Seconds between polls outside market hours (default: 15 min)
        """
        self.db_path = db_path
        self.check_interval = check_interval  # Exposed for status API
        self.market_hours_interval = market_hours_interval
        self.off_hours_interval = off_hours_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._evaluator = AlertEvaluator(db=get_db(db_path))
        self._running = False
        self._lifecycle_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def active_interval(self) -> int:
        """Return the current effective check interval based on market hours."""
        if _is_market_hours():
            return self.market_hours_interval
        return self.off_hours_interval

    @property
    def is_market_hours(self) -> bool:
        """Whether it's currently US market hours."""
        return _is_market_hours()

    def start(self):
        """Start the alert monitoring thread."""
        with self._lifecycle_lock:
            if self.is_running:
                logger.info("Already running, skipping start")
                return

            self._stop_event.clear()
            self._running = True  # Set before start for consistent is_running
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="AlertMonitorThread",
                daemon=True,  # Dies with the process
            )
            self._thread.start()
            market_status = "MARKET OPEN" if _is_market_hours() else "AFTER HOURS"
            logger.info("Started (%s, interval=%ds)", market_status, self.active_interval)

    def stop(self):
        """Signal the thread to stop and wait for it to finish."""
        with self._lifecycle_lock:
            if not self.is_running:
                return
            logger.info("Stopping...")
            self._stop_event.set()
        # Join outside the lock so the poll_loop can finish cleanly
        if self._thread:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning("Thread did not stop within 10s")
        self._running = False
        logger.info("Stopped")

    def _poll_loop(self):
        """Main polling loop: fetch rules, evaluate, fire alerts.

        Adapts wait interval based on whether the US stock market is open.
        If the shared yfinance breaker is open, the loop sleeps for at least
        the breaker's remaining cooldown so we don't hammer a throttled source.
        """
        # Initial delay to let the server fully start
        self._stop_event.wait(5)

        while not self._stop_event.is_set():
            try:
                from tradingagents.dataflows.yfinance_limiter import get_yfinance_limiter
                limiter = get_yfinance_limiter()
                if limiter.is_open():
                    remaining = int(limiter.cooldown_remaining()) + 1
                    logger.warning(
                        "yfinance breaker OPEN; skipping cycle, sleeping %ds",
                        remaining,
                    )
                    self._stop_event.wait(remaining)
                    continue
            except Exception:
                # Limiter is best-effort; never block the loop on import/state issues.
                pass

            try:
                self._run_cycle()
            except Exception as e:
                logger.error("Error in poll cycle: %s", e)

            # Pick interval based on market hours; if the breaker was tripped
            # inside the cycle, honor its remaining cooldown too.
            interval = self.active_interval
            try:
                from tradingagents.dataflows.yfinance_limiter import get_yfinance_limiter
                remaining = int(get_yfinance_limiter().cooldown_remaining())
                if remaining > interval:
                    interval = remaining
            except Exception:
                pass
            self._stop_event.wait(interval)

    def _run_cycle(self):
        """Single evaluation cycle: check all active rules.

        Bounded by a wall-clock budget so a single stuck yfinance call
        inside ``evaluate_rule`` (e.g. ``.option_chain`` / ``.info``) can
        no longer pin subsequent cycles forever.
        """
        import time

        db = get_db(self.db_path)
        rules = db.get_active_rules()

        if not rules:
            return

        # Clear per-cycle cache (so each cycle gets fresh data)
        self._evaluator.clear_cache()

        # Group rules by ticker for efficient data fetching
        ticker_rules = {}
        for rule in rules:
            ticker = rule["ticker"]
            if ticker not in ticker_rules:
                ticker_rules[ticker] = []
            ticker_rules[ticker].append(rule)

        # Cap the entire cycle at roughly the polling interval — there is
        # no value in spending two polls processing one. We give ourselves
        # 80% of the active interval so the next cycle can still start on
        # time even if we abandoned work.
        cycle_deadline = time.monotonic() + max(30.0, 0.8 * float(self.active_interval))

        alerts_fired = 0
        rules_checked = 0
        rules_skipped_deadline = 0

        for ticker, ticker_rule_list in ticker_rules.items():
            for rule in ticker_rule_list:
                if time.monotonic() >= cycle_deadline:
                    rules_skipped_deadline += 1
                    continue
                rules_checked += 1
                try:
                    triggered, message, data = self._evaluator.evaluate_rule(rule)
                    if triggered and message:
                        db.fire_alert(rule["id"], message, data)
                        alerts_fired += 1
                        logger.info("FIRED: %s", message)
                except Exception as e:
                    logger.error("Error evaluating rule #%d (%s): %s", rule['id'], ticker, e)

        if rules_skipped_deadline:
            logger.warning(
                "Cycle hit deadline; skipped %d rule(s). Will retry next cycle.",
                rules_skipped_deadline,
            )

        if self._evaluator.rate_limited_this_cycle:
            logger.warning(
                "Cycle rate-limited by yfinance: %d ticker skips (breaker-aware)",
                self._evaluator.rate_limited_count_this_cycle,
            )
        if alerts_fired > 0:
            logger.info("Cycle complete: %d rules checked, %d alerts fired", rules_checked, alerts_fired)
