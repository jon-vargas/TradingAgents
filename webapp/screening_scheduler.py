"""
ScreeningScheduler — Background thread that pre-warms screening caches and
runs scheduled scans on built-in and auto-generated watchlists.

Schedule (all times US Eastern):
  - Pre-market warm (8:30 AM): Fetch OHLCV + info for all scannable watchlists
  - Intraday refresh (every 2 hours during market): Re-warm OHLCV data
  - Post-close snapshot (4:15 PM): Full scan on all scannable watchlists, save results
  - Post-close auto-alerts: Create alerts for top N tickers from each scan
  - Post-close auto-analyze: Run full analysis on top N tickers (optional, off by default)
  - Weekly auto-backtest (configurable day): Compute forward returns for past analyses
  - Weekly adaptive weights (configurable day): Recompute signal weights from backtested data
  - Auto-discovery (manual): Triggered on-demand via API/UI to detect trending themes,
    discover tickers via Perplexity, and create/renew/expire auto-watchlists

Reuses the AlertMonitor threading pattern for clean lifecycle management.
"""

import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from tradingagents.reporting.database import ResearchDatabase, get_db
from tradingagents.dataflows.cache import get_cache
from tradingagents.default_config import DEFAULT_CONFIG

logger = logging.getLogger("tradingagents.scheduler")


# US Eastern time helpers (same as alert_monitor.py)
_ET_OFFSET_STANDARD = timedelta(hours=-5)
_ET_OFFSET_DST = timedelta(hours=-4)


def _is_us_dst(dt_utc: datetime) -> bool:
    year = dt_utc.year
    mar1 = datetime(year, 3, 1)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1 = datetime(year, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return dst_start <= dt_utc.replace(tzinfo=None) < dst_end


def _now_et() -> datetime:
    utc_now = datetime.now(timezone.utc)
    offset = _ET_OFFSET_DST if _is_us_dst(utc_now) else _ET_OFFSET_STANDARD
    return (utc_now + offset).replace(tzinfo=None)


def _is_weekday() -> bool:
    return _now_et().weekday() < 5


def _auto_discovery_kwargs(theme_spec: Any, model: str) -> Dict[str, Any]:
    """Build discover_opportunities kwargs so auto inherits Discover catalog gates."""
    return {
        "theme": getattr(theme_spec, "theme", ""),
        "criteria": getattr(theme_spec, "criteria", ""),
        "market_cap_filter": getattr(theme_spec, "market_cap_filter", ""),
        "model": model,
        "skip_cache": True,
        "template_id": getattr(theme_spec, "catalog_id", "") or "",
    }


class ScreeningScheduler:
    """
    Background scheduler for automated screening operations.

    Phases:
    1. Pre-market warm (8:30 AM ET): cache OHLCV for all built-in watchlists
    2. Intraday refresh: re-warm every 2 hours during market hours
    3. Post-close snapshot (4:15 PM ET): full scan + save results

    Usage:
        scheduler = ScreeningScheduler(db_path="research.db")
        scheduler.start()  # On app startup
        scheduler.stop()   # On app shutdown
    """

    def __init__(
        self,
        db_path: str = "research.db",
        poll_interval: int = 300,  # Check schedule every 5 minutes
        config: Dict[str, Any] = None,
    ):
        self.db_path = db_path
        self.poll_interval = poll_interval
        screening_cfg = (config or DEFAULT_CONFIG).get("screening", {})
        self._screening_cfg = screening_cfg
        self._config = screening_cfg.get("scheduler", {})
        self._movers_cfg = screening_cfg.get("movers", {})
        self._intraday_cache_policy = str(
            screening_cfg.get("intraday_cache_policy") or "ttl_only"
        ).strip().lower()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lifecycle_lock = threading.Lock()
        self._last_premarket_date: Optional[str] = None
        self._last_intraday_hour: Optional[int] = None
        self._last_intraday_refresh_time: Optional[str] = None
        self._last_postclose_date: Optional[str] = None
        self._last_backtest_date: Optional[str] = None
        self._last_adaptive_weights_date: Optional[str] = None
        self._last_auto_discovery_date: Optional[str] = None
        self._last_builtin_refresh_month: Optional[str] = None
        self._last_exchange_resolution_hydration_date: Optional[str] = None
        self._last_builtin_refresh_proposal_at: Optional[str] = None
        self._auto_discovery_lock = threading.Lock()
        self._auto_discovery_running = False
        self._auto_discovery_thread: Optional[threading.Thread] = None
        self._current_auto_discovery_run_id: Optional[str] = None
        self._last_auto_discovery_started_at: Optional[str] = None
        self._last_auto_discovery_finished_at: Optional[str] = None
        self._last_auto_discovery_result: Optional[Dict[str, Any]] = None
        self._movers_lock = threading.Lock()
        self._movers_running = False
        self._last_movers_date: Optional[str] = None
        self._last_movers_scan_at: Optional[str] = None
        self._last_movers_snapshot_id: Optional[str] = None
        self._last_movers_error: Optional[str] = None
        # ------------------------------------------------------------------
        # Per-job overlap guards + observable state
        # ------------------------------------------------------------------
        # Each long-running scheduler job gets its own lock/running flag/
        # last-finished-at field so we can (a) refuse to start a second
        # copy of the same job while one is in flight (this is what hung
        # the dashboard during the May 9 incident) and (b) surface state
        # through get_status() for ops visibility.
        self._postclose_lock = threading.Lock()
        self._postclose_running = False
        self._last_postclose_started_at: Optional[str] = None
        self._last_postclose_finished_at: Optional[str] = None
        self._last_postclose_error: Optional[str] = None

        self._premarket_lock = threading.Lock()
        self._premarket_running = False
        self._last_premarket_started_at: Optional[str] = None
        self._last_premarket_finished_at: Optional[str] = None
        self._last_premarket_error: Optional[str] = None

        self._hydration_lock = threading.Lock()
        self._hydration_running = False
        self._last_hydration_started_at: Optional[str] = None
        self._last_hydration_finished_at: Optional[str] = None
        self._last_hydration_error: Optional[str] = None

        self._backtest_lock = threading.Lock()
        self._backtest_running = False
        self._last_backtest_started_at: Optional[str] = None
        self._last_backtest_finished_at: Optional[str] = None
        self._last_backtest_error: Optional[str] = None

        # Per-watchlist wall-clock budget for ScreeningEngine.scan(). At the
        # default of 600s a single bad watchlist can no longer pin the
        # scheduler thread for hours — it returns whatever partial data it
        # has and the next watchlist proceeds.
        scheduler_cfg = screening_cfg.get("scheduler", {})
        self._per_watchlist_scan_budget = max(
            60.0,
            float(scheduler_cfg.get("per_watchlist_scan_budget_seconds", 600.0)),
        )
        # Hard upper bound on the entire post-close snapshot job. Defaults
        # to 1 hour. If we don't finish in time we abandon remaining
        # watchlists and log.
        self._postclose_total_budget = max(
            300.0,
            float(scheduler_cfg.get("postclose_total_budget_seconds", 3600.0)),
        )
        self._premarket_total_budget = max(
            300.0,
            float(scheduler_cfg.get("premarket_total_budget_seconds", 1800.0)),
        )
        self._hydration_total_budget = max(
            120.0,
            float(scheduler_cfg.get("hydration_total_budget_seconds", 600.0)),
        )

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self):
        """Start the scheduler thread."""
        with self._lifecycle_lock:
            if self.is_running:
                logger.info("Already running, skipping start")
                return
            self._stop_event.clear()
            self._running = True  # Set before start so is_running is immediately consistent
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="ScreeningSchedulerThread",
                daemon=True,
            )
            self._thread.start()
            logger.info("Started (poll every %ds)", self.poll_interval)

    def stop(self):
        """Signal the thread to stop and wait for it to finish."""
        with self._lifecycle_lock:
            if not self.is_running:
                return
            logger.info("Stopping...")
            self._stop_event.set()
        # Join outside the lock so poll_loop can finish cleanly
        if self._thread:
            self._thread.join(timeout=15)
        self._running = False
        logger.info("Stopped")

    def get_status(self) -> Dict[str, Any]:
        """Return current status for the API."""
        et = _now_et()
        day_name = et.strftime("%A").lower()
        try:
            postclose_eligible = self._get_scannable_watchlists(apply_postclose_policy=True)
            postclose_eligible_names = [str(w.get("name") or "") for w in postclose_eligible]
        except Exception:
            postclose_eligible_names = []
        return {
            "running": self.is_running,
            "poll_interval": self.poll_interval,
            "current_time_et": et.strftime("%H:%M:%S"),
            "is_weekday": _is_weekday(),
            "postclose_policy": {
                "weekly_run_day": str(self._postclose_policy().get("weekly_run_day", "friday")),
                "eligible_today_count": len(postclose_eligible_names),
                "eligible_today": postclose_eligible_names,
            },
            "last_premarket_warm": self._last_premarket_date,
            "last_intraday_refresh": self._last_intraday_refresh_time,
            "intraday_cache_policy": self._intraday_cache_policy,
            "last_postclose_scan": self._last_postclose_date,
            "last_auto_backtest": self._last_backtest_date,
            "last_adaptive_weights": self._last_adaptive_weights_date,
            "last_auto_discovery": self._last_auto_discovery_date,
            "last_exchange_resolution_hydration": self._last_exchange_resolution_hydration_date,
            "last_builtin_refresh_proposal_at": self._last_builtin_refresh_proposal_at,
            "auto_discovery_running": self.get_auto_discovery_status().get("running", False),
            "movers_enabled": bool(self._movers_cfg.get("enabled", False)),
            "movers_running": bool(self._movers_running),
            "last_movers_date": self._last_movers_date,
            "last_movers_scan_at": self._last_movers_scan_at,
            "last_movers_snapshot_id": self._last_movers_snapshot_id,
            "movers_error": self._last_movers_error,
            "postclose_running": bool(self._postclose_running),
            "last_postclose_started_at": self._last_postclose_started_at,
            "last_postclose_finished_at": self._last_postclose_finished_at,
            "postclose_error": self._last_postclose_error,
            "premarket_running": bool(self._premarket_running),
            "last_premarket_started_at": self._last_premarket_started_at,
            "last_premarket_finished_at": self._last_premarket_finished_at,
            "premarket_error": self._last_premarket_error,
            "hydration_running": bool(self._hydration_running),
            "last_hydration_started_at": self._last_hydration_started_at,
            "last_hydration_finished_at": self._last_hydration_finished_at,
            "hydration_error": self._last_hydration_error,
            "backtest_running": bool(self._backtest_running),
            "last_backtest_started_at": self._last_backtest_started_at,
            "last_backtest_finished_at": self._last_backtest_finished_at,
            "backtest_error": self._last_backtest_error,
            "per_watchlist_scan_budget_seconds": self._per_watchlist_scan_budget,
            "automation": {
                "auto_alerts": self._config.get("auto_alerts", {}).get("enabled", False),
                "auto_analyze": self._config.get("auto_analyze", {}).get("enabled", False),
                "auto_backtest": self._config.get("auto_backtest", {}).get("enabled", False),
                "auto_adaptive_weights": self._config.get("auto_adaptive_weights", {}).get("enabled", False),
                "auto_discovery": self._config.get("auto_discovery", {}).get("enabled", False),
                "exchange_resolution_hydration": self._config.get("exchange_resolution_hydration", {}).get("enabled", False),
                "builtin_refresh": self._config.get("builtin_refresh", {}).get("enabled", False),
                "movers_postclose": bool(
                    self._movers_cfg.get("enabled", False)
                    and self._movers_cfg.get("auto_scan_postclose_enabled", False)
                    and not self._movers_cfg.get("manual_only", True)
                ),
            },
        }

    def get_auto_discovery_status(self) -> Dict[str, Any]:
        """Return manual auto-discovery run state and the last result."""
        with self._auto_discovery_lock:
            return {
                "running": self._auto_discovery_running,
                "run_id": self._current_auto_discovery_run_id,
                "last_started_at": self._last_auto_discovery_started_at,
                "last_finished_at": self._last_auto_discovery_finished_at,
                "last_result": self._last_auto_discovery_result,
            }

    def trigger_auto_discovery(self) -> Dict[str, Any]:
        """Start an on-demand auto-discovery run in a background thread."""
        with self._auto_discovery_lock:
            if self._auto_discovery_running:
                return {
                    "started": False,
                    "running": True,
                    "run_id": self._current_auto_discovery_run_id,
                    "last_started_at": self._last_auto_discovery_started_at,
                }

            run_id = f"ad-{int(time.time() * 1000)}"
            started_at = datetime.now(timezone.utc).isoformat()
            self._auto_discovery_running = True
            self._current_auto_discovery_run_id = run_id
            self._last_auto_discovery_started_at = started_at
            self._last_auto_discovery_finished_at = None
            self._auto_discovery_thread = threading.Thread(
                target=self._run_auto_discovery_worker,
                args=(run_id,),
                name=f"AutoDiscovery-{run_id}",
                daemon=True,
            )
            self._auto_discovery_thread.start()
            return {
                "started": True,
                "running": True,
                "run_id": run_id,
                "last_started_at": started_at,
            }

    def _run_auto_discovery_worker(self, run_id: str):
        """Worker wrapper that updates run state around _run_auto_discovery()."""
        try:
            result = self._run_auto_discovery(run_id=run_id)
            result["status"] = "completed"
        except Exception as e:
            logger.error("Auto-discovery worker failed: %s", e, exc_info=True)
            result = {
                "created_count": 0,
                "renewed_count": 0,
                "expired_count": 0,
                "stale_count": 0,
                "skipped_budget": 0,
                "duration_sec": 0.0,
                "errors": [str(e)],
                "status": "failed",
            }

        finished_at = datetime.now(timezone.utc).isoformat()
        with self._auto_discovery_lock:
            self._auto_discovery_running = False
            self._last_auto_discovery_finished_at = finished_at
            self._last_auto_discovery_date = finished_at[:10]
            self._last_auto_discovery_result = {
                **result,
                "run_id": run_id,
                "finished_at": finished_at,
            }

        try:
            db = get_db(self.db_path)
            themes_found = int(result.get("themes_found") or 0)
            created = int(result.get("created_count") or 0)
            theme_label = f"Auto-discovery · {themes_found} theme{'s' if themes_found != 1 else ''}"
            if created:
                theme_label += f" · {created} list{'s' if created != 1 else ''} created"
            db.save_discovery_run(
                source="auto",
                external_run_id=run_id,
                theme=theme_label,
                ticker_count=created,
                validated_count=created,
                status=result.get("status", "completed"),
                error="; ".join(result.get("errors") or []) or None,
                summary=result,
                run_at=finished_at,
            )
        except Exception as exc:
            logger.warning("Failed to persist auto-discovery run %s: %s", run_id, exc)

    def _poll_loop(self):
        """Main scheduling loop."""
        # Initial delay so the app fully starts
        self._stop_event.wait(10)

        while not self._stop_event.is_set():
            try:
                self._check_schedule()
            except Exception as e:
                logger.error("Error in schedule check: %s", e)
            self._stop_event.wait(self.poll_interval)

    def _check_schedule(self):
        """Check if any scheduled task should run now."""
        et = _now_et()
        today = et.strftime("%Y-%m-%d")
        hour = et.hour
        minute = et.minute
        day_name = et.strftime("%A").lower()

        if not _is_weekday():
            # Weekend-only jobs: backtest and adaptive weights can run on weekends
            self._check_weekly_jobs(today, hour, minute, day_name)
            return

        # Pre-market warm: 8:25-8:35 AM ET
        if 8 == hour and 25 <= minute <= 35 and self._last_premarket_date != today:
            self._last_premarket_date = today
            self._run_premarket_warm()

        # Intraday refresh: every 2 hours from 10 AM to 4 PM, at :00-:05
        if 10 <= hour <= 15 and hour % 2 == 0 and minute <= 5:
            if self._last_intraday_hour != hour:
                self._last_intraday_hour = hour
                self._last_intraday_refresh_time = f"{today} {hour:02d}:{minute:02d} ET"
                self._run_intraday_refresh()

        # Daily exchange-resolution hydration to reduce strict-export misses.
        hydration_cfg = self._config.get("exchange_resolution_hydration", {})
        if hydration_cfg.get("enabled", True):
            run_hour = int(hydration_cfg.get("run_hour", 7))
            minute_window = max(1, int(hydration_cfg.get("minute_window", 10)))
            if hour == run_hour and minute <= minute_window and self._last_exchange_resolution_hydration_date != today:
                self._last_exchange_resolution_hydration_date = today
                self._run_exchange_resolution_hydration()

        # Post-close snapshot: 4:15-4:25 PM ET
        if 16 == hour and 15 <= minute <= 25 and self._last_postclose_date != today:
            self._last_postclose_date = today
            self._run_postclose_snapshot()
            if (
                self._movers_cfg.get("enabled")
                and self._movers_cfg.get("auto_scan_postclose_enabled")
                and not self._movers_cfg.get("manual_only", True)
            ):
                self._run_postclose_movers(today)

        # Weekly jobs can also run on weekdays
        self._check_weekly_jobs(today, hour, minute, day_name)

    def _check_weekly_jobs(self, today: str, hour: int, minute: int, day_name: str):
        """Check scheduled maintenance jobs (weekly + monthly proposal jobs)."""
        # Auto-backtest: run at 6:00 AM on configured day
        bt_cfg = self._config.get("auto_backtest", {})
        if bt_cfg.get("enabled") and hour == 6 and minute <= 10:
            bt_day = bt_cfg.get("run_day", "sunday").lower()
            if (bt_day == "daily" or bt_day == day_name) and self._last_backtest_date != today:
                self._last_backtest_date = today
                self._run_auto_backtest()

        # Adaptive weights: run at 7:00 AM on configured day
        aw_cfg = self._config.get("auto_adaptive_weights", {})
        if aw_cfg.get("enabled") and hour == 7 and minute <= 10:
            aw_day = aw_cfg.get("run_day", "monday").lower()
            if (aw_day == "daily" or aw_day == day_name) and self._last_adaptive_weights_date != today:
                self._last_adaptive_weights_date = today
                self._run_adaptive_weights()

        # Built-in refresh proposal: run once per month on configured day/hour
        br_cfg = self._config.get("builtin_refresh", {})
        if br_cfg.get("enabled"):
            run_hour = int(br_cfg.get("run_hour", 8))
            run_day = str(br_cfg.get("run_day", "sunday")).lower()
            month_key = today[:7]
            if hour == run_hour and minute <= 10:
                if (run_day == "daily" or run_day == day_name) and self._last_builtin_refresh_month != month_key:
                    self._last_builtin_refresh_month = month_key
                    self._run_builtin_refresh_proposal(source="scheduler")


    def _postclose_policy(self) -> Dict[str, Any]:
        return dict(self._config.get("postclose") or {})

    def _is_watchlist_eligible_for_scheduled_scan(self, name: str, day_name: str) -> bool:
        """Apply exclude + weekly cadence rules for post-close / pre-market scans."""
        wl_name = str(name or "").strip()
        if not wl_name:
            return False
        pc = self._postclose_policy()
        exclude = {
            str(n).strip()
            for n in pc.get("exclude_watchlist_names", [])
            if str(n).strip()
        }
        if wl_name in exclude:
            return False
        weekly = {
            str(n).strip()
            for n in pc.get("weekly_watchlist_names", [])
            if str(n).strip()
        }
        if wl_name in weekly:
            run_day = str(pc.get("weekly_run_day", "friday")).strip().lower()
            return day_name == run_day
        return True

    def _sort_watchlists_for_scheduled_scan(self, watchlists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Run priority desks first so time-budget cuts hit large indices last."""
        pc = self._postclose_policy()
        priority = [
            str(n).strip()
            for n in pc.get("priority_watchlist_names", [])
            if str(n).strip()
        ]
        rank = {n: i for i, n in enumerate(priority)}

        def _sort_key(w: Dict[str, Any]) -> tuple:
            name = str(w.get("name") or "")
            return (rank.get(name, len(priority)), int(w.get("id") or 0))

        return sorted(watchlists, key=_sort_key)

    def _get_scannable_watchlists(self, apply_postclose_policy: bool = False) -> List[Dict[str, Any]]:
        """Get scannable watchlists (built-in + auto, plus configured user lists)."""
        try:
            db = get_db(self.db_path)
            watchlists = db.get_watchlists()
            pc = self._postclose_policy()
            include_user = {
                str(n).strip()
                for n in pc.get("include_user_watchlist_names", [])
                if str(n).strip()
            }
            eligible: List[Dict[str, Any]] = []
            for w in watchlists:
                if not str(w.get("tickers") or "").strip():
                    continue
                src = str(w.get("source") or "").strip().lower()
                name = str(w.get("name") or "").strip()
                if src in ("built-in", "auto"):
                    eligible.append(w)
                elif (
                    apply_postclose_policy
                    and src == "user"
                    and name in include_user
                ):
                    eligible.append(w)
            if apply_postclose_policy:
                day_name = _now_et().strftime("%A").lower()
                eligible = [
                    w
                    for w in eligible
                    if self._is_watchlist_eligible_for_scheduled_scan(
                        str(w.get("name") or ""), day_name
                    )
                ]
                eligible = self._sort_watchlists_for_scheduled_scan(eligible)
            return eligible
        except Exception as e:
            logger.error("Failed to load watchlists: %s", e)
            return []

    def _run_exchange_resolution_hydration(self):
        """Prewarm ticker metadata/exchange resolution cache for strict exports."""
        with self._hydration_lock:
            if self._hydration_running:
                logger.warning(
                    "Exchange-resolution hydration already running (started %s); skipping duplicate trigger",
                    self._last_hydration_started_at,
                )
                return
            self._hydration_running = True
            self._last_hydration_started_at = datetime.now(timezone.utc).isoformat()
            self._last_hydration_error = None

        t0 = time.monotonic()
        try:
            from tradingagents.screening.ticker_resolver import resolve_and_cache

            cfg = self._config.get("exchange_resolution_hydration", {})
            include_sources = {str(s).strip().lower() for s in cfg.get("include_sources", ["built-in", "user", "auto"])}
            max_tickers = max(100, int(cfg.get("max_tickers", 2000)))
            max_workers = max(1, int(cfg.get("max_workers", 12)))
            max_age_days = max(1, int(cfg.get("max_age_days", 7)))

            db = get_db(self.db_path)
            watchlists = db.get_watchlists()
            tickers: List[str] = []
            for wl in watchlists:
                source = str(wl.get("source", "")).strip().lower()
                if include_sources and source not in include_sources:
                    continue
                tickers.extend(
                    t.strip().upper()
                    for t in str(wl.get("tickers") or "").split(",")
                    if t and t.strip()
                )
            deduped = list(dict.fromkeys(tickers))[:max_tickers]
            if not deduped:
                logger.info("Exchange-resolution hydration skipped: no tickers")
                return

            resolve_and_cache(
                deduped,
                db,
                max_workers=min(max_workers, len(deduped)),
                max_age_days=max_age_days,
                total_budget_seconds=self._hydration_total_budget,
            )
            logger.info(
                "Exchange-resolution hydration complete: tickers=%d max_age_days=%d in %.1fs",
                len(deduped),
                max_age_days,
                time.monotonic() - t0,
            )
        except Exception as e:
            self._last_hydration_error = str(e)
            logger.warning("Exchange-resolution hydration failed: %s", e)
        finally:
            self._last_hydration_finished_at = datetime.now(timezone.utc).isoformat()
            with self._hydration_lock:
                self._hydration_running = False

    def _run_auto_discovery(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Run auto-discovery once and return a structured summary."""
        import json as _json

        t0 = time.monotonic()
        ad_cfg = self._config.get("auto_discovery", {})
        hard_timeout = int(ad_cfg.get("hard_timeout_seconds", 600))
        max_themes = ad_cfg.get("max_themes", 5)
        expiry_days = ad_cfg.get("expiry_days", 21)
        renewal_days = ad_cfg.get("renewal_extension_days", 7)
        full_refresh_age = ad_cfg.get("full_refresh_age_days", 28)
        monthly_budget = ad_cfg.get("monthly_budget", 20)
        model = ad_cfg.get("model", "sonar")
        drift_threshold = ad_cfg.get("drift_threshold", 0.4)
        run_label = run_id or "manual"

        summary: Dict[str, Any] = {
            "created_count": 0,
            "renewed_count": 0,
            "expired_count": 0,
            "stale_count": 0,
            "skipped_budget": 0,
            "themes_found": 0,
            "skip_reason": None,
            "duration_sec": 0.0,
            "errors": [],
            "run_id": run_label,
        }

        logger.info(
            "=== Auto-Discovery starting (run_id=%s, max_themes=%d, model=%s) ===",
            run_label,
            max_themes,
            model,
        )

        try:
            from tradingagents.screening.macro_overlay import compute_sector_momentum
            from tradingagents.dataflows.yfinance_extended import get_macro_snapshot
            from tradingagents.dataflows.index_regime import (
                index_regime_display_label,
                index_regime_storage_fields,
            )
            from tradingagents.screening.theme_detector import ThemeDetector, compute_drift
            from tradingagents.dataflows.interface import discover_opportunities
            from tradingagents.dataflows.usage_tracker import get_tracker

            db = get_db(self.db_path)
            tracker = get_tracker()

            # 1. Gather market data
            sector_momentum = compute_sector_momentum()
            macro_snapshot = get_macro_snapshot()

            # 2. Detect themes
            detector = ThemeDetector(max_themes=max_themes)
            themes = detector.detect_themes(sector_momentum, macro_snapshot)
            summary["themes_found"] = len(themes)
            logger.info("Auto-discovery detected %d themes", len(themes))
            if not themes:
                regime_label = index_regime_display_label(macro_snapshot)
                regime = macro_snapshot.get("market_regime", "unknown")
                if regime == "unknown":
                    skip_reason = "market regime unknown — macro snapshot unavailable"
                else:
                    vix_value = macro_snapshot.get("vix_value") or (
                        macro_snapshot.get("indices", {}).get("vix", {}).get("current", 0.0)
                    )
                    skip_reason = (
                        f"no templates matched (index regime={regime_label}, VIX={vix_value:.1f}); "
                        "check sector momentum or VIX constraints"
                    )
                logger.info("No themes matched: %s", skip_reason)
                summary["skip_reason"] = skip_reason
                summary["duration_sec"] = round(time.monotonic() - t0, 2)
                return summary

            # 3. Build current snapshot for drift comparison
            current_snapshot = {
                "sector_ranks": sector_momentum.get("sector_ranks", {}),
                **index_regime_storage_fields(macro_snapshot),
            }

            # 4. Process existing auto-watchlists: renew, mark stale, or expire
            existing_auto = db.get_auto_watchlists()
            existing_by_name = {w["name"]: w for w in existing_auto}

            expired_count = db.expire_auto_watchlists()
            summary["expired_count"] = expired_count
            if expired_count:
                logger.info("Expired %d auto-watchlists", expired_count)
                existing_auto = db.get_auto_watchlists()
                existing_by_name = {w["name"]: w for w in existing_auto}

            from datetime import datetime as _dt, timedelta as _td

            current_theme_names = {t.name for t in themes}
            for wl in existing_auto:
                if time.monotonic() - t0 > hard_timeout:
                    msg = "Auto-discovery hit 10-min hard timeout during renewal phase"
                    logger.warning(msg)
                    summary["errors"].append(msg)
                    break

                wl_name = wl["name"]
                stored_snap = {}
                if wl.get("sector_snapshot"):
                    try:
                        stored_snap = _json.loads(wl["sector_snapshot"])
                    except (ValueError, TypeError):
                        pass

                drift = compute_drift(stored_snap, current_snapshot) if stored_snap else 1.0

                if drift < drift_threshold and wl_name in current_theme_names:
                    new_expires = (_dt.now() + _td(days=renewal_days)).isoformat()
                    new_snap = _json.dumps(current_snapshot)

                    tickers_update = None
                    created = _dt.fromisoformat(wl["created_at"])
                    age_days = (_dt.now() - created).days
                    if age_days >= full_refresh_age:
                        if tracker.can_use_auto_discovery(monthly_budget):
                            if time.monotonic() - t0 < hard_timeout - 60:
                                matching_theme = next((t for t in themes if t.name == wl_name), None)
                                if matching_theme:
                                    logger.info("Full refresh for '%s' (age=%d days)", wl_name, age_days)
                                    result = discover_opportunities(
                                        **_auto_discovery_kwargs(matching_theme, model)
                                    )
                                    tracker.record_auto_discovery_use()
                                    valid = [t["ticker"] for t in result.get("tickers", []) if t.get("validated")]
                                    if valid:
                                        tickers_update = ",".join(valid)
                        else:
                            summary["skipped_budget"] += 1

                    renewed = db.renew_auto_watchlist(
                        wl["id"],
                        new_expires_at=new_expires,
                        sector_snapshot=new_snap,
                        tickers=tickers_update,
                    )
                    if renewed:
                        summary["renewed_count"] += 1
                    logger.info("Renewed '%s' (drift=%.3f, expires=%s)", wl_name, drift, new_expires)
                elif drift >= drift_threshold:
                    summary["stale_count"] += 1
                    logger.info(
                        "'%s' is stale (drift=%.3f >= %.3f); letting expires_at count down",
                        wl_name,
                        drift,
                        drift_threshold,
                    )

            # 5. Create new watchlists for themes that don't already exist
            for idx, theme_spec in enumerate(themes):
                if time.monotonic() - t0 > hard_timeout:
                    msg = "Auto-discovery hit 10-min hard timeout; stopping new theme creation"
                    logger.warning(msg)
                    summary["errors"].append(msg)
                    break

                if theme_spec.name in existing_by_name:
                    continue

                if not tracker.can_use_auto_discovery(monthly_budget):
                    remaining = len(themes) - idx
                    summary["skipped_budget"] += max(1, remaining)
                    logger.warning("Auto-discovery budget exhausted; skipping remaining themes")
                    break

                logger.info("Discovering tickers for new theme: '%s'", theme_spec.name)
                result = discover_opportunities(
                    **_auto_discovery_kwargs(theme_spec, model)
                )
                tracker.record_auto_discovery_use()

                valid_tickers = [t["ticker"] for t in result.get("tickers", []) if t.get("validated")]
                if not valid_tickers:
                    logger.warning("No valid tickers for theme '%s'; skipping", theme_spec.name)
                    continue

                snapshot_data = {
                    "sector_ranks": sector_momentum.get("sector_ranks", {}),
                    **index_regime_storage_fields(macro_snapshot),
                    "primary_sector": theme_spec.primary_sector,
                }
                expires = (_dt.now() + _td(days=expiry_days)).isoformat()

                try:
                    wl_id = db.create_auto_watchlist(
                        name=theme_spec.name,
                        description=f"Auto-generated: {theme_spec.theme}",
                        tickers=",".join(valid_tickers),
                        default_preset=theme_spec.preset,
                        default_investment_profile=theme_spec.profile,
                        theme_rationale=theme_spec.rationale,
                        sector_snapshot=_json.dumps(snapshot_data),
                        expires_at=expires,
                    )
                    summary["created_count"] += 1
                    logger.info(
                        "Created auto-watchlist '%s' (id=%d, %d tickers, expires=%s)",
                        theme_spec.name,
                        wl_id,
                        len(valid_tickers),
                        expires,
                    )
                except Exception as e:
                    msg = f"Failed to create auto-watchlist '{theme_spec.name}': {e}"
                    summary["errors"].append(msg)
                    logger.error(msg)

        except Exception as e:
            summary["errors"].append(str(e))
            logger.error("Auto-discovery failed: %s", e, exc_info=True)

        summary["duration_sec"] = round(time.monotonic() - t0, 2)
        logger.info(
            "=== Auto-Discovery finished (run_id=%s) in %.1fs; created=%d renewed=%d expired=%d stale=%d skipped_budget=%d errors=%d ===",
            run_label,
            summary["duration_sec"],
            summary["created_count"],
            summary["renewed_count"],
            summary["expired_count"],
            summary["stale_count"],
            summary["skipped_budget"],
            len(summary["errors"]),
        )
        return summary

    def _run_builtin_refresh_proposal(self, source: str = "scheduler") -> Dict[str, Any]:
        """Generate a proposal for built-in refresh (manual apply required)."""
        try:
            from tradingagents.screening.builtin_refresh import build_refresh_proposal

            db = get_db(self.db_path)
            wrapped_cfg = {"screening": {"scheduler": {"builtin_refresh": self._config.get("builtin_refresh", {})}}}
            proposal = build_refresh_proposal(db=db, config=wrapped_cfg, source=source)
            self._last_builtin_refresh_proposal_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "Built-in refresh proposal generated: id=%s items=%d source=%s",
                proposal.get("id"),
                len(proposal.get("items", [])),
                source,
            )
            return {
                "ok": True,
                "proposal_id": proposal.get("id"),
                "created_at": proposal.get("created_at"),
                "item_count": len(proposal.get("items", [])),
            }
        except Exception as e:
            logger.error("Built-in refresh proposal failed: %s", e, exc_info=True)
            return {"ok": False, "error": str(e)}

    def _run_premarket_warm(self):
        """Pre-market: warm OHLCV + info cache for all scannable watchlists.

        Same hang protections as _run_postclose_snapshot: overlap lock,
        total wall-clock budget, per-watchlist deadline.
        """
        with self._premarket_lock:
            if self._premarket_running:
                logger.warning(
                    "Pre-market warm already running (started %s); skipping duplicate trigger",
                    self._last_premarket_started_at,
                )
                return
            self._premarket_running = True
            self._last_premarket_started_at = datetime.now(timezone.utc).isoformat()
            self._last_premarket_error = None

        t0 = time.monotonic()
        try:
            logger.info(
                "Running pre-market cache warm (total_budget=%.0fs, per_watchlist_budget=%.0fs)...",
                self._premarket_total_budget,
                self._per_watchlist_scan_budget,
            )
            watchlists = self._get_scannable_watchlists(apply_postclose_policy=True)
            if not watchlists:
                logger.info("No scannable watchlists found")
                return

            from tradingagents.screening.engine import ScreeningEngine
            engine = ScreeningEngine(db=get_db(self.db_path))
            total_tickers = 0
            warmed = 0

            for wl in watchlists:
                if self._stop_event.is_set():
                    logger.info("Pre-market: stop requested; aborting after %d watchlist(s)", warmed)
                    break
                if time.monotonic() - t0 > self._premarket_total_budget:
                    logger.warning(
                        "Pre-market total budget (%.0fs) exhausted; %d watchlist(s) skipped",
                        self._premarket_total_budget,
                        len(watchlists) - warmed,
                    )
                    break

                tickers = [t.strip() for t in wl["tickers"].split(",") if t.strip()]
                if not tickers:
                    continue
                try:
                    remaining_total = max(30.0, self._premarket_total_budget - (time.monotonic() - t0))
                    wl_budget = min(self._per_watchlist_scan_budget, remaining_total)
                    # Running a scan warms the cache as a side effect.
                    # Note: omit watchlist_id so results are NOT persisted.
                    engine.scan(
                        tickers=tickers,
                        enable_enhanced=False,
                        time_budget_seconds=wl_budget,
                    )
                    total_tickers += len(tickers)
                    warmed += 1
                except Exception as e:
                    logger.error("Pre-market warm failed for '%s': %s", wl['name'], e)
                time.sleep(2)

            logger.info("Pre-market warm complete: %d tickers across %d watchlists in %.1fs",
                        total_tickers, warmed, time.monotonic() - t0)
        except Exception as e:
            self._last_premarket_error = str(e)
            logger.error("Pre-market warm failed: %s", e, exc_info=True)
        finally:
            self._last_premarket_finished_at = datetime.now(timezone.utc).isoformat()
            with self._premarket_lock:
                self._premarket_running = False

    def _run_intraday_refresh(self):
        """Intraday: optionally invalidate screening OHLCV caches."""
        policy = self._intraday_cache_policy
        logger.info("Intraday cache refresh (policy=%s)...", policy)
        if policy == "ttl_only":
            logger.info("Intraday refresh skipped (ttl_only); relying on cache TTL + post-close")
            return
        try:
            from tradingagents.dataflows.screening_ohlcv_cache import (
                invalidate_screening_price_caches,
            )

            cache = get_cache()
            evicted = invalidate_screening_price_caches(cache)
            logger.info("Invalidated %d screening cache entries (full_invalidate)", evicted)
        except Exception as e:
            logger.error("Intraday refresh failed: %s", e)

    def _run_postclose_snapshot(self):
        """Post-close: full scan on all built-in watchlists, then run automations.

        Hardened against hangs:
        - Overlap lock prevents a second copy from ever starting while one
          is still running (the May 9 dashboard incident root cause).
        - Total wall-clock budget (postclose_total_budget_seconds, default
          1h) abandons remaining watchlists if the job runs over.
        - Per-watchlist budget (per_watchlist_scan_budget_seconds, default
          10m) passes a deadline into ScreeningEngine.scan() so a single
          stuck yfinance call can't pin the whole job.
        """
        with self._postclose_lock:
            if self._postclose_running:
                logger.warning(
                    "Post-close scan already running (started %s); skipping duplicate trigger",
                    self._last_postclose_started_at,
                )
                return
            self._postclose_running = True
            self._last_postclose_started_at = datetime.now(timezone.utc).isoformat()
            self._last_postclose_error = None

        t0 = time.monotonic()
        try:
            logger.info(
                "Running post-close snapshot scans (total_budget=%.0fs, per_watchlist_budget=%.0fs)...",
                self._postclose_total_budget,
                self._per_watchlist_scan_budget,
            )
            watchlists = self._get_scannable_watchlists(apply_postclose_policy=True)
            if not watchlists:
                logger.info("Post-close: no watchlists eligible today (policy/cadence)")
                return

            logger.info(
                "Post-close: %d watchlist(s) eligible today (policy applied, priority order)",
                len(watchlists),
            )

            from tradingagents.screening.engine import ScreeningEngine
            db = get_db(self.db_path)
            engine = ScreeningEngine(db=db)

            runs_saved = 0
            scan_results = {}  # watchlist_id → (run_id, results, watchlist)
            for wl in watchlists:
                if self._stop_event.is_set():
                    logger.info("Post-close: stop requested; aborting after %d watchlist(s)", runs_saved)
                    break
                if time.monotonic() - t0 > self._postclose_total_budget:
                    logger.warning(
                        "Post-close total budget (%.0fs) exhausted; %d watchlist(s) skipped",
                        self._postclose_total_budget,
                        len(watchlists) - runs_saved,
                    )
                    break

                tickers = [t.strip() for t in wl["tickers"].split(",") if t.strip()]
                if not tickers:
                    continue
                try:
                    # Cap each watchlist by the per-scan budget AND by the
                    # time we have left in the overall budget — whichever
                    # is smaller. Floor at 30s so the deadline is always
                    # meaningfully reachable.
                    remaining_total = max(30.0, self._postclose_total_budget - (time.monotonic() - t0))
                    wl_budget = min(self._per_watchlist_scan_budget, remaining_total)
                    results = engine.scan(
                        tickers=tickers,
                        watchlist_id=wl["id"],
                        enable_enhanced=True,
                        time_budget_seconds=wl_budget,
                    )
                    runs_saved += 1
                    run_id = getattr(engine, "last_run_id", None)
                    if run_id:
                        scan_results[wl["id"]] = (int(run_id), results, wl)
                    else:
                        logger.warning("Missing run_id after post-close scan for '%s'; skipping automations for this watchlist", wl["name"])
                    logger.info("Post-close: '%s' → %d results", wl['name'], len(results))
                except Exception as e:
                    logger.error("Post-close scan failed for '%s': %s", wl['name'], e)
                time.sleep(3)

            logger.info("Post-close snapshot complete: %d watchlists scanned in %.1fs",
                        runs_saved, time.monotonic() - t0)

            # --- Post-scan automations ---
            if scan_results:
                self._run_auto_alerts(db, scan_results)
                self._run_auto_analyze(db, scan_results)
        except Exception as e:
            self._last_postclose_error = str(e)
            logger.error("Post-close snapshot failed: %s", e, exc_info=True)
        finally:
            self._last_postclose_finished_at = datetime.now(timezone.utc).isoformat()
            with self._postclose_lock:
                self._postclose_running = False

    def _run_postclose_movers(self, today: str):
        """Optional post-close movers scan with overlap lock."""
        if self._movers_cfg.get("manual_only", True):
            logger.info("Movers scheduler skipped (manual_only=true)")
            return
        if self._last_movers_date == today:
            return
        with self._movers_lock:
            if self._movers_running:
                logger.info("Movers post-close scan already running; skipping overlap")
                return
            self._movers_running = True
        try:
            from tradingagents.screening.movers import MoversIntelligenceService

            db = get_db(self.db_path)
            service = MoversIntelligenceService(db=db, config=DEFAULT_CONFIG)
            result = service.run_scan(
                include_losers=self._movers_cfg.get("include_losers", True),
                top_n=int(self._movers_cfg.get("default_top_n", 25)),
                source_lists=None,
                allow_preclose_skip=False,
            )
            if result.get("skipped"):
                self._last_movers_error = result.get("message", "movers skipped")
                return

            self._last_movers_date = today
            self._last_movers_scan_at = datetime.now(timezone.utc).isoformat()
            self._last_movers_snapshot_id = str(result.get("snapshot_id"))
            self._last_movers_error = None
            logger.info(
                "Post-close movers scan complete (snapshot=%s)",
                self._last_movers_snapshot_id,
            )
        except Exception as e:
            self._last_movers_error = str(e)
            logger.error("Post-close movers scan failed: %s", e, exc_info=True)
        finally:
            with self._movers_lock:
                self._movers_running = False

    def _run_auto_alerts(self, db: ResearchDatabase, scan_results: Dict):
        """Create alerts for top N tickers from each screening run."""
        cfg = self._config.get("auto_alerts", {})
        if not cfg.get("enabled"):
            return

        top_n = cfg.get("top_n", 5)
        total_created = 0

        for wl_id, (run_id, results, wl) in scan_results.items():
            if not results:
                continue
            try:
                # Sort by composite_score and take top N
                # ScreeningResult is a dataclass — use attribute access, not .get()
                sorted_results = sorted(results, key=lambda r: getattr(r, "composite_score", 0), reverse=True)[:top_n]

                # Load existing active rules to prevent duplicates
                existing_rules = db.get_active_rules()
                existing_tickers = set()
                for rule in existing_rules:
                    try:
                        cond = rule.get("condition", {})
                        if isinstance(cond, str):
                            import json
                            cond = json.loads(cond)
                        existing_tickers.add(rule.get("ticker", "").upper())
                    except Exception as e:
                        logger.debug("Failed to parse alert rule condition: %s", e)

                for r in sorted_results:
                    ticker = getattr(r, "ticker", "").upper()
                    if ticker in existing_tickers:
                        continue  # Skip duplicate

                    score = getattr(r, "composite_score", 0)
                    try:
                        db.create_alert_rule(
                            ticker=ticker,
                            alert_type="volume_spike",
                            condition={"threshold": 2.0, "auto_from_screening": True, "run_id": run_id},
                            message=f"Auto-alert: {ticker} scored {score:.1f} in {wl['name']} screening",
                        )
                        total_created += 1
                        existing_tickers.add(ticker)
                    except Exception as e:
                        logger.error("Auto-alert failed for %s: %s", ticker, e)

            except Exception as e:
                logger.error("Auto-alerts failed for '%s': %s", wl['name'], e)

        if total_created > 0:
            logger.info("Auto-alerts: created %d alerts from post-close scans", total_created)

    def _run_auto_analyze(self, db: ResearchDatabase, scan_results: Dict):
        """Run full analysis on top N tickers from the highest-scoring watchlist scans."""
        cfg = self._config.get("auto_analyze", {})
        if not cfg.get("enabled"):
            return

        top_n = cfg.get("top_n", 3)
        mode = cfg.get("mode", "quick")
        risk_profile = cfg.get("risk_profile", "growth")
        delay = cfg.get("delay_seconds", 90)

        # Collect top tickers across all watchlists, deduplicated
        # ScreeningResult is a dataclass — use attribute access, not .get()
        all_candidates = []
        for wl_id, (run_id, results, wl) in scan_results.items():
            for r in results:
                all_candidates.append({
                    "ticker": getattr(r, "ticker", ""),
                    "score": getattr(r, "composite_score", 0),
                    "watchlist": wl,
                })

        # Sort by score, deduplicate, take top N
        seen = set()
        top_tickers = []
        profile_for_ticker = {}
        for c in sorted(all_candidates, key=lambda x: x["score"], reverse=True):
            ticker = c["ticker"].upper()
            if ticker in seen:
                continue
            seen.add(ticker)
            top_tickers.append(ticker)
            # Resolve investment profile from the originating watchlist
            profile_for_ticker[ticker] = c["watchlist"].get("default_investment_profile")
            if len(top_tickers) >= top_n:
                break

        if not top_tickers:
            return

        logger.info("Auto-analyze: starting %d tickers (%s mode): %s", len(top_tickers), mode, ', '.join(top_tickers))

        try:
            from tradingagents.research import ResearchAgent
            from tradingagents.default_config import DEFAULT_CONFIG, get_config_for_mode

            config = get_config_for_mode(mode, DEFAULT_CONFIG)
            config["risk_profile"] = risk_profile

            agent = ResearchAgent(
                config=config,
                output_dir="research_output",
                db_path=self.db_path,
                auto_report=True,
                auto_save=True,
                debug=False,
            )

            for i, ticker in enumerate(top_tickers):
                if self._stop_event.is_set():
                    logger.info("Auto-analyze: stopping early (shutdown requested)")
                    break
                try:
                    profile_key = profile_for_ticker.get(ticker) or None
                    today = datetime.now().strftime("%Y-%m-%d")
                    result = agent.analyze(
                        ticker,
                        today,
                        investment_profile_key=profile_key,
                        investment_profile_source="watchlist" if profile_key else None,
                    )
                    logger.info("Auto-analyze [%d/%d]: %s → %s", i + 1, len(top_tickers), ticker, result.decision)
                except Exception as e:
                    logger.error("Auto-analyze failed for %s: %s", ticker, e)

                if i < len(top_tickers) - 1:
                    time.sleep(delay)

            logger.info("Auto-analyze complete: %d tickers processed", len(top_tickers))

        except Exception as e:
            logger.error("Auto-analyze setup failed: %s", e)

    # =========================================================================
    # WEEKLY AUTOMATION JOBS
    # =========================================================================

    def _run_auto_backtest(self):
        """Compute forward returns for past analyses AND screening runs.

        Guarded with an overlap lock so a weekly cadence + manual trigger
        can't stack two backtest passes and starve the rest of the
        scheduler / DB connection pool.
        """
        with self._backtest_lock:
            if self._backtest_running:
                logger.warning(
                    "Auto-backtest already running (started %s); skipping duplicate trigger",
                    self._last_backtest_started_at,
                )
                return
            self._backtest_running = True
            self._last_backtest_started_at = datetime.now(timezone.utc).isoformat()
            self._last_backtest_error = None
        try:
            self._auto_backtest_inner()
        except Exception as e:
            self._last_backtest_error = str(e)
            logger.error("Auto-backtest failed: %s", e, exc_info=True)
        finally:
            self._last_backtest_finished_at = datetime.now(timezone.utc).isoformat()
            with self._backtest_lock:
                self._backtest_running = False

    def _auto_backtest_inner(self):
        cfg = self._config.get("auto_backtest", {})
        min_age = cfg.get("min_age_days", 7)
        max_age = cfg.get("max_age_days", 90)
        batch_size = cfg.get("batch_size", 20)

        # Part A: Backtest screening runs (populates screening_results.return_7d
        # which feeds compute_adaptive_weights)
        self._backtest_screening_runs(min_age)

        # Part B: Backtest analyses
        logger.info("Auto-backtest: checking for analyses %d-%d days old...", min_age, max_age)

        try:
            db = get_db(self.db_path)
            pending = db.get_analyses_needing_backtest(
                min_age_days=min_age,
                max_age_days=max_age,
                limit=batch_size,
            )

            if not pending:
                logger.info("Auto-backtest: no analyses need backtesting")
                return

            logger.info("Auto-backtest: %d analyses to backtest", len(pending))

            import yfinance as yf
            from datetime import datetime as dt

            updated = 0
            for analysis in pending:
                if self._stop_event.is_set():
                    break

                ticker = analysis["ticker"]
                analysis_date = analysis["analysis_date"]
                analysis_id = analysis["id"]
                decision = (analysis.get("decision") or "").upper()

                try:
                    # Fetch historical prices around the analysis date
                    start_date = analysis_date
                    # Need 30 trading days after analysis date (~45 calendar days)
                    end_dt = dt.strptime(analysis_date, "%Y-%m-%d") + timedelta(days=45)
                    end_date = min(end_dt, dt.now()).strftime("%Y-%m-%d")

                    hist = yf.download(
                        ticker, start=start_date, end=end_date,
                        progress=False, timeout=15,
                    )

                    if hist.empty or len(hist) < 2:
                        continue

                    # Get price at analysis date (first available close)
                    price_at = float(hist["Close"].iloc[0])

                    # Forward returns: 7, 14, 30 trading days after analysis
                    price_7d = float(hist["Close"].iloc[min(7, len(hist) - 1)]) if len(hist) > 7 else None
                    price_14d = float(hist["Close"].iloc[min(14, len(hist) - 1)]) if len(hist) > 14 else None
                    price_30d = float(hist["Close"].iloc[min(30, len(hist) - 1)]) if len(hist) > 30 else None

                    ret_7d = ((price_7d / price_at) - 1) * 100 if price_7d else None
                    ret_14d = ((price_14d / price_at) - 1) * 100 if price_14d else None
                    ret_30d = ((price_30d / price_at) - 1) * 100 if price_30d else None

                    # Was the decision correct?
                    was_correct = None
                    if ret_7d is not None:
                        if "BUY" in decision:
                            was_correct = ret_7d > 0
                        elif "SELL" in decision:
                            was_correct = ret_7d < 0
                        # HOLD is considered correct if absolute return < 3%
                        elif "HOLD" in decision:
                            was_correct = abs(ret_7d) < 3.0

                    db.update_backtest_outcomes(
                        analysis_id=analysis_id,
                        price_at_analysis=price_at,
                        price_after_7d=price_7d,
                        price_after_14d=price_14d,
                        price_after_30d=price_30d,
                        actual_return_7d=ret_7d,
                        actual_return_14d=ret_14d,
                        actual_return_30d=ret_30d,
                        was_correct=was_correct,
                    )
                    updated += 1

                except Exception as e:
                    logger.error("Auto-backtest failed for %s (id=%d): %s", ticker, analysis_id, e)

                time.sleep(1)  # Gentle rate limiting on yfinance

            logger.info("Auto-backtest complete: %d/%d analyses updated", updated, len(pending))

        except Exception as e:
            logger.error("Auto-backtest error: %s", e)

    def _backtest_screening_runs(self, min_age_days: int = 7):
        """Backtest recent screening runs to populate return_7d on screening_results."""
        try:
            from tradingagents.backtesting.engine import backtest_screening_run
            db = get_db(self.db_path)

            # Get screening runs that are old enough and haven't been backtested
            runs = db.get_screening_runs(limit=50)
            if not runs:
                return

            now = datetime.now()
            backtested = 0
            for run in runs:
                if self._stop_event.is_set():
                    break
                try:
                    run_date = datetime.fromisoformat(run.get("run_at") or run.get("created_at") or "")
                except (ValueError, TypeError):
                    continue

                age_days = (now - run_date).days
                if age_days < min_age_days:
                    continue  # Too fresh

                # Check if results already have return_7d populated
                results = db.get_screening_results(run["id"])
                if not results:
                    continue
                # If any result already has return data, skip this run
                if any(r.get("return_7d") is not None for r in results):
                    continue

                try:
                    outcome = backtest_screening_run(run["id"], db_path=self.db_path)
                    updated = outcome.get("updated", 0)
                    if updated > 0:
                        backtested += 1
                        logger.info("Screening backtest: run %d → %d results updated", run['id'], updated)
                except Exception as e:
                    logger.error("Screening backtest failed for run %d: %s", run['id'], e)

                time.sleep(1)  # Rate limit

            if backtested > 0:
                logger.info("Screening backtest complete: %d runs backtested", backtested)

        except Exception as e:
            logger.error("Screening backtest error: %s", e)

    def _run_adaptive_weights(self):
        """Recompute adaptive screening weights from backtested data."""
        cfg = self._config.get("auto_adaptive_weights", {})
        min_samples = cfg.get("min_samples", 30)

        logger.info("Adaptive weights: recomputing from backtested data...")

        try:
            from tradingagents.screening.engine import ScreeningEngine
            db = get_db(self.db_path)
            engine = ScreeningEngine(db=db)

            weights = engine.compute_adaptive_weights(min_samples=min_samples)

            if weights is None:
                logger.info("Adaptive weights: insufficient data (need %d+ backtested results)", min_samples)
                return

            # Log the computed weights for visibility
            top_signals = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:5]
            top_str = ", ".join(f"{k}={v:.3f}" for k, v in top_signals)
            logger.info("Adaptive weights computed: top signals: %s", top_str)
            logger.info("Adaptive weights available via /api/screening/adaptive-weights")

            # Also compute signal hit-rate performance (Feature 17)
            try:
                perf = engine.compute_signal_performance(min_samples=min_samples)
                if perf:
                    logger.info("Signal performance updated: %d signal-period entries", len(perf))
            except Exception as sp_err:
                logger.error("Signal performance computation failed: %s", sp_err)

        except Exception as e:
            logger.error("Adaptive weights error: %s", e)
