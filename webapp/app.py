import json
import os
import uuid
import csv
import queue
import re
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import threading
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from contextlib import asynccontextmanager

from tradingagents.default_config import DEFAULT_CONFIG, get_config_for_mode
from tradingagents.research import ResearchAgent
from tradingagents.reporting.context_qc import enrich_analysis_api_record
from tradingagents.reporting.database import ResearchDatabase, get_db
from tradingagents.backtesting import BacktestEngine, compute_calibration
from tradingagents.backtesting.metrics import empty_stats_payload, stats_api_payload, summarize_analyses
from tradingagents.dataflows.index_regime import normalize_index_regime
from tradingagents.dataflows.yfinance_extended import (
    get_macro_snapshot,
    get_estimate_revisions,
    get_rating_changes,
    get_earnings_quality,
    compute_intrinsic_value,
    get_weekly_technicals,
    compute_scenario_analysis,
)
from tradingagents.utils.tradingview_links import (
    exchange_from_info,
    resolve_exchange_from_info,
    validate_tradingview_token_online,
    tradingview_symbol_token,
    tradingview_symbol_url,
    tradingview_chart_url,
    tradingview_txt_content,
)
from webapp.alert_monitor import AlertMonitor
from webapp.screening_scheduler import ScreeningScheduler

import logging

load_dotenv()

logger = logging.getLogger("tradingagents.webapp")

# Optional: set this in env to open symbols in your saved TradingView layout.
# Example: TRADINGVIEW_CHART_ID=TxiihJ7N
_TRADINGVIEW_CHART_ID = (os.getenv("TRADINGVIEW_CHART_ID") or "").strip() or None

# ---------- Path safety helpers ----------
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_ALLOWED_OUTPUT_DIRS = {"research_output", "exports", "archive"}
_ALLOWED_DB_NAMES = {"research.db", "test.db"}


def _safe_output_dir(output_dir: str) -> str:
    """Validate output_dir stays under the project root."""
    resolved = (_PROJECT_ROOT / output_dir).resolve()
    if not str(resolved).startswith(str(_PROJECT_ROOT)):
        raise HTTPException(400, "Invalid output_dir: path traversal detected")
    return str(resolved)


def _safe_db_path(db_path: str) -> str:
    """Validate db_path stays under the project root."""
    resolved = (_PROJECT_ROOT / db_path).resolve()
    if not str(resolved).startswith(str(_PROJECT_ROOT)):
        raise HTTPException(400, "Invalid db_path: path traversal detected")
    return db_path  # Return original relative path (ResearchDatabase uses it relative to cwd)


def _safe_file_path(file_path: str, base_dir: str = "research_output") -> Path:
    """Validate a file path stays under the given base directory."""
    base = (_PROJECT_ROOT / base_dir).resolve()
    resolved = (_PROJECT_ROOT / file_path).resolve()
    if not str(resolved).startswith(str(base)):
        raise HTTPException(400, "Invalid file path: path traversal detected")
    return resolved


# ---------- Startup validation ----------
def _validate_env():
    """Fail fast if critical environment variables are missing."""
    required = {
        "OPENAI_API_KEY": "Required for all LLM-powered analyses",
    }
    # Finnhub is required if it's the configured news vendor
    vendors = DEFAULT_CONFIG.get("data_vendors", {})
    if vendors.get("news_data", "finnhub") == "finnhub":
        required["FINNHUB_API_KEY"] = "Required for news data (default vendor)"

    missing = []
    placeholder = []
    for key, desc in required.items():
        val = os.getenv(key, "")
        if not val:
            missing.append(f"  {key} — {desc}")
        elif val.startswith("your_") or val == "sk-xxx":
            placeholder.append(f"  {key} — {desc}")

    if missing or placeholder:
        msg = "\n⚠️  ENVIRONMENT VARIABLE ISSUES:\n"
        if missing:
            msg += "\n  MISSING (analyses will fail):\n" + "\n".join(missing) + "\n"
        if placeholder:
            msg += "\n  PLACEHOLDER VALUES (update in .env):\n" + "\n".join(placeholder) + "\n"
        msg += "\n  See .env.example for setup instructions.\n"
        logger.warning(msg)


def _check_db_connectivity(db_path: str = "research.db"):
    """Verify database is accessible at startup."""
    try:
        db = get_db(db_path)
        with db._connect() as conn:
            conn.execute("SELECT 1")
        logger.info("Database OK: %s", db_path)
    except Exception as e:
        logger.warning("Database check failed: %s", e)


# Background services
_alert_monitor = AlertMonitor(db_path="research.db", check_interval=300)
_screening_scheduler = ScreeningScheduler(
    db_path="research.db",
    poll_interval=DEFAULT_CONFIG.get("screening", {}).get("scheduler", {}).get("poll_interval", 300),
)


_cache_cleanup_timer: Optional[threading.Timer] = None


def _bootstrap_registry_materialization_background(db_path: str = "research.db") -> None:
    """Best-effort first-boot materialization for registry-backed built-ins."""
    def _worker():
        try:
            db = get_db(db_path)
            pending = db.get_registry_bootstrap_candidates() if hasattr(db, "get_registry_bootstrap_candidates") else []
            target_names = {"S&P 500", "S&P 400", "S&P 600", "Russell 2000"}
            pending = [w for w in pending if str(w.get("name") or "") in target_names]
            if not pending:
                return
            from tradingagents.screening.builtin_refresh import build_refresh_proposal
            proposal = build_refresh_proposal(db=db, config=DEFAULT_CONFIG, source="bootstrap_refresh")
            pid = int(proposal.get("id") or 0)
            if pid > 0:
                db.apply_builtin_refresh_proposal(
                    pid,
                    force_apply=True,
                    force_reason="bootstrap_first_materialization",
                    guard_config=(
                        DEFAULT_CONFIG.get("screening", {})
                        .get("scheduler", {})
                        .get("builtin_refresh", {})
                    ),
                    bootstrap=True,
                )
                logger.info("Bootstrap registry materialization applied: proposal_id=%s", pid)
        except Exception as exc:
            logger.warning("Bootstrap registry materialization skipped: %s", exc)

    t = threading.Thread(target=_worker, daemon=True, name="registry-bootstrap-materialization")
    t.start()


def _periodic_cache_cleanup():
    """Run cache.cleanup_expired() and reschedule for the next hour."""
    global _cache_cleanup_timer
    try:
        from tradingagents.dataflows.cache import get_cache
        evicted = get_cache().cleanup_expired()
        if evicted:
            logger.info("Periodic cache cleanup: evicted %d expired entries", evicted)
    except Exception as exc:
        logger.debug("Periodic cache cleanup error: %s", exc)
    _cache_cleanup_timer = threading.Timer(3600, _periodic_cache_cleanup)
    _cache_cleanup_timer.daemon = True
    _cache_cleanup_timer.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: validate env, start/stop background services."""
    global _cache_cleanup_timer

    # Structured logging setup (Feature 19)
    from tradingagents.utils.logging_config import setup_logging
    setup_logging(level="INFO")

    # Pre-flight checks
    _validate_env()
    _check_db_connectivity()

    # Cleanup stale cache entries at startup
    try:
        from tradingagents.dataflows.cache import get_cache
        cache = get_cache()
        evicted = cache.cleanup_expired()
        if evicted:
            logger.info("Cache cleanup: evicted %d expired entries", evicted)
    except Exception as e:
        logger.info("Cache cleanup skipped: %s", e)

    # Start periodic cache cleanup (hourly)
    _cache_cleanup_timer = threading.Timer(3600, _periodic_cache_cleanup)
    _cache_cleanup_timer.daemon = True
    _cache_cleanup_timer.start()

    # Start services
    _alert_monitor.start()
    if DEFAULT_CONFIG.get("screening", {}).get("scheduler", {}).get("enabled", True):
        _screening_scheduler.start()
    _bootstrap_registry_materialization_background(db_path="research.db")
    yield
    # Shutdown
    if _cache_cleanup_timer:
        _cache_cleanup_timer.cancel()
    _screening_scheduler.stop()
    _alert_monitor.stop()
    _executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="TradingAgents Local API", lifespan=lifespan)


# ---------- Global exception handler ----------
from fastapi.responses import JSONResponse


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions and return structured 500 responses."""
    import traceback
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    logger.error("".join(tb[-3:]))
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {str(exc)[:200]}"},
    )


# Static files and templates
WEBAPP_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=WEBAPP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEBAPP_DIR / "templates")
_executor = ThreadPoolExecutor(max_workers=4)
_jobs_lock = Lock()
_jobs: Dict[str, Dict[str, object]] = {}

# In-memory response cache for expensive, infrequently-changing endpoints
import time as _time
_response_cache: Dict[str, tuple] = {}   # key → (data, expire_ts)
_RESPONSE_CACHE_TTL = 600  # 10 minutes
_movers_idempotency_cache: Dict[str, tuple] = {}  # key -> (response, expire_ts)
_long_horizon_idempotency_cache: Dict[str, tuple] = {}  # key -> (response, expire_ts)
_movers_last_status: Dict[str, object] = {
    "last_run_at": None,
    "last_error": None,
    "last_snapshot_id": None,
}
_long_horizon_last_status: Dict[str, object] = {
    "last_run_at": None,
    "last_error": None,
    "last_run_id": None,
}


def _cached_response(key: str, ttl: int = _RESPONSE_CACHE_TTL):
    """Return cached response dict if valid, else None."""
    entry = _response_cache.get(key)
    if entry and entry[1] > _time.time():
        return entry[0]
    return None


def _set_response_cache(key: str, data, ttl: int = _RESPONSE_CACHE_TTL):
    _response_cache[key] = (data, _time.time() + ttl)


def _invalidate_response_cache(prefix: str = ""):
    """Invalidate cache entries matching prefix (or all if empty)."""
    keys = [k for k in _response_cache if k.startswith(prefix)] if prefix else list(_response_cache)
    for k in keys:
        _response_cache.pop(k, None)


def _error_envelope(code: str, message: str, retryable: bool = False, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        }
    }


def _extract_movers_idempotency_key(request: Request, body_key: Optional[str]) -> str:
    header_key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    return (body_key or header_key or "").strip()


def _extract_long_horizon_idempotency_key(request: Request, body_key: Optional[str]) -> str:
    header_key = request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key")
    return (body_key or header_key or "").strip()


def _load_idempotent_movers_response(key: str) -> Optional[Dict[str, Any]]:
    if not key:
        return None
    payload = _movers_idempotency_cache.get(key)
    if not payload:
        return None
    value, expires_at = payload
    if expires_at <= _time.time():
        _movers_idempotency_cache.pop(key, None)
        return None
    return value


def _store_idempotent_movers_response(key: str, response: Dict[str, Any]) -> None:
    if not key:
        return
    ttl = int(DEFAULT_CONFIG.get("screening", {}).get("movers", {}).get("idempotency_window_seconds", 600))
    _movers_idempotency_cache[key] = (response, _time.time() + max(ttl, 60))


def _load_idempotent_long_horizon_response(key: str) -> Optional[Dict[str, Any]]:
    if not key:
        return None
    payload = _long_horizon_idempotency_cache.get(key)
    if not payload:
        return None
    value, expires_at = payload
    if expires_at <= _time.time():
        _long_horizon_idempotency_cache.pop(key, None)
        return None
    return value


def _store_idempotent_long_horizon_response(key: str, response: Dict[str, Any]) -> None:
    if not key:
        return
    ttl = int(
        DEFAULT_CONFIG.get("screening", {})
        .get("long_horizon", {})
        .get("idempotency_window_seconds", 900)
    )
    _long_horizon_idempotency_cache[key] = (response, _time.time() + max(ttl, 60))


class AnalyzeRequest(BaseModel):
    ticker: str = Field(..., min_length=1)
    date: str = Field(..., description="YYYY-MM-DD")
    mode: str = Field("standard", description="quick|standard|deep")
    risk_profile: str = Field("growth", description="growth|conservative")
    investment_profile: Optional[str] = Field(None, description="Investment profile key (e.g. large_cap_core, dividend_income, high_growth, commodity_cyclical, momentum_speculative)")
    cooldown_seconds: Optional[int] = Field(None, ge=30, le=600, description="Optional queue cooldown override")
    output_dir: str = "research_output"
    db_path: str = "research.db"
    screening_run_id: Optional[int] = None
    watchlist_id: Optional[int] = None


class AnalyzeQueueRequest(BaseModel):
    ticker: str = Field(..., min_length=1)
    date: Optional[str] = Field(None, description="YYYY-MM-DD (defaults to today)")
    mode: str = Field("standard", description="quick|standard|deep")
    risk_profile: str = Field("growth", description="aggressive|growth|conservative")
    investment_profile: Optional[str] = Field(None, description="Investment profile key")
    watchlist_id: Optional[int] = Field(None, description="Optional watchlist context for auto profile")
    cooldown_seconds: Optional[int] = Field(None, ge=30, le=600, description="Optional queue cooldown override")
    output_dir: str = "research_output"
    db_path: str = "research.db"
    requested_from: str = Field("ui", description="UI source tag (e.g. screener, analyze_page)")
    screening_run_id: Optional[int] = None


class AnalyzeQueueItem(BaseModel):
    ticker: str = Field(..., min_length=1)
    watchlist_id: Optional[int] = None
    investment_profile: Optional[str] = None
    screening_run_id: Optional[int] = None


class AnalyzeQueueBatchRequest(BaseModel):
    items: Optional[List[AnalyzeQueueItem]] = None
    tickers: Optional[List[str]] = None
    date: Optional[str] = Field(None, description="YYYY-MM-DD (defaults to today)")
    mode: str = Field("standard", description="quick|standard|deep")
    risk_profile: str = Field("growth", description="aggressive|growth|conservative")
    investment_profile: Optional[str] = Field(None, description="Investment profile key")
    output_dir: str = "research_output"
    db_path: str = "research.db"
    cooldown_seconds: Optional[int] = Field(None, ge=30, le=600)
    requested_from: str = Field("screener_batch", description="UI source tag")
    screening_run_id: Optional[int] = None


class BatchRequest(BaseModel):
    tickers: List[str] = Field(..., min_length=1)
    date: str = Field(..., description="YYYY-MM-DD")
    mode: str = Field("standard", description="quick|standard|deep")
    risk_profile: str = Field("growth", description="aggressive|growth|conservative")
    investment_profile: Optional[str] = Field(None, description="Investment profile key")
    delay: int = Field(60, ge=30, le=300, description="Delay between analyses (60s+ recommended)")
    output_dir: str = "research_output"
    db_path: str = "research.db"
    snapshot_filter: Optional[str] = None


class ScreenRequest(BaseModel):
    watchlist_id: Optional[int] = None
    tickers: Optional[List[str]] = None
    date: Optional[str] = None
    preset: Optional[str] = None
    weights: Optional[Dict[str, float]] = None
    mode: Optional[str] = Field(
        "standard",
        description="Scan strategy: standard | reversal_buildup | early_momentum. Not the same as AnalyzeTopNRequest.mode (quick/standard/deep).",
    )
    db_path: str = "research.db"


class ScreenAllRequest(BaseModel):
    mode: Optional[str] = Field(
        "standard",
        description="Scan strategy: standard | reversal_buildup | early_momentum. Independent of scan_all.mode (per_watchlist | union_buckets).",
    )
    db_path: str = "research.db"


class MoversScanRequest(BaseModel):
    include_losers: Optional[bool] = None
    top_n: int = Field(25, ge=1, le=100)
    source_lists: Optional[List[str]] = None
    horizons: Optional[List[str]] = None
    respect_freshness_guard: bool = Field(
        False,
        description="When true, skip scans before post-close cutoff. Defaults to false for manual intraday scans.",
    )
    idempotency_key: Optional[str] = None
    db_path: str = "research.db"


class MoversRunsRequest(BaseModel):
    limit: int = Field(20, ge=1, le=200)
    offset: int = Field(0, ge=0)


class LongHorizonScanRequest(BaseModel):
    watchlist_id: Optional[int] = None
    tickers: Optional[List[str]] = None
    use_expanded_universe: bool = True
    horizon: str = Field("long_6to12m", pattern="^(long_6to12m|long_12to36m)$")
    top_n: int = Field(30, ge=1, le=200)
    date: Optional[str] = Field(None, description="YYYY-MM-DD")
    overrides: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None
    db_path: str = "research.db"


class LongHorizonRunsRequest(BaseModel):
    limit: int = Field(20, ge=1, le=200)
    offset: int = Field(0, ge=0)


class LongHorizonUnderwriteRequest(BaseModel):
    top_n: int = Field(10, ge=1, le=50)
    template_version: Optional[str] = None
    date: Optional[str] = Field(None, description="YYYY-MM-DD")
    idempotency_key: Optional[str] = None
    db_path: str = "research.db"


class LongHorizonAllocateRequest(BaseModel):
    policy_name: str = "default"
    policy_version: Optional[str] = None
    idempotency_key: Optional[str] = None
    db_path: str = "research.db"


class BuiltinRefreshApplyRequest(BaseModel):
    force_apply: bool = False
    force_reason: str = ""


class WatchlistRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    tickers: str = ""
    default_preset: Optional[str] = None
    default_investment_profile: Optional[str] = None


class TickerModRequest(BaseModel):
    ticker: str = Field(..., min_length=1)


class AnalyzeTopNRequest(BaseModel):
    tickers: List[str] = Field(..., min_length=1)
    date: str = Field(..., description="YYYY-MM-DD")
    mode: str = Field("standard")
    risk_profile: str = Field("growth")
    investment_profile: Optional[str] = Field(None, description="Investment profile key")
    delay: int = Field(60, ge=30, le=300)
    output_dir: str = "research_output"
    db_path: str = "research.db"
    screening_run_id: Optional[int] = None


class DiscoverRequest(BaseModel):
    """Request body for Perplexity-powered ticker discovery."""
    theme: str = Field(..., min_length=5)
    criteria: str = ""
    max_results: int = Field(default=20, ge=5, le=50)
    market_cap_filter: str = ""
    template_id: Optional[str] = None
    prompt_pack: Optional[str] = None
    asset_policy: Optional[str] = None
    auto_create_watchlist: bool = False
    watchlist_name: str = ""
    watchlist_preset: Optional[str] = None
    watchlist_profile: Optional[str] = None
    db_path: str = "research.db"


class AlertRuleRequest(BaseModel):
    """Request body for creating an alert rule."""
    ticker: str = Field(..., min_length=1, description="Stock ticker to monitor")
    alert_type: str = Field(
        ...,
        description="Alert type: price_cross, volume_spike, rsi_extreme, ma_crossover, earnings_approaching, rating_change, short_interest_spike",
    )
    condition: dict = Field(default_factory=dict, description="Type-specific condition parameters")


_JOBS_MAX_AGE_HOURS = 24  # Evict completed/failed jobs older than this
_JOBS_MAX_COUNT = 200     # Hard cap on total jobs in memory


def _evict_stale_jobs():
    """Remove completed/failed jobs older than _JOBS_MAX_AGE_HOURS. Must be called with _jobs_lock held."""
    if len(_jobs) < _JOBS_MAX_COUNT // 2:
        return  # Only evict when approaching the limit
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=_JOBS_MAX_AGE_HOURS)).isoformat()
    stale_ids = [
        jid for jid, job in _jobs.items()
        if job.get("status") in ("completed", "failed")
        and (job.get("ended_at") or job.get("created_at", "")) < cutoff
    ]
    for jid in stale_ids:
        del _jobs[jid]
    # Hard cap: if still over limit, remove oldest completed jobs
    if len(_jobs) > _JOBS_MAX_COUNT:
        completed = sorted(
            [(jid, job) for jid, job in _jobs.items() if job.get("status") in ("completed", "failed")],
            key=lambda x: x[1].get("created_at", ""),
        )
        for jid, _ in completed[:len(_jobs) - _JOBS_MAX_COUNT]:
            del _jobs[jid]


def _set_job(job_id: str, data: Dict[str, object]) -> None:
    with _jobs_lock:
        _evict_stale_jobs()
        _jobs[job_id] = data


def _update_job(job_id: str, **updates: object) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id, {})
        job.update(updates)
        _jobs[job_id] = job


_MAX_ACTIVE_JOBS = 20  # Prevent unbounded queue growth

_ANALYSIS_QUEUE_MAX_PENDING = 50
_ANALYSIS_QUEUE_DEFAULT_COOLDOWN_SECONDS = 120
_VALID_ANALYSIS_MODES = {"quick", "standard", "deep"}
_VALID_RISK_PROFILES = {"aggressive", "growth", "conservative"}
_TICKER_PATTERN = re.compile(r"^[A-Z0-9.\-]{1,8}$")


class GlobalAnalysisExecutionGate:
    """Shared execution gate for all LLM-heavy analysis/batch jobs."""

    def __init__(self, cooldown_seconds: int = _ANALYSIS_QUEUE_DEFAULT_COOLDOWN_SECONDS):
        self._base_cooldown_seconds = cooldown_seconds
        self._cooldown_seconds = cooldown_seconds
        self._adaptive_multiplier = 1
        self._last_finished_at: Optional[datetime] = None
        self._cooldown_until: Optional[datetime] = None
        self._lock = Lock()

    def _wait_for_cooldown_locked(self) -> None:
        while True:
            if not self._last_finished_at:
                self._cooldown_until = None
                return
            elapsed = (datetime.now(timezone.utc) - self._last_finished_at).total_seconds()
            remaining = int(self._cooldown_seconds - elapsed)
            if remaining <= 0:
                self._cooldown_until = None
                return
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=remaining)
            _time.sleep(min(1.0, float(remaining)))

    def run(self, fn, *args, **kwargs):
        """Run fn with global serialization and shared cooldown."""
        with self._lock:
            self._wait_for_cooldown_locked()
            try:
                return fn(*args, **kwargs)
            finally:
                self._last_finished_at = datetime.now(timezone.utc)
                self._cooldown_until = None

    def apply_tpm_backoff(self, max_multiplier: int = 4) -> None:
        with self._lock:
            self._adaptive_multiplier = min(max_multiplier, self._adaptive_multiplier + 1)
            self._cooldown_seconds = min(
                600,
                int(self._base_cooldown_seconds * self._adaptive_multiplier),
            )
            logger.warning(
                "Global TPM backoff applied: base=%ss multiplier=%sx effective=%ss",
                self._base_cooldown_seconds,
                self._adaptive_multiplier,
                self._cooldown_seconds,
            )

    def reset_tpm_backoff(self) -> None:
        with self._lock:
            if self._adaptive_multiplier != 1:
                logger.info(
                    "Resetting global adaptive cooldown: effective=%ss -> base=%ss",
                    self._cooldown_seconds,
                    self._base_cooldown_seconds,
                )
            self._adaptive_multiplier = 1
            self._cooldown_seconds = self._base_cooldown_seconds

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            cooldown_remaining = 0
            cooldown_until_iso = None
            if self._cooldown_until:
                remaining = int((self._cooldown_until - datetime.now(timezone.utc)).total_seconds())
                if remaining > 0:
                    cooldown_remaining = remaining
                    cooldown_until_iso = self._cooldown_until.isoformat()
                else:
                    self._cooldown_until = None
            return {
                "base_cooldown_seconds": self._base_cooldown_seconds,
                "cooldown_seconds": self._cooldown_seconds,
                "adaptive_multiplier": self._adaptive_multiplier,
                "cooldown_remaining_seconds": cooldown_remaining,
                "cooldown_until": cooldown_until_iso,
            }


_global_analysis_gate = GlobalAnalysisExecutionGate()


def _normalize_analysis_ticker(raw: str) -> str:
    ticker = (raw or "").strip().upper()
    if not _TICKER_PATTERN.match(ticker):
        raise HTTPException(status_code=400, detail="Invalid ticker format. Use 1-8 chars (A-Z, 0-9, . or -).")
    return ticker


def _parse_watchlist_ticker_payload(raw, *, reject_invalid: bool = False):
    from tradingagents.screening.discovery import parse_watchlist_tickers

    valid, rejected = parse_watchlist_tickers(raw)
    if reject_invalid and rejected:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid ticker symbols: "
                + ", ".join(rejected)
                + ". Use 1-8 characters (A-Z, 0-9, . or -) with no spaces."
            ),
        )
    return valid, rejected


def _normalize_analysis_date(raw: Optional[str]) -> str:
    if not raw:
        return date.today().isoformat()
    value = raw.strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.") from exc
    return value


def _normalize_analysis_mode(raw: Optional[str]) -> str:
    mode = (raw or "standard").strip().lower()
    return mode if mode in _VALID_ANALYSIS_MODES else "standard"


def _normalize_risk_profile(raw: Optional[str]) -> str:
    risk = (raw or "growth").strip().lower()
    return risk if risk in _VALID_RISK_PROFILES else "growth"


def _resolve_investment_profile_key(
    profile_key: Optional[str],
    watchlist_id: Optional[int],
    db_path: str = "research.db",
) -> Optional[str]:
    explicit = (profile_key or "").strip() or None
    if explicit:
        return explicit
    if not watchlist_id:
        return None
    try:
        db = get_db(db_path)
        wl = db.get_watchlist(watchlist_id)
        if wl:
            return wl.get("default_investment_profile")
    except Exception as exc:
        logger.debug("Failed to auto-resolve investment profile for watchlist %s: %s", watchlist_id, exc)
    return None


def _analysis_dedupe_key(ticker: str, asof_date: str, mode: str, risk_profile: str) -> str:
    return f"{ticker}|{asof_date}|{mode}|{risk_profile}"


class AnalysisQueueManager:
    """In-memory FIFO queue for single-ticker analyses with cooldown."""

    def __init__(self, cooldown_seconds: int = _ANALYSIS_QUEUE_DEFAULT_COOLDOWN_SECONDS, max_pending: int = _ANALYSIS_QUEUE_MAX_PENDING):
        self._base_cooldown_seconds = cooldown_seconds
        self._cooldown_seconds = cooldown_seconds
        self._adaptive_multiplier = 1
        self._max_pending = max_pending
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = Lock()
        self._pending_order: List[str] = []
        self._pending_requests: Dict[str, AnalyzeRequest] = {}
        self._cancelled_ids: set[str] = set()
        self._active_keys: set[str] = set()
        self._job_keys: Dict[str, str] = {}
        self._current_job_id: Optional[str] = None
        self._cooldown_until: Optional[datetime] = None
        self._last_finished_at: Optional[datetime] = None
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="analysis-queue-worker")
        self._worker.start()

    def set_cooldown(self, seconds: int) -> int:
        safe_seconds = max(30, min(600, int(seconds)))
        with self._lock:
            self._base_cooldown_seconds = safe_seconds
            self._cooldown_seconds = safe_seconds
            self._adaptive_multiplier = 1
        return safe_seconds

    def enqueue(self, req: AnalyzeRequest, metadata: Dict[str, Any], dedupe_key: str) -> Dict[str, Any]:
        with _jobs_lock:
            active = sum(1 for j in _jobs.values() if j.get("status") in ("queued", "running"))
            if active >= _MAX_ACTIVE_JOBS:
                raise HTTPException(status_code=503, detail=f"Too many jobs in flight ({active}). Please wait.")

        with self._lock:
            duplicate_job_id = next((jid for jid, key in self._job_keys.items() if key == dedupe_key), None)
            if duplicate_job_id:
                raise HTTPException(
                    status_code=409,
                    detail=f"{req.ticker} is already queued or running for {req.date} ({req.mode}/{req.risk_profile}).",
                )
            if len(self._pending_order) >= self._max_pending:
                raise HTTPException(
                    status_code=429,
                    detail=f"Analysis queue is full ({self._max_pending} pending). Please wait for it to drain.",
                )

            job_id = str(uuid.uuid4())
            self._pending_order.append(job_id)
            self._pending_requests[job_id] = req
            self._job_keys[job_id] = dedupe_key
            self._active_keys.add(dedupe_key)
            queue_position = len(self._pending_order)
            total_queued = len(self._pending_order) + (1 if self._current_job_id else 0)

        job_data = {
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result": None,
            "error": None,
            "job_type": "analysis_queue",
            "queue_position": queue_position,
            "queue_total": total_queued,
            **metadata,
        }
        _set_job(job_id, job_data)
        self._queue.put(job_id)
        return {"job_id": job_id, "queue_position": queue_position, "queue_total": total_queued}

    def remove_pending(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._pending_requests:
                return False
            self._pending_requests.pop(job_id, None)
            self._pending_order = [jid for jid in self._pending_order if jid != job_id]
            self._cancelled_ids.add(job_id)
            dedupe_key = self._job_keys.pop(job_id, None)
            if dedupe_key:
                self._active_keys.discard(dedupe_key)

        _update_job(
            job_id,
            status="cancelled",
            error="Removed from queue by user",
            ended_at=datetime.now(timezone.utc).isoformat(),
        )
        return True

    def clear_pending(self) -> List[str]:
        with self._lock:
            job_ids = list(self._pending_order)
        removed = []
        for job_id in job_ids:
            if self.remove_pending(job_id):
                removed.append(job_id)
        return removed

    def status_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            cooldown_remaining = 0
            cooldown_until_iso = None
            if self._cooldown_until:
                remaining = int((self._cooldown_until - datetime.now(timezone.utc)).total_seconds())
                if remaining > 0:
                    cooldown_remaining = remaining
                    cooldown_until_iso = self._cooldown_until.isoformat()
                else:
                    self._cooldown_until = None

            return {
                "current_job_id": self._current_job_id,
                "pending_job_ids": list(self._pending_order),
                "pending_count": len(self._pending_order),
                "base_cooldown_seconds": self._base_cooldown_seconds,
                "cooldown_seconds": self._cooldown_seconds,
                "adaptive_multiplier": self._adaptive_multiplier,
                "cooldown_remaining_seconds": cooldown_remaining,
                "cooldown_until": cooldown_until_iso,
                "max_pending": self._max_pending,
            }

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            return self._current_job_id == job_id

    def _apply_tpm_backoff(self, max_multiplier: int = 4) -> None:
        with self._lock:
            self._adaptive_multiplier = min(max_multiplier, self._adaptive_multiplier + 1)
            self._cooldown_seconds = min(
                600,
                int(self._base_cooldown_seconds * self._adaptive_multiplier),
            )
            logger.warning(
                "TPM backoff applied: base=%ss multiplier=%sx effective=%ss",
                self._base_cooldown_seconds,
                self._adaptive_multiplier,
                self._cooldown_seconds,
            )

    def _reset_tpm_backoff(self) -> None:
        with self._lock:
            if self._adaptive_multiplier != 1:
                logger.info(
                    "Resetting adaptive cooldown: effective=%ss -> base=%ss",
                    self._cooldown_seconds,
                    self._base_cooldown_seconds,
                )
            self._adaptive_multiplier = 1
            self._cooldown_seconds = self._base_cooldown_seconds

    def _wait_for_cooldown_if_needed(self) -> None:
        while True:
            with self._lock:
                if not self._last_finished_at:
                    self._cooldown_until = None
                    return
                elapsed = (datetime.now(timezone.utc) - self._last_finished_at).total_seconds()
                remaining = int(self._cooldown_seconds - elapsed)
                if remaining <= 0:
                    self._cooldown_until = None
                    return
                self._cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=remaining)
            _time.sleep(min(1.0, float(remaining)))

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()

            with self._lock:
                if job_id in self._cancelled_ids:
                    self._cancelled_ids.discard(job_id)
                    continue
                req = self._pending_requests.pop(job_id, None)
                self._pending_order = [jid for jid in self._pending_order if jid != job_id]
                self._current_job_id = job_id

            if not req:
                with self._lock:
                    self._current_job_id = None
                continue

            self._wait_for_cooldown_if_needed()
            _update_job(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat())

            try:
                result = _global_analysis_gate.run(_run_analysis, req, degraded=False)
                _update_job(job_id, status="completed", result=result, ended_at=datetime.now(timezone.utc).isoformat())
                self._reset_tpm_backoff()
                _global_analysis_gate.reset_tpm_backoff()
            except Exception as exc:
                adaptive_cfg = DEFAULT_CONFIG.get("token_budget", {}).get("adaptive_degrade", {})
                adaptive_enabled = bool(adaptive_cfg.get("enabled", True) and adaptive_cfg.get("feature_flag", True))
                retry_on_tpm = bool(adaptive_cfg.get("retry_on_tpm", True))
                max_multiplier = max(1, int(adaptive_cfg.get("cooldown_multiplier_max", 4)))
                retry_sleep_seconds = max(5, int(adaptive_cfg.get("cooldown_retry_sleep_seconds", 45)))

                if adaptive_enabled and retry_on_tpm and _is_tpm_rate_limit_error(exc):
                    logger.warning("TPM limit detected for %s — retrying with degraded token profile", req.ticker)
                    _update_job(
                        job_id,
                        status="running",
                        error="TPM limit detected; retrying with degraded token profile",
                    )
                    self._apply_tpm_backoff(max_multiplier=max_multiplier)
                    _global_analysis_gate.apply_tpm_backoff(max_multiplier=max_multiplier)
                    _time.sleep(min(120, retry_sleep_seconds))
                    try:
                        result = _global_analysis_gate.run(_run_analysis, req, degraded=True)
                        result["degraded"] = True
                        result["degraded_reason"] = "tpm_rate_limit_retry"
                        _update_job(
                            job_id,
                            status="completed",
                            result=result,
                            error=None,
                            ended_at=datetime.now(timezone.utc).isoformat(),
                        )
                        self._reset_tpm_backoff()
                        _global_analysis_gate.reset_tpm_backoff()
                    except Exception as retry_exc:
                        _update_job(job_id, status="failed", error=str(retry_exc), ended_at=datetime.now(timezone.utc).isoformat())
                else:
                    _update_job(job_id, status="failed", error=str(exc), ended_at=datetime.now(timezone.utc).isoformat())
            finally:
                with self._lock:
                    self._current_job_id = None
                    self._last_finished_at = datetime.now(timezone.utc)
                    self._cooldown_until = None
                    dedupe_key = self._job_keys.pop(job_id, None)
                    if dedupe_key:
                        self._active_keys.discard(dedupe_key)


_analysis_queue = AnalysisQueueManager()


def _resolve_metadata_background(tickers: List[str], db_path: str = "research.db"):
    """Fire-and-forget metadata resolution for a list of tickers.

    Runs in the background thread pool so it never blocks the API response.
    Safe to call with any list; stale/missing tickers are resolved automatically.
    """
    def _do():
        try:
            from tradingagents.screening.ticker_resolver import resolve_and_cache
            db = get_db(db_path)
            resolve_and_cache(tickers, db, max_workers=8, max_age_days=7)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Background metadata resolution failed: %s", e)

    if tickers:
        _executor.submit(_do)


def _submit_job(fn, metadata: Dict[str, object] = None, *args, **kwargs) -> str:
    """Submit a background job with optional metadata (ticker, mode, etc.)."""
    degraded_retry_fn = kwargs.pop("_degraded_retry_fn", None)

    # Guard against queue overload
    with _jobs_lock:
        active = sum(1 for j in _jobs.values() if j.get("status") in ("queued", "running"))
        if active >= _MAX_ACTIVE_JOBS:
            raise HTTPException(503, f"Too many jobs in flight ({active}). Please wait for some to finish.")
    job_id = str(uuid.uuid4())
    job_data = {
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "result": None,
        "error": None,
    }
    # Include metadata (ticker, mode, risk_profile, job_type) for UI display
    if metadata:
        job_data.update(metadata)
    _set_job(job_id, job_data)

    def runner():
        _update_job(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat())
        try:
            is_batch_job = bool((metadata or {}).get("job_type") == "batch")
            if is_batch_job:
                result = _global_analysis_gate.run(fn, *args, **kwargs)
            else:
                result = fn(*args, **kwargs)
            _update_job(job_id, status="completed", result=result, ended_at=datetime.now(timezone.utc).isoformat())
            if is_batch_job:
                _global_analysis_gate.reset_tpm_backoff()
        except Exception as exc:
            adaptive_cfg = DEFAULT_CONFIG.get("token_budget", {}).get("adaptive_degrade", {})
            adaptive_enabled = bool(adaptive_cfg.get("enabled", True) and adaptive_cfg.get("feature_flag", True))
            retry_on_tpm = bool(adaptive_cfg.get("retry_on_tpm", True))
            retry_sleep_seconds = max(5, int(adaptive_cfg.get("cooldown_retry_sleep_seconds", 45)))
            is_batch_job = bool((metadata or {}).get("job_type") == "batch")

            if (
                is_batch_job
                and adaptive_enabled
                and retry_on_tpm
                and callable(degraded_retry_fn)
                and _is_tpm_rate_limit_error(exc)
            ):
                logger.warning("TPM limit detected for batch job %s — retrying with degraded profile", job_id)
                _update_job(
                    job_id,
                    status="running",
                    error="TPM limit detected; retrying batch with degraded token profile",
                )
                max_multiplier = max(1, int(adaptive_cfg.get("cooldown_multiplier_max", 4)))
                _global_analysis_gate.apply_tpm_backoff(max_multiplier=max_multiplier)
                _time.sleep(min(120, retry_sleep_seconds))
                try:
                    result = _global_analysis_gate.run(degraded_retry_fn)
                    if isinstance(result, dict):
                        result["degraded"] = True
                        result["degraded_reason"] = "tpm_rate_limit_retry"
                    _update_job(
                        job_id,
                        status="completed",
                        result=result,
                        error=None,
                        ended_at=datetime.now(timezone.utc).isoformat(),
                    )
                    _global_analysis_gate.reset_tpm_backoff()
                except Exception as retry_exc:
                    _update_job(job_id, status="failed", error=str(retry_exc), ended_at=datetime.now(timezone.utc).isoformat())
            else:
                _update_job(job_id, status="failed", error=str(exc), ended_at=datetime.now(timezone.utc).isoformat())

    _executor.submit(runner)
    return job_id


def _get_active_jobs() -> List[Dict[str, object]]:
    """Get all jobs that are currently running or queued."""
    with _jobs_lock:
        return [
            {"job_id": jid, **job}
            for jid, job in _jobs.items()
            if job.get("status") in ("queued", "running")
        ]


def _resolve_investment_profile(profile_key: Optional[str]) -> Optional[dict]:
    """Resolve an investment profile key to its full dict from DEFAULT_CONFIG.
    Returns None if key is invalid or missing (agents fall back to generic prompts)."""
    if not profile_key:
        return None
    profiles = DEFAULT_CONFIG.get("investment_profiles", {})
    return profiles.get(profile_key)


def _is_tpm_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        ("429" in text or "rate_limit_exceeded" in text)
        and ("tokens per min" in text or "tpm" in text or "request too large" in text)
    )


def _apply_degraded_token_profile(config: Dict[str, Any]) -> Dict[str, Any]:
    degraded = deepcopy(config)
    budget = degraded.setdefault("token_budget", {})
    analyst = budget.setdefault("analyst", {})
    tools = budget.setdefault("tools", {})
    llm = budget.setdefault("llm", {})

    analyst["max_context_tokens"] = min(int(analyst.get("max_context_tokens", 16000)), 11000)
    analyst["max_messages"] = min(int(analyst.get("max_messages", 18)), 12)
    analyst["market_max_tool_messages"] = min(int(analyst.get("market_max_tool_messages", 5)), 4)
    analyst["social_max_tool_messages"] = min(int(analyst.get("social_max_tool_messages", 4)), 3)
    analyst["news_max_tool_messages"] = min(int(analyst.get("news_max_tool_messages", 5)), 4)
    analyst["fundamentals_max_tool_messages"] = min(int(analyst.get("fundamentals_max_tool_messages", 5)), 4)
    analyst["market_max_indicators"] = min(int(analyst.get("market_max_indicators", 4)), 3)
    analyst["fundamentals_enriched_context_max_tokens"] = min(
        int(analyst.get("fundamentals_enriched_context_max_tokens", 6000)),
        4200,
    )

    tools["get_stock_data"] = min(int(tools.get("get_stock_data", 5500)), 4200)
    tools["get_indicators"] = min(int(tools.get("get_indicators", 2800)), 2000)
    tools["get_fundamentals"] = min(int(tools.get("get_fundamentals", 5500)), 4200)
    tools["get_balance_sheet"] = min(int(tools.get("get_balance_sheet", 3800)), 3000)
    tools["get_cashflow"] = min(int(tools.get("get_cashflow", 3800)), 3000)
    tools["get_income_statement"] = min(int(tools.get("get_income_statement", 3800)), 3000)
    tools["get_news"] = min(int(tools.get("get_news", 2800)), 2000)
    tools["get_global_news"] = min(int(tools.get("get_global_news", 1900)), 1400)
    tools["get_insider_sentiment"] = min(int(tools.get("get_insider_sentiment", 1100)), 900)
    tools["get_insider_transactions"] = min(int(tools.get("get_insider_transactions", 1400)), 1000)

    llm["quick_max_output_tokens"] = min(int(llm.get("quick_max_output_tokens", 1600)), 1200)
    llm["deep_max_output_tokens"] = min(int(llm.get("deep_max_output_tokens", 2200)), 1800)

    degraded["max_debate_rounds"] = min(int(degraded.get("max_debate_rounds", 1)), 1)
    degraded["max_risk_discuss_rounds"] = min(int(degraded.get("max_risk_discuss_rounds", 1)), 1)
    return degraded


def _run_analysis(req: AnalyzeRequest, degraded: bool = False) -> Dict[str, object]:
    config = get_config_for_mode(req.mode, DEFAULT_CONFIG)
    if degraded:
        config = _apply_degraded_token_profile(config)
    # Apply risk profile (independent of research mode)
    config["risk_profile"] = req.risk_profile
    logger.info(
        "Starting analysis: %s mode=%s risk_profile=%s investment_profile=%s degraded=%s",
        req.ticker,
        req.mode,
        req.risk_profile,
        req.investment_profile or "auto",
        degraded,
    )
    screening_context = None
    if req.screening_run_id is not None:
        try:
            db = get_db(req.db_path)
            rows = db.get_screening_results(req.screening_run_id)
            row = next(
                (item for item in rows if str(item.get("ticker") or "").upper() == req.ticker),
                None,
            )
            if row:
                run_meta = db.get_screening_run(req.screening_run_id) or {}
                criteria = _parse_run_criteria(run_meta.get("criteria"))
                enriched = _enrich_screening_rows(
                    [row],
                    db=db,
                    preset=str(criteria.get("preset") or "default"),
                )
                if enriched:
                    from tradingagents.screening.context_packet import (
                        build_context_packet,
                        format_screening_context,
                    )

                    screening_context = dict(
                        build_context_packet(
                            enriched[0],
                            run_id=req.screening_run_id,
                            preset=str(criteria.get("preset") or "default"),
                        )
                    )
                    screening_context["summary_text"] = format_screening_context(
                        screening_context
                    )
        except Exception:
            logger.debug(
                "Could not build screening context for queued analysis",
                exc_info=True,
            )

    agent = ResearchAgent(
        config=config,
        output_dir=req.output_dir,
        db_path=req.db_path,
        auto_report=True,
        auto_save=True,
        debug=False,
    )
    result = agent.analyze(
        req.ticker,
        req.date,
        investment_profile_key=req.investment_profile,
        watchlist_id=req.watchlist_id,
        screening_run_id=req.screening_run_id,
        screening_context=screening_context,
    )
    from tradingagents.reporting.position_action import (
        extract_decision_json_from_text,
        format_decision_label,
        parse_position_action_from_state,
    )

    position_action = parse_position_action_from_state(result.state or {})
    if not position_action:
        position_action = str(
            extract_decision_json_from_text(result.final_decision_text or "").get("position_action") or ""
        )
    return {
        "analysis_id": result.analysis_id,
        "decision": result.decision,
        "position_action": position_action or None,
        "decision_label": format_decision_label(result.decision, position_action),
        "html_path": result.html_path,
        "pdf_path": result.pdf_path,
        "mode": req.mode,
        "risk_profile": req.risk_profile,
        "investment_profile": (result.state or {}).get("investment_profile"),
        "screening_run_id": req.screening_run_id,
    }


def _run_batch(req: BatchRequest, degraded: bool = False) -> Dict[str, object]:
    config = get_config_for_mode(req.mode, DEFAULT_CONFIG)
    if degraded:
        config = _apply_degraded_token_profile(config)
    # Apply risk profile (independent of research mode)
    config["risk_profile"] = req.risk_profile
    agent = ResearchAgent(
        config=config,
        output_dir=req.output_dir,
        db_path=req.db_path,
        auto_report=True,
        auto_save=True,
        debug=False,
    )
    results = agent.batch_analyze(
        req.tickers,
        req.date,
        delay_seconds=req.delay,
        save_summary=True,
        summary_snapshot_filter=req.snapshot_filter,
        investment_profile_key=req.investment_profile,
    )
    return {"count": len(results), "degraded": degraded}


def _enqueue_analysis_request(payload: AnalyzeQueueRequest) -> Dict[str, object]:
    if payload.cooldown_seconds is not None:
        applied = _analysis_queue.set_cooldown(payload.cooldown_seconds)
        logger.info("Analysis queue cooldown updated to %ss", applied)

    ticker = _normalize_analysis_ticker(payload.ticker)
    asof_date = _normalize_analysis_date(payload.date)
    mode = _normalize_analysis_mode(payload.mode)
    risk_profile = _normalize_risk_profile(payload.risk_profile)
    inv_profile_key = (payload.investment_profile or "").strip() or None

    req = AnalyzeRequest(
        ticker=ticker,
        date=asof_date,
        mode=mode,
        risk_profile=risk_profile,
        investment_profile=inv_profile_key,
        watchlist_id=payload.watchlist_id,
        output_dir=payload.output_dir,
        db_path=payload.db_path,
        screening_run_id=payload.screening_run_id,
    )
    metadata = {
        "ticker": ticker,
        "date": asof_date,
        "mode": mode,
        "risk_profile": risk_profile,
        "investment_profile": inv_profile_key,
        "watchlist_id": payload.watchlist_id,
        "requested_from": payload.requested_from or "ui",
        "screening_run_id": payload.screening_run_id,
    }
    dedupe_key = _analysis_dedupe_key(ticker, asof_date, mode, risk_profile)
    queued = _analysis_queue.enqueue(req, metadata, dedupe_key)
    return {
        "job_id": queued["job_id"],
        "queued": True,
        "queue_position": queued["queue_position"],
        "queue_total": queued["queue_total"],
        "cooldown_seconds": _analysis_queue.status_snapshot().get("cooldown_seconds"),
        **metadata,
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, object]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        # Keep 404 semantics, but include terminal status fields so older
        # frontend pollers (that didn't check res.ok) can still stop polling
        # instead of looping forever on stale IDs after an app restart.
        return JSONResponse(
            status_code=404,
            content={
                "job_id": job_id,
                "status": "failed",
                "error": "Job not found",
                "detail": "Job not found",
            },
        )
    return {"job_id": job_id, **job}


@app.get("/api/jobs")
def list_jobs(active_only: bool = True) -> Dict[str, object]:
    """List all jobs, optionally filtered to active (queued/running) only."""
    with _jobs_lock:
        if active_only:
            jobs = [
                {"job_id": jid, **job}
                for jid, job in _jobs.items()
                if job.get("status") in ("queued", "running")
            ]
        else:
            jobs = [{"job_id": jid, **job} for jid, job in _jobs.items()]
    # Sort by created_at descending (most recent first)
    jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/api/analyses")
def list_analyses(
    ticker: Optional[str] = None,
    limit: int = 50,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    risk_profile: Optional[str] = None,
    analysis_mode: Optional[str] = None,
    db_path: str = "research.db",
) -> List[Dict[str, object]]:
    db = get_db(db_path)
    if start_date or end_date or risk_profile or analysis_mode:
        analyses = db.get_analyses_by_date_range(
            start_date=start_date,
            end_date=end_date,
            ticker=ticker,
            risk_profile=risk_profile,
            analysis_mode=analysis_mode,
            limit=limit,
        )
    elif ticker:
        analyses = db.get_analyses_by_ticker(ticker, limit)
    else:
        analyses = db.get_recent_analyses(limit)

    output = []
    for analysis in analyses:
        warnings = []
        if analysis.report_warnings:
            try:
                warnings = json.loads(analysis.report_warnings)
            except json.JSONDecodeError:
                warnings = [analysis.report_warnings]
        output.append(
            enrich_analysis_api_record(
                {
                "id": analysis.id,
                "ticker": analysis.ticker,
                "analysis_date": analysis.analysis_date,
                "created_at": analysis.created_at,
                "decision": analysis.decision,
                "confidence": analysis.confidence,
                "risk_profile": getattr(analysis, "risk_profile", "") or "",
                "investment_profile": getattr(analysis, "investment_profile", "") or "",
                "analysis_mode": getattr(analysis, "analysis_mode", "") or "",
                "data_quality_score": getattr(analysis, "data_quality_score", 0),
                "qa_warnings": warnings,
                "decision_json": getattr(analysis, "decision_json", "") or "",
                "sec_snapshot": bool(analysis.sec_filings_snapshot),
                "transcript_snapshot": bool(analysis.earnings_transcript_snapshot),
                "notes": getattr(analysis, "notes", "") or "",
                "tags": getattr(analysis, "tags", "") or "",
                "alpha_7d": getattr(analysis, "alpha_7d", None),
                "alpha_14d": getattr(analysis, "alpha_14d", None),
                "alpha_30d": getattr(analysis, "alpha_30d", None),
                "signal_summary": getattr(analysis, "signal_summary", "") or "",
                }
            )
        )
    return output


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: int, db_path: str = "research.db") -> Dict[str, object]:
    db = get_db(db_path)
    analysis = db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return enrich_analysis_api_record(dict(analysis.__dict__))


def _find_report_file(base_dir: str, filename: str) -> Optional[str]:
    """Find a report file, checking subdirectory first then flat root (backward compat)."""
    subdir_path = os.path.join(base_dir, "reports", filename)
    if os.path.exists(subdir_path):
        return subdir_path
    root_path = os.path.join(base_dir, filename)
    if os.path.exists(root_path):
        return root_path
    return None


def _resolve_analysis_report_html(
    output_dir: str,
    ticker: str,
    analysis_date: str,
    analysis_mode: Optional[str],
) -> Optional[str]:
    from tradingagents.reporting.report_paths import resolve_report_html

    resolved = resolve_report_html(output_dir, ticker, analysis_date, analysis_mode)
    return str(resolved) if resolved is not None else None


def _resolve_analysis_report_pdf(
    output_dir: str,
    ticker: str,
    analysis_date: str,
    analysis_mode: Optional[str],
) -> Optional[str]:
    from tradingagents.reporting.report_paths import resolve_report_pdf

    resolved = resolve_report_pdf(output_dir, ticker, analysis_date, analysis_mode)
    return str(resolved) if resolved is not None else None


# Extract the main .page wrapper from a generated report document so the
# embedded viewer can mount it without leaking the standalone document's
# inline <style> block (which is hard-coded to a light-only palette and
# would otherwise override the app theme in dark modes). Falls back to a
# <body>…</body> extraction, then to the raw string — we never want the
# viewer to fail just because the generator format changed.
_REPORT_PAGE_RE = re.compile(
    r"<div\s+class=\"page\"[^>]*>.*?</div>\s*</body>",
    re.DOTALL | re.IGNORECASE,
)
_REPORT_BODY_RE = re.compile(
    r"<body[^>]*>(.*?)</body>",
    re.DOTALL | re.IGNORECASE,
)


def _extract_report_body(html: str) -> str:
    """Return the inner content suitable for embedding (no <html>/<head>/<style>)."""
    m = _REPORT_PAGE_RE.search(html)
    if m:
        # Re-match to grab just the <div class="page">…</div> (strip trailing </body>)
        page_match = re.search(
            r"(<div\s+class=\"page\"[^>]*>.*?</div>)\s*</body>",
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if page_match:
            return page_match.group(1)
    body_match = _REPORT_BODY_RE.search(html)
    if body_match:
        return body_match.group(1)
    return html


@app.get("/api/reports/{analysis_id}")
def get_report(
    analysis_id: int,
    output_dir: str = "research_output",
    db_path: str = "research.db",
    embed: int = 1,
) -> Dict[str, object]:
    """Return a report's HTML.

    By default (``embed=1``) returns only the inner body content so the
    viewer can render it inside a theme-scoped container. Pass
    ``embed=0`` to get the original self-contained document (used by the
    raw-HTML endpoint below; not typically consumed by the viewer JS).
    """
    db_path = _safe_db_path(db_path)
    safe_dir = _safe_output_dir(output_dir)
    db = get_db(db_path)
    analysis = db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    path = _resolve_analysis_report_html(
        safe_dir,
        analysis.ticker,
        analysis.analysis_date,
        getattr(analysis, "analysis_mode", None),
    )
    if not path:
        raise HTTPException(status_code=404, detail="Report not found")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    payload_html = _extract_report_body(content) if embed else content
    return {
        "analysis_id": analysis_id,
        "html": payload_html,
        "embedded": bool(embed),
    }


@app.get("/api/reports/{analysis_id}/raw", response_class=HTMLResponse)
def get_report_raw(
    analysis_id: int,
    output_dir: str = "research_output",
    db_path: str = "research.db",
):
    """Serve the unmodified standalone report HTML.

    Used by the viewer's "HTML" export button (open in a new tab for
    sharing/printing) so the full light-themed, print-ready document is
    delivered instead of the body-only embed payload.
    """
    db_path = _safe_db_path(db_path)
    safe_dir = _safe_output_dir(output_dir)
    db = get_db(db_path)
    analysis = db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    path = _resolve_analysis_report_html(
        safe_dir,
        analysis.ticker,
        analysis.analysis_date,
        getattr(analysis, "analysis_mode", None),
    )
    if not path:
        raise HTTPException(status_code=404, detail="Report not found")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, status_code=200)


@app.post("/api/reports/{analysis_id}/pdf")
def generate_pdf(analysis_id: int, output_dir: str = "research_output", db_path: str = "research.db"):
    """Generate PDF from HTML report and return as download."""
    from fastapi.responses import FileResponse
    
    db_path = _safe_db_path(db_path)
    safe_dir = _safe_output_dir(output_dir)
    db = get_db(db_path)
    analysis = db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    
    html_path = _resolve_analysis_report_html(
        safe_dir,
        analysis.ticker,
        analysis.analysis_date,
        getattr(analysis, "analysis_mode", None),
    )
    if not html_path:
        raise HTTPException(status_code=404, detail="HTML report not found")

    pdf_path = _resolve_analysis_report_pdf(
        safe_dir,
        analysis.ticker,
        analysis.analysis_date,
        getattr(analysis, "analysis_mode", None),
    )
    if not pdf_path:
        from tradingagents.reporting.report_paths import write_report_pdf_path

        pdf_path = str(
            write_report_pdf_path(
                safe_dir,
                analysis.ticker,
                analysis.analysis_date,
                getattr(analysis, "analysis_mode", None),
            )
        )
    
    download_name = os.path.basename(pdf_path)

    # Check if PDF already exists and is newer than HTML
    if os.path.exists(pdf_path) and os.path.getmtime(pdf_path) > os.path.getmtime(html_path):
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=download_name,
        )
    
    # Generate PDF from HTML
    try:
        from weasyprint import HTML
        HTML(filename=html_path).write_pdf(pdf_path)
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="PDF generation requires weasyprint. Install with: pip install weasyprint"
        )
    except Exception as e:
        # Check for common system dependency issues
        error_msg = str(e)
        if "pango" in error_msg.lower() or "cairo" in error_msg.lower():
            raise HTTPException(
                status_code=500,
                detail="PDF generation requires system libraries. On macOS: brew install pango cairo. On Linux: apt-get install libpango-1.0-0 libcairo2. Alternatively, use your browser's Print to PDF."
            )
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {error_msg}")
    
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=download_name,
    )


@app.get("/api/report-diff")
def get_report_diff(
    ticker: Optional[str] = None,
    date_a: Optional[str] = None,
    date_b: Optional[str] = None,
    file_a: Optional[str] = None,
    file_b: Optional[str] = None,
    output_dir: str = "research_output",
) -> Dict[str, object]:
    from tradingagents.reporting.report_diff import diff_reports

    # Validate file paths if provided
    safe_dir = _safe_output_dir(output_dir)
    if file_a:
        _safe_file_path(file_a)
    if file_b:
        _safe_file_path(file_b)

    try:
        diff = diff_reports(
            file_a=file_a,
            file_b=file_b,
            ticker=ticker,
            date_a=date_a,
            date_b=date_b,
            output_dir=output_dir,
        )
        return {"diff": diff}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/snapshots")
def list_snapshots(
    snapshot_type: Optional[str] = None,
    ticker: Optional[str] = None,
    limit: int = 100,
    db_path: str = "research.db",
) -> List[Dict[str, object]]:
    db = get_db(db_path)
    return db.get_snapshots(snapshot_type=snapshot_type, ticker=ticker, limit=limit)


@app.get("/api/qa")
def qa_summary(limit: int = 200, db_path: str = "research.db") -> Dict[str, object]:
    db = get_db(db_path)
    analyses = db.get_recent_analyses(limit)
    warning_counts: Dict[str, int] = {}
    for analysis in analyses:
        raw = analysis.report_warnings or "[]"
        try:
            warnings = json.loads(raw)
        except json.JSONDecodeError:
            warnings = [raw]
        for warning in warnings:
            warning_counts[warning] = warning_counts.get(warning, 0) + 1
    return {"total_analyses": len(analyses), "warnings": warning_counts}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> Dict[str, object]:
    payload = AnalyzeQueueRequest(
        ticker=req.ticker,
        date=req.date,
        mode=req.mode,
        risk_profile=req.risk_profile,
        investment_profile=req.investment_profile,
        cooldown_seconds=req.cooldown_seconds,
        output_dir=req.output_dir,
        db_path=req.db_path,
        requested_from="analyze_api",
        screening_run_id=req.screening_run_id,
    )
    return _enqueue_analysis_request(payload)


@app.post("/api/analyze/queue")
def analyze_queue(req: AnalyzeQueueRequest) -> Dict[str, object]:
    return _enqueue_analysis_request(req)


@app.post("/api/analyze/queue/batch")
def analyze_queue_batch(req: AnalyzeQueueBatchRequest) -> Dict[str, object]:
    if req.cooldown_seconds is not None:
        applied = _analysis_queue.set_cooldown(req.cooldown_seconds)
        logger.info("Analysis queue cooldown updated to %ss", applied)

    items: List[AnalyzeQueueItem] = []
    if req.items:
        items.extend(req.items)
    if req.tickers:
        items.extend(AnalyzeQueueItem(ticker=t) for t in req.tickers)
    if not items:
        raise HTTPException(status_code=400, detail="Provide at least one ticker via items or tickers.")

    queued_jobs = []
    skipped = []
    for item in items:
        try:
            payload = AnalyzeQueueRequest(
                ticker=item.ticker,
                date=req.date,
                mode=req.mode,
                risk_profile=req.risk_profile,
                investment_profile=item.investment_profile or req.investment_profile,
                watchlist_id=item.watchlist_id,
                output_dir=req.output_dir,
                db_path=req.db_path,
                requested_from=req.requested_from or "screener_batch",
                screening_run_id=item.screening_run_id or req.screening_run_id,
            )
            queued_jobs.append(_enqueue_analysis_request(payload))
        except HTTPException as exc:
            skipped.append({"ticker": (item.ticker or "").upper(), "status_code": exc.status_code, "reason": str(exc.detail)})

    snapshot = _analysis_queue.status_snapshot()
    return {
        "queued_count": len(queued_jobs),
        "queued_jobs": queued_jobs,
        "skipped": skipped,
        "queue_length": snapshot["pending_count"] + (1 if snapshot["current_job_id"] else 0),
        "cooldown_seconds": snapshot["cooldown_seconds"],
    }


@app.get("/api/analyze/queue/status")
def analyze_queue_status() -> Dict[str, object]:
    snapshot = _analysis_queue.status_snapshot()
    global_snapshot = _global_analysis_gate.snapshot()
    with _jobs_lock:
        current = _jobs.get(snapshot["current_job_id"]) if snapshot["current_job_id"] else None
        pending = [
            {"job_id": jid, **_jobs.get(jid, {})}
            for jid in snapshot["pending_job_ids"]
        ]
        recent_completed = sorted(
            [
                {"job_id": jid, **job}
                for jid, job in _jobs.items()
                if job.get("job_type") == "analysis_queue" and job.get("status") in ("completed", "failed", "cancelled")
            ],
            key=lambda x: x.get("ended_at", ""),
            reverse=True,
        )[:8]

    return {
        "running": {"job_id": snapshot["current_job_id"], **(current or {})} if snapshot["current_job_id"] else None,
        "pending": pending,
        "recent_completed": recent_completed,
        "pending_count": snapshot["pending_count"],
        "queue_length": snapshot["pending_count"] + (1 if snapshot["current_job_id"] else 0),
        "cooldown_seconds": snapshot["cooldown_seconds"],
        "cooldown_remaining_seconds": snapshot["cooldown_remaining_seconds"],
        "cooldown_until": snapshot["cooldown_until"],
        "max_pending": snapshot["max_pending"],
        "global_execution_gate": global_snapshot,
        "persistence": "memory",
    }


@app.delete("/api/analyze/queue/{job_id}")
def analyze_queue_remove(job_id: str) -> Dict[str, object]:
    if _analysis_queue.is_running(job_id):
        raise HTTPException(status_code=409, detail="Cannot remove a running analysis. Wait for completion.")
    removed = _analysis_queue.remove_pending(job_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Queued job not found.")
    return {"removed": True, "job_id": job_id}


@app.post("/api/analyze/queue/clear")
def analyze_queue_clear() -> Dict[str, object]:
    removed = _analysis_queue.clear_pending()
    return {"cleared": len(removed), "job_ids": removed}


@app.post("/api/batch")
def batch(req: BatchRequest) -> Dict[str, object]:
    metadata = {
        "job_type": "batch",
        "tickers": [t.upper() for t in req.tickers],
        "date": req.date,
        "mode": req.mode,
        "risk_profile": req.risk_profile,
    }
    job_id = _submit_job(
        _run_batch,
        metadata,
        req,
        _degraded_retry_fn=lambda: _run_batch(req, degraded=True),
    )
    return {"job_id": job_id, **metadata}


# =============================================================================
# BACKTESTING ENDPOINTS
# =============================================================================


class BacktestRequest(BaseModel):
    ticker: Optional[str] = None
    limit: int = Field(50, ge=1, le=500)
    lookahead_days: List[int] = Field(default=[7, 14, 30])
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    slippage_bps: float = Field(0.0, ge=0, le=100)
    transaction_cost_bps: float = Field(0.0, ge=0, le=100)
    db_path: str = "research.db"


class BacktestSingleRequest(BaseModel):
    analysis_id: int
    lookahead_days: List[int] = Field(default=[7, 14, 30])
    slippage_bps: float = Field(0.0, ge=0, le=100)
    transaction_cost_bps: float = Field(0.0, ge=0, le=100)
    db_path: str = "research.db"


def _run_backtest(req: BacktestRequest) -> Dict[str, object]:
    """Run backtest in background thread."""
    engine = BacktestEngine(db_path=req.db_path)
    result = engine.run(
        ticker=req.ticker,
        limit=req.limit,
        lookahead_days=req.lookahead_days,
        start_date=req.start_date,
        end_date=req.end_date,
        slippage_bps=req.slippage_bps,
        transaction_cost_bps=req.transaction_cost_bps,
    )
    return result


@app.post("/api/backtest")
def run_backtest(req: BacktestRequest) -> Dict[str, object]:
    """Run a backtest against stored analyses."""
    metadata = {
        "job_type": "backtest",
        "ticker": (req.ticker or "all").upper(),
        "limit": req.limit,
        "lookahead_days": req.lookahead_days,
    }
    job_id = _submit_job(_run_backtest, metadata, req)
    return {"job_id": job_id, **metadata}


@app.post("/api/backtest/single")
def run_backtest_single(req: BacktestSingleRequest) -> Dict[str, object]:
    """Run backtest for a single analysis by ID."""
    db = get_db(req.db_path)
    analysis = db.get_analysis(req.analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")

    engine = BacktestEngine(db_path=req.db_path)
    updated, ret_value, was_correct, skip_reason = engine._update_analysis_outcomes(
        analysis,
        req.lookahead_days,
        slippage_bps=req.slippage_bps,
        transaction_cost_bps=req.transaction_cost_bps,
    )

    if not updated:
        return {
            "analysis_id": req.analysis_id,
            "ticker": analysis.ticker,
            "updated": False,
            "skip_reason": skip_reason,
        }

    # Re-fetch to get updated data
    refreshed = db.get_analysis(req.analysis_id)
    return {
        "analysis_id": req.analysis_id,
        "ticker": analysis.ticker,
        "updated": True,
        "was_correct": was_correct,
        "return_value": ret_value,
        "price_at_analysis": refreshed.price_at_analysis if refreshed else None,
        "price_after_7d": refreshed.price_after_7d if refreshed else None,
        "price_after_14d": refreshed.price_after_14d if refreshed else None,
        "price_after_30d": refreshed.price_after_30d if refreshed else None,
        "actual_return_7d": refreshed.actual_return_7d if refreshed else None,
        "actual_return_14d": refreshed.actual_return_14d if refreshed else None,
        "actual_return_30d": refreshed.actual_return_30d if refreshed else None,
        "alpha_7d": refreshed.alpha_7d if refreshed else None,
        "alpha_14d": refreshed.alpha_14d if refreshed else None,
        "alpha_30d": refreshed.alpha_30d if refreshed else None,
    }


@app.get("/api/backtest/runs")
def list_backtest_runs(
    limit: int = 20,
    ticker: Optional[str] = None,
    run_start: Optional[str] = None,
    run_end: Optional[str] = None,
    db_path: str = "research.db",
) -> List[Dict[str, object]]:
    """List past backtest runs."""
    db = get_db(db_path)
    runs = db.get_backtest_runs(
        limit=limit,
        ticker=ticker,
        run_start_date=run_start,
        run_end_date=run_end,
    )
    return [
        {
            "id": run.id,
            "run_at": run.run_at,
            "ticker": run.ticker,
            "start_date": run.start_date,
            "end_date": run.end_date,
            "limit_count": run.limit_count,
            "lookahead_days": run.lookahead_days,
            "slippage_bps": run.slippage_bps,
            "transaction_cost_bps": run.transaction_cost_bps,
            "updated_count": run.updated_count,
            "skipped_count": run.skipped_count,
            "avg_return": run.avg_return,
            "win_rate": run.win_rate,
            "accuracy": run.accuracy,
            "strategy_sharpe": run.strategy_sharpe,
            "strategy_sortino": run.strategy_sortino,
            "avg_alpha_30d": run.avg_alpha_30d,
            "avg_signed_return_7d": run.avg_signed_return_7d,
            "return_vol_7d": run.return_vol_7d,
        }
        for run in runs
    ]


@app.get("/api/backtest/calibration")
def get_calibration(db_path: str = "research.db") -> Dict[str, object]:
    """Get confidence calibration data."""
    try:
        result = compute_calibration(db_path=db_path)
        return result
    except Exception as e:
        return {"bins": [], "meta": {"error": str(e)}}


@app.get("/api/backtest/stats")
def backtest_stats(db_path: str = "research.db") -> Dict[str, object]:
    """Get overall backtesting statistics (signed strategy + unsigned tape)."""
    db = get_db(db_path)
    analyses = db.get_backtested_analyses(limit=500)
    if not analyses:
        return empty_stats_payload()
    return stats_api_payload(summarize_analyses(analyses))


@app.get("/health")
def health() -> Dict[str, object]:
    """Liveness probe — always returns ok if the process is up."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/health/ready")
def health_ready(db_path: str = "research.db") -> Dict[str, object]:
    """Readiness probe — verifies database, cache, and critical env vars."""
    checks: Dict[str, Any] = {}

    # Database check
    try:
        db = get_db(db_path)
        with db._connect() as conn:
            conn.execute("SELECT COUNT(*) FROM analyses")
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    # Cache directory check
    cache_dir = DEFAULT_CONFIG.get("data_cache_dir", "tradingagents/dataflows/data_cache")
    checks["cache_dir"] = "ok" if os.path.isdir(cache_dir) else "missing"

    # Output directory check
    output_dir = "research_output"
    checks["output_dir"] = "ok" if os.path.isdir(output_dir) else "missing"

    # Critical env vars
    checks["OPENAI_API_KEY"] = "set" if os.getenv("OPENAI_API_KEY") else "missing"

    # Background services
    checks["alert_monitor"] = "running" if _alert_monitor.is_running else "stopped"
    checks["screening_scheduler"] = "running" if _screening_scheduler.is_running else "stopped"

    # Active jobs
    active_jobs = sum(1 for j in _jobs.values() if j.get("status") in ("queued", "running"))
    checks["active_jobs"] = active_jobs

    # yfinance rate-limit breaker status (ok when closed, "rate_limited:<Ns>" when open)
    try:
        from tradingagents.dataflows.yfinance_limiter import get_yfinance_limiter
        yf_status = get_yfinance_limiter().status()
        if yf_status.get("open"):
            checks["yfinance_limiter"] = (
                f"rate_limited:{int(yf_status.get('cooldown_remaining_seconds') or 0)}s"
            )
        else:
            checks["yfinance_limiter"] = "ok"
        checks["yfinance_limiter_total_trips"] = int(yf_status.get("total_trips") or 0)
    except Exception as exc:
        checks["yfinance_limiter"] = f"unknown:{exc}"

    # Latest built-in refresh quality — surface tier split + invalid ratio so
    # operators can tell at a glance whether the validator is healthy.
    try:
        db = get_db(db_path)
        with db._connect() as conn:
            row = conn.execute(
                """
                SELECT duration_seconds, context_json FROM runtime_metrics
                WHERE metric_key = 'builtin_refresh_invalid_ratio'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        if row:
            ctx = {}
            try:
                ctx = json.loads(row["context_json"] or "{}")
            except Exception:
                ctx = {}
            checks["builtin_refresh_invalid_ratio"] = round(float(row["duration_seconds"] or 0.0), 4)
            checks["builtin_refresh_tier1"] = int(ctx.get("tier1_validated") or 0)
            checks["builtin_refresh_tier2"] = int(ctx.get("tier2_validated") or 0)
            checks["builtin_refresh_tier3"] = int(ctx.get("tier3_validated") or 0)
            checks["builtin_refresh_format_rejected"] = int(ctx.get("format_rejected") or 0)
    except Exception as exc:
        checks["builtin_refresh_metrics"] = f"unknown:{exc}"

    # Ticker health rollup — how many symbols are currently eligible for eviction.
    try:
        db = get_db(db_path)
        stale = db.get_stale_ticker_candidates(min_failures=3, min_days_since_success=2)
        checks["ticker_health_stale_candidates"] = len(stale)
    except Exception as exc:
        checks["ticker_health_stale_candidates"] = f"unknown:{exc}"

    # Only include non-numeric, non-string-structured checks in the overall gate.
    overall = "healthy" if all(
        (isinstance(v, (int, float)))
        or v in ("ok", "set", "running")
        or (isinstance(v, str) and v.startswith("rate_limited:"))
        for v in checks.values()
    ) else "degraded"

    return {"status": overall, "checks": checks, "timestamp": datetime.now(timezone.utc).isoformat()}


# =============================================================================
# PAGE ROUTES (HTML Templates)
# =============================================================================


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Redirect to dashboard."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    """Dashboard page."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/analyze", response_class=HTMLResponse)
def analyze_page(request: Request):
    """Single ticker analysis page."""
    return templates.TemplateResponse("analyze.html", {"request": request})


@app.get("/batch", response_class=HTMLResponse)
def batch_page(request: Request):
    """Batch analysis page."""
    return templates.TemplateResponse("batch.html", {"request": request})


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    """History list page."""
    return templates.TemplateResponse("history.html", {"request": request})


@app.get("/history/{analysis_id}", response_class=HTMLResponse)
def report_page(request: Request, analysis_id: int):
    """Report viewer page."""
    return templates.TemplateResponse("report.html", {"request": request, "analysis_id": analysis_id})


@app.get("/backtest", response_class=HTMLResponse)
def backtest_page(request: Request):
    """Backtesting page."""
    return templates.TemplateResponse("backtest.html", {"request": request})


@app.get("/snapshots", response_class=HTMLResponse)
def snapshots_page(request: Request):
    """Snapshots library page."""
    return templates.TemplateResponse("snapshots.html", {"request": request})


@app.get("/qa", response_class=HTMLResponse)
def qa_page(request: Request):
    """QA / Health page."""
    return templates.TemplateResponse("qa.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    """Settings page."""
    return templates.TemplateResponse("settings.html", {"request": request})


# =============================================================================
# KPI / GUIDANCE / RISK SEARCH ENDPOINTS
# =============================================================================


@app.get("/api/kpis")
def search_kpis(
    ticker: Optional[str] = None,
    name: Optional[str] = None,
    limit: int = 50,
    db_path: str = "research.db",
) -> List[Dict[str, object]]:
    """Search indexed KPIs from earnings snapshots."""
    db = get_db(db_path)
    return db.search_kpis(ticker=ticker, kpi_name=name, limit=limit)


@app.get("/api/guidance")
def search_guidance(
    ticker: Optional[str] = None,
    metric: Optional[str] = None,
    limit: int = 50,
    db_path: str = "research.db",
) -> List[Dict[str, object]]:
    """Search indexed guidance from earnings snapshots."""
    db = get_db(db_path)
    return db.search_guidance(ticker=ticker, metric=metric, limit=limit)


@app.get("/api/risks")
def search_risks(
    ticker: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 50,
    db_path: str = "research.db",
) -> List[Dict[str, object]]:
    """Search indexed risk factors from SEC snapshots."""
    db = get_db(db_path)
    return db.search_risks(ticker=ticker, keyword=keyword, limit=limit)


@app.get("/api/kpi-deltas/{ticker}")
def get_kpi_deltas(
    ticker: str,
    analysis_id: Optional[int] = None,
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Get KPI deltas between current and previous analysis."""
    db = get_db(db_path)
    if analysis_id is None:
        analyses = db.get_analyses_by_ticker(ticker.upper(), limit=1)
        if not analyses:
            raise HTTPException(status_code=404, detail="No analyses found for ticker")
        analysis_id = analyses[0].id
    deltas = db.compute_kpi_deltas(ticker, analysis_id)
    return {"ticker": ticker, "analysis_id": analysis_id, "deltas": deltas}


@app.get("/api/guidance-shifts/{ticker}")
def get_guidance_shifts(
    ticker: str,
    analysis_id: Optional[int] = None,
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Get guidance shifts between current and previous analysis."""
    db = get_db(db_path)
    if analysis_id is None:
        analyses = db.get_analyses_by_ticker(ticker.upper(), limit=1)
        if not analyses:
            raise HTTPException(status_code=404, detail="No analyses found for ticker")
        analysis_id = analyses[0].id
    shifts = db.compute_guidance_shifts(ticker, analysis_id)
    return {"ticker": ticker, "analysis_id": analysis_id, "shifts": shifts}


# =============================================================================
# DASHBOARD STATS ENDPOINT
# =============================================================================


# =============================================================================
# DASHBOARD
# =============================================================================


def _index_regime_payload(macro: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize index regime fields for dashboard/API consumers."""
    return normalize_index_regime(macro)


def _live_index_regime() -> Dict[str, Any]:
    try:
        return _index_regime_payload(get_macro_snapshot())
    except Exception:
        return _index_regime_payload({})


@app.get("/api/dashboard")
def dashboard_stats(db_path: str = "research.db") -> Dict[str, object]:
    """Get dashboard summary statistics."""
    live_index_regime = _live_index_regime()
    db = get_db(db_path)
    recent = db.get_recent_analyses(limit=100)

    total_analyses = len(recent)
    if total_analyses == 0:
        return {
            "total_analyses": 0,
            "recent_count": 0,
            "avg_duration": 0,
            "avg_confidence": 0,
            "avg_quality": 0,
            "qa_warning_count": 0,
            "decision_counts": {"BUY": 0, "SELL": 0, "HOLD": 0},
            "risk_profile_counts": {"aggressive": 0, "growth": 0, "conservative": 0},
            "analysis_mode_counts": {"quick": 0, "standard": 0, "deep": 0},
            "profile_stats": {
                "aggressive": {"count": 0, "backtested": 0, "wins": 0, "accuracy": 0, "avg_return": 0},
                "growth": {"count": 0, "backtested": 0, "wins": 0, "accuracy": 0, "avg_return": 0},
                "conservative": {"count": 0, "backtested": 0, "wins": 0, "accuracy": 0, "avg_return": 0},
            },
            "sec_snapshot_count": 0,
            "transcript_snapshot_count": 0,
            "backtested_count": 0,
            "backtest_wins": 0,
            "backtest_losses": 0,
            "backtest_accuracy": 0,
            "avg_return": 0,
            "index_regime": live_index_regime,
            "regime_label": live_index_regime.get("index_regime_label", "Unknown"),
            "market_regime": live_index_regime.get("index_trend", "").upper()
            if live_index_regime.get("index_trend") not in (None, "", "unknown")
            else "",
        }

    durations = [a.duration_seconds for a in recent if a.duration_seconds]
    confidences = [a.confidence for a in recent if a.confidence]
    qualities = [a.data_quality_score for a in recent if a.data_quality_score]

    qa_warning_count = 0
    decision_counts: Dict[str, int] = {"BUY": 0, "SELL": 0, "HOLD": 0}
    risk_profile_counts: Dict[str, int] = {"aggressive": 0, "growth": 0, "conservative": 0}
    analysis_mode_counts: Dict[str, int] = {"quick": 0, "standard": 0, "deep": 0}
    
    # Risk profile counts on the recent-100 activity set
    profile_stats = {
        "aggressive": {"count": 0, "backtested": 0, "wins": 0, "accuracy": 0, "avg_return": 0},
        "growth": {"count": 0, "backtested": 0, "wins": 0, "accuracy": 0, "avg_return": 0},
        "conservative": {"count": 0, "backtested": 0, "wins": 0, "accuracy": 0, "avg_return": 0},
    }
    
    sec_count = 0
    transcript_count = 0

    for analysis in recent:
        decision = (analysis.decision or "").upper()
        if "BUY" in decision:
            decision_counts["BUY"] += 1
        elif "SELL" in decision:
            decision_counts["SELL"] += 1
        elif "HOLD" in decision:
            decision_counts["HOLD"] += 1

        # Track risk profile counts
        profile = getattr(analysis, "risk_profile", "") or ""
        if profile in risk_profile_counts:
            risk_profile_counts[profile] += 1
            profile_stats[profile]["count"] = risk_profile_counts[profile]
        
        # Track analysis mode counts
        mode = getattr(analysis, "analysis_mode", "") or ""
        if mode in analysis_mode_counts:
            analysis_mode_counts[mode] += 1

        if analysis.has_sec_snapshot:
            sec_count += 1
        if analysis.has_transcript_snapshot:
            transcript_count += 1

        raw = analysis.report_warnings or "[]"
        try:
            warnings = json.loads(raw)
            qa_warning_count += len(warnings)
        except json.JSONDecodeError:
            qa_warning_count += 1

    backtested = db.get_backtested_analyses(limit=500)
    book = stats_api_payload(summarize_analyses(backtested)) if backtested else empty_stats_payload()
    dir_n = book.get("directional_n_7d") or 0
    dir_hits = book.get("directional_hits_7d") or 0
    backtested_count = book.get("total_backtested") or 0
    backtest_wins = dir_hits
    backtest_losses = max(dir_n - dir_hits, 0)
    backtest_accuracy = book.get("directional_accuracy_7d") or 0
    avg_return = book.get("avg_signed_return_7d") if book.get("avg_signed_return_7d") is not None else 0

    for profile, row in (book.get("by_risk_profile") or {}).items():
        if profile not in profile_stats:
            continue
        profile_stats[profile]["backtested"] = row.get("n_signed_7d") or 0
        profile_stats[profile]["wins"] = row.get("wins") or 0
        profile_stats[profile]["accuracy"] = row.get("accuracy") or 0
        profile_stats[profile]["avg_return"] = row.get("avg_signed_return_7d") or 0

    return {
        "total_analyses": total_analyses,
        "recent_count": min(total_analyses, 10),
        "avg_duration": round(sum(durations) / len(durations), 1) if durations else 0,
        "avg_confidence": round(sum(confidences) / len(confidences), 1) if confidences else 0,
        "avg_quality": round(sum(qualities) / len(qualities), 1) if qualities else 0,
        "qa_warning_count": qa_warning_count,
        "decision_counts": decision_counts,
        "risk_profile_counts": risk_profile_counts,
        "analysis_mode_counts": analysis_mode_counts,
        "profile_stats": profile_stats,
        "sec_snapshot_count": sec_count,
        "transcript_snapshot_count": transcript_count,
        "backtested_count": backtested_count,
        "backtest_wins": backtest_wins,
        "backtest_losses": backtest_losses,
        "backtest_accuracy": backtest_accuracy,
        "avg_return": avg_return,
        "avg_tape_return_7d": book.get("avg_tape_return_7d") or 0,
        "n_signed_7d": book.get("n_signed_7d") or 0,
        "index_regime": live_index_regime,
        "regime_label": live_index_regime.get("index_regime_label", "Unknown"),
        "market_regime": live_index_regime.get("index_trend", "").upper()
        if live_index_regime.get("index_trend") not in (None, "", "unknown")
        else "",
    }


# =============================================================================
# EXPORTS ENDPOINT
# =============================================================================


@app.get("/api/exports")
def get_exports(
    export_type: str = "summary",
    ticker: Optional[str] = None,
    date: Optional[str] = None,
    output_dir: str = "research_output",
) -> Dict[str, object]:
    """
    Get export file contents.
    
    export_type: summary | metadata | provenance | watchlist
    """
    import glob

    output_dir = _safe_output_dir(output_dir)
    if not os.path.isdir(output_dir):
        raise HTTPException(status_code=404, detail="Output directory not found")

    # Build filename pattern
    if export_type == "summary":
        pattern = "batch_summary_*.csv"
    elif export_type == "metadata":
        pattern = "batch_summary_meta_*.csv"
    elif export_type == "watchlist":
        pattern = "batch_watchlist_brief_*.csv"
    elif export_type == "provenance":
        pattern = "provenance_*.csv"
    else:
        raise HTTPException(status_code=400, detail=f"Unknown export type: {export_type}")

    # Filter by date/ticker if provided
    files = glob.glob(os.path.join(output_dir, pattern))
    if date:
        files = [f for f in files if date in os.path.basename(f)]
    if ticker:
        files = [f for f in files if ticker.upper() in os.path.basename(f).upper()]

    if not files:
        raise HTTPException(status_code=404, detail="No matching export files found")

    # Return most recent file
    files.sort(key=os.path.getmtime, reverse=True)
    latest = files[0]

    with open(latest, "r", encoding="utf-8") as handle:
        content = handle.read()

    return {
        "filename": os.path.basename(latest),
        "export_type": export_type,
        "content": content,
    }


@app.get("/api/exports/list")
def list_exports(
    output_dir: str = "research_output",
) -> List[Dict[str, object]]:
    """List available export files."""
    import glob

    if not os.path.isdir(output_dir):
        return []

    patterns = [
        ("summary", "batch_summary_*.csv"),
        ("metadata", "batch_summary_meta_*.csv"),
        ("watchlist_md", "batch_watchlist_brief_*.md"),
        ("watchlist_json", "batch_watchlist_brief_*.json"),
        ("watchlist_csv", "batch_watchlist_brief_*.csv"),
    ]

    exports = []
    for export_type, pattern in patterns:
        for filepath in glob.glob(os.path.join(output_dir, pattern)):
            stat = os.stat(filepath)
            exports.append({
                "filename": os.path.basename(filepath),
                "export_type": export_type,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

    exports.sort(key=lambda x: x["modified_at"], reverse=True)
    return exports


# =============================================================================
# SETTINGS ENDPOINT
# =============================================================================

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "settings.json")

DEFAULT_SETTINGS = {
    "default_mode": "standard",
    "output_dir": "research_output",
    "db_path": "research.db",
    "default_delay": 30,
}


def _load_settings() -> Dict[str, object]:
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load settings from %s: %s", SETTINGS_PATH, e)
    return DEFAULT_SETTINGS.copy()


def _save_settings(settings: Dict[str, object]) -> None:
    with open(SETTINGS_PATH, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)


@app.get("/api/settings")
def get_settings() -> Dict[str, object]:
    """Get current settings."""
    settings = _load_settings()
    # Add API key status (masked)
    settings["api_keys"] = {
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "perplexity": bool(os.getenv("PERPLEXITY_API_KEY")),
        "alpha_vantage": bool(os.getenv("ALPHA_VANTAGE_API_KEY")),
        "finnhub": bool(os.getenv("FINNHUB_API_KEY")),
    }
    return settings


class SettingsUpdate(BaseModel):
    default_mode: Optional[str] = None
    default_risk_profile: Optional[str] = None
    output_dir: Optional[str] = None
    db_path: Optional[str] = None
    default_delay: Optional[int] = None


class NotesUpdate(BaseModel):
    notes: str


class TagsUpdate(BaseModel):
    tags: str


class QualityUpdate(BaseModel):
    quality_label: str = Field("", description="Optional qualitative label, e.g. gold/silver/needs-work")
    quality_score: Optional[float] = Field(None, ge=0, le=100, description="Optional 0-100 quality score")
    quality_notes: str = Field("", description="Reviewer notes about analysis/report quality")
    evaluated_by: str = Field("manual", description="Source of annotation (manual/qa/auto)")


class SavedViewCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    filters: Dict[str, object] = Field(default_factory=dict)


@app.put("/api/settings")
def update_settings(updates: SettingsUpdate) -> Dict[str, object]:
    """Update settings."""
    settings = _load_settings()
    update_dict = updates.dict(exclude_none=True)
    settings.update(update_dict)
    _save_settings(settings)
    return settings


# =============================================================================
# CACHE MANAGEMENT ENDPOINTS
# =============================================================================


@app.get("/api/cache")
def get_cache_info() -> Dict[str, object]:
    """Get cache statistics (memory + disk tiers)."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.dataflows.cache import get_cache

    cache_dir = DEFAULT_CONFIG.get("data_cache_dir", "tradingagents/dataflows/data_cache")

    disk_info: Dict[str, object] = {"exists": False, "file_count": 0, "size_mb": 0}
    if os.path.isdir(cache_dir):
        files = [f for f in os.listdir(cache_dir) if f.endswith(".json")]
        total_size = sum(
            os.path.getsize(os.path.join(cache_dir, f))
            for f in files
            if os.path.isfile(os.path.join(cache_dir, f))
        )
        disk_info = {
            "exists": True,
            "file_count": len(files),
            "size_mb": round(total_size / (1024 * 1024), 2),
            "path": cache_dir,
        }

    # Merge in-memory DataCache stats
    try:
        dc_stats = get_cache().get_stats()
    except Exception as e:
        logger.debug("Cache stats unavailable: %s", e)
        dc_stats = {}

    return {
        **disk_info,
        "memory_entries": dc_stats.get("memory_entries", 0),
        "hits": dc_stats.get("hits", 0),
        "misses": dc_stats.get("misses", 0),
        "hit_rate": dc_stats.get("hit_rate", "0.0%"),
        "sets": dc_stats.get("sets", 0),
        "evictions": dc_stats.get("evictions", 0),
    }


@app.delete("/api/cache")
def clear_cache() -> Dict[str, object]:
    """Clear all cached API responses."""
    from tradingagents.default_config import DEFAULT_CONFIG
    
    cache_dir = DEFAULT_CONFIG.get("data_cache_dir", "tradingagents/dataflows/data_cache")
    
    if not os.path.isdir(cache_dir):
        return {"cleared": 0, "size_freed_mb": 0}
    
    files = [f for f in os.listdir(cache_dir) if f.endswith(".json")]
    total_size = 0
    cleared = 0
    
    for f in files:
        path = os.path.join(cache_dir, f)
        if os.path.isfile(path):
            total_size += os.path.getsize(path)
            os.remove(path)
            cleared += 1
    
    return {
        "cleared": cleared,
        "size_freed_mb": round(total_size / (1024 * 1024), 2),
    }


# =============================================================================
# NOTES & TAGS ENDPOINTS
# =============================================================================


@app.put("/api/analyses/{analysis_id}/notes")
def update_analysis_notes(analysis_id: int, body: NotesUpdate, db_path: str = "research.db") -> Dict[str, object]:
    """Update notes for an analysis."""
    db = get_db(db_path)
    success = db.update_notes(analysis_id, body.notes)
    if not success:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {"analysis_id": analysis_id, "notes": body.notes}


@app.put("/api/analyses/{analysis_id}/tags")
def update_analysis_tags(analysis_id: int, body: TagsUpdate, db_path: str = "research.db") -> Dict[str, object]:
    """Update tags for an analysis (comma-separated)."""
    db = get_db(db_path)
    success = db.update_tags(analysis_id, body.tags)
    if not success:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {"analysis_id": analysis_id, "tags": body.tags}


@app.put("/api/analyses/{analysis_id}/quality")
def update_analysis_quality(analysis_id: int, body: QualityUpdate, db_path: str = "research.db") -> Dict[str, object]:
    """Upsert a quality annotation for an analysis run."""
    db = get_db(db_path)
    analysis = db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    db.upsert_analysis_quality(
        analysis_id=analysis_id,
        quality_label=body.quality_label,
        quality_score=body.quality_score,
        quality_notes=body.quality_notes,
        evaluated_by=body.evaluated_by,
    )
    saved = db.get_analysis_quality(analysis_id) or {}
    return {"analysis_id": analysis_id, **saved}


@app.get("/api/analyses/{analysis_id}/quality")
def get_analysis_quality(analysis_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Get the quality annotation for an analysis run."""
    db = get_db(db_path)
    analysis = db.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    data = db.get_analysis_quality(analysis_id)
    return data or {
        "analysis_id": analysis_id,
        "quality_label": "",
        "quality_score": None,
        "quality_notes": "",
        "evaluated_by": "",
    }


@app.get("/api/tags")
def list_all_tags(db_path: str = "research.db") -> List[str]:
    """Get all unique tags across analyses."""
    db = get_db(db_path)
    return db.get_all_tags()


@app.delete("/api/analyses/{analysis_id}")
def delete_analysis(
    analysis_id: int,
    output_dir: str = "research_output",
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Delete an analysis and its associated report files.
    
    This removes:
    - Database record and all related snapshot data
    - HTML report file
    - PDF report file (if exists)
    """
    db_path = _safe_db_path(db_path)
    _safe_output_dir(output_dir)
    db = get_db(db_path)
    analysis = db.delete_analysis(analysis_id)
    
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")
    
    # Delete associated files (mode-qualified and legacy names)
    files_deleted = []
    from tradingagents.reporting.report_paths import (
        legacy_report_basename,
        report_basename,
    )

    mode = getattr(analysis, "analysis_mode", None) or "standard"
    basenames = {
        report_basename(analysis.ticker, analysis.analysis_date, mode),
        legacy_report_basename(analysis.ticker, analysis.analysis_date),
    }
    for basename in basenames:
        for ext in ("html", "pdf"):
            filename = f"{basename}.{ext}"
            found = _find_report_file(output_dir, filename)
            if found and found not in files_deleted:
                os.remove(found)
                files_deleted.append(found)
    
    return {
        "deleted": True,
        "analysis_id": analysis_id,
        "ticker": analysis.ticker,
        "analysis_date": analysis.analysis_date,
        "files_deleted": files_deleted,
    }


@app.get("/api/analyses/by-tag/{tag}")
def get_analyses_by_tag(tag: str, limit: int = 50, db_path: str = "research.db") -> List[Dict[str, object]]:
    """Get analyses with a specific tag."""
    db = get_db(db_path)
    analyses = db.get_analyses_by_tag(tag, limit)
    return [
        {
            "id": a.id,
            "ticker": a.ticker,
            "analysis_date": a.analysis_date,
            "created_at": a.created_at,
            "decision": a.decision,
            "confidence": a.confidence,
            "notes": a.notes or "",
            "tags": a.tags or "",
        }
        for a in analyses
    ]


# =============================================================================
# SAVED VIEWS ENDPOINTS
# =============================================================================


@app.get("/api/views")
def list_saved_views(db_path: str = "research.db") -> List[Dict[str, object]]:
    """List all saved views."""
    db = get_db(db_path)
    views = db.list_saved_views()
    return [
        {
            "id": v.id,
            "name": v.name,
            "description": v.description,
            "filters": json.loads(v.filters) if v.filters else {},
            "created_at": v.created_at,
            "updated_at": v.updated_at,
        }
        for v in views
    ]


@app.post("/api/views")
def create_saved_view(body: SavedViewCreate, db_path: str = "research.db") -> Dict[str, object]:
    """Create a new saved view."""
    from tradingagents.reporting.database import SavedView
    
    db = get_db(db_path)
    view = SavedView(
        name=body.name,
        description=body.description,
        filters=json.dumps(body.filters),
    )
    view_id = db.save_view(view)
    return {"id": view_id, "name": body.name}


@app.get("/api/views/{view_id}")
def get_saved_view(view_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Get a saved view by ID."""
    db = get_db(db_path)
    view = db.get_saved_view(view_id)
    if not view:
        raise HTTPException(status_code=404, detail="View not found")
    return {
        "id": view.id,
        "name": view.name,
        "description": view.description,
        "filters": json.loads(view.filters) if view.filters else {},
        "created_at": view.created_at,
        "updated_at": view.updated_at,
    }


@app.put("/api/views/{view_id}")
def update_saved_view(view_id: int, body: SavedViewCreate, db_path: str = "research.db") -> Dict[str, object]:
    """Update a saved view."""
    from tradingagents.reporting.database import SavedView
    
    db = get_db(db_path)
    existing = db.get_saved_view(view_id)
    if not existing:
        raise HTTPException(status_code=404, detail="View not found")
    
    view = SavedView(
        id=view_id,
        name=body.name,
        description=body.description,
        filters=json.dumps(body.filters),
        created_at=existing.created_at,
    )
    db.save_view(view)
    return {"id": view_id, "name": body.name}


@app.delete("/api/views/{view_id}")
def delete_saved_view(view_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Delete a saved view."""
    db = get_db(db_path)
    success = db.delete_saved_view(view_id)
    if not success:
        raise HTTPException(status_code=404, detail="View not found")
    return {"deleted": True, "id": view_id}


# =============================================================================
# COMPARE PAGE ROUTE
# =============================================================================


@app.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request):
    """Report comparison page."""
    return templates.TemplateResponse("compare.html", {"request": request})


# =============================================================================
# SCREENER PAGE ROUTE + API ENDPOINTS (Tier 2 Phase 2)
# =============================================================================


@app.get("/screener", response_class=HTMLResponse)
def screener_page(request: Request):
    """Watchlist screener page."""
    return templates.TemplateResponse("screener.html", {"request": request})


@app.post("/api/screen")
def run_screening(body: ScreenRequest) -> Dict[str, object]:
    """Submit a screening job. Returns job_id for polling."""
    from tradingagents.screening import ScreeningEngine
    from tradingagents.default_config import DEFAULT_CONFIG

    db = get_db(body.db_path)
    date = body.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if bool(body.watchlist_id) == bool(body.tickers):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of watchlist_id or tickers (watchlist_id XOR tickers).",
        )

    # Resolve tickers from watchlist or direct list
    tickers = []
    watchlist_id = body.watchlist_id
    watchlist_name = "Custom"

    if body.watchlist_id:
        wl = db.get_watchlist(body.watchlist_id)
        if not wl:
            raise HTTPException(status_code=404, detail="Watchlist not found")
        tickers = [t.strip() for t in wl["tickers"].split(",") if t.strip()]
        watchlist_name = wl["name"]
    elif body.tickers:
        tickers = [t.strip().upper() for t in body.tickers if t.strip()]

    valid, rejected = _parse_watchlist_ticker_payload(tickers, reject_invalid=False)
    if rejected:
        logger.warning("Skipping invalid screening tickers: %s", rejected)
    tickers = valid
    if not tickers:
        raise HTTPException(status_code=400, detail="No valid tickers to screen")

    screen_mode = _normalize_screen_mode(body.mode)
    criteria_meta: Dict[str, Any] = {}
    if screen_mode == "reversal_buildup":
        criteria_meta["strategy"] = "reversal_buildup"
    elif screen_mode == "early_momentum":
        criteria_meta["strategy"] = "early_momentum"
    elif screen_mode == "base_coil":
        criteria_meta["strategy"] = "base_coil"
    elif screen_mode == "multi_sleeve":
        criteria_meta["strategy"] = "multi_sleeve"
    elif body.watchlist_id:
        criteria_meta["strategy"] = "standard"
        criteria_meta["source_mode"] = "single_watchlist"

    def _run_screening():
        from dataclasses import asdict as _asdict
        engine = ScreeningEngine(config=DEFAULT_CONFIG, db=db)
        results = engine.scan(
            tickers=tickers,
            date=date,
            preset=body.preset,
            weights=body.weights,
            watchlist_id=watchlist_id,
            criteria_meta=criteria_meta or None,
        )
        if screen_mode == "reversal_buildup":
            top_5 = [
                {
                    "ticker": r.ticker,
                    "score": _reversal_payload_score(r),
                    "direction": r.direction,
                    "phase": ((r.signals or {}).get("_reversal_buildup") or {}).get("phase"),
                }
                for r in results[:5]
            ]
        elif screen_mode == "early_momentum":
            from tradingagents.screening.early_momentum import lift_momentum_fields
            top_5 = []
            for r in results[:5]:
                row = lift_momentum_fields({"ticker": r.ticker, "signals": r.signals or {}})
                top_5.append({
                    "ticker": r.ticker,
                    "score": row.get("momentum_score"),
                    "bucket": row.get("momentum_bucket"),
                    "direction": r.direction,
                })
        elif screen_mode == "base_coil":
            from tradingagents.screening.base_coil import lift_base_fields
            top_5 = []
            for r in results[:5]:
                row = lift_base_fields({"ticker": r.ticker, "signals": r.signals or {}})
                top_5.append({
                    "ticker": r.ticker,
                    "score": row.get("base_score"),
                    "days": row.get("base_days"),
                    "direction": r.direction,
                })
        elif screen_mode == "multi_sleeve":
            from tradingagents.screening.multi_sleeve import lift_multi_sleeve_fields
            top_5 = []
            for r in results[:5]:
                row = lift_multi_sleeve_fields({"ticker": r.ticker, "signals": r.signals or {}})
                ms = row.get("multi_sleeve") or {}
                top_5.append({
                    "ticker": r.ticker,
                    "lead_sleeve": ms.get("lead_sleeve"),
                    "score": ms.get("lead_score"),
                    "phase": ms.get("lead_phase"),
                    "side": ms.get("lead_side"),
                    "bucket": ms.get("lead_bucket"),
                    "lead_percentile": ms.get("lead_percentile"),
                    "direction_conflict": ms.get("direction_conflict"),
                })
        else:
            top_5 = [
                {"ticker": r.ticker, "score": r.composite_score, "direction": r.direction}
                for r in results[:5]
            ]
        payload: Dict[str, Any] = {
            "ticker_count": len(tickers),
            "results_count": len(results),
            "top_5": top_5,
            "strategy": criteria_meta.get("strategy") or "standard",
        }
        # Ticker-only scans (no watchlist_id) are not persisted to the DB.
        # Embed the full serialized result list in the job payload so the UI
        # can render results directly without a history round-trip.
        if watchlist_id is None:
            rows = [_asdict(r) for r in results]
            if screen_mode == "early_momentum":
                from tradingagents.screening.early_momentum import lift_momentum_fields, rank_momentum_rows
                payload["results"] = rank_momentum_rows([lift_momentum_fields(dict(r)) for r in rows])
            elif screen_mode == "base_coil":
                from tradingagents.screening.base_coil import lift_base_fields, rank_base_rows
                payload["results"] = rank_base_rows([lift_base_fields(dict(r)) for r in rows])
            elif screen_mode == "multi_sleeve":
                from tradingagents.screening.multi_sleeve import lift_multi_sleeve_fields, rank_multi_sleeve_rows
                payload["results"] = rank_multi_sleeve_rows([lift_multi_sleeve_fields(dict(r)) for r in rows])
            else:
                payload["results"] = _enrich_screening_rows(
                    rows,
                    db=db,
                    preset=body.preset,
                )
        return payload

    metadata = {
        "job_type": "screening",
        "watchlist": watchlist_name,
        "ticker_count": len(tickers),
        "preset": body.preset,
        "mode": screen_mode,
        "strategy": criteria_meta.get("strategy") or "standard",
    }
    job_id = _submit_job(_run_screening, metadata)
    return {"job_id": job_id, "ticker_count": len(tickers)}


def _normalize_movers_horizons(raw_horizons: Optional[List[str]]) -> List[str]:
    if not raw_horizons:
        return ["short", "medium"]
    allowed = {"short", "medium"}
    cleaned = []
    for item in raw_horizons:
        key = str(item or "").strip().lower()
        if key in allowed and key not in cleaned:
            cleaned.append(key)
    return cleaned


def _validate_movers_sources(raw_sources: Optional[List[str]]) -> List[str]:
    if not raw_sources:
        return []
    allowed = {"day_gainers", "day_losers", "most_actives", "small_mid_caps"}
    cleaned = []
    for item in raw_sources:
        key = str(item or "").strip()
        if key in allowed and key not in cleaned:
            cleaned.append(key)
    return cleaned


def _parse_run_criteria(criteria_raw: Any) -> Dict[str, Any]:
    if isinstance(criteria_raw, dict):
        return criteria_raw
    if isinstance(criteria_raw, str) and criteria_raw:
        try:
            return json.loads(criteria_raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


_SCAN_ALL_EXCLUDED_STRATEGIES = frozenset({
    "reversal_buildup",
    "reversal_all_union",
    "early_momentum",
    "early_momentum_union",
    "base_coil",
    "base_coil_union",
    "movers",
    "long_horizon",
    "multi_sleeve",
})
_REVERSAL_BATCH_STRATEGIES = frozenset({"reversal_buildup", "reversal_all_union"})
_MOMENTUM_BATCH_STRATEGIES = frozenset({"early_momentum", "early_momentum_union"})
_BASE_BATCH_STRATEGIES = frozenset({"base_coil", "base_coil_union"})
_MULTI_SLEEVE_STRATEGIES = frozenset({"multi_sleeve"})
_WATCHLIST_SCREEN_MODES = frozenset({
    "standard", "reversal_buildup", "early_momentum", "base_coil", "multi_sleeve",
})
_SCAN_ALL_SCREEN_MODES = frozenset({"standard", "reversal_buildup", "early_momentum", "base_coil"})


def _run_strategy(run: Optional[Dict[str, Any]]) -> str:
    """Resolve strategy from screening_runs.strategy or criteria JSON."""
    run = run or {}
    col = str(run.get("strategy") or "").strip()
    if col:
        return col
    return str(_parse_run_criteria(run.get("criteria")).get("strategy") or "").strip()


def _is_scan_all_provenance_run(run: Optional[Dict[str, Any]]) -> bool:
    """True when a run was produced by Opportunity Scan All, not Run Screening."""
    run = run or {}
    strategy = _run_strategy(run)
    if strategy in _SCAN_ALL_EXCLUDED_STRATEGIES - {"movers", "long_horizon"}:
        return False
    if strategy in {"screen_all", "screen_all_union"}:
        return True
    criteria = _parse_run_criteria(run.get("criteria"))
    if (
        criteria.get("early_momentum_all_job_id")
        or criteria.get("reversal_all_job_id")
        or criteria.get("base_coil_all_job_id")
    ):
        return False
    if criteria.get("screen_all_job_id"):
        return True
    if criteria.get("source_mode") in {"per_watchlist", "union_buckets"}:
        if strategy in {"screen_all", "screen_all_union"}:
            return True
    return False


def _board_now() -> datetime:
    """Clock for Cross-Watchlist recency. Tests may patch this."""
    return datetime.now()


def _parse_run_datetime(run: Optional[Dict[str, Any]]) -> Optional[datetime]:
    raw = (run or {}).get("run_at") or ""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _is_lens_scan_all_run(run: Optional[Dict[str, Any]], lens: str) -> bool:
    """True when this run is a Scan All batch for the requested lens."""
    if lens == "opportunity":
        return _is_scan_all_provenance_run(run)
    strategy = _run_strategy(run)
    criteria = _parse_run_criteria((run or {}).get("criteria"))
    source = str(criteria.get("source_mode") or "")
    if lens == "reversal":
        if strategy not in _REVERSAL_BATCH_STRATEGIES:
            return False
        return bool(criteria.get("reversal_all_job_id") or source in {"per_watchlist", "union_buckets"})
    if lens == "momentum":
        if strategy not in _MOMENTUM_BATCH_STRATEGIES:
            return False
        return bool(criteria.get("early_momentum_all_job_id") or source in {"per_watchlist", "union_buckets"})
    if lens == "base":
        if strategy not in _BASE_BATCH_STRATEGIES:
            return False
        return bool(criteria.get("base_coil_all_job_id") or source in {"per_watchlist", "union_buckets"})
    return False


def _run_matches_lens(run: Optional[Dict[str, Any]], lens: str) -> bool:
    strategy = _run_strategy(run)
    if lens == "reversal":
        return strategy in _REVERSAL_BATCH_STRATEGIES
    if lens == "momentum":
        return strategy in _MOMENTUM_BATCH_STRATEGIES
    if lens == "base":
        return strategy in _BASE_BATCH_STRATEGIES
    return strategy not in _SCAN_ALL_EXCLUDED_STRATEGIES


def _scan_all_job_id_for_lens(run: Optional[Dict[str, Any]], lens: str) -> Optional[str]:
    criteria = _parse_run_criteria((run or {}).get("criteria"))
    if lens == "reversal":
        job = str(criteria.get("reversal_all_job_id") or "").strip()
    elif lens == "momentum":
        job = str(criteria.get("early_momentum_all_job_id") or "").strip()
    elif lens == "base":
        job = str(criteria.get("base_coil_all_job_id") or "").strip()
    else:
        job = str(criteria.get("screen_all_job_id") or "").strip()
    return job or None


def _candidate_screening_runs(db, min_results: int = 1) -> List[Dict[str, Any]]:
    getter = getattr(db, "get_latest_screening_runs_per_watchlist", None)
    if callable(getter):
        rows = getter(min_results=min_results) or []
        return list(rows)
    runs = db.get_screening_runs(limit=2000)
    out: List[Dict[str, Any]] = []
    for run in runs:
        if not run.get("watchlist_id"):
            continue
        count = run.get("results_count")
        if count is not None and int(count or 0) < int(min_results):
            continue
        out.append(run)
    return out


def _board_source_for_selected(
    selected: Dict[str, Any],
    matching_for_watchlist: List[Dict[str, Any]],
    lens: str = "opportunity",
) -> str:
    if _is_lens_scan_all_run(selected, lens):
        return "scan_all"
    sel_dt = _parse_run_datetime(selected)
    sel_id = int(selected.get("id") or 0)
    for other in matching_for_watchlist:
        if int(other.get("id") or 0) == sel_id:
            continue
        if not _is_lens_scan_all_run(other, lens):
            continue
        other_dt = _parse_run_datetime(other)
        other_id = int(other.get("id") or 0)
        older = False
        if sel_dt is None or other_dt is None:
            older = True
        elif other_dt < sel_dt or (other_dt == sel_dt and other_id < sel_id):
            older = True
        if older:
            return "overlay"
    return "single"


def _older_scan_all_for_overlay(
    selected: Dict[str, Any],
    matching_for_watchlist: List[Dict[str, Any]],
    lens: str = "opportunity",
) -> Optional[Dict[str, Any]]:
    sel_dt = _parse_run_datetime(selected)
    sel_id = int(selected.get("id") or 0)
    older: List[Dict[str, Any]] = []
    for other in matching_for_watchlist:
        if int(other.get("id") or 0) == sel_id:
            continue
        if not _is_lens_scan_all_run(other, lens):
            continue
        other_dt = _parse_run_datetime(other)
        if sel_dt is not None and other_dt is not None:
            if other_dt > sel_dt or (other_dt == sel_dt and int(other.get("id") or 0) > sel_id):
                continue
        older.append(other)
    if not older:
        return None
    return max(
        older,
        key=lambda r: (_parse_run_datetime(r) or datetime.min, int(r.get("id") or 0)),
    )


def _latest_runs_for_lens(
    db,
    lens: str,
    *,
    min_results: int = 1,
) -> Dict[str, Any]:
    """Pick the latest matching-lens run per watchlist for Cross-Watchlist.

    Lenses are isolated: a newer reversal run does not hide an older opportunity
    run on the Opportunity board (and vice versa).
    """
    if lens not in {"reversal", "momentum", "base", "opportunity"}:
        lens = "opportunity"
    scan_all_cfg = DEFAULT_CONFIG.get("screening", {}).get("scan_all", {}) or {}
    include_singles = bool(scan_all_cfg.get("overlay_fresher_single_runs", True))
    max_age_days = max(0, int(scan_all_cfg.get("board_max_age_days", 30) or 0))
    batch_gap_minutes = max(15, int(scan_all_cfg.get("batch_gap_minutes", 45)))
    now = _board_now()

    candidates = _candidate_screening_runs(db, min_results=min_results)
    matching: List[Dict[str, Any]] = []
    for run in candidates:
        if not run.get("watchlist_id"):
            continue
        count = run.get("results_count")
        if count is not None and int(count or 0) < int(min_results):
            continue
        if not _run_matches_lens(run, lens):
            continue
        if not include_singles and not _is_lens_scan_all_run(run, lens):
            continue
        matching.append(run)

    empty_detail = {
        "reversal": "No reversal Scan All batch yet. Run Scan All with Scan mode = Reversal buildup (single-list runs stay in Screening Results).",
        "momentum": "No momentum Scan All batch yet. Run Scan All with Scan mode = Early momentum (single-list runs stay in Screening Results).",
        "base": "No base Scan All batch yet. Base lists names still inside the range (single-list runs stay in Screening History).",
        "opportunity": "No opportunity Scan All batch yet. Run Scan All (single-list runs stay in Screening Results).",
    }.get(lens, "No runs found.")
    if not matching:
        raise HTTPException(404, empty_detail)

    by_watchlist: Dict[Any, List[Dict[str, Any]]] = {}
    for run in matching:
        by_watchlist.setdefault(run.get("watchlist_id"), []).append(run)

    def _run_sort_key(run: Dict[str, Any]) -> tuple:
        dt = _parse_run_datetime(run) or datetime.min
        return (dt, int(run.get("id") or 0))

    selected_runs: List[Dict[str, Any]] = []
    for _wl_id, rows in by_watchlist.items():
        rows_sorted = sorted(rows, key=_run_sort_key, reverse=True)
        selected_runs.append(rows_sorted[0])

    scan_all_matching = [r for r in matching if _is_lens_scan_all_run(r, lens)]
    latest_job_id = None
    if scan_all_matching:
        newest_scan_all = max(scan_all_matching, key=_run_sort_key)
        latest_job_id = _scan_all_job_id_for_lens(newest_scan_all, lens)

    kept: List[Dict[str, Any]] = []
    for run in selected_runs:
        dt = _parse_run_datetime(run)
        in_age = dt is not None and (now - dt).days <= max_age_days
        job = _scan_all_job_id_for_lens(run, lens)
        on_latest_job = bool(latest_job_id) and job == latest_job_id
        if in_age or on_latest_job:
            kept.append(run)

    if not kept:
        raise HTTPException(404, empty_detail)

    overlays: List[Dict[str, Any]] = []
    annotated: List[Dict[str, Any]] = []
    for run in kept:
        wl_rows = by_watchlist.get(run.get("watchlist_id"), [])
        source = _board_source_for_selected(run, wl_rows, lens)
        # Orphan single-list runs belong in Screening Results only — not on the
        # Cross-Watchlist board unless they overlay a prior Scan All row.
        if source == "single":
            continue
        row = dict(run)
        row["_board_source"] = source
        if source == "overlay":
            prior = _older_scan_all_for_overlay(run, wl_rows, lens)
            overlays.append({
                "watchlist_id": run.get("watchlist_id"),
                "watchlist_name": run.get("watchlist_name") or (prior or {}).get("watchlist_name"),
                "batch_run_id": (prior or {}).get("id"),
                "batch_run_at": (prior or {}).get("run_at"),
                "overlay_run_id": run.get("id"),
                "overlay_run_at": run.get("run_at"),
            })
        annotated.append(row)

    if not annotated:
        raise HTTPException(404, empty_detail)

    source_mix = {"scan_all": 0, "single": 0, "overlay": 0}
    for run in annotated:
        src = str(run.get("_board_source") or "")
        if src in source_mix:
            source_mix[src] = int(source_mix.get(src, 0) or 0) + 1

    scan_all_jobs = {
        _scan_all_job_id_for_lens(r, lens)
        for r in annotated
        if r.get("_board_source") == "scan_all"
    }
    non_empty_jobs = {j for j in scan_all_jobs if j}
    all_scan_all = all(r.get("_board_source") == "scan_all" for r in annotated)
    mixed = (not all_scan_all) or len(non_empty_jobs) > 1 or (
        bool(non_empty_jobs) and any(not _scan_all_job_id_for_lens(r, lens) for r in annotated)
    )

    run_times = [dt for dt in (_parse_run_datetime(r) for r in annotated) if dt is not None]
    batch_window = {
        "from": min(run_times).isoformat() if run_times else None,
        "to": max(run_times).isoformat() if run_times else None,
        "gap_minutes": batch_gap_minutes,
    }
    batch_criteria = _parse_run_criteria(annotated[0].get("criteria")) if annotated else {}
    if mixed:
        source_mode = "mixed"
        universe_requested = None
        universe_scanned = None
        universe_truncated = None
        batch_criteria = {}
    else:
        source_mode = batch_criteria.get("source_mode")
        universe_requested = batch_criteria.get("universe_requested")
        universe_scanned = batch_criteria.get("universe_scanned")
        universe_truncated = bool(batch_criteria.get("universe_truncated", False))

    return {
        "runs": annotated,
        "overlays": overlays,
        "source_mix": source_mix,
        "source_mode": source_mode,
        "universe_requested": universe_requested,
        "universe_scanned": universe_scanned,
        "universe_truncated": universe_truncated,
        "batch_window": batch_window,
        "batch_criteria": batch_criteria,
    }


def _is_single_watchlist_overlay_eligible(run: Optional[Dict[str, Any]]) -> bool:
    """Runs that can replace a stale Scan All row in Cross-Watchlist."""
    run = run or {}
    if not run.get("watchlist_id"):
        return False
    if int(run.get("results_count") or 0) <= 0:
        return False
    if _run_strategy(run) in _SCAN_ALL_EXCLUDED_STRATEGIES:
        return False
    if _is_scan_all_provenance_run(run):
        return False
    return True


def _apply_fresher_single_watchlist_overlays(
    batch_runs: List[Dict[str, Any]],
    db: ResearchDatabase,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Swap batch runs for newer per-watchlist Run Screening when available."""
    scan_all_cfg = DEFAULT_CONFIG.get("screening", {}).get("scan_all", {}) or {}
    if not scan_all_cfg.get("overlay_fresher_single_runs", True):
        return batch_runs, []

    overlays: List[Dict[str, Any]] = []
    updated: List[Dict[str, Any]] = []
    for run in batch_runs:
        wl_id = run.get("watchlist_id")
        batch_at = str(run.get("run_at") or "")
        if not wl_id or not batch_at:
            updated.append(run)
            continue
        fresher = db.get_latest_watchlist_run_after(
            int(wl_id),
            batch_at,
            min_results=1,
        )
        if (
            fresher
            and fresher.get("id") != run.get("id")
            and _is_single_watchlist_overlay_eligible(fresher)
        ):
            overlays.append({
                "watchlist_id": wl_id,
                "watchlist_name": run.get("watchlist_name") or fresher.get("watchlist_name"),
                "batch_run_id": run.get("id"),
                "batch_run_at": batch_at,
                "overlay_run_id": fresher.get("id"),
                "overlay_run_at": fresher.get("run_at"),
            })
            updated.append(fresher)
        else:
            updated.append(run)
    return updated, overlays


def _normalize_screen_mode(raw: Optional[str], *, for_scan_all: bool = False) -> str:
    mode = str(raw or "standard").strip().lower()
    allowed = _SCAN_ALL_SCREEN_MODES if for_scan_all else _WATCHLIST_SCREEN_MODES
    if mode not in allowed:
        if for_scan_all and mode == "multi_sleeve":
            raise HTTPException(
                status_code=400,
                detail="multi_sleeve is watchlist Run Scan only; Scan All runs one lens at a time",
            )
        raise HTTPException(
            status_code=400,
            detail=(
                "mode must be 'standard', 'reversal_buildup', 'early_momentum', or 'base_coil'"
                + ("" if for_scan_all else ", or 'multi_sleeve'")
            ),
        )
    return mode


def _screen_all_engine_criteria(screen_mode: str, source_mode: str, job_id: str) -> Dict[str, Any]:
    """Criteria passed into engine.scan for a Scan All job.

    Reversal batches always use strategy=reversal_buildup so the engine
    scores/ranks via the reversal payload and forces enhanced off.
    """
    meta: Dict[str, Any] = {
        "source_mode": source_mode,
    }
    if screen_mode == "reversal_buildup":
        meta["strategy"] = "reversal_buildup"
        meta["reversal_all_job_id"] = job_id
    elif screen_mode == "early_momentum":
        meta["strategy"] = "early_momentum_union" if source_mode == "union_buckets" else "early_momentum"
        meta["early_momentum_all_job_id"] = job_id
        if source_mode == "union_buckets":
            meta["run_enrich"] = False
    elif screen_mode == "base_coil":
        meta["strategy"] = "base_coil_union" if source_mode == "union_buckets" else "base_coil"
        meta["base_coil_all_job_id"] = job_id
    else:
        meta["screen_all_job_id"] = job_id
        meta["strategy"] = "screen_all_union" if source_mode == "union_buckets" else "screen_all"
    return meta


def _screen_all_persist_strategy(screen_mode: str, source_mode: str) -> str:
    """Strategy stamped on persisted per-watchlist runs after a Scan All job."""
    if screen_mode == "reversal_buildup":
        return "reversal_all_union" if source_mode == "union_buckets" else "reversal_buildup"
    if screen_mode == "early_momentum":
        return "early_momentum_union" if source_mode == "union_buckets" else "early_momentum"
    if screen_mode == "base_coil":
        return "base_coil_union" if source_mode == "union_buckets" else "base_coil"
    return "screen_all_union" if source_mode == "union_buckets" else "screen_all"


def _job_cancel_requested(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id) or {}
        return bool(job.get("cancel_requested"))


def _rank_rows_by_momentum(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from tradingagents.screening.early_momentum import lift_momentum_fields, momentum_sort_key

    ranked = [lift_momentum_fields(dict(row)) for row in rows]
    ranked.sort(key=lambda r: momentum_sort_key(r.get("early_momentum"), str(r.get("ticker") or "")))
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
    return ranked


def _rank_rows_by_base(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from tradingagents.screening.base_coil import base_sort_key, lift_base_fields

    ranked = [lift_base_fields(dict(row)) for row in rows]
    ranked = [row for row in ranked if (row.get("base_coil") or {}).get("on_board") is not False]
    ranked.sort(key=lambda r: base_sort_key(r.get("base_coil"), str(r.get("ticker") or "")))
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
    return ranked


def _rank_rows_by_reversal(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from tradingagents.screening.reversal_buildup import lift_reversal_fields, reversal_sort_key

    ranked = [lift_reversal_fields(dict(row)) for row in rows]
    ranked.sort(key=lambda r: reversal_sort_key(r.get("reversal_buildup"), str(r.get("ticker") or "")))
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
    return ranked


def _lift_reversal_buildup(row: Dict[str, Any]) -> Dict[str, Any]:
    from tradingagents.screening.reversal_buildup import lift_reversal_fields
    return lift_reversal_fields(row)


def _reversal_payload_score(result: Any) -> float:
    signals = getattr(result, "signals", None) or {}
    payload = signals.get("_reversal_buildup") if isinstance(signals, dict) else {}
    if not isinstance(payload, dict):
        return 0.0
    try:
        return float(payload.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _locked_movers_row(raw_row: Any) -> Dict[str, Any]:
    row = raw_row if isinstance(raw_row, dict) else {}
    reason_codes = row.get("reason_codes")
    source_tags = row.get("source_tags")
    if not isinstance(reason_codes, list):
        reason_codes = []
    if not isinstance(source_tags, list):
        source_tags = []
    try:
        composite_score = float(row.get("composite_score", 0.0))
    except (TypeError, ValueError):
        composite_score = 0.0
    try:
        price_change_pct = float(row.get("price_change_pct", 0.0))
    except (TypeError, ValueError):
        price_change_pct = 0.0
    return {
        "ticker": str(row.get("ticker") or "").upper(),
        "composite_score": composite_score,
        "catalyst_type": str(row.get("catalyst_type") or "unclassified"),
        "reason_codes": [str(x) for x in reason_codes if str(x).strip()],
        "primary_bucket": str(row.get("primary_bucket") or "unknown"),
        "source_tags": [str(x) for x in source_tags if str(x).strip()],
        "price_change_pct": price_change_pct,
    }


def _locked_movers_runs_payload(raw_runs: Dict[str, Any], requested_horizons: List[str]) -> Dict[str, Any]:
    requested = set(requested_horizons or [])
    payload: Dict[str, Any] = {}
    for horizon in ("short", "medium"):
        if horizon in requested and isinstance(raw_runs.get(horizon), dict):
            run_data = raw_runs.get(horizon) or {}
            rows = run_data.get("top", [])
            if not isinstance(rows, list):
                rows = []
            payload[horizon] = {
                "run_id": run_data.get("run_id"),
                "preset": run_data.get("preset"),
                "top": [_locked_movers_row(r) for r in rows],
            }
        else:
            payload[horizon] = {"run_id": None, "preset": None, "top": []}
    return payload


def _require_movers_strategy_run(db: ResearchDatabase, run_id: int) -> Dict[str, Any]:
    run = db.get_screening_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Movers run not found")
    criteria = _parse_run_criteria(run.get("criteria"))
    if criteria.get("strategy") != "movers":
        raise HTTPException(status_code=404, detail="Run is not a movers strategy run")
    run["criteria"] = criteria
    return run


def _prepare_movers_board(
    db: ResearchDatabase,
    results: List[Dict[str, Any]],
    details_map: Dict[str, Dict[str, Any]],
    criteria: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Attach tape context, drop ineligible names, and split board vs remainder."""
    from tradingagents.screening.movers import MoversIntelligenceService

    merged = _attach_movers_details(results, details_map)
    horizon = str(criteria.get("horizon") or "")
    asof = str(criteria.get("asof_date") or "")
    service = MoversIntelligenceService(db=db)
    source_map: Dict[str, Dict[str, Any]] = {}
    if asof and hasattr(db, "get_movers_snapshots"):
        try:
            source_map = MoversIntelligenceService.source_map_from_snapshot_rows(
                db.get_movers_snapshots(asof)
            )
        except Exception:
            source_map = {}
    for row in merged:
        if horizon:
            row.setdefault("horizon", horizon)
        ctx = source_map.get(str(row.get("ticker") or "").upper(), {})
        if ctx.get("market_cap"):
            row["market_cap"] = ctx.get("market_cap")
        if not row.get("price_change_pct") and ctx.get("price_change_pct") is not None:
            row["price_change_pct"] = ctx.get("price_change_pct")
    merged = [r for r in merged if service.is_board_eligible(r.get("ticker"), r)]
    try:
        top_n = int(criteria.get("top_n") or 25)
    except (TypeError, ValueError):
        top_n = 25
    board = MoversIntelligenceService.select_board(
        merged, top_n, include_losers=bool(criteria.get("include_losers", True))
    )
    board_tickers = {str(r.get("ticker") or "").upper() for r in board}
    rest = [r for r in merged if str(r.get("ticker") or "").upper() not in board_tickers]
    rest.sort(key=MoversIntelligenceService.rank_key)
    return board, rest


def _decorate_movers_expand_fields(
    rows: List[Dict[str, Any]],
    run: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Lift the same expand-panel fields Scan All / BMY rows expose."""
    watchlist_id = (run or {}).get("watchlist_id")
    criteria = (run or {}).get("criteria") or {}
    if not isinstance(criteria, dict):
        criteria = {}
    preset = criteria.get("preset")
    decorated: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        _lift_screening_contract_fields(item)
        signals = item.get("signals") if isinstance(item.get("signals"), dict) else {}
        meta = signals.get("_screening_meta") if isinstance(signals.get("_screening_meta"), dict) else {}
        for field in ("signal_coverage_pct", "tier_reached", "valuation_gap_detail", "weekly_detail"):
            if item.get(field) is None and meta.get(field) is not None:
                item[field] = meta[field]
        if item.get("weekly_detail") is None and isinstance(signals.get("weekly_detail"), dict):
            item["weekly_detail"] = signals.get("weekly_detail")
        if watchlist_id is not None:
            item.setdefault("watchlist_id", watchlist_id)
        if preset:
            item.setdefault("preset", preset)
        decorated.append(item)
    return decorated


def _attach_movers_details(results: List[Dict[str, Any]], details_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = []
    for row in results:
        ticker = str(row.get("ticker", "")).upper()
        details = details_map.get(ticker, {})
        if isinstance(row.get("signals"), str):
            try:
                row["signals"] = json.loads(row["signals"])
            except (json.JSONDecodeError, TypeError):
                row["signals"] = {}
        if isinstance(row.get("signal_deltas"), str):
            try:
                row["signal_deltas"] = json.loads(row["signal_deltas"])
            except (json.JSONDecodeError, TypeError):
                row["signal_deltas"] = {}
        row["catalyst_type"] = details.get("catalyst_type", "unclassified")
        row["reason_codes"] = details.get("reason_codes", [])
        row["primary_bucket"] = details.get("primary_bucket", "unknown")
        row["source_tags"] = details.get("source_tags", [])
        breakdown = details.get("score_breakdown_json") or {}
        if not isinstance(breakdown, dict):
            breakdown = {}
        raw_change = details.get("price_change_pct")
        if raw_change in (None, "", 0, 0.0):
            raw_change = breakdown.get("price_change_pct")
        try:
            row["price_change_pct"] = float(raw_change or 0.0)
        except (TypeError, ValueError):
            row["price_change_pct"] = 0.0
        if breakdown.get("horizon") and not row.get("horizon"):
            row["horizon"] = breakdown.get("horizon")
        merged.append(row)
    return merged


def _require_long_horizon_strategy_run(db: ResearchDatabase, run_id: int) -> Dict[str, Any]:
    run = db.get_screening_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Long-horizon run not found")
    criteria = _parse_run_criteria(run.get("criteria"))
    if criteria.get("strategy") != "long_horizon":
        raise HTTPException(status_code=404, detail="Run is not a long_horizon strategy run")
    run["criteria"] = criteria
    return run


def _latest_movers_run_id(db: ResearchDatabase, horizon: Optional[str] = None) -> Optional[int]:
    runs = db.get_movers_runs(limit=200, offset=0) if hasattr(db, "get_movers_runs") else []
    target = (horizon or "").strip().lower()
    for run in runs:
        criteria = _parse_run_criteria(run.get("criteria"))
        if target and criteria.get("horizon") != target:
            continue
        return int(run["id"])
    return None


@app.post("/api/movers/scan")
def run_movers_scan(body: MoversScanRequest, request: Request) -> object:
    """Run movers ingestion + dual horizon scans with locked response schema."""
    horizons = _normalize_movers_horizons(body.horizons)
    if not horizons:
        return JSONResponse(
            status_code=422,
            content=_error_envelope(
                code="invalid_horizons",
                message="At least one horizon is required (short|medium).",
                retryable=False,
            ),
        )

    source_lists = _validate_movers_sources(body.source_lists)
    if body.source_lists and not source_lists:
        return JSONResponse(
            status_code=422,
            content=_error_envelope(
                code="invalid_sources",
                message="source_lists must use known movers sources only.",
                retryable=False,
                details={"allowed": ["day_gainers", "day_losers", "most_actives", "small_mid_caps"]},
            ),
        )

    idem_key = _extract_movers_idempotency_key(request, body.idempotency_key)
    cached = _load_idempotent_movers_response(idem_key)
    if cached is not None:
        return cached

    try:
        from tradingagents.screening.movers import MoversIntelligenceService

        db = get_db(body.db_path)
        service = MoversIntelligenceService(db=db, config=DEFAULT_CONFIG)
        result = service.run_scan(
            include_losers=body.include_losers,
            top_n=body.top_n,
            source_lists=source_lists or None,
            allow_preclose_skip=bool(body.respect_freshness_guard),
        )
        if result.get("skipped"):
            _movers_last_status["last_error"] = result.get("message") or result.get("skip_reason")
            return JSONResponse(
                status_code=409,
                content=_error_envelope(
                    code="freshness_guard",
                    message=result.get("message", "Movers scan skipped."),
                    retryable=True,
                    details={"skip_reason": result.get("skip_reason"), "as_of_et": result.get("as_of_et")},
                ),
            )

        runs = result.get("runs", {})
        missing_horizons = [h for h in horizons if h not in runs]
        if missing_horizons:
            return JSONResponse(
                status_code=422,
                content=_error_envelope(
                    code="horizon_filter_empty",
                    message="Requested horizons are not available for this scan.",
                    retryable=False,
                    details={"missing_horizons": missing_horizons},
                ),
            )

        response_payload = {
            "snapshot_id": result.get("snapshot_id"),
            "watchlist_id": result.get("watchlist_id"),
            "as_of_et": result.get("as_of_et"),
            "runs": _locked_movers_runs_payload(runs, horizons),
            "degraded": bool(result.get("degraded")),
            "source_errors": result.get("source_errors", []),
        }
        _movers_last_status["last_run_at"] = datetime.now(timezone.utc).isoformat()
        _movers_last_status["last_error"] = None
        _movers_last_status["last_snapshot_id"] = result.get("snapshot_id")
        _store_idempotent_movers_response(idem_key, response_payload)
        return response_payload
    except ValueError as exc:
        _movers_last_status["last_error"] = str(exc)
        return JSONResponse(
            status_code=422,
            content=_error_envelope(code="scan_validation_failed", message=str(exc), retryable=False),
        )
    except Exception as exc:
        _movers_last_status["last_error"] = str(exc)
        return JSONResponse(
            status_code=500,
            content=_error_envelope(
                code="scan_failed",
                message="Movers scan failed.",
                retryable=True,
                details={"error": str(exc)[:300]},
            ),
        )


@app.get("/api/movers/status")
def movers_status() -> Dict[str, object]:
    scheduler = _screening_scheduler.get_status()
    movers_cfg = DEFAULT_CONFIG.get("screening", {}).get("movers", {})
    manual_only = bool(movers_cfg.get("manual_only", True))
    scheduler_enabled = bool(movers_cfg.get("auto_scan_postclose_enabled", False)) and not manual_only
    return {
        "last_run_at": _movers_last_status.get("last_run_at"),
        "last_error": _movers_last_status.get("last_error"),
        "last_snapshot_id": _movers_last_status.get("last_snapshot_id"),
        "movers_enabled": bool(movers_cfg.get("enabled", False)),
        "manual_only": manual_only,
        "scheduler_effective_enabled": scheduler_enabled,
        "kill_switches": {
            "strict_builtin_apply_gate": bool(
                DEFAULT_CONFIG.get("screening", {})
                .get("scheduler", {})
                .get("builtin_refresh", {})
                .get("enforce_high_risk_apply", True)
            ),
            "movers_scheduler_enabled": bool(movers_cfg.get("auto_scan_postclose_enabled", False)),
        },
        "scheduler": {
            "movers_enabled": scheduler.get("movers_enabled"),
            "movers_running": scheduler.get("movers_running"),
            "last_movers_date": scheduler.get("last_movers_date"),
            "last_movers_scan_at": scheduler.get("last_movers_scan_at"),
            "last_movers_snapshot_id": scheduler.get("last_movers_snapshot_id"),
            "movers_error": scheduler.get("movers_error"),
        },
    }


@app.get("/api/movers/runs")
def list_movers_runs(limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0), db_path: str = "research.db") -> Dict[str, object]:
    db = get_db(db_path)
    runs = db.get_movers_runs(limit=limit, offset=offset) if hasattr(db, "get_movers_runs") else []
    return {"count": len(runs), "limit": limit, "offset": offset, "runs": runs}


@app.get("/api/movers/runs/latest")
def latest_movers_run(horizon: Optional[str] = Query(None, pattern="^(short|medium)?$"), db_path: str = "research.db") -> Dict[str, object]:
    db = get_db(db_path)
    run_id = _latest_movers_run_id(db, horizon=horizon)
    if not run_id:
        raise HTTPException(status_code=404, detail="No movers run found.")
    run = db.get_screening_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Movers run not found.")
    run["criteria"] = _parse_run_criteria(run.get("criteria"))
    return run


@app.get("/api/movers/runs/{run_id}")
def get_movers_run(run_id: int, db_path: str = "research.db") -> Dict[str, object]:
    db = get_db(db_path)
    run = _require_movers_strategy_run(db, run_id)
    criteria = run.get("criteria", {})

    results = db.get_screening_results(run_id)
    details_map = db.get_movers_run_details(run_id) if hasattr(db, "get_movers_run_details") else {}
    board, rest = _prepare_movers_board(db, results, details_map, criteria)
    payload_run = {**run, "criteria": criteria}
    board = _decorate_movers_expand_fields(board, payload_run)
    rest = _decorate_movers_expand_fields(rest, payload_run)
    return {
        "run_id": run_id,
        "run": payload_run,
        "count": len(board),
        "results": board + rest,
    }


@app.post("/api/movers/runs/{run_id}/queue-top")
def queue_movers_top(
    run_id: int,
    top_n: int = Query(5, ge=1, le=50),
    date: Optional[str] = None,
    mode: str = "standard",
    risk_profile: str = "growth",
    db_path: str = "research.db",
) -> Dict[str, object]:
    db = get_db(db_path)
    _require_movers_strategy_run(db, run_id)
    results = db.get_screening_results(run_id)
    if not results:
        raise HTTPException(status_code=404, detail="Movers run not found")

    details_map = db.get_movers_run_details(run_id) if hasattr(db, "get_movers_run_details") else {}
    run = db.get_screening_run(run_id) or {}
    criteria = _parse_run_criteria(run.get("criteria"))
    board, _rest = _prepare_movers_board(db, results, details_map, criteria)
    top = board[:top_n]
    items = [
        AnalyzeQueueItem(ticker=str(row.get("ticker", "")).upper(), screening_run_id=run_id)
        for row in top
        if row.get("ticker")
    ]
    if not items:
        raise HTTPException(status_code=400, detail="No tickers available in movers run")
    payload = AnalyzeQueueBatchRequest(
        items=items,
        date=date,
        mode=mode,
        risk_profile=risk_profile,
        db_path=db_path,
        requested_from="movers_queue_top",
        screening_run_id=run_id,
    )
    return analyze_queue_batch(payload)


@app.post("/api/movers/runs/{run_id}/alerts")
def movers_alerts(run_id: int, top_n: int = Query(5, ge=1, le=50), db_path: str = "research.db") -> Dict[str, object]:
    db = get_db(db_path)
    _require_movers_strategy_run(db, run_id)
    return create_screening_auto_alerts(run_id=run_id, top_n=top_n, db_path=db_path)


@app.post("/api/long-horizon/scan")
def run_long_horizon_scan(body: LongHorizonScanRequest, request: Request) -> object:
    idem_key = _extract_long_horizon_idempotency_key(request, body.idempotency_key)
    cached = _load_idempotent_long_horizon_response(idem_key)
    if cached is not None:
        return cached

    if not body.watchlist_id and not body.tickers and not body.use_expanded_universe:
        return JSONResponse(
            status_code=422,
            content=_error_envelope(
                code="invalid_universe",
                message="Provide watchlist_id, tickers, or enable expanded universe.",
                retryable=False,
            ),
        )
    try:
        from tradingagents.screening.long_horizon import LongHorizonService

        db = get_db(body.db_path)
        service = LongHorizonService(db=db, config=DEFAULT_CONFIG)
        result = service.run_scan(
            horizon=body.horizon,
            top_n=body.top_n,
            watchlist_id=body.watchlist_id,
            tickers=body.tickers,
            date=body.date,
            use_expanded_universe=bool(body.use_expanded_universe),
        )
        response_payload = {
            "run_id": result.get("run_id"),
            "as_of": result.get("as_of"),
            "horizon": result.get("horizon"),
            "ranked_rows": result.get("top", []),
            "excluded": result.get("excluded", []),
            "run_snapshot_hash": result.get("run_snapshot_hash"),
            "meta": result.get("meta", {}),
            "allocation": result.get("allocation", {}),
        }
        _long_horizon_last_status["last_run_at"] = datetime.now(timezone.utc).isoformat()
        _long_horizon_last_status["last_error"] = None
        _long_horizon_last_status["last_run_id"] = result.get("run_id")
        _store_idempotent_long_horizon_response(idem_key, response_payload)
        return response_payload
    except ValueError as exc:
        msg = str(exc)
        _long_horizon_last_status["last_error"] = msg
        code = "scan_failed"
        if "horizon" in msg.lower():
            code = "invalid_horizon"
        elif "universe" in msg.lower():
            code = "invalid_universe"
        elif "eligible tickers" in msg.lower():
            code = "insufficient_data"
        return JSONResponse(
            status_code=422,
            content=_error_envelope(code=code, message=msg, retryable=False),
        )
    except Exception as exc:
        _long_horizon_last_status["last_error"] = str(exc)
        return JSONResponse(
            status_code=500,
            content=_error_envelope(
                code="scan_failed",
                message="Long-horizon scan failed.",
                retryable=True,
                details={"error": str(exc)[:300]},
            ),
        )


@app.get("/api/long-horizon/status")
def long_horizon_status() -> Dict[str, object]:
    cfg = DEFAULT_CONFIG.get("screening", {}).get("long_horizon", {})
    return {
        "last_run_at": _long_horizon_last_status.get("last_run_at"),
        "last_error": _long_horizon_last_status.get("last_error"),
        "last_run_id": _long_horizon_last_status.get("last_run_id"),
        "enabled": bool(cfg.get("enabled", False)),
        "manual_only": bool(cfg.get("manual_only", True)),
        "automation": cfg.get("automation", {}),
        "underwriting_limits": cfg.get("underwriting", {}),
    }


@app.get("/api/long-horizon/runs")
def list_long_horizon_runs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db_path: str = "research.db",
) -> Dict[str, object]:
    db = get_db(db_path)
    runs = db.get_long_horizon_runs(limit=limit, offset=offset) if hasattr(db, "get_long_horizon_runs") else []
    return {"count": len(runs), "limit": limit, "offset": offset, "runs": runs}


@app.get("/api/long-horizon/runs/{run_id}")
def get_long_horizon_run(run_id: int, db_path: str = "research.db") -> Dict[str, object]:
    db = get_db(db_path)
    run = _require_long_horizon_strategy_run(db, run_id)
    results = db.get_screening_results(run_id)
    details = db.get_long_horizon_run_details(run_id) if hasattr(db, "get_long_horizon_run_details") else []
    from tradingagents.screening.long_horizon import LongHorizonService

    details_map = {str(d.get("ticker", "")).upper(): d for d in details}
    if details:
        screen_map = {str(r.get("ticker", "")).upper(): r for r in results}
        merged = []
        for d in details:
            ticker = str(d.get("ticker", "")).upper()
            enriched = dict(screen_map.get(ticker) or {})
            enriched.update(d)
            merged.append(enriched)
        merged = LongHorizonService.sort_overlay_rows(merged)
        allocation = LongHorizonService.allocation_from_overlay(merged)
    else:
        merged = list(results)
        allocation = {"weights": [], "residual_cash_weight": 1.0, "reason_codes": []}
    underwriting = db.get_long_horizon_underwriting(run_id) if hasattr(db, "get_long_horizon_underwriting") else []
    meta = db.get_long_horizon_run_meta(run_id) if hasattr(db, "get_long_horizon_run_meta") else {}
    return {
        "run_id": run_id,
        "run": run,
        "count": len(merged),
        "results": merged,
        "allocation": allocation,
        "underwriting": underwriting,
        "meta": meta.get("meta_json", {}),
    }


@app.post("/api/long-horizon/runs/{run_id}/underwrite-top")
def long_horizon_underwrite_top(run_id: int, body: LongHorizonUnderwriteRequest, request: Request) -> object:
    idem_key = _extract_long_horizon_idempotency_key(request, body.idempotency_key)
    cache_key = f"underwrite:{run_id}:{idem_key}" if idem_key else ""
    cached = _load_idempotent_long_horizon_response(cache_key)
    if cached is not None:
        return cached
    try:
        from tradingagents.screening.long_horizon import LongHorizonService

        db = get_db(body.db_path)
        _require_long_horizon_strategy_run(db, run_id)
        service = LongHorizonService(db=db, config=DEFAULT_CONFIG)
        result = service.underwrite_top(
            run_id=run_id,
            top_n=body.top_n,
            date=body.date,
            idempotency_key=body.idempotency_key,
        )
        _store_idempotent_long_horizon_response(cache_key, result)
        return result
    except ValueError as exc:
        msg = str(exc)
        code = "underwrite_failed"
        if msg == "run_not_found":
            code = "run_not_found"
        elif msg == "not_long_horizon_run":
            code = "not_long_horizon_run"
        elif msg == "budget_exceeded":
            code = "budget_exceeded"
        return JSONResponse(
            status_code=422 if code != "run_not_found" else 404,
            content=_error_envelope(code=code, message=msg, retryable=code in {"budget_exceeded"}),
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=_error_envelope(
                code="underwrite_failed",
                message="Long-horizon underwriting failed.",
                retryable=True,
                details={"error": str(exc)[:300]},
            ),
        )


@app.post("/api/long-horizon/runs/{run_id}/allocate")
def long_horizon_allocate(run_id: int, body: LongHorizonAllocateRequest, request: Request) -> object:
    idem_key = _extract_long_horizon_idempotency_key(request, body.idempotency_key)
    cache_key = f"allocate:{run_id}:{idem_key}" if idem_key else ""
    cached = _load_idempotent_long_horizon_response(cache_key)
    if cached is not None:
        return cached
    try:
        from tradingagents.screening.long_horizon import LongHorizonService

        db = get_db(body.db_path)
        _require_long_horizon_strategy_run(db, run_id)
        service = LongHorizonService(db=db, config=DEFAULT_CONFIG)
        result = service.allocate(run_id=run_id, policy_name=body.policy_name)
        _store_idempotent_long_horizon_response(cache_key, result)
        return result
    except ValueError as exc:
        msg = str(exc)
        code = "allocation_infeasible"
        if msg == "run_not_found":
            code = "run_not_found"
        elif msg == "not_long_horizon_run":
            code = "not_long_horizon_run"
        return JSONResponse(
            status_code=422 if code != "run_not_found" else 404,
            content=_error_envelope(code=code, message=msg, retryable=False),
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=_error_envelope(
                code="allocation_infeasible",
                message="Long-horizon allocation failed.",
                retryable=True,
                details={"error": str(exc)[:300]},
            ),
        )


def _group_compare_runs(
    runs: List[Dict[str, Any]],
    per_list: int = 4,
) -> Dict[str, List[Dict[str, Any]]]:
    """Group scored, non-QA runs by watchlist for Compare selectors."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in runs or []:
        if int(r.get("results_count") or 0) <= 0:
            continue
        strat = str(r.get("strategy") or "")
        if strat.startswith("qa_"):
            continue
        key = str(r.get("watchlist_name") or "Unknown")
        grouped.setdefault(key, [])
        if len(grouped[key]) < per_list:
            grouped[key].append(r)
    return grouped


def _is_scan_all_eligible_watchlist(wl: Dict[str, object], movers_prefix: str = "Movers") -> bool:
    """Desk lists only — exclude tape (movers) and Perplexity auto-discovery lists."""
    source = str(wl.get("source") or "").strip().lower()
    if source in {"movers", "auto"}:
        return False
    if str(wl.get("name") or "").startswith(f"{movers_prefix}:"):
        return False
    return True


@app.post("/api/screen-all")
def run_screening_all(
    body: Optional[ScreenAllRequest] = None,
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Scan ALL watchlists sequentially in a single background job.

    Macro snapshot and sector momentum are fetched once and cached for 1 hour,
    so they're automatically shared across all watchlist scans.

    ``body.mode`` selects standard (opportunity) vs reversal_buildup. The
    configured ``screening.scan_all.mode`` still chooses per_watchlist vs
    union_buckets.

    Runs on a **dedicated daemon thread** (not the shared ThreadPoolExecutor)
    so it never blocks API responsiveness during long-running scans.

    Returns a job_id that can be polled via GET /api/jobs/{job_id}.
    Progress updates are written to the job's ``progress`` field.
    """
    import threading as _threading

    req = body or ScreenAllRequest()
    screen_mode = _normalize_screen_mode(req.mode, for_scan_all=True)
    db_path = req.db_path or db_path
    db = get_db(db_path)
    scan_all_cfg = DEFAULT_CONFIG.get("screening", {}).get("scan_all", {})
    union_enabled = bool(scan_all_cfg.get("union_feature_flag", True))
    scan_mode = str(scan_all_cfg.get("mode", "per_watchlist")).strip().lower()
    if scan_mode == "sequential":
        scan_mode = "per_watchlist"
    if scan_mode not in {"per_watchlist", "union_buckets"}:
        scan_mode = "per_watchlist"
    if scan_mode == "union_buckets" and not union_enabled:
        scan_mode = "per_watchlist"
    hard_max = int(DEFAULT_CONFIG.get("screening", {}).get("hard_max_watchlist_size", 2000))
    union_cap = min(
        max(200, int(scan_all_cfg.get("union_max_tickers", hard_max))),
        hard_max,
    )
    watchlists = db.get_watchlists()
    scannable = []
    total_tickers = 0
    movers_prefix = str(
        ((DEFAULT_CONFIG.get("screening") or {}).get("movers") or {}).get("watchlist_name_prefix")
        or "Movers"
    )
    for wl in watchlists:
        if not _is_scan_all_eligible_watchlist(wl, movers_prefix):
            continue
        tickers = [t.strip() for t in (wl.get("tickers") or "").split(",") if t.strip()]
        if tickers:
            scannable.append({"id": wl["id"], "name": wl["name"], "tickers": tickers,
                              "preset": wl.get("default_preset")})
            total_tickers += len(tickers)

    if not scannable:
        raise HTTPException(status_code=400, detail="No watchlists with tickers found")

    # Guard against queue overload (reuse same check as _submit_job)
    with _jobs_lock:
        active = sum(1 for j in _jobs.values() if j.get("status") in ("queued", "running"))
        if active >= _MAX_ACTIVE_JOBS:
            raise HTTPException(503, f"Too many jobs in flight ({active}). Please wait.")

    job_id = str(uuid.uuid4())
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    job_data = {
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "result": None,
        "error": None,
        "job_type": "screen_all",
        "watchlist_count": len(scannable),
        "ticker_count": total_tickers,
        "mode": scan_mode,
        "screen_mode": screen_mode,
    }
    _set_job(job_id, job_data)

    def _run_all_thread():
        """Runs on a dedicated daemon thread to avoid blocking the API thread pool."""
        _update_job(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat())
        started_monotonic = _time.monotonic()
        try:
            from tradingagents.screening.engine import ScreeningEngine
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.dataflows.yfinance_limiter import get_yfinance_limiter
            from dataclasses import asdict as _asdict

            limiter = get_yfinance_limiter()
            if limiter.is_open():
                cooldown = limiter.cooldown_remaining()
                if cooldown > 0:
                    _update_job(
                        job_id,
                        progress={
                            "current": 0,
                            "total": len(scannable),
                            "watchlist": "waiting_for_yfinance_cooldown",
                            "completed": 0,
                            "total_results": 0,
                            "mode": scan_mode,
                            "screen_mode": screen_mode,
                            "cooldown_seconds": round(cooldown, 2),
                        },
                    )
                    _time.sleep(cooldown)

            # Pre-warm macro data so the cache is hot before the first large
            # watchlist scan.  Both functions are cached (1 h TTL) — this call
            # is a no-op on subsequent invocations within the same hour, so it
            # is safe to call unconditionally.
            try:
                from tradingagents.screening.macro_overlay import get_macro_overlay_context
                get_macro_overlay_context()
                logger.info("scan-all: macro data pre-warmed (cache seeded for watchlist scans)")
            except Exception as _me:
                logger.warning("scan-all: macro pre-warm failed (non-fatal): %s", _me)

            engine = ScreeningEngine(config=DEFAULT_CONFIG, db=get_db(db_path))
            completed = 0
            total_results = 0
            failed = []
            mode_used = scan_mode
            momentum_enrich_cache: Dict[str, Dict[str, Any]] = {}

            if scan_mode == "union_buckets":
                universe = []
                by_watchlist = {}
                for wl in scannable:
                    wl_tickers = [str(t).upper() for t in wl["tickers"] if str(t).strip()]
                    by_watchlist[int(wl["id"])] = set(wl_tickers)
                    universe.extend(wl_tickers)
                deduped = list(dict.fromkeys(universe))

                bucket_cfg = DEFAULT_CONFIG.get("screening", {}).get("scan_buckets", {})
                bucket_enabled = bool(bucket_cfg.get("enabled", True))
                bucket_rows: Dict[str, List[str]] = {}
                if bucket_enabled:
                    for bucket_name in ("large_cap", "mid_cap", "small_cap", "small_cap_breadth"):
                        b = bucket_cfg.get(bucket_name, {}) or {}
                        index_key = str(b.get("index_key") or "").strip().lower()
                        max_tickers = max(0, int(b.get("max_tickers", 0)))
                        if not index_key or max_tickers <= 0:
                            continue
                        latest = db.get_latest_index_constituents(index_key=index_key, in_scope_only=True)
                        bucket_rows[bucket_name] = [
                            str(r.get("ticker") or "").upper()
                            for r in latest[:max_tickers]
                            if str(r.get("ticker") or "").strip()
                        ]
                if bucket_rows:
                    deduped = list(
                        dict.fromkeys(
                            bucket_rows.get("large_cap", [])
                            + bucket_rows.get("mid_cap", [])
                            + bucket_rows.get("small_cap", [])
                            + bucket_rows.get("small_cap_breadth", [])
                            + deduped
                        )
                    )
                universe_requested = len(deduped)
                capped_universe = deduped[:union_cap]
                engine_meta = _screen_all_engine_criteria(screen_mode, "union_buckets", job_id)
                persist_strategy = _screen_all_persist_strategy(screen_mode, "union_buckets")
                def _union_scan_progress(info):
                    phase = str((info or {}).get("phase") or "scan")
                    current = int((info or {}).get("current") or 0)
                    total = max(1, int((info or {}).get("total") or 1))
                    _update_job(
                        job_id,
                        progress={
                            "current": current,
                            "total": total,
                            "watchlist": f"union:{phase}",
                            "completed": 0,
                            "total_results": 0,
                            "mode": mode_used,
                            "screen_mode": screen_mode,
                            "phase": phase,
                        },
                    )

                engine_meta["_cancel_check"] = lambda: _job_cancel_requested(job_id)
                engine_meta["_progress_cb"] = _union_scan_progress
                union_results = engine.scan(
                    tickers=capped_universe,
                    date=date,
                    watchlist_id=None,
                    preset=None,
                    # Momentum keeps enhanced Yahoo (Form 4 / revisions / SI). Reversal and Base are OHLCV-only.
                    enable_enhanced=screen_mode not in {"reversal_buildup", "base_coil"},
                    criteria_meta=engine_meta,
                    progress_cb=_union_scan_progress,
                )
                if _job_cancel_requested(job_id):
                    _update_job(
                        job_id,
                        status="cancelled",
                        error="Cancelled by operator",
                        ended_at=datetime.now(timezone.utc).isoformat(),
                    )
                    return
                if screen_mode == "early_momentum":
                    engine_meta["_enrich_cache"] = momentum_enrich_cache
                    try:
                        union_results = engine.finalize_early_momentum(
                            union_results,
                            scan_date=date,
                            cancel_check=lambda: _job_cancel_requested(job_id),
                            progress_cb=_union_scan_progress,
                            enrich_cache=momentum_enrich_cache,
                        )
                    except RuntimeError as em_exc:
                        if "cancelled" in str(em_exc).lower():
                            _update_job(
                                job_id,
                                status="cancelled",
                                error="Cancelled during momentum enrich",
                                ended_at=datetime.now(timezone.utc).isoformat(),
                            )
                            return
                        raise
                union_rows = [_asdict(r) for r in union_results]
                if screen_mode == "reversal_buildup":
                    union_rows = _rank_rows_by_reversal(union_rows)
                elif screen_mode == "early_momentum":
                    union_rows = _rank_rows_by_momentum(union_rows)
                elif screen_mode == "base_coil":
                    union_rows = _rank_rows_by_base(union_rows)
                else:
                    union_rows = sorted(union_rows, key=lambda r: float(r.get("composite_score", 0)), reverse=True)

                if not union_rows:
                    result = {
                        "watchlists_scanned": 0,
                        "watchlists_failed": 0,
                        "failed_names": [],
                        "total_results": 0,
                        "mode": mode_used,
                        "screen_mode": screen_mode,
                        "union_ticker_count": len(capped_universe),
                        "union_dropped_by_cap_count": max(0, universe_requested - len(capped_universe)),
                        "universe_requested": universe_requested,
                        "universe_scanned": len(capped_universe),
                        "universe_truncated": universe_requested > len(capped_universe),
                        "bucket_counts": {k: len(v) for k, v in bucket_rows.items()},
                        "duration_seconds": round(_time.monotonic() - started_monotonic, 3),
                        "zero_results": True,
                    }
                    _update_job(
                        job_id,
                        status="failed",
                        error=(
                            "scan_all_zero_results: no scored tickers "
                            "(yfinance breaker or data outage). Empty projections were not persisted."
                        ),
                        result=result,
                        ended_at=datetime.now(timezone.utc).isoformat(),
                    )
                    return

                for i, wl in enumerate(scannable):
                    _update_job(job_id, progress={
                        "current": i + 1,
                        "total": len(scannable),
                        "watchlist": wl["name"],
                        "completed": completed,
                        "total_results": total_results,
                        "mode": mode_used,
                        "screen_mode": screen_mode,
                    })
                    wl_set = by_watchlist.get(int(wl["id"]), set())
                    projected = [dict(r) for r in union_rows if str(r.get("ticker", "")).upper() in wl_set]
                    if screen_mode == "reversal_buildup":
                        projected = _rank_rows_by_reversal(projected)
                    elif screen_mode == "early_momentum":
                        projected = _rank_rows_by_momentum(projected)
                    elif screen_mode == "base_coil":
                        projected = _rank_rows_by_base(projected)
                    else:
                        for rank, row in enumerate(projected, start=1):
                            row["rank"] = rank
                    for row in projected:
                        row["preset"] = wl.get("preset") or "default"
                    persist_criteria = {
                        "strategy": persist_strategy,
                        "source_mode": "union_buckets",
                        "preset": wl.get("preset") or "default",
                        "universe_requested": universe_requested,
                        "universe_scanned": len(capped_universe),
                        "universe_truncated": universe_requested > len(capped_universe),
                    }
                    if screen_mode == "reversal_buildup":
                        persist_criteria["reversal_all_job_id"] = job_id
                    elif screen_mode == "early_momentum":
                        persist_criteria["early_momentum_all_job_id"] = job_id
                        gated = sum(
                            1 for r in union_rows
                            if ((r.get("signals") or {}).get("_early_momentum") or {}).get("passed_gates") is False
                        )
                        persist_criteria["gated_out_count"] = gated
                    elif screen_mode == "base_coil":
                        persist_criteria["base_coil_all_job_id"] = job_id
                    else:
                        persist_criteria["screen_all_job_id"] = job_id
                    run_id = db.save_screening_run(
                        watchlist_id=wl["id"],
                        criteria=json.dumps(persist_criteria),
                        ticker_count=len(wl_set),
                        results_count=len(projected),
                    )
                    db.save_screening_results(run_id, projected)
                    completed += 1
                    total_results += len(projected)

                result = {
                    "watchlists_scanned": completed,
                    "watchlists_failed": 0,
                    "failed_names": [],
                    "total_results": total_results,
                    "mode": mode_used,
                    "screen_mode": screen_mode,
                    "union_ticker_count": len(capped_universe),
                    "union_dropped_by_cap_count": max(0, universe_requested - len(capped_universe)),
                    "universe_requested": universe_requested,
                    "universe_scanned": len(capped_universe),
                    "universe_truncated": universe_requested > len(capped_universe),
                    "bucket_counts": {k: len(v) for k, v in bucket_rows.items()},
                    "duration_seconds": round(_time.monotonic() - started_monotonic, 3),
                }
                try:
                    db.record_runtime_metric(
                        "scan_all_duration_seconds",
                        float(result.get("duration_seconds") or 0.0),
                        context={"mode": mode_used, "screen_mode": screen_mode, "watchlist_count": len(scannable)},
                    )
                except Exception:
                    pass
                _update_job(
                    job_id,
                    status="completed",
                    result=result,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                )
                return

            for i, wl in enumerate(scannable):
                if _job_cancel_requested(job_id):
                    _update_job(
                        job_id,
                        status="cancelled",
                        error="Cancelled by operator",
                        ended_at=datetime.now(timezone.utc).isoformat(),
                    )
                    return
                _update_job(job_id, progress={
                    "current": i + 1,
                    "total": len(scannable),
                    "watchlist": wl["name"],
                    "completed": completed,
                    "total_results": total_results,
                    "screen_mode": screen_mode,
                })
                try:
                    # Keep macro cache warm between watchlists — long batches can
                    # exceed the 1 h TTL while yfinance is rate-limited.
                    try:
                        from tradingagents.screening.macro_overlay import get_macro_overlay_context
                        get_macro_overlay_context()
                    except Exception:
                        pass

                    wl_criteria = _screen_all_engine_criteria(screen_mode, "per_watchlist", job_id)
                    wl_criteria["_cancel_check"] = lambda: _job_cancel_requested(job_id)
                    if screen_mode == "early_momentum":
                        wl_criteria["_enrich_cache"] = momentum_enrich_cache
                    results = engine.scan(
                        tickers=wl["tickers"],
                        date=date,
                        watchlist_id=wl["id"],
                        preset=wl.get("preset"),
                        # Momentum keeps enhanced Yahoo (Form 4 / revisions / SI). Reversal is OHLCV-only.
                        enable_enhanced=screen_mode not in {"reversal_buildup", "base_coil"},
                        criteria_meta=wl_criteria,
                    )
                    completed += 1
                    total_results += len(results)
                except Exception as exc:
                    logger.error("Scan-all: '%s' failed: %s", wl["name"], exc)
                    failed.append(wl["name"])

                if i < len(scannable) - 1:
                    pause = max(2.0, float(limiter.cooldown_remaining()))
                    _time.sleep(pause)

            result = {
                "watchlists_scanned": completed,
                "watchlists_failed": len(failed),
                "failed_names": failed,
                "total_results": total_results,
                "mode": mode_used,
                "screen_mode": screen_mode,
                "duration_seconds": round(_time.monotonic() - started_monotonic, 3),
            }
            try:
                db.record_runtime_metric(
                    "scan_all_duration_seconds",
                    float(result.get("duration_seconds") or 0.0),
                    context={"mode": mode_used, "screen_mode": screen_mode, "watchlist_count": len(scannable)},
                )
            except Exception:
                pass
            _update_job(job_id, status="completed", result=result,
                        ended_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            logger.error("Scan-all job failed: %s", exc)
            _update_job(job_id, status="failed", error=str(exc),
                        ended_at=datetime.now(timezone.utc).isoformat())

    t = _threading.Thread(target=_run_all_thread, daemon=True, name=f"screen-all-{job_id[:8]}")
    t.start()
    return {
        "job_id": job_id,
        "watchlist_count": len(scannable),
        "ticker_count": total_tickers,
        "mode": scan_mode,
        "screen_mode": screen_mode,
        "union_feature_flag": union_enabled,
    }


@app.post("/api/screen-all/{job_id}/cancel")
def cancel_screen_all(job_id: str) -> Dict[str, object]:
    """Cooperatively cancel a running Scan All job."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.get("job_type") != "screen_all":
            raise HTTPException(status_code=400, detail="Not a Scan All job")
        if job.get("status") not in ("queued", "running"):
            raise HTTPException(status_code=400, detail=f"Job already {job.get('status')}")
        job["cancel_requested"] = True
    _update_job(job_id, status="cancelled", error="Cancel requested by operator")
    return {"job_id": job_id, "status": "cancelled"}


def _compute_opportunity_score(
    composite_score: Optional[float],
    entry_quality: Optional[float],
    macro_fit: Optional[float],
    composite_fundamental: Optional[float] = None,
    weekly_alignment: Optional[float] = None,
) -> float:
    """Compute blended opportunity score (0-100). Delegates to canonical module."""
    from tradingagents.screening.opportunity_score import compute_opportunity_score

    score, _meta = compute_opportunity_score(
        composite_score,
        entry_quality,
        macro_fit,
        composite_fundamental=composite_fundamental,
        weekly_alignment=weekly_alignment,
    )
    return score


def _apply_sector_relative_scores(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """Set sector-relative opportunity deltas and return eligible medians."""
    sector_scores: Dict[str, List[float]] = {}
    for row in rows:
        if row.get("opportunity_score") is not None:
            sector_scores.setdefault(row.get("sector") or "Unknown", []).append(
                float(row["opportunity_score"])
            )

    sector_medians: Dict[str, float] = {}
    for sector, scores in sector_scores.items():
        if len(scores) >= 3:
            sorted_scores = sorted(scores)
            middle = len(sorted_scores) // 2
            sector_medians[sector] = (
                sorted_scores[middle]
                if len(sorted_scores) % 2
                else (sorted_scores[middle - 1] + sorted_scores[middle]) / 2
            )

    for row in rows:
        sector = row.get("sector") or "Unknown"
        median = sector_medians.get(sector)
        opp = row.get("opportunity_score")
        row["sector_relative_score"] = (
            round(float(opp) - median, 1)
            if opp is not None and median is not None
            else None
        )
    return sector_medians


def _enrich_screening_rows(
    rows: List[Dict[str, Any]],
    *,
    db: ResearchDatabase,
    screening_config: Optional[Dict[str, Any]] = None,
    preset: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Expose one consistent, overlay-adjusted screening row contract.

    Screening runs persist some enhanced fields under ``signals._screening_meta``;
    ephemeral scans retain coverage/tier as dataclass fields. This helper handles
    both representations and must be used before presenting or exporting ranks.
    """
    from tradingagents.screening.scan_all_overlays import apply_scan_all_overlays

    enriched = [dict(row) for row in rows]
    tickers = [str(row.get("ticker") or "").upper() for row in enriched if row.get("ticker")]
    meta_bulk = db.get_ticker_metadata_bulk(tickers) if tickers else {}
    cfg = screening_config or DEFAULT_CONFIG.get("screening", {})

    null_adv_tickers = set()
    pre_overlay_by_ticker: Dict[str, Dict[str, Any]] = {}
    for row in enriched:
        ticker = str(row.get("ticker") or "").upper()
        row["ticker"] = ticker
        signals = row.get("signals") or {}
        if isinstance(signals, str):
            try:
                signals = json.loads(signals)
            except (json.JSONDecodeError, TypeError):
                signals = {}
        row["signals"] = signals if isinstance(signals, dict) else {}
        _lift_screening_contract_fields(row)
        meta_block = row["signals"].get("_screening_meta") or {}
        if not isinstance(meta_block, dict):
            meta_block = {}
        for field in ("signal_coverage_pct", "tier_reached", "valuation_gap_detail"):
            if row.get(field) is None and meta_block.get(field) is not None:
                row[field] = meta_block[field]
        _lift_reversal_buildup(row)

        metadata = meta_bulk.get(ticker, {}) or {}
        row["ticker_metadata"] = metadata
        row["avg_dollar_volume_usd"] = metadata.get("avg_dollar_volume_usd")
        row["sector"] = metadata.get("sector") or "Unknown"
        row["market_cap_tier"] = metadata.get("market_cap_tier") or "unknown"
        if metadata.get("market_cap") is not None:
            row["market_cap"] = metadata.get("market_cap")
        if preset and not row.get("preset"):
            row["preset"] = preset
        if row["avg_dollar_volume_usd"] is None:
            null_adv_tickers.add(ticker)
        pre_overlay_by_ticker[ticker] = dict(row)

    overlaid = apply_scan_all_overlays(enriched, screening_config=cfg, db=db)
    overlay_by_ticker = {str(row.get("ticker") or "").upper(): row for row in overlaid}

    # A missing ADV is unknown rather than an illiquidity failure in enriched
    # primary views. In exclude mode it was omitted before this normalization,
    # so recover it through penalize mode without marking it excluded.
    if cfg.get("liquidity_gate_mode", "penalize") == "exclude":
        recovery_cfg = dict(cfg)
        recovery_cfg["liquidity_gate_mode"] = "penalize"
        for ticker in null_adv_tickers:
            if ticker not in overlay_by_ticker:
                recovered = apply_scan_all_overlays(
                    [pre_overlay_by_ticker[ticker]],
                    screening_config=recovery_cfg,
                    db=db,
                )
                if recovered:
                    overlay_by_ticker[ticker] = recovered[0]

    output = []
    for row in enriched:
        ticker = str(row.get("ticker") or "").upper()
        enriched_row = overlay_by_ticker.get(ticker)
        if not enriched_row:
            continue
        if ticker in null_adv_tickers:
            enriched_row["liquidity_pass"] = None
            enriched_row.pop("liquidity_excluded", None)
        output.append(enriched_row)

    _apply_sector_relative_scores(output)
    return output


def _get_reversal_all_consolidated(
    top: int = 20,
    bottom: int = 10,
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Consolidate the latest matching reversal run per watchlist."""
    from tradingagents.screening.reversal_buildup import (
        lift_reversal_fields,
        rank_reversal_rows,
        reversal_sort_key,
    )

    db = get_db(db_path)
    selection = _latest_runs_for_lens(db, "reversal")
    batch_runs = selection["runs"]
    single_run_overlays = selection["overlays"]
    batch_window = selection["batch_window"]

    watchlist_summaries = []
    all_results: List[Dict[str, Any]] = []
    watchlists_per_ticker: Dict[str, set] = {}

    for run in batch_runs:
        results = db.get_screening_results(run["id"])
        if not results:
            continue
        run_criteria = _parse_run_criteria(run.get("criteria"))
        run_preset = str(run_criteria.get("preset") or "default")
        lifted_rows = []
        for r in results:
            signals = r.get("signals", {})
            if isinstance(signals, str):
                try:
                    signals = json.loads(signals)
                except (json.JSONDecodeError, TypeError):
                    signals = {}
            row = dict(r)
            row["signals"] = signals if isinstance(signals, dict) else {}
            lift_reversal_fields(row)
            row["watchlist_name"] = run.get("watchlist_name", "Unknown")
            row["watchlist_id"] = run.get("watchlist_id")
            row["run_id"] = run["id"]
            row["preset"] = run_preset
            lifted_rows.append(row)
            all_results.append(row)
            ticker = str(row.get("ticker") or "").upper()
            if ticker:
                watchlists_per_ticker.setdefault(ticker, set()).add(row["watchlist_name"])

        scores = [float(r.get("reversal_score") or 0.0) for r in lifted_rows if r.get("reversal_score") is not None]
        top_row = min(
            lifted_rows,
            key=lambda rr: reversal_sort_key(rr.get("reversal_buildup"), str(rr.get("ticker") or "")),
        ) if lifted_rows else None
        watchlist_summaries.append({
            "watchlist_id": run.get("watchlist_id"),
            "watchlist_name": run.get("watchlist_name", "Unknown"),
            "run_id": run["id"],
            "run_at": run.get("run_at"),
            "source": run.get("_board_source") or "scan_all",
            "ticker_count": len(lifted_rows),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "max_score": round(max(scores), 1) if scores else 0,
            "early_turn_count": sum(1 for r in lifted_rows if r.get("reversal_phase") == "early_turn"),
            "confirmed_count": sum(1 for r in lifted_rows if r.get("reversal_phase") == "confirmed"),
            "long_count": sum(1 for r in lifted_rows if r.get("reversal_side") == "long"),
            "short_count": sum(1 for r in lifted_rows if r.get("reversal_side") == "short"),
            "top_ticker": top_row.get("ticker") if top_row else None,
            "top_score": round(float(top_row.get("reversal_score") or 0.0), 1) if top_row else None,
        })

    best_per_ticker: Dict[str, dict] = {}
    for r in all_results:
        ticker = str(r.get("ticker") or "").upper()
        if not ticker:
            continue
        r["ticker"] = ticker
        if ticker not in best_per_ticker:
            best_per_ticker[ticker] = r
            continue
        incumbent = best_per_ticker[ticker]
        if reversal_sort_key(r.get("reversal_buildup"), ticker) < reversal_sort_key(
            incumbent.get("reversal_buildup"), ticker
        ):
            best_per_ticker[ticker] = r

    deduped = list(best_per_ticker.values())
    for r in deduped:
        wl_set = watchlists_per_ticker.get(r["ticker"], set())
        r["watchlist_breadth"] = len(wl_set)
        r["watchlists"] = sorted(wl_set)

    deduped = _enrich_screening_rows(deduped, db=db)
    for r in deduped:
        lift_reversal_fields(r)

    visible = rank_reversal_rows(deduped)
    long_setups = [r for r in visible if r.get("reversal_side") == "long"][:top]
    short_setups = [r for r in visible if r.get("reversal_side") == "short"][:top]
    combined = rank_reversal_rows(visible, top_n=top)

    rev_scores = [float(r.get("reversal_score") or 0.0) for r in visible if r.get("reversal_score") is not None]
    return {
        "lens": "reversal",
        "batch_run_count": len(batch_runs),
        "single_run_overlays": single_run_overlays,
        "source_mix": selection["source_mix"],
        "batch_window": batch_window,
        "source_mode": selection["source_mode"],
        "universe_requested": selection["universe_requested"],
        "universe_scanned": selection["universe_scanned"],
        "universe_truncated": selection["universe_truncated"],
        "watchlist_summaries": sorted(watchlist_summaries, key=lambda x: x.get("avg_score") or 0, reverse=True),
        "long_setups": long_setups,
        "short_setups": short_setups,
        "top_opportunities": combined,
        "stratified_top_opportunities": {},
        "weakest_signals": [],
        "laggards": [],
        "short_candidates": short_setups,
        "totals": {
            "unique_tickers": len(deduped),
            "visible_tickers": len(visible),
            "total_scored": len(all_results),
            "avg_reversal_score": round(sum(rev_scores) / len(rev_scores), 1) if rev_scores else 0,
            "long_count": sum(1 for r in visible if r.get("reversal_side") == "long"),
            "short_count": sum(1 for r in visible if r.get("reversal_side") == "short"),
            "early_turn_count": sum(1 for r in visible if r.get("reversal_phase") == "early_turn"),
            "confirmed_count": sum(1 for r in visible if r.get("reversal_phase") == "confirmed"),
        },
    }


def _get_momentum_all_consolidated(
    top: int = 20,
    bottom: int = 10,
    db_path: str = "research.db",
    hide_illiquid: bool = False,
) -> Dict[str, object]:
    """Consolidate latest Early Momentum runs — rank by score_final, no Opp overlays."""
    from tradingagents.screening.early_momentum import (
        lift_momentum_fields,
        momentum_sort_key,
        rank_momentum_rows,
    )

    db = get_db(db_path)
    selection = _latest_runs_for_lens(db, "momentum")
    batch_runs = selection["runs"]
    single_run_overlays = selection["overlays"]
    batch_window = selection["batch_window"]

    watchlist_summaries = []
    all_results: List[Dict[str, Any]] = []
    watchlists_per_ticker: Dict[str, set] = {}
    gated_out_count = 0

    for run in batch_runs:
        results = db.get_screening_results(run["id"])
        if not results:
            continue
        run_criteria = _parse_run_criteria(run.get("criteria"))
        run_preset = str(run_criteria.get("preset") or "default")
        gated_out_count += int(run_criteria.get("gated_out_count") or 0)
        lifted_rows = []
        for r in results:
            signals = r.get("signals", {})
            if isinstance(signals, str):
                try:
                    signals = json.loads(signals)
                except (json.JSONDecodeError, TypeError):
                    signals = {}
            row = dict(r)
            row["signals"] = signals if isinstance(signals, dict) else {}
            lift_momentum_fields(row)
            if row.get("passed_gates") is False:
                continue
            em = row.get("early_momentum") or {}
            if isinstance(em, dict) and em.get("passed_gates") is False:
                continue
            row["watchlist_name"] = run.get("watchlist_name", "Unknown")
            row["watchlist_id"] = run.get("watchlist_id")
            row["run_id"] = run["id"]
            row["preset"] = run_preset
            lifted_rows.append(row)
            all_results.append(row)
            ticker = str(row.get("ticker") or "").upper()
            if ticker:
                watchlists_per_ticker.setdefault(ticker, set()).add(row["watchlist_name"])

        scores = [
            float(r.get("momentum_score") or 0.0)
            for r in lifted_rows
            if r.get("momentum_score") is not None
        ]
        top_row = min(
            lifted_rows,
            key=lambda rr: momentum_sort_key(rr.get("early_momentum"), str(rr.get("ticker") or "")),
        ) if lifted_rows else None
        watchlist_summaries.append({
            "watchlist_id": run.get("watchlist_id"),
            "watchlist_name": run.get("watchlist_name", "Unknown"),
            "run_id": run["id"],
            "run_at": run.get("run_at"),
            "source": run.get("_board_source") or "scan_all",
            "ticker_count": len(lifted_rows),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "max_score": round(max(scores), 1) if scores else 0,
            "hc_count": sum(1 for r in lifted_rows if r.get("momentum_bucket") == "high_conviction"),
            "confirmed_count": sum(1 for r in lifted_rows if r.get("momentum_bucket") == "confirmed"),
            "watch_count": sum(1 for r in lifted_rows if r.get("momentum_bucket") == "watch"),
            "event_count": sum(1 for r in lifted_rows if r.get("momentum_event_flag")),
            "top_ticker": top_row.get("ticker") if top_row else None,
            "top_score": round(float(top_row.get("momentum_score") or 0.0), 1) if top_row else None,
        })

    best_per_ticker: Dict[str, dict] = {}
    for r in all_results:
        ticker = str(r.get("ticker") or "").upper()
        if not ticker:
            continue
        r["ticker"] = ticker
        if ticker not in best_per_ticker:
            best_per_ticker[ticker] = r
            continue
        incumbent = best_per_ticker[ticker]
        if momentum_sort_key(r.get("early_momentum"), ticker) < momentum_sort_key(
            incumbent.get("early_momentum"), ticker
        ):
            best_per_ticker[ticker] = r

    deduped = list(best_per_ticker.values())
    for r in deduped:
        wl_set = watchlists_per_ticker.get(r["ticker"], set())
        r["watchlist_breadth"] = len(wl_set)
        r["watchlists"] = sorted(wl_set)

    if hide_illiquid:
        deduped = [r for r in deduped if not r.get("liquidity_excluded")]

    for r in deduped:
        lift_momentum_fields(r)

    visible = rank_momentum_rows(deduped)
    combined = rank_momentum_rows(visible, top_n=top)
    mom_scores = [float(r.get("momentum_score") or 0.0) for r in visible if r.get("momentum_score") is not None]

    return {
        "lens": "momentum",
        "batch_run_count": len(batch_runs),
        "single_run_overlays": single_run_overlays,
        "source_mix": selection["source_mix"],
        "batch_window": batch_window,
        "source_mode": selection["source_mode"],
        "universe_requested": selection["universe_requested"],
        "universe_scanned": selection["universe_scanned"],
        "universe_truncated": selection["universe_truncated"],
        "gated_out_count": gated_out_count,
        "watchlist_summaries": sorted(watchlist_summaries, key=lambda x: x.get("avg_score") or 0, reverse=True),
        "top_opportunities": combined,
        "weakest_signals": rank_momentum_rows(visible)[-bottom:] if bottom else [],
        "totals": {
            "unique_tickers": len(deduped),
            "visible_tickers": len(visible),
            "total_scored": len(all_results),
            "avg_momentum_score": round(sum(mom_scores) / len(mom_scores), 1) if mom_scores else 0,
            "hc_count": sum(1 for r in visible if r.get("momentum_bucket") == "high_conviction"),
            "confirmed_count": sum(1 for r in visible if r.get("momentum_bucket") == "confirmed"),
            "watch_count": sum(1 for r in visible if r.get("momentum_bucket") == "watch"),
            "event_count": sum(1 for r in visible if r.get("momentum_event_flag")),
        },
    }


def _get_base_all_consolidated(
    top: int = 20,
    bottom: int = 10,
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Consolidate latest Base coil runs — names still inside the range."""
    from tradingagents.screening.base_coil import (
        base_sort_key,
        lift_base_fields,
        rank_base_rows,
    )

    db = get_db(db_path)
    selection = _latest_runs_for_lens(db, "base")
    batch_runs = selection["runs"]
    single_run_overlays = selection["overlays"]
    batch_window = selection["batch_window"]

    watchlist_summaries = []
    all_results: List[Dict[str, Any]] = []
    watchlists_per_ticker: Dict[str, set] = {}

    for run in batch_runs:
        results = db.get_screening_results(run["id"])
        if not results:
            continue
        run_criteria = _parse_run_criteria(run.get("criteria"))
        run_preset = str(run_criteria.get("preset") or "default")
        lifted_rows = []
        for r in results:
            signals = r.get("signals", {})
            if isinstance(signals, str):
                try:
                    signals = json.loads(signals)
                except (json.JSONDecodeError, TypeError):
                    signals = {}
            row = dict(r)
            row["signals"] = signals if isinstance(signals, dict) else {}
            lift_base_fields(row)
            if (row.get("base_coil") or {}).get("on_board") is False:
                continue
            row["watchlist_name"] = run.get("watchlist_name", "Unknown")
            row["watchlist_id"] = run.get("watchlist_id")
            row["run_id"] = run["id"]
            row["preset"] = run_preset
            lifted_rows.append(row)
            all_results.append(row)
            ticker = str(row.get("ticker") or "").upper()
            if ticker:
                watchlists_per_ticker.setdefault(ticker, set()).add(row["watchlist_name"])

        scores = [
            float(r.get("base_score") or 0.0)
            for r in lifted_rows
            if r.get("base_score") is not None
        ]
        top_row = min(
            lifted_rows,
            key=lambda rr: base_sort_key(rr.get("base_coil"), str(rr.get("ticker") or "")),
        ) if lifted_rows else None
        watchlist_summaries.append({
            "watchlist_id": run.get("watchlist_id"),
            "watchlist_name": run.get("watchlist_name", "Unknown"),
            "run_id": run["id"],
            "run_at": run.get("run_at"),
            "source": run.get("_board_source") or "scan_all",
            "ticker_count": len(lifted_rows),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "max_score": round(max(scores), 1) if scores else 0,
            "top_ticker": top_row.get("ticker") if top_row else None,
            "top_score": round(float(top_row.get("base_score") or 0.0), 1) if top_row else None,
        })

    best_per_ticker: Dict[str, dict] = {}
    for r in all_results:
        ticker = str(r.get("ticker") or "").upper()
        if not ticker:
            continue
        r["ticker"] = ticker
        if ticker not in best_per_ticker:
            best_per_ticker[ticker] = r
            continue
        incumbent = best_per_ticker[ticker]
        if base_sort_key(r.get("base_coil"), ticker) < base_sort_key(
            incumbent.get("base_coil"), ticker
        ):
            best_per_ticker[ticker] = r

    deduped = list(best_per_ticker.values())
    for r in deduped:
        wl_set = watchlists_per_ticker.get(r["ticker"], set())
        r["watchlist_breadth"] = len(wl_set)
        r["watchlists"] = sorted(wl_set)
        lift_base_fields(r)

    visible = rank_base_rows(deduped)
    combined = visible[:top]
    base_scores = [float(r.get("base_score") or 0.0) for r in visible if r.get("base_score") is not None]

    return {
        "lens": "base",
        "batch_run_count": len(batch_runs),
        "single_run_overlays": single_run_overlays,
        "source_mix": selection["source_mix"],
        "batch_window": batch_window,
        "source_mode": selection["source_mode"],
        "universe_requested": selection["universe_requested"],
        "universe_scanned": selection["universe_scanned"],
        "universe_truncated": selection["universe_truncated"],
        "watchlist_summaries": sorted(watchlist_summaries, key=lambda x: x.get("avg_score") or 0, reverse=True),
        "top_opportunities": combined,
        "weakest_signals": visible[-bottom:] if bottom else [],
        "totals": {
            "unique_tickers": len(deduped),
            "visible_tickers": len(visible),
            "total_scored": len(all_results),
            "avg_base_score": round(sum(base_scores) / len(base_scores), 1) if base_scores else 0,
        },
    }


def _canonical_preset_name(name: Optional[str]) -> str:
    from tradingagents.screening.discovery import canonical_preset_name

    return canonical_preset_name(name)


_FAMILY_SORT_KEYS = {
    "quality": "Quality",
    "value": "Value",
    "income": "Income",
}


def _lift_screening_contract_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Lift resolved_preset + factor_scorecard from signals JSON onto the row."""
    signals = row.get("signals") or {}
    if isinstance(signals, str):
        try:
            signals = json.loads(signals)
        except (json.JSONDecodeError, TypeError):
            signals = {}
    if not isinstance(signals, dict):
        signals = {}
    row["signals"] = signals
    meta = signals.get("_screening_meta") if isinstance(signals.get("_screening_meta"), dict) else {}
    row["resolved_preset"] = _canonical_preset_name(
        row.get("resolved_preset") or meta.get("resolved_preset") or row.get("preset") or "default"
    )
    raw_fs = signals.pop("_factor_scorecard", None)
    if raw_fs is not None:
        if isinstance(raw_fs, str):
            try:
                raw_fs = json.loads(raw_fs)
            except (json.JSONDecodeError, TypeError):
                raw_fs = None
        row["factor_scorecard"] = raw_fs if isinstance(raw_fs, dict) else None
    else:
        row.setdefault("factor_scorecard", None)
    fs = row.get("factor_scorecard")
    if isinstance(fs, dict):
        for family, value in list(fs.items()):
            if value is None:
                continue
            try:
                fs[family] = float(value)
            except (TypeError, ValueError):
                fs[family] = None
    return row


def _row_matches_book(row: Dict[str, Any], book: Optional[str]) -> bool:
    if not book or str(book).strip().lower() in {"all", ""}:
        return True
    book_key = str(book).strip()
    resolved = _canonical_preset_name(row.get("resolved_preset"))
    run_preset = _canonical_preset_name(row.get("preset"))
    if book_key.lower() == "default":
        if resolved in {"default", "", None} or str(resolved).lower() == "default":
            return True
        return run_preset in {"default", ""} and resolved in {"default", ""}
    target = _canonical_preset_name(book_key)
    return resolved == target or run_preset == target


def _sort_scan_all_rows(rows: List[Dict[str, Any]], sort: str) -> List[Dict[str, Any]]:
    key = str(sort or "opportunity_score").strip().lower()
    if key == "sector_relative_score":
        return sorted(
            rows,
            key=lambda x: (x.get("sector_relative_score") is None, -(x.get("sector_relative_score") or -999)),
        )
    family = _FAMILY_SORT_KEYS.get(key)
    if family:
        def _family_key(row: Dict[str, Any]):
            fs = row.get("factor_scorecard") if isinstance(row.get("factor_scorecard"), dict) else {}
            val = fs.get(family)
            if val is None:
                return (1, 0.0)
            try:
                return (0, -float(val))
            except (TypeError, ValueError):
                return (1, 0.0)
        return sorted(rows, key=_family_key)
    return sorted(rows, key=lambda x: x.get("opportunity_score", 0) or 0, reverse=True)


def _row_passes_hide_filters(
    row: Dict[str, Any],
    *,
    hide_illiquid: bool = False,
    hide_blackout: bool = False,
    hide_coverage: bool = False,
    hide_counter_trend: bool = False,
    coverage_threshold: float = 60.0,
) -> bool:
    """Match Cross-Watchlist hide-illiquid / blackout / coverage / counter-trend checkboxes."""
    if hide_illiquid and row.get("liquidity_pass") is False:
        return False
    if hide_blackout and "earnings_blackout" in (row.get("event_risk_flags") or []):
        return False
    if hide_coverage:
        cov = row.get("signal_coverage_pct")
        if cov is not None:
            try:
                if float(cov) < float(coverage_threshold):
                    return False
            except (TypeError, ValueError):
                pass
    if hide_counter_trend and row.get("weekly_counter_trend") is True:
        return False
    return True


def _merge_filtered_summaries(
    original: List[Dict[str, Any]],
    filtered_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep every watchlist card; dim those with zero rows in the active view.

    Dedup attributes a ticker to one list. Replacing ``ticker_count`` with that
    winner count made S&P 100 look like an 18-name scan. Keep the scored-list
    stats and expose winner-attribution separately as ``visible_count``.
    """
    filtered = {s["watchlist_id"]: s for s in _rebuild_watchlist_summaries(filtered_rows)}
    merged: List[Dict[str, Any]] = []
    for summary in original:
        wl_id = summary.get("watchlist_id")
        rebuilt = filtered.get(wl_id)
        out = dict(summary)
        if rebuilt:
            out["visible_count"] = rebuilt.get("visible_count", 0)
            out["top_ticker"] = rebuilt.get("top_ticker")
            out["top_score"] = rebuilt.get("top_score")
        else:
            out["visible_count"] = 0
        merged.append(out)
    return merged


def _headline_top_opportunities(rows: List[Dict[str, Any]], top: int) -> List[Dict[str, Any]]:
    """Rotate cap bands / ETFs with a coverage+ADV floor for the default table."""
    from tradingagents.screening.scan_all_overlays import headline_top_opportunities

    headline_cfg = (
        (DEFAULT_CONFIG.get("screening") or {}).get("scan_all", {}) or {}
    ).get("headline") or {}
    return headline_top_opportunities(rows, top, headline_cfg=headline_cfg)


def _rebuild_watchlist_summaries(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_wl: Dict[Any, List[Dict[str, Any]]] = {}
    meta_by_wl: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        wl_id = row.get("watchlist_id")
        by_wl.setdefault(wl_id, []).append(row)
        meta_by_wl.setdefault(wl_id, {
            "watchlist_id": wl_id,
            "watchlist_name": row.get("watchlist_name", "Unknown"),
            "run_id": row.get("run_id"),
        })
    summaries = []
    for wl_id, group in by_wl.items():
        scores = [r.get("composite_score") or 0 for r in group]
        opp = [r.get("opportunity_score") for r in group if r.get("opportunity_score") is not None]
        macro = [r.get("macro_fit") for r in group if r.get("macro_fit") is not None]
        eq = [r.get("entry_quality") for r in group if r.get("entry_quality") is not None]
        bullish = sum(1 for r in group if r.get("direction") == "bullish")
        top = max(group, key=lambda r: r.get("opportunity_score") or 0) if group else None
        meta = meta_by_wl.get(wl_id, {})
        summaries.append({
            "watchlist_id": wl_id,
            "watchlist_name": meta.get("watchlist_name", "Unknown"),
            "run_id": meta.get("run_id"),
            "ticker_count": len(group),
            "visible_count": len(group),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "max_score": round(max(scores), 1) if scores else 0,
            "bullish_count": bullish,
            "bearish_count": len(group) - bullish,
            "avg_macro_fit": round(sum(macro) / len(macro), 1) if macro else None,
            "avg_entry_quality": round(sum(eq) / len(eq), 1) if eq else None,
            "avg_opportunity_score": round(sum(opp) / len(opp), 1) if opp else None,
            "top_ticker": top.get("ticker") if top else None,
            "top_score": round(top.get("opportunity_score") or 0, 1) if top else None,
        })
    return summaries


@app.get("/api/screen-all/latest")
def get_scan_all_consolidated(
    top: int = Query(20, ge=1, le=500),
    bottom: int = Query(10, ge=1, le=500),
    sort: str = Query("opportunity_score", description="Sort key: opportunity_score, sector_relative_score, quality, value, income"),
    book: Optional[str] = Query(None, description="Operator book filter (resolved_preset). Empty/all = no filter."),
    hide_illiquid: bool = Query(False),
    hide_blackout: bool = Query(False),
    hide_coverage: bool = Query(False),
    hide_counter_trend: bool = Query(False),
    strategy: str = Query("standard", description="Lens: standard (opportunity), reversal_buildup, early_momentum, or base_coil"),
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Get consolidated Cross-Watchlist results from the latest matching run per list.

    For each watchlist, uses the newest screening run that matches this lens
    (Opportunity, Reversal, or Early Momentum). Scan All and Run Screening both
    appear; a newer matching single replaces that list's older Scan All row.
    Aggregates results,
    deduplicates tickers, and returns:
      - ``watchlist_summaries``: per-watchlist stats (avg score, top pick, direction split)
      - ``top_opportunities``: best N tickers across all watchlists
      - ``weakest_signals``: bottom N tickers across all watchlists
      - ``totals``: overall counts and averages

    Pass ``strategy=reversal_buildup`` for the reversal lens (long/short setups).
    """
    if not isinstance(strategy, str):
        strategy = str(getattr(strategy, "default", "standard") or "standard")
    if not isinstance(book, str) and book is not None:
        book = getattr(book, "default", None)
    if not isinstance(sort, str):
        sort = str(getattr(sort, "default", "opportunity_score") or "opportunity_score")
    if not isinstance(top, int):
        top = int(getattr(top, "default", 20) or 20)
    if not isinstance(bottom, int):
        bottom = int(getattr(bottom, "default", 10) or 10)
    hide_illiquid = bool(getattr(hide_illiquid, "default", hide_illiquid))
    hide_blackout = bool(getattr(hide_blackout, "default", hide_blackout))
    hide_coverage = bool(getattr(hide_coverage, "default", hide_coverage))
    hide_counter_trend = bool(getattr(hide_counter_trend, "default", hide_counter_trend))
    lens = str(strategy or "standard").strip().lower()
    if lens in {"reversal_buildup", "reversal", "reversal_all_union"}:
        return _get_reversal_all_consolidated(top=top, bottom=bottom, db_path=db_path)
    if lens in {"early_momentum", "momentum", "early_momentum_union"}:
        return _get_momentum_all_consolidated(
            top=top,
            bottom=bottom,
            db_path=db_path,
            hide_illiquid=hide_illiquid,
        )
    if lens in {"base_coil", "base", "base_coil_union"}:
        return _get_base_all_consolidated(top=top, bottom=bottom, db_path=db_path)
    db = get_db(db_path)
    selection = _latest_runs_for_lens(db, "opportunity")
    batch_runs = selection["runs"]
    single_run_overlays = selection["overlays"]
    batch_window = selection["batch_window"]
    batch_source_mode = selection["source_mode"]
    scan_all_cfg = DEFAULT_CONFIG.get("screening", {}).get("scan_all", {}) or {}

    # Fetch results for each run and build consolidated view
    watchlist_summaries = []
    all_results = []

    for run in batch_runs:
        results = db.get_screening_results(run["id"])
        if not results:
            continue
        run_criteria = _parse_run_criteria(run.get("criteria"))
        run_preset = str(run_criteria.get("preset") or "default")

        scores = [r["composite_score"] for r in results]
        bullish = sum(1 for r in results if r.get("direction") == "bullish")
        macro_scores = [r["macro_fit"] for r in results if r.get("macro_fit") is not None]
        eq_scores = [r["entry_quality"] for r in results if r.get("entry_quality") is not None]
        run_rows = []
        for r in results:
            opportunity_score = _compute_opportunity_score(
                r.get("composite_score"),
                r.get("entry_quality"),
                r.get("macro_fit"),
                composite_fundamental=r.get("composite_fundamental"),
            )
            run_rows.append({"ticker": r.get("ticker"), "opportunity_score": opportunity_score})
        opp_scores = [rr["opportunity_score"] for rr in run_rows]
        top_ticker = max(run_rows, key=lambda rr: rr["opportunity_score"]) if run_rows else None

        watchlist_summaries.append({
            "watchlist_id": run.get("watchlist_id"),
            "watchlist_name": run.get("watchlist_name", "Unknown"),
            "run_id": run["id"],
            "run_at": run.get("run_at"),
            "source": run.get("_board_source") or "scan_all",
            "ticker_count": len(results),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "max_score": round(max(scores), 1) if scores else 0,
            "bullish_count": bullish,
            "bearish_count": len(results) - bullish,
            "avg_macro_fit": round(sum(macro_scores) / len(macro_scores), 1) if macro_scores else None,
            "avg_entry_quality": round(sum(eq_scores) / len(eq_scores), 1) if eq_scores else None,
            "avg_opportunity_score": round(sum(opp_scores) / len(opp_scores), 1) if opp_scores else None,
            "top_ticker": top_ticker["ticker"] if top_ticker else None,
            "top_score": round(top_ticker["opportunity_score"], 1) if top_ticker else None,
        })

        for r in results:
            # Parse JSON fields not already deserialized by DB layer
            signals = r.get("signals", {})
            if isinstance(signals, str):
                try:
                    signals = json.loads(signals)
                except (json.JSONDecodeError, TypeError):
                    signals = {}

            opportunity_score = _compute_opportunity_score(
                r.get("composite_score"),
                r.get("entry_quality"),
                r.get("macro_fit"),
                composite_fundamental=r.get("composite_fundamental"),
            )
            meta_block = signals.get("_screening_meta") if isinstance(signals, dict) else {}
            if not isinstance(meta_block, dict):
                meta_block = {}
            row_out = {
                "ticker": r["ticker"],
                "composite_score": r["composite_score"],
                "opportunity_score": opportunity_score,
                "direction": r.get("direction", ""),
                "rank": r.get("rank", 0),
                "macro_fit": r.get("macro_fit"),
                "entry_quality": r.get("entry_quality"),
                "entry_quality_signals": r.get("entry_quality_signals"),
                "signals": signals,
                "macro_breakdown": r.get("macro_breakdown"),
                "index_regime": r.get("index_regime"),
                "index_regime_label": r.get("index_regime_label"),
                "index_trend": r.get("index_trend"),
                "index_stress": r.get("index_stress"),
                "watchlist_name": run.get("watchlist_name", "Unknown"),
                "watchlist_id": run.get("watchlist_id"),
                "run_id": run["id"],
                "preset": run_preset,
                "composite_fundamental": r.get("composite_fundamental"),
                "signal_coverage_pct": meta_block.get("signal_coverage_pct"),
                "tier_reached": meta_block.get("tier_reached"),
                "valuation_gap_detail": meta_block.get("valuation_gap_detail"),
                # Risk dimension (added with universe expansion). risk_score is
                # 0-100 (higher=riskier); risk_components is a dict of sub-
                # scores keyed by component name; asset_class is a row-level
                # tag (equity/etf/adr) snapshotted at screen time.
                "risk_score": r.get("risk_score"),
                "risk_components": r.get("risk_components"),
                "asset_class": r.get("asset_class"),
            }
            all_results.append(_lift_screening_contract_fields(row_out))

    # Preset-normalize opportunity scores per watchlist to remove the
    # "preset shopping" bias in dedup. Some presets (e.g. momentum_hunter)
    # tend to produce systematically higher composites than others
    # (dividend_income) simply because of weight distribution. Picking "max
    # raw opp" when a ticker appears in multiple watchlists therefore
    # systematically prefers the preset whose weights happen to score THIS
    # ticker higher, not the watchlist that is actually most relevant to it.
    #
    # Fix: compute a watchlist-level z-score for each row and prefer the
    # highest z-score during dedup. We keep raw opportunity_score on the
    # response for display, but add ``opportunity_z`` so callers can inspect
    # the normalization.
    import statistics as _stats
    per_wl_scores: Dict[Any, List[float]] = {}
    for r in all_results:
        opp = r.get("opportunity_score")
        if opp is None:
            continue
        per_wl_scores.setdefault(r.get("watchlist_id"), []).append(float(opp))
    per_wl_stats: Dict[Any, tuple[float, float]] = {}
    for wl_id, vals in per_wl_scores.items():
        if not vals:
            continue
        mean = _stats.fmean(vals)
        stdev = _stats.pstdev(vals) if len(vals) > 1 else 0.0
        per_wl_stats[wl_id] = (mean, stdev)
    for r in all_results:
        wl_id = r.get("watchlist_id")
        mean, stdev = per_wl_stats.get(wl_id, (0.0, 0.0))
        opp = r.get("opportunity_score")
        if opp is None or stdev <= 1e-6:
            r["opportunity_z"] = 0.0
        else:
            r["opportunity_z"] = round((float(opp) - mean) / stdev, 3)

    # Deduplicate: keep the ticker's appearance with the highest *normalized*
    # opportunity_z score. Accumulate watchlist_breadth and mean opp for
    # diagnostics alongside.
    best_per_ticker: Dict[str, dict] = {}
    occurrence_counts: Dict[str, int] = {}
    opp_sum_per_ticker: Dict[str, float] = {}
    watchlists_per_ticker: Dict[str, set] = {}
    for r in all_results:
        t = r["ticker"]
        occurrence_counts[t] = occurrence_counts.get(t, 0) + 1
        opp_sum_per_ticker[t] = opp_sum_per_ticker.get(t, 0.0) + float(r.get("opportunity_score") or 0.0)
        wl_name = r.get("watchlist_name")
        if wl_name:
            watchlists_per_ticker.setdefault(t, set()).add(wl_name)
        # Prefer rows with a higher z-score; tie-break on raw opportunity_score
        # so that within a single watchlist we still pick the higher row.
        if t not in best_per_ticker:
            best_per_ticker[t] = r
        else:
            incumbent = best_per_ticker[t]
            inc_key = (incumbent.get("opportunity_z", 0.0), incumbent.get("opportunity_score", 0.0))
            new_key = (r.get("opportunity_z", 0.0), r.get("opportunity_score", 0.0))
            if new_key > inc_key:
                best_per_ticker[t] = r

    # Display sort stays on raw opp so the leaderboard ordering matches what
    # users see in each per-watchlist view. Z-score dictates which row wins
    # the dedup coin flip, not the display ordering.
    deduped = sorted(best_per_ticker.values(), key=lambda x: x["opportunity_score"], reverse=True)

    # Dedup winner selection intentionally remains pre-overlay (z-normalized
    # within each watchlist). The UI-facing score and sector-relative metric
    # below use the final overlay-adjusted opportunity score.
    deduped = _enrich_screening_rows(deduped, db=db)

    book_key = (book or "").strip()
    cov_threshold = float(
        (DEFAULT_CONFIG.get("screening") or {}).get("coverage_penalty", {}).get("threshold", 60)
        or 60
    )
    if book_key and book_key.lower() not in {"all", ""}:
        deduped = [r for r in deduped if _row_matches_book(r, book_key)]
    if hide_illiquid or hide_blackout or hide_coverage:
        deduped = [
            r for r in deduped
            if _row_passes_hide_filters(
                r,
                hide_illiquid=hide_illiquid,
                hide_blackout=hide_blackout,
                hide_coverage=hide_coverage,
                coverage_threshold=cov_threshold,
            )
        ]

    # Per-list scans only compute weekly data for enhanced-funnel survivors.
    # Hydrate a bounded pre-display pool here so MTF can actually promote
    # candidates displaced by a counter-trend top result.
    weekly_cfg = scan_all_cfg.get("weekly_mtf", {}) or {}
    weekly_hydrated_count = 0
    weekly_insufficient_count = 0
    weekly_hydrate_duration_ms = 0
    if weekly_cfg.get("enabled", True) and deduped:
        from time import monotonic as _monotonic
        from tradingagents.screening.scan_all_mtf import (
            apply_mtf_confluence_flags,
            collect_leaderboard_mtf_targets,
            hydrate_weekly_mtf,
            merge_hydrated_rows,
        )

        configured_cap = max(0, int(weekly_cfg.get("hydrate_top_n_cap", 60)))
        hard_cap = max(1, int(weekly_cfg.get("hard_max_hydrate_top_n", 150)))
        hydrate_pool_size = min(len(deduped), min(hard_cap, max(top, configured_cap)))
        hydrate_started = _monotonic()
        hydrated = hydrate_weekly_mtf(
            deduped[:hydrate_pool_size],
            cap=hydrate_pool_size,
        )
        weekly_hydrate_duration_ms = round((_monotonic() - hydrate_started) * 1000)
        weekly_hydrated_count = len(hydrated.hydrated_tickers)
        weekly_insufficient_count = len(hydrated.insufficient_history_tickers)
        deduped = hydrated.rows + deduped[hydrate_pool_size:]

        # Optional independent short-thesis confirmation. This remains off by
        # default but is fully wired when an operator enables it.
        if weekly_cfg.get("hydrate_short_candidates", False):
            short_cap = max(0, int(weekly_cfg.get("short_hydrate_cap", 20)))
            short_pool = [
                row for row in sorted(
                    (row for row in deduped if row.get("direction") == "bearish"),
                    key=lambda row: row.get("opportunity_score", 0),
                )
                if str(row.get("ticker") or "").upper() not in hydrated.hydrated_tickers
            ][:short_cap]
            if short_pool:
                short_hydrated = hydrate_weekly_mtf(short_pool, cap=short_cap)
                replacement_by_ticker = {
                    str(row.get("ticker") or "").upper(): row
                    for row in short_hydrated.rows
                }
                deduped = [
                    replacement_by_ticker.get(str(row.get("ticker") or "").upper(), row)
                    for row in deduped
                ]
                hydrated.hydrated_tickers.update(short_hydrated.hydrated_tickers)
                hydrated.insufficient_history_tickers.update(
                    short_hydrated.insufficient_history_tickers
                )
                weekly_hydrated_count = len(hydrated.hydrated_tickers)
                weekly_insufficient_count = len(hydrated.insufficient_history_tickers)

        # Reuse the canonical enrichment/overlay path after mutating weekly
        # values rather than creating a second opportunity score formula.
        if hydrated.hydrated_tickers:
            deduped = _enrich_screening_rows(deduped, db=db)
        deduped = apply_mtf_confluence_flags(
            deduped,
            weekly_bullish_threshold=float(weekly_cfg.get("weekly_bullish_threshold", 0.65)),
            weekly_bearish_threshold=float(weekly_cfg.get("weekly_bearish_threshold", 0.35)),
        )
        if not weekly_cfg.get("confluence_demote_enabled", True):
            for row in deduped:
                row["weekly_counter_trend"] = False
        try:
            db.record_runtime_metric(
                "scan_all_weekly_hydrate_duration_seconds",
                weekly_hydrate_duration_ms / 1000.0,
                context={
                    "candidate_pool_size": hydrate_pool_size,
                    "hydrated_count": weekly_hydrated_count,
                    "insufficient_history_count": weekly_insufficient_count,
                },
            )
        except Exception:
            logger.debug("Could not record Scan All weekly hydrate metric", exc_info=True)

    sector_medians = _apply_sector_relative_scores(deduped)

    # Annotate each deduped row with within-batch percentile (rank within THIS
    # run's cohort), watchlist breadth, market_cap_tier, and sector-relative
    # delta. The UI already exposes an historical percentile (last 10 runs);
    # batch_percentile complements it so callers can see how the ticker stacks
    # up in today's universe specifically. The sector_relative_score shows how
    # a ticker compares vs. its sector median today — useful for isolating
    # "outperformers within a weak sector" even when the absolute opp score
    # looks mediocre.
    total_deduped = len(deduped) or 1
    for rank, r in enumerate(deduped):
        ticker = r["ticker"]
        # Rank 0 is the top → 100th percentile
        r["batch_percentile"] = round(
            (1.0 - rank / total_deduped) * 100, 1
        ) if total_deduped > 1 else 100.0
        wl_set = watchlists_per_ticker.get(ticker, set())
        r["watchlist_breadth"] = len(wl_set)
        # Expose the full list of watchlist names so the UI can show
        # "also in: S&P 500, NASDAQ 100" context for cross-list tickers.
        r["watchlists"] = sorted(wl_set)
        occ = occurrence_counts.get(ticker, 1)
        r["mean_opportunity_across_lists"] = round(
            opp_sum_per_ticker.get(ticker, 0.0) / max(1, occ), 1
        )
    deduped = _sort_scan_all_rows(deduped, sort)

    # Guarantee weekly MTF on every row we actually display. Pre-sort hydrate
    # (above) can miss names that move into the headline top-N only after overlay
    # re-scoring — e.g. LMRI at #1 with mtf=unknown while #2–#4 are aligned.
    if weekly_cfg.get("enabled", True) and weekly_cfg.get("ensure_display_hydrate", True) and deduped:
        stratified_top_k = max(3, top // 3)
        display_targets = collect_leaderboard_mtf_targets(
            deduped,
            top=top,
            stratified_top_k=stratified_top_k,
            headline_cfg=scan_all_cfg.get("headline") or {},
        )
        if display_targets:
            from time import monotonic as _monotonic

            backfill_started = _monotonic()
            backfill = hydrate_weekly_mtf(display_targets, cap=len(display_targets))
            weekly_hydrate_duration_ms += round((_monotonic() - backfill_started) * 1000)
            weekly_hydrated_count += len(backfill.hydrated_tickers)
            weekly_insufficient_count += len(backfill.insufficient_history_tickers)
            deduped = merge_hydrated_rows(deduped, backfill)
            if backfill.hydrated_tickers:
                deduped = _enrich_screening_rows(deduped, db=db)
            deduped = apply_mtf_confluence_flags(
                deduped,
                weekly_bullish_threshold=float(weekly_cfg.get("weekly_bullish_threshold", 0.65)),
                weekly_bearish_threshold=float(weekly_cfg.get("weekly_bearish_threshold", 0.35)),
            )
            if not weekly_cfg.get("confluence_demote_enabled", True):
                for row in deduped:
                    row["weekly_counter_trend"] = False
            sector_medians = _apply_sector_relative_scores(deduped)
            deduped = _sort_scan_all_rows(deduped, sort)

    if hide_counter_trend:
        deduped = [
            r for r in deduped
            if _row_passes_hide_filters(r, hide_counter_trend=True)
        ]
    watchlist_summaries = _merge_filtered_summaries(watchlist_summaries, deduped)
    deduped = _sort_scan_all_rows(deduped, sort)

    # Percentile is a rank attribute and must reflect the post-overlay/post-MTF
    # ordering above, not the pre-hydrate candidate order.
    total_deduped = len(deduped) or 1
    for rank, row in enumerate(deduped):
        row["batch_percentile"] = round(
            (1.0 - rank / total_deduped) * 100, 1
        ) if total_deduped > 1 else 100.0

    # Stratified top-N by market-cap tier. Prevents small-cap volatility
    # dominance in the headline "top opportunities" table when a single
    # Russell 2000 refresh floods the leaderboard with 60-opportunity-score
    # micro-caps. Each tier gets its own top_K bucket so operators can see
    # "best large-cap / best mid-cap / best small-cap" at a glance.
    stratified_buckets: Dict[str, List[dict]] = {
        "mega_large": [],
        "mid": [],
        "small_micro": [],
    }
    for r in deduped:
        tier = (r.get("market_cap_tier") or "unknown").lower()
        if tier in ("mega", "large"):
            stratified_buckets["mega_large"].append(r)
        elif tier == "mid":
            stratified_buckets["mid"].append(r)
        elif tier in ("small", "micro"):
            stratified_buckets["small_micro"].append(r)
        # "unknown" → not stratified; remains in top_opportunities global view
    stratified_top_k = max(3, top // 3)  # Roughly split the top budget 3 ways
    stratified = {
        band: rows[:stratified_top_k]
        for band, rows in stratified_buckets.items()
    }

    # Short-candidates = the lowest-opp tickers that are actually signalling
    # bearish/neutral conviction. This replaces the old "weakest_signals" which
    # naively took ``deduped[-N:]`` and produced nonsensical "bullish JNJ with
    # composite=4" rows in strong regimes. We still keep ``weakest_signals`` on
    # the response for backward compatibility (same payload), but we also
    # surface ``short_candidates`` (bearish only) and ``laggards`` (all
    # low-opp) so the UI can render them separately.
    non_bullish = [r for r in deduped if r.get("direction") in {"bearish", "neutral"}]
    short_candidates = [r for r in deduped if r.get("direction") == "bearish"]
    short_candidates_sorted = sorted(
        short_candidates, key=lambda x: x["opportunity_score"]
    )[:bottom]
    laggards_sorted = list(reversed(deduped[-bottom:])) if len(deduped) >= bottom else list(reversed(deduped))

    # Stats
    all_scores = [r["composite_score"] for r in deduped]
    all_opp = [r["opportunity_score"] for r in deduped if r.get("opportunity_score") is not None]
    all_macro = [r["macro_fit"] for r in deduped if r.get("macro_fit") is not None]
    all_eq = [r["entry_quality"] for r in deduped if r.get("entry_quality") is not None]

    direction_counts = {
        "bullish": sum(1 for r in deduped if r.get("direction") == "bullish"),
        "bearish": sum(1 for r in deduped if r.get("direction") == "bearish"),
        "neutral": sum(1 for r in deduped if r.get("direction") == "neutral"),
    }

    return {
        "lens": "opportunity",
        "batch_run_count": len(batch_runs),
        "single_run_overlays": single_run_overlays,
        "source_mix": selection["source_mix"],
        "batch_window": batch_window,
        "source_mode": batch_source_mode,
        "universe_requested": selection["universe_requested"],
        "universe_scanned": selection["universe_scanned"],
        "universe_truncated": selection["universe_truncated"],
        "book": book_key or "all",
        "sort": sort,
        "watchlist_summaries": sorted(watchlist_summaries, key=lambda x: x.get("avg_score") or 0, reverse=True),
        "top_opportunities": _headline_top_opportunities(deduped, top),
        "stratified_top_opportunities": stratified,
        "sector_medians": {k: round(v, 1) for k, v in sector_medians.items()},
        # Backward compatible alias: same list as laggards for clients that
        # still consume ``weakest_signals``. Prefer ``short_candidates`` or
        # ``laggards`` in new code.
        "weakest_signals": laggards_sorted,
        "laggards": laggards_sorted,
        "short_candidates": short_candidates_sorted,
        "totals": {
            "unique_tickers": len(deduped),
            "total_scored": len(deduped),
            "avg_score": round(sum(all_scores) / len(all_scores), 1) if all_scores else 0,
            "avg_opportunity_score": round(sum(all_opp) / len(all_opp), 1) if all_opp else None,
            "avg_macro_fit": round(sum(all_macro) / len(all_macro), 1) if all_macro else None,
            "avg_entry_quality": round(sum(all_eq) / len(all_eq), 1) if all_eq else None,
            "bullish_count": direction_counts["bullish"],
            "bearish_count": direction_counts["bearish"],
            "neutral_count": direction_counts["neutral"],
            "non_bullish_count": len(non_bullish),
            "weekly_hydrated_count": weekly_hydrated_count,
            "weekly_insufficient_history_count": weekly_insufficient_count,
            "weekly_hydrate_duration_ms": weekly_hydrate_duration_ms,
            "weekly_counter_trend_count": sum(
                1 for row in deduped if row.get("weekly_counter_trend")
            ),
        },
    }


def _normalize_tradingview_export_format(export_format: str) -> str:
    fmt = (export_format or "txt").strip().lower()
    if fmt not in {"txt", "csv"}:
        raise HTTPException(status_code=400, detail="format must be 'txt' or 'csv'")
    return fmt


def _annotate_with_tradingview_symbols(
    rows: List[Dict[str, Any]],
    *,
    db: Optional[ResearchDatabase] = None,
    strict: bool = False,
    online: bool = False,
    online_timeout_s: float = 1.0,
    max_metadata_refreshes: Optional[int] = None,
    hard_timeout_seconds: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Attach exchange + TradingView token fields to screening rows.

    - If `strict=True`, unresolved exchange resolutions yield empty `tv_symbol` (TXT import safety).
    - If `strict=False`, we preserve compatibility by allowing default exchange fallback in `tv_symbol`.
    """
    out: List[Dict[str, Any]] = []
    exchange_cache: Dict[str, Dict[str, Any]] = {}

    # Freshness budget: TradingView exchange metadata should be stable within the ticker info TTL window.
    # (We avoid pulling CacheConfig here to keep this hot path small.)
    freshness_seconds = 21600  # 6 hours
    # `ticker_exchange_resolution.updated_at` is stored as a naive ISO timestamp
    # using `datetime.now().isoformat()` (local time). Compare using local epoch
    # to avoid timezone-related freshness window mismatches.
    now_ts = datetime.now().timestamp()
    start_monotonic = _time.monotonic()
    total_rows = max(1, len(rows))
    if hard_timeout_seconds is None:
        # Scale timeout with export size while keeping an upper bound.
        hard_timeout_seconds = min(20.0, max(4.0, total_rows * 0.35))
    if max_metadata_refreshes is None:
        # Scale metadata refresh budget with list size.
        max_metadata_refreshes = min(1500, max(100, int(total_rows * 2.5)))
    metadata_refreshes = 0

    for row in rows:
        ticker = (row.get("ticker") or "").upper().strip()
        if not ticker:
            continue

        if ticker not in exchange_cache:
            persisted: Optional[Dict[str, Any]] = None
            if db:
                try:
                    persisted = db.get_ticker_exchange_resolution(ticker)
                except Exception:
                    persisted = None

            if persisted and persisted.get("updated_at"):
                try:
                    updated_at_ts = datetime.fromisoformat(str(persisted["updated_at"])).timestamp()
                except Exception:
                    updated_at_ts = None
                if updated_at_ts is not None and (now_ts - updated_at_ts) <= freshness_seconds:
                    exchange_cache[ticker] = {
                        "exchange": persisted.get("primary_exchange") or "",
                        "source": "db_cached",
                        "confidence": float(persisted.get("exchange_confidence") or 0.5),
                        "raw_exchange": persisted.get("raw_exchange"),
                        "source_timestamp": persisted.get("source_timestamp"),
                        "candidate_exchanges": [persisted.get("primary_exchange")] if persisted.get("primary_exchange") else [],
                    }
                else:
                    persisted = None

            if ticker not in exchange_cache:
                elapsed = _time.monotonic() - start_monotonic
                if elapsed > hard_timeout_seconds:
                    exchange_cache[ticker] = {
                        "exchange": "",
                        "source": "hard_timeout_budget_exceeded",
                        "confidence": 0.0,
                        "raw_exchange": None,
                        "candidate_exchanges": [],
                    }
                elif metadata_refreshes >= max_metadata_refreshes:
                    exchange_cache[ticker] = {
                        "exchange": "",
                        "source": "metadata_budget_exceeded",
                        "confidence": 0.0,
                        "raw_exchange": None,
                        "candidate_exchanges": [],
                    }
                else:
                    try:
                        info = _get_ticker_info_cached(ticker)
                    except Exception:
                        info = {}
                    metadata_refreshes += 1
                    exchange_cache[ticker] = resolve_exchange_from_info(ticker, info)

                # Persist deterministic resolution when possible.
                try:
                    if db and exchange_cache[ticker].get("exchange"):
                        db.save_ticker_exchange_resolution(
                            ticker=ticker,
                            primary_exchange=exchange_cache[ticker]["exchange"],
                            exchange_source=exchange_cache[ticker].get("source"),
                            exchange_confidence=exchange_cache[ticker].get("confidence"),
                            raw_exchange=exchange_cache[ticker].get("raw_exchange"),
                            source_timestamp=exchange_cache[ticker].get("source_timestamp") or datetime.now(timezone.utc).isoformat(),
                        )
                except Exception:
                    # Export must remain robust: persistence errors should not block exports.
                    pass

        resolution = exchange_cache[ticker]
        exchange = resolution.get("exchange") or ""
        resolution_status = "resolved" if exchange else "unresolved"
        resolution_reason = str(resolution.get("source") or "")
        resolution_confidence = float(resolution.get("confidence") or 0.0)

        candidate_symbols = []
        for ex in (resolution.get("candidate_exchanges") or []):
            if ex:
                candidate_symbols.append(f"{ex}:{ticker}")

        # Strict export path: if unresolved, return empty token.
        tv_symbol = tradingview_symbol_token(
            ticker=ticker,
            exchange=exchange or None,
            strict=strict,
        )

        # Optional online verification for strict TradingView TXT imports.
        # If the online check fails/times out, we fall back to offline-only results.
        if strict and online and tv_symbol:
            try:
                ok = validate_tradingview_token_online(tv_symbol, timeout_s=online_timeout_s)
                if not ok:
                    tv_symbol = ""
                    resolution_status = "unresolved"
                    resolution_reason = "online_invalid"
            except Exception:
                # Online verification must never hard-fail export.
                pass

        out.append(
            {
                **row,
                "ticker": ticker,
                "exchange": exchange,
                "tv_symbol": tv_symbol,
                "resolution_status": resolution_status,
                "resolution_reason": resolution_reason,
                "candidate_symbols": ",".join(candidate_symbols),
                "resolution_confidence": resolution_confidence,
            }
        )

    return out


def _retry_unresolved_tradingview_rows(
    rows: List[Dict[str, Any]],
    *,
    db: ResearchDatabase,
    strict: bool,
    online: bool,
    online_timeout_s: float,
) -> List[Dict[str, Any]]:
    """
    Retry unresolved symbols with a focused second pass.

    Strategy:
    1) prewarm only unresolved tickers via resolver cache flow
    2) rerun annotation on unresolved subset with larger per-item budget
    3) merge successful retries back in-place
    """
    unresolved_idx = [i for i, row in enumerate(rows) if not str(row.get("tv_symbol") or "").strip()]
    if not unresolved_idx:
        return rows

    try:
        from tradingagents.screening.ticker_resolver import resolve_and_cache

        unresolved_tickers = list(
            dict.fromkeys(
                str(rows[i].get("ticker") or "").upper().strip()
                for i in unresolved_idx
                if str(rows[i].get("ticker") or "").strip()
            )
        )
        if unresolved_tickers:
            resolve_and_cache(
                unresolved_tickers,
                db,
                max_workers=min(16, max(4, len(unresolved_tickers))),
                max_age_days=3,
            )
    except Exception:
        return rows

    retry_input = [{"ticker": rows[i].get("ticker")} for i in unresolved_idx]
    retry_rows = _annotate_with_tradingview_symbols(
        retry_input,
        db=db,
        strict=strict,
        online=online,
        online_timeout_s=online_timeout_s,
        max_metadata_refreshes=max(200, len(retry_input) * 4),
        hard_timeout_seconds=max(6.0, len(retry_input) * 0.5),
    )
    for idx, retry_row in zip(unresolved_idx, retry_rows):
        if str(retry_row.get("tv_symbol") or "").strip():
            rows[idx] = {**rows[idx], **retry_row}
    return rows


def _render_tradingview_companion_csv(rows: List[Dict[str, Any]]) -> str:
    """Render companion CSV for TradingView exports.

    The ``asset_class`` and ``risk_score`` columns were added with the
    universe-broadening pass so external workflows (spreadsheets, TV
    watchlists, Slack digests) can filter/group without a round-trip to
    the DB. They are optional on the source rows — if a caller passes a
    row dict that predates those fields we serialize empty strings.
    """
    buffer = StringIO()
    fieldnames = [
        "ticker",
        "exchange",
        "tv_symbol",
        "asset_class",
        "resolution_status",
        "resolution_reason",
        "candidate_symbols",
        "opportunity_score",
        "composite_score",
        "entry_quality",
        "risk_score",
        "direction",
        "watchlist_name",
        "run_id",
        "macro_fit",
        "rank",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "ticker": row.get("ticker", ""),
                "exchange": row.get("exchange", ""),
                "tv_symbol": row.get("tv_symbol", ""),
                "asset_class": row.get("asset_class", "") or "",
                "resolution_status": row.get("resolution_status", ""),
                "resolution_reason": row.get("resolution_reason", ""),
                "candidate_symbols": row.get("candidate_symbols", ""),
                "opportunity_score": row.get("opportunity_score"),
                "composite_score": row.get("composite_score"),
                "entry_quality": row.get("entry_quality"),
                "risk_score": row.get("risk_score"),
                "direction": row.get("direction", ""),
                "watchlist_name": row.get("watchlist_name", ""),
                "run_id": row.get("run_id"),
                "macro_fit": row.get("macro_fit"),
                "rank": row.get("rank"),
            }
        )
    return buffer.getvalue()


@app.get("/api/screen-all/latest/export/tradingview")
def export_scan_all_latest_tradingview(
    top: int = Query(20, ge=1, le=500),
    format: str = "txt",
    strict: Optional[bool] = None,
    include_unresolved_csv: bool = True,
    strategy: str = Query("standard", description="Lens: standard or reversal_buildup"),
    sort: str = Query("opportunity_score"),
    book: Optional[str] = Query(None),
    hide_illiquid: bool = Query(False),
    hide_blackout: bool = Query(False),
    hide_coverage: bool = Query(False),
    hide_counter_trend: bool = Query(False),
    db_path: str = "research.db",
    online: bool = False,
    online_timeout_s: float = 1.0,
):
    """Export top opportunities from latest scan-all batch for TradingView imports."""
    from tradingagents.screening.ticker_resolver import resolve_and_cache

    export_format = _normalize_tradingview_export_format(format)
    effective_strict = bool(strict) if strict is not None else (export_format == "txt")
    data = get_scan_all_consolidated(
        top=top,
        bottom=10,
        sort=sort,
        book=book,
        hide_illiquid=hide_illiquid,
        hide_blackout=hide_blackout,
        hide_coverage=hide_coverage,
        hide_counter_trend=hide_counter_trend,
        strategy=strategy,
        db_path=db_path,
    )
    db = get_db(db_path)
    export_rows = data.get("top_opportunities", [])
    if data.get("lens") == "reversal":
        from tradingagents.screening.reversal_buildup import rank_reversal_rows
        export_rows = rank_reversal_rows(
            list(data.get("long_setups") or []) + list(data.get("short_setups") or []),
            top_n=top,
        )
    # Prewarm metadata/exchange resolution in bulk to avoid per-row timeout drops.
    try:
        prewarm_tickers = [
            (r.get("ticker") or "").upper().strip()
            for r in export_rows[:top]
            if (r.get("ticker") or "").strip()
        ]
        if prewarm_tickers:
            resolve_and_cache(prewarm_tickers, db, max_workers=8, max_age_days=7)
    except Exception:
        # Export path should remain robust even if prewarm fails.
        pass

    top_rows = _annotate_with_tradingview_symbols(
        export_rows[:top],
        db=db,
        strict=effective_strict,
        online=bool(online),
        online_timeout_s=float(online_timeout_s),
    )
    if effective_strict:
        top_rows = _retry_unresolved_tradingview_rows(
            top_rows,
            db=db,
            strict=effective_strict,
            online=bool(online),
            online_timeout_s=float(online_timeout_s),
        )
    if not top_rows:
        raise HTTPException(status_code=404, detail="No scan-all opportunities available")

    resolved_count = sum(1 for r in top_rows if (r.get("tv_symbol") or "").strip())
    unresolved_count = len(top_rows) - resolved_count
    strict_applied = bool(effective_strict)

    source_breakdown: Dict[str, int] = {}
    unresolved_reasons: Dict[str, int] = {}
    for r in top_rows:
        reason = str(r.get("resolution_reason") or "").strip() or "unknown"
        source_breakdown[reason] = source_breakdown.get(reason, 0) + 1
        if not (r.get("tv_symbol") or "").strip():
            unresolved_reasons[reason] = unresolved_reasons.get(reason, 0) + 1

    confidence_histogram: Dict[str, int] = {"0-0.49": 0, "0.5-0.79": 0, "0.8-1.0": 0}
    for r in top_rows:
        c = float(r.get("resolution_confidence") or 0.0)
        if c >= 0.8:
            confidence_histogram["0.8-1.0"] += 1
        elif c >= 0.5:
            confidence_histogram["0.5-0.79"] += 1
        else:
            confidence_histogram["0-0.49"] += 1

    fallback_path_count = sum(
        1
        for r in top_rows
        if str(r.get("resolution_reason") or "").strip() and str(r.get("resolution_reason")) != "db_cached"
    )
    top_unresolved_reasons = sorted(unresolved_reasons.items(), key=lambda x: x[1], reverse=True)[:5]

    if export_format == "txt":
        content = tradingview_txt_content([r.get("tv_symbol", "") for r in top_rows])
        filename = f"scan_all_top_{top}_tradingview.txt"
        media_type = "text/plain"
    else:
        csv_rows = top_rows
        if not include_unresolved_csv:
            csv_rows = [r for r in top_rows if r.get("resolution_status") == "resolved"]
        content = _render_tradingview_companion_csv(csv_rows)
        filename = f"scan_all_top_{top}_tradingview.csv"
        media_type = "text/csv"

    logger.info(
        "TradingView export scan-all: format=%s strict_applied=%s resolved=%d unresolved=%d resolver_source_breakdown=%s confidence_histogram=%s fallback_path_count=%d top_unresolved_reasons=%s",
        export_format,
        strict_applied,
        resolved_count,
        unresolved_count,
        source_breakdown,
        confidence_histogram,
        fallback_path_count,
        top_unresolved_reasons,
    )

    return PlainTextResponse(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "resolved_count": str(resolved_count),
            "unresolved_count": str(unresolved_count),
            "strict_applied": str(strict_applied).lower(),
        },
    )


@app.get("/api/screen/runs/{run_id}/export/tradingview")
def export_screen_run_tradingview(
    run_id: int,
    top: int = Query(20, ge=1, le=500),
    format: str = "txt",
    strict: Optional[bool] = None,
    include_unresolved_csv: bool = True,
    db_path: str = "research.db",
    online: bool = False,
    online_timeout_s: float = 1.0,
):
    """Export top opportunities from a specific screening run for TradingView imports."""
    from tradingagents.screening.ticker_resolver import resolve_and_cache

    export_format = _normalize_tradingview_export_format(format)
    db = get_db(db_path)
    effective_strict = bool(strict) if strict is not None else (export_format == "txt")
    run_meta = db.get_screening_run(run_id)
    if not run_meta:
        raise HTTPException(status_code=404, detail="Screening run not found")

    results = db.get_screening_results(run_id)
    if not results:
        raise HTTPException(status_code=404, detail="Screening run has no results")

    watchlist_name = run_meta.get("watchlist_name", "Unknown")
    criteria = _parse_run_criteria(run_meta.get("criteria"))
    run_strategy = _run_strategy(run_meta)
    if run_strategy in _MULTI_SLEEVE_STRATEGIES:
        from tradingagents.screening.multi_sleeve import lift_multi_sleeve_fields, rank_multi_sleeve_rows
        ranked = rank_multi_sleeve_rows([lift_multi_sleeve_fields(r) for r in results])[:top]
    else:
        enriched_results = _enrich_screening_rows(
            results,
            db=db,
            preset=str(criteria.get("preset") or "default"),
        )
        if run_strategy == "reversal_buildup":
            from tradingagents.screening.reversal_buildup import rank_reversal_rows
            ranked = rank_reversal_rows(enriched_results, top_n=top)
        else:
            enriched_results.sort(key=lambda row: float(row.get("opportunity_score") or 0.0), reverse=True)
            ranked = enriched_results[:top]
    top_rows = [
        {
            "ticker": row.get("ticker"),
            "opportunity_score": row.get("opportunity_score"),
            "composite_score": row.get("composite_score"),
            "entry_quality": row.get("entry_quality"),
            "direction": row.get("direction", ""),
            "rank": row.get("rank"),
            "macro_fit": row.get("macro_fit"),
            # Expose asset_class + risk_score so the CSV companion can
            # carry the full v2-universe context into external workflows.
            "asset_class": row.get("asset_class"),
            "risk_score": row.get("risk_score"),
            "reversal_score": row.get("reversal_score"),
            "reversal_phase": row.get("reversal_phase"),
            "watchlist_name": watchlist_name,
            "run_id": run_id,
        }
        for row in ranked
    ]
    # Prewarm metadata/exchange resolution in bulk to avoid per-row timeout drops.
    try:
        prewarm_tickers = [str(r.get("ticker") or "").upper().strip() for r in top_rows if (r.get("ticker") or "").strip()]
        if prewarm_tickers:
            resolve_and_cache(prewarm_tickers, db, max_workers=8, max_age_days=7)
    except Exception:
        pass
    top_rows = _annotate_with_tradingview_symbols(
        top_rows,
        db=db,
        strict=effective_strict,
        online=bool(online),
        online_timeout_s=float(online_timeout_s),
    )
    if effective_strict:
        top_rows = _retry_unresolved_tradingview_rows(
            top_rows,
            db=db,
            strict=effective_strict,
            online=bool(online),
            online_timeout_s=float(online_timeout_s),
        )

    if export_format == "txt":
        content = tradingview_txt_content([r.get("tv_symbol", "") for r in top_rows])
        filename = f"run_{run_id}_top_{top}_tradingview.txt"
        media_type = "text/plain"
    else:
        csv_rows = top_rows
        if not include_unresolved_csv:
            csv_rows = [r for r in top_rows if r.get("resolution_status") == "resolved"]
        content = _render_tradingview_companion_csv(csv_rows)
        filename = f"run_{run_id}_top_{top}_tradingview.csv"
        media_type = "text/csv"

    resolved_count = sum(1 for r in top_rows if (r.get("tv_symbol") or "").strip())
    unresolved_count = len(top_rows) - resolved_count
    strict_applied = bool(effective_strict)

    source_breakdown: Dict[str, int] = {}
    unresolved_reasons: Dict[str, int] = {}
    for r in top_rows:
        reason = str(r.get("resolution_reason") or "").strip() or "unknown"
        source_breakdown[reason] = source_breakdown.get(reason, 0) + 1
        if not (r.get("tv_symbol") or "").strip():
            unresolved_reasons[reason] = unresolved_reasons.get(reason, 0) + 1

    confidence_histogram: Dict[str, int] = {"0-0.49": 0, "0.5-0.79": 0, "0.8-1.0": 0}
    for r in top_rows:
        c = float(r.get("resolution_confidence") or 0.0)
        if c >= 0.8:
            confidence_histogram["0.8-1.0"] += 1
        elif c >= 0.5:
            confidence_histogram["0.5-0.79"] += 1
        else:
            confidence_histogram["0-0.49"] += 1

    fallback_path_count = sum(
        1
        for r in top_rows
        if str(r.get("resolution_reason") or "").strip() and str(r.get("resolution_reason")) != "db_cached"
    )
    top_unresolved_reasons = sorted(unresolved_reasons.items(), key=lambda x: x[1], reverse=True)[:5]

    logger.info(
        "TradingView export run: run_id=%s format=%s strict_applied=%s resolved=%d unresolved=%d resolver_source_breakdown=%s confidence_histogram=%s fallback_path_count=%d top_unresolved_reasons=%s",
        run_id,
        export_format,
        strict_applied,
        resolved_count,
        unresolved_count,
        source_breakdown,
        confidence_histogram,
        fallback_path_count,
        top_unresolved_reasons,
    )

    return PlainTextResponse(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "resolved_count": str(resolved_count),
            "unresolved_count": str(unresolved_count),
            "strict_applied": str(strict_applied).lower(),
        },
    )


@app.get("/api/screen/runs")
def list_screening_runs(limit: int = 20, db_path: str = "research.db") -> List[Dict[str, object]]:
    """List past screening runs."""
    db = get_db(db_path)
    runs = db.get_screening_runs(limit=limit)
    return runs


@app.get("/api/screen/runs/{run_id}")
def get_screening_run_results(run_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Get results for a specific screening run."""
    db = get_db(db_path)
    run_meta = db.get_screening_run(run_id)
    results = db.get_screening_results(run_id)
    if not results:
        raise HTTPException(status_code=404, detail="Screening run not found or has no results")

    # Parse JSON text fields
    for r in results:
        if isinstance(r.get("signals"), str):
            try:
                r["signals"] = json.loads(r["signals"])
            except (json.JSONDecodeError, TypeError):
                r["signals"] = {}
        if isinstance(r.get("signal_deltas"), str):
            try:
                r["signal_deltas"] = json.loads(r["signal_deltas"])
            except (json.JSONDecodeError, TypeError):
                r["signal_deltas"] = {}
        # macro_breakdown and entry_quality_signals are already deserialized by the DB layer

        # factor_scorecard is embedded in signals under a private key at save time.
        # Lift it to the top level so the expand-row UI can read it directly
        # without needing to know the storage implementation detail.
        # Use `is not None` (not `or {}`) — an empty dict is falsy in Python and
        # would produce a throwaway copy rather than a reference to r["signals"].
        signals = r.get("signals") if r.get("signals") is not None else {}
        raw_fs = signals.pop("_factor_scorecard", None)
        if raw_fs is not None:
            if isinstance(raw_fs, str):
                try:
                    r["factor_scorecard"] = json.loads(raw_fs)
                except (json.JSONDecodeError, TypeError):
                    r["factor_scorecard"] = None
            else:
                r["factor_scorecard"] = raw_fs
        else:
            r.setdefault("factor_scorecard", None)

    criteria = _parse_run_criteria((run_meta or {}).get("criteria"))
    strategy = _run_strategy(run_meta)
    if strategy in _MOMENTUM_BATCH_STRATEGIES:
        from tradingagents.screening.early_momentum import lift_momentum_fields, rank_momentum_rows
        results = rank_momentum_rows([lift_momentum_fields(r) for r in results])
    elif strategy in _BASE_BATCH_STRATEGIES:
        from tradingagents.screening.base_coil import lift_base_fields, rank_base_rows
        results = rank_base_rows([lift_base_fields(r) for r in results])
    elif strategy in _MULTI_SLEEVE_STRATEGIES:
        from tradingagents.screening.multi_sleeve import lift_multi_sleeve_fields, rank_multi_sleeve_rows
        results = rank_multi_sleeve_rows([lift_multi_sleeve_fields(r) for r in results])
    else:
        results = _enrich_screening_rows(
            results,
            db=db,
            preset=str(criteria.get("preset") or "default"),
        )
    return {
        "run_id": run_id,
        "count": len(results),
        "results": results,
        "strategy": strategy or "standard",
    }


@app.post("/api/screen/runs/{run_id}/analyze")
def analyze_top_n_from_screening(run_id: int, body: AnalyzeTopNRequest) -> Dict[str, object]:
    """Take top N tickers from a screening run and submit to batch analysis."""
    db = get_db(body.db_path)
    results = db.get_screening_results(run_id)
    if not results:
        raise HTTPException(status_code=404, detail="Screening run not found")

    # Use the provided tickers (UI sends pre-selected ones)
    tickers = [t.upper().strip() for t in body.tickers if t.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="No tickers provided")

    # Resolve investment profile — explicit param takes priority, otherwise
    # auto-resolve from the watchlist associated with this screening run.
    inv_profile_key = body.investment_profile
    if not inv_profile_key:
        # Try to auto-resolve from the screening run's watchlist
        try:
            run_meta = db.get_screening_run(run_id) or {}
            wl_id = run_meta.get("watchlist_id")
            if wl_id:
                wl = db.get_watchlist(wl_id)
                if wl:
                    inv_profile_key = wl.get("default_investment_profile")
        except Exception as e:
            logger.debug("Auto-resolve investment profile from screening run failed: %s", e)
    inv_profile = _resolve_investment_profile(inv_profile_key)

    from tradingagents.screening.context_packet import build_context_packet, format_screening_context

    run_meta = db.get_screening_run(run_id) or {}
    preset_name = run_meta.get("criteria")
    try:
        crit = json.loads(preset_name) if isinstance(preset_name, str) else {}
        preset_name = crit.get("preset") or "default"
    except Exception:
        preset_name = "default"
    rows_by_ticker = {str(r.get("ticker", "")).upper(): r for r in results}
    screening_context_map: Dict[str, dict] = {}
    for t in tickers:
        row = rows_by_ticker.get(t)
        if not row:
            continue
        opp = _compute_opportunity_score(
            row.get("composite_score"),
            row.get("entry_quality"),
            row.get("macro_fit"),
            composite_fundamental=row.get("composite_fundamental"),
        )
        enriched = dict(row)
        enriched["opportunity_score"] = opp
        pkt = build_context_packet(enriched, run_id=run_id, preset=str(preset_name))
        pkt = dict(pkt)
        pkt["summary_text"] = format_screening_context(pkt)
        screening_context_map[t] = pkt

    def _run_batch(degraded: bool = False):
        from tradingagents.research import ResearchAgent
        config = get_config_for_mode(body.mode)
        if degraded:
            config = _apply_degraded_token_profile(config)
        config["risk_profile"] = body.risk_profile
        agent = ResearchAgent(
            config=config,
            output_dir=body.output_dir,
            db_path=body.db_path,
            auto_report=True,
            auto_save=True,
            debug=False,
        )
        return agent.batch_analyze(
            tickers=tickers,
            date=body.date,
            delay_seconds=body.delay,
            investment_profile=inv_profile,
            screening_run_id=run_id,
            screening_context=screening_context_map,
        )

    metadata = {
        "job_type": "batch",
        "tickers": ",".join(tickers),
        "mode": body.mode,
        "risk_profile": body.risk_profile,
        "investment_profile": inv_profile_key or "default",
        "screening_run_id": run_id,
    }
    job_id = _submit_job(
        _run_batch,
        metadata,
        _degraded_retry_fn=lambda: _run_batch(degraded=True),
    )
    return {"job_id": job_id, "tickers": tickers, "screening_run_id": run_id}


# =============================================================================
# WATCHLIST API ENDPOINTS
# =============================================================================


@app.get("/api/watchlists")
def list_watchlists(db_path: str = "research.db") -> List[Dict[str, object]]:
    """List all watchlists."""
    db = get_db(db_path)
    watchlists = db.get_watchlists()
    for w in watchlists:
        tickers = w.get("tickers", "")
        valid, _rejected = _parse_watchlist_ticker_payload(tickers, reject_invalid=False)
        w["ticker_count"] = len(valid)
    return watchlists


@app.get("/api/watchlists/universe/metrics")
def get_watchlist_universe_metrics(db_path: str = "research.db") -> Dict[str, object]:
    """Return watchlist overlap/truncation/hydration baselines for QA gates."""
    db = get_db(db_path)
    metrics = db.compute_watchlist_universe_metrics()
    p95_window_days = int(DEFAULT_CONFIG.get("screening", {}).get("p95_window_days", 7))
    if hasattr(db, "get_runtime_metric_p95"):
        measured_scan_all_p95 = db.get_runtime_metric_p95(
            "scan_all_duration_seconds", since_days=p95_window_days
        )
        measured_expanded_p95 = db.get_runtime_metric_p95(
            "expanded_scan_duration_seconds", since_days=p95_window_days
        )
    else:
        measured_scan_all_p95 = None
        measured_expanded_p95 = None
    budgets = {
        "scan_all_p95_sec_budget": 300,
        "expanded_scan_p95_sec_budget": 420,
        "hydration_cap_utilization_min": 0.80,
        "parity_churn_max": 0.10,
    }
    metrics["measured"] = {
        "scan_all_p95_sec": measured_scan_all_p95,
        "expanded_scan_p95_sec": measured_expanded_p95,
        "p95_window_days": p95_window_days,
    }
    metrics["qa_budgets"] = budgets

    # Refresh validator health — tier split + null-cap/invalid/format-reject counts
    # from the most recent builtin_refresh run. Surfacing these makes it obvious
    # when curation regresses (e.g. Finnhub/AV outage) without crawling logs.
    try:
        with db._connect() as conn:
            row = conn.execute(
                """
                SELECT duration_seconds, context_json, created_at FROM runtime_metrics
                WHERE metric_key = 'builtin_refresh_invalid_ratio'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        refresh_health: Dict[str, Any] = {"available": False}
        if row:
            try:
                ctx = json.loads(row["context_json"] or "{}")
            except Exception:
                ctx = {}
            refresh_health = {
                "available": True,
                "recorded_at": row["created_at"],
                "invalid_ratio": round(float(row["duration_seconds"] or 0.0), 4),
                "tier1_validated": int(ctx.get("tier1_validated") or 0),
                "tier2_validated": int(ctx.get("tier2_validated") or 0),
                "tier3_validated": int(ctx.get("tier3_validated") or 0),
                "format_rejected": int(ctx.get("format_rejected") or 0),
                "total_validated": int(ctx.get("total_validated") or 0),
                "total_invalid": int(ctx.get("total_invalid") or 0),
            }
        metrics["refresh_health"] = refresh_health
    except Exception as exc:
        metrics["refresh_health"] = {"available": False, "error": str(exc)}

    # Ticker health — eligible for eviction today + recently-evicted count (24h)
    try:
        stale = db.get_stale_ticker_candidates(min_failures=3, min_days_since_success=2)
        with db._connect() as conn:
            evicted_24h_row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM ticker_health
                WHERE evicted_at >= datetime('now', '-1 day')
                """
            ).fetchone()
        metrics["ticker_health"] = {
            "stale_candidates": len(stale),
            "evicted_last_24h": int(evicted_24h_row["c"] if evicted_24h_row else 0),
            "thresholds": {"min_failures": 3, "min_days_since_success": 2},
        }
    except Exception as exc:
        metrics["ticker_health"] = {"error": str(exc)}

    return metrics


@app.get("/api/watchlists/migration/precheck")
def watchlist_migration_precheck(db_path: str = "research.db") -> Dict[str, object]:
    """Migration safety checks for clean-restart + legacy alias compatibility."""
    db = get_db(db_path)
    all_watchlists = db.get_watchlists()
    aliases_path = Path("docs/migration/watchlist_aliases_v2.json")
    alias_rows: Dict[str, Dict[str, str]] = {}
    if aliases_path.exists():
        try:
            alias_rows = json.loads(aliases_path.read_text(encoding="utf-8"))
        except Exception:
            alias_rows = {}
    legacy_aliases = {
        old: str((payload or {}).get("target_watchlist_name") or "")
        for old, payload in alias_rows.items()
        if str((payload or {}).get("target_watchlist_name") or "").strip()
    }
    if not legacy_aliases:
        legacy_aliases = {
            "S&P 500 Top 100": "S&P 500",
            "Russell 2000 Top 100": "Russell 2000",
        }
    by_name = {str(w.get("name") or ""): w for w in all_watchlists}
    collisions = [
        {"legacy": old, "target": new}
        for old, new in legacy_aliases.items()
        if old in by_name and new in by_name
    ]
    scan_all_running = False
    with _jobs_lock:
        for job in _jobs.values():
            if str(job.get("job_type") or "") == "screen_all" and str(job.get("status") or "") in {"queued", "running"}:
                scan_all_running = True
                break
    return {
        "ok": not collisions and not scan_all_running,
        "collisions": collisions,
        "scan_all_running": scan_all_running,
        "legacy_aliases": legacy_aliases,
        "legacy_alias_details": alias_rows,
        "watchlist_count": len(all_watchlists),
    }


@app.get("/api/watchlists/parity-diff")
def get_watchlist_parity_diff(
    left: str = Query(..., min_length=1),
    right: str = Query(..., min_length=1),
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Compare two built-in watchlists and return parity/churn diagnostics."""
    db = get_db(db_path)
    return db.watchlist_parity_diff(left_name=left, right_name=right)


@app.post("/api/watchlists")
def create_or_update_watchlist(body: WatchlistRequest, db_path: str = "research.db") -> Dict[str, object]:
    """Create a new watchlist."""
    db = get_db(db_path)
    valid, _rejected = _parse_watchlist_ticker_payload(body.tickers, reject_invalid=True)
    tickers_csv = ",".join(valid)
    wid = db.create_watchlist(
        name=body.name, description=body.description, tickers=tickers_csv,
        default_preset=body.default_preset, default_investment_profile=body.default_investment_profile,
    )
    # Auto-resolve metadata for new tickers in background
    _resolve_metadata_background(valid, db_path)
    return {"id": wid, "name": body.name}


@app.put("/api/watchlists/{watchlist_id}")
def update_watchlist(watchlist_id: int, body: WatchlistRequest, db_path: str = "research.db") -> Dict[str, object]:
    """Update an existing watchlist."""
    db = get_db(db_path)
    valid, _rejected = _parse_watchlist_ticker_payload(body.tickers, reject_invalid=True)
    tickers_csv = ",".join(valid)
    success = db.update_watchlist(
        watchlist_id, name=body.name, description=body.description, tickers=tickers_csv,
        default_preset=body.default_preset, default_investment_profile=body.default_investment_profile,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    # Auto-resolve metadata for updated tickers in background
    _resolve_metadata_background(valid, db_path)
    return {"id": watchlist_id, "name": body.name}


@app.post("/api/watchlists/{watchlist_id}/clone")
def clone_watchlist(watchlist_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Clone any watchlist (including built-in) as a new user watchlist."""
    db = get_db(db_path)
    source = db.get_watchlist(watchlist_id)
    if not source:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    new_name = f"{source['name']} (Copy)"
    # Ensure unique name
    existing = {w["name"] for w in db.get_watchlists()}
    counter = 1
    while new_name in existing:
        counter += 1
        new_name = f"{source['name']} (Copy {counter})"
    wid = db.create_watchlist(
        name=new_name,
        description=source.get("description") or "",
        tickers=source.get("tickers") or "",
        default_preset=source.get("default_preset"),
        default_investment_profile=source.get("default_investment_profile"),
    )
    # Auto-resolve metadata for cloned tickers in background
    _resolve_metadata_background([t.strip() for t in (source.get("tickers") or "").split(",") if t.strip()], db_path)
    return {"id": wid, "name": new_name, "cloned_from": source["name"]}


@app.put("/api/watchlists/{watchlist_id}/add-ticker")
def add_ticker_to_watchlist(watchlist_id: int, body: TickerModRequest, db_path: str = "research.db") -> Dict[str, object]:
    """Add a single ticker to a watchlist."""
    db = get_db(db_path)
    wl = db.get_watchlist(watchlist_id)
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    if wl.get("source") == "built-in":
        raise HTTPException(status_code=403, detail="Cannot modify built-in watchlist. Clone it first.")
    ticker_valid, _rejected = _parse_watchlist_ticker_payload(body.ticker, reject_invalid=True)
    ticker = ticker_valid[0] if ticker_valid else ""
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker is required")
    existing, _ = _parse_watchlist_ticker_payload(wl.get("tickers") or "", reject_invalid=False)
    if ticker in existing:
        return {"id": watchlist_id, "ticker": ticker, "action": "already_exists", "ticker_count": len(existing)}
    existing.append(ticker)
    db.update_watchlist(watchlist_id, tickers=",".join(existing))
    # Auto-resolve metadata for the new ticker in background
    _resolve_metadata_background([ticker], db_path)
    return {"id": watchlist_id, "ticker": ticker, "action": "added", "ticker_count": len(existing)}


@app.put("/api/watchlists/{watchlist_id}/remove-ticker")
def remove_ticker_from_watchlist(watchlist_id: int, body: TickerModRequest, db_path: str = "research.db") -> Dict[str, object]:
    """Remove a single ticker from a watchlist."""
    db = get_db(db_path)
    wl = db.get_watchlist(watchlist_id)
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    if wl.get("source") == "built-in":
        raise HTTPException(status_code=403, detail="Cannot modify built-in watchlist. Clone it first.")
    ticker = body.ticker.upper().strip()
    existing = [t.strip() for t in (wl.get("tickers") or "").split(",") if t.strip()]
    if ticker not in existing:
        return {"id": watchlist_id, "ticker": ticker, "action": "not_found", "ticker_count": len(existing)}
    existing.remove(ticker)
    db.update_watchlist(watchlist_id, tickers=",".join(existing))
    return {"id": watchlist_id, "ticker": ticker, "action": "removed", "ticker_count": len(existing)}


@app.get("/api/watchlists/{watchlist_id}/export")
def export_watchlist(
    watchlist_id: int,
    db_path: str = "research.db",
    format: str = "plain_csv",
    strict: Optional[bool] = None,
    online: bool = False,
    online_timeout_s: float = 1.0,
):
    """Export watchlist tickers in one of several TradingView-friendly formats."""
    from fastapi.responses import PlainTextResponse
    from tradingagents.screening.ticker_resolver import resolve_and_cache
    db = get_db(db_path)
    wl = db.get_watchlist(watchlist_id)
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    tickers = [t.strip() for t in (wl.get("tickers") or "").split(",") if t.strip()]
    name_slug = wl["name"].replace(" ", "_").lower()

    fmt = (format or "plain_csv").strip().lower()
    if fmt not in {"plain_csv", "tradingview_txt", "tradingview_csv"}:
        raise HTTPException(status_code=400, detail="format must be plain_csv|tradingview_txt|tradingview_csv")

    effective_strict: bool = bool(strict) if strict is not None else (fmt == "tradingview_txt")

    if fmt == "plain_csv":
        csv_content = "Ticker\n" + "\n".join(tickers)
        return PlainTextResponse(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name_slug}_tickers.csv"'},
        )

    # Prewarm metadata/exchange resolution in bulk for TradingView exports.
    try:
        if tickers:
            resolve_and_cache(tickers, db, max_workers=8, max_age_days=7)
    except Exception:
        pass

    # TradingView formats: resolve primary exchange and emit EXCHANGE:TICKER tokens.
    annotated = _annotate_with_tradingview_symbols(
        [{"ticker": t} for t in tickers],
        db=db,
        strict=effective_strict,
        online=bool(online),
        online_timeout_s=float(online_timeout_s),
    )
    if effective_strict:
        annotated = _retry_unresolved_tradingview_rows(
            annotated,
            db=db,
            strict=effective_strict,
            online=bool(online),
            online_timeout_s=float(online_timeout_s),
        )
    tv_symbols = [r.get("tv_symbol", "") for r in annotated]

    resolved_count = sum(1 for t in tv_symbols if (t or "").strip())
    unresolved_count = len(tv_symbols) - resolved_count
    strict_applied = bool(effective_strict)

    source_breakdown: Dict[str, int] = {}
    unresolved_reasons: Dict[str, int] = {}
    for r in annotated:
        reason = str(r.get("resolution_reason") or "").strip() or "unknown"
        source_breakdown[reason] = source_breakdown.get(reason, 0) + 1
        if not (r.get("tv_symbol") or "").strip():
            unresolved_reasons[reason] = unresolved_reasons.get(reason, 0) + 1

    confidence_histogram: Dict[str, int] = {"0-0.49": 0, "0.5-0.79": 0, "0.8-1.0": 0}
    for r in annotated:
        c = float(r.get("resolution_confidence") or 0.0)
        if c >= 0.8:
            confidence_histogram["0.8-1.0"] += 1
        elif c >= 0.5:
            confidence_histogram["0.5-0.79"] += 1
        else:
            confidence_histogram["0-0.49"] += 1

    fallback_path_count = sum(
        1
        for r in annotated
        if str(r.get("resolution_reason") or "").strip() and str(r.get("resolution_reason")) != "db_cached"
    )
    top_unresolved_reasons = sorted(unresolved_reasons.items(), key=lambda x: x[1], reverse=True)[:5]

    logger.info(
        "TradingView export watchlist: watchlist_id=%s format=%s strict_applied=%s resolved=%d unresolved=%d resolver_source_breakdown=%s confidence_histogram=%s fallback_path_count=%d top_unresolved_reasons=%s",
        watchlist_id,
        fmt,
        strict_applied,
        resolved_count,
        unresolved_count,
        source_breakdown,
        confidence_histogram,
        fallback_path_count,
        top_unresolved_reasons,
    )

    if fmt == "tradingview_txt":
        content = tradingview_txt_content(tv_symbols)
        return PlainTextResponse(
            content=content,
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{name_slug}_tradingview.txt"',
                "resolved_count": str(resolved_count),
                "unresolved_count": str(unresolved_count),
                "strict_applied": str(strict_applied).lower(),
            },
        )

    # tradingview_csv: include unresolved diagnostics contract while preserving TradingView-friendly first column.
    buffer = StringIO()
    fieldnames = ["Ticker", "resolution_status", "resolution_reason", "candidate_symbols"]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for r in annotated:
        writer.writerow(
            {
                "Ticker": r.get("tv_symbol", "") or "",
                "resolution_status": r.get("resolution_status", ""),
                "resolution_reason": r.get("resolution_reason", ""),
                "candidate_symbols": r.get("candidate_symbols", ""),
            }
        )
    return PlainTextResponse(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{name_slug}_tradingview.csv"',
            "resolved_count": str(resolved_count),
            "unresolved_count": str(unresolved_count),
            "strict_applied": str(strict_applied).lower(),
        },
    )


@app.delete("/api/watchlists/{watchlist_id}")
def delete_watchlist(watchlist_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Delete a watchlist (built-in watchlists cannot be deleted)."""
    db = get_db(db_path)
    wl = db.get_watchlist(watchlist_id)
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    if wl.get("source") == "built-in":
        raise HTTPException(status_code=403, detail="Cannot delete built-in watchlist.")
    success = db.delete_watchlist(watchlist_id)
    if not success:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return {"deleted": True, "id": watchlist_id}


@app.post("/api/watchlists/{watchlist_id}/pin")
def pin_auto_watchlist(watchlist_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Pin an auto-watchlist, converting it to a user-owned watchlist."""
    db = get_db(db_path)
    wl = db.get_watchlist(watchlist_id)
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    if wl.get("source") != "auto":
        raise HTTPException(status_code=400, detail="Only auto-generated watchlists can be pinned.")
    success = db.pin_auto_watchlist(watchlist_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to pin watchlist")
    return {"pinned": True, "id": watchlist_id}


class WatchlistImportRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    tickers_raw: str = Field(..., min_length=1)  # CSV, newline, or comma-separated
    default_preset: Optional[str] = None
    default_investment_profile: Optional[str] = None


@app.post("/api/watchlists/import")
def import_watchlist(body: WatchlistImportRequest, db_path: str = "research.db") -> Dict[str, object]:
    """Import a watchlist from raw ticker text (CSV, newline, comma, tab, or space separated).

    Parses common formats, deduplicates, uppercases, and creates a new watchlist.
    Auto-resolves metadata for imported tickers in background.
    """
    import re
    # Parse tickers from raw text — handle CSV, newlines, tabs, commas, spaces, semicolons
    raw = body.tickers_raw.strip()
    # Split on any common delimiter
    tokens = re.split(r'[,\n\r\t;|]+', raw)
    # Clean each token: strip whitespace, remove quotes, skip header-like values
    skip_headers = {"ticker", "symbol", "stock", "name", "tickers", "symbols", ""}
    tickers = []
    seen = set()
    for token in tokens:
        t = token.strip().strip('"').strip("'").upper()
        # Basic ticker validation: 1-10 chars, alphanumeric + dots/dashes
        if t and t.lower() not in skip_headers and len(t) <= 10 and re.match(r'^[A-Z0-9.\-]+$', t):
            if t not in seen:
                seen.add(t)
                tickers.append(t)

    if not tickers:
        raise HTTPException(status_code=400, detail="No valid tickers found in input")

    db = get_db(db_path)
    tickers_str = ",".join(tickers)
    wid = db.create_watchlist(
        name=body.name, description=body.description, tickers=tickers_str,
        default_preset=body.default_preset, default_investment_profile=body.default_investment_profile,
    )
    # Auto-resolve metadata in background
    _resolve_metadata_background(tickers, db_path)
    return {
        "id": wid,
        "name": body.name,
        "ticker_count": len(tickers),
        "tickers": tickers,
        "parsed_from": len(tokens),
        "deduplicated": len(tokens) - len(tickers) if len(tokens) > len(tickers) else 0,
    }


@app.post("/api/watchlists/builtins/refresh/propose")
def propose_builtin_refresh(db_path: str = "research.db") -> Dict[str, object]:
    """Build a proposal for refreshing built-in watchlists (manual apply only)."""
    from tradingagents.screening.builtin_refresh import build_refresh_proposal

    db = get_db(db_path)
    proposal = build_refresh_proposal(db=db, config=DEFAULT_CONFIG, source="manual_api")
    _screening_scheduler._last_builtin_refresh_proposal_at = datetime.now(timezone.utc).isoformat()
    return proposal


@app.get("/api/watchlists/builtins/refresh/proposals")
def list_builtin_refresh_proposals(
    limit: int = Query(20, ge=1, le=200),
    db_path: str = "research.db",
) -> List[Dict[str, object]]:
    """List recent built-in refresh proposals."""
    db = get_db(db_path)
    return db.list_builtin_refresh_proposals(limit=limit)


@app.get("/api/watchlists/builtins/refresh/proposals/{proposal_id}")
def get_builtin_refresh_proposal(proposal_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Get full details for a built-in refresh proposal."""
    db = get_db(db_path)
    proposal = db.get_builtin_refresh_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal


@app.post("/api/watchlists/builtins/refresh/proposals/{proposal_id}/apply")
def apply_builtin_refresh_proposal(
    proposal_id: int,
    body: Optional[BuiltinRefreshApplyRequest] = None,
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Apply a previously generated built-in refresh proposal atomically."""
    db = get_db(db_path)
    policy = (
        DEFAULT_CONFIG.get("screening", {})
        .get("scheduler", {})
        .get("builtin_refresh", {})
    )
    force_apply = bool(body.force_apply) if body else False
    force_reason = (body.force_reason if body else "") or ""
    result = db.apply_builtin_refresh_proposal(
        proposal_id,
        force_apply=force_apply,
        force_reason=force_reason,
        guard_config=policy,
    )
    if result.get("message") == "Proposal not found":
        raise HTTPException(status_code=404, detail="Proposal not found")
    if result.get("message") == "Force apply reason required":
        raise HTTPException(status_code=400, detail="Force apply reason required")
    if result.get("requires_force_apply"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": result.get("message", "Force apply required"),
                "risk_items": result.get("risk_items", []),
                "proposal_id": proposal_id,
            },
        )
    return {
        "result": result,
        "proposal": db.get_builtin_refresh_proposal(proposal_id),
    }


@app.post("/api/watchlists/builtins/deprecate/apply")
def apply_builtin_deprecations(db_path: str = "research.db") -> Dict[str, object]:
    """Hard-delete deprecated built-ins (idempotent)."""
    deprecated = [
        "Dow Jones 30",
        "ARK Innovation",
        "Recent IPOs",
        "High Short Interest",
        "Russell 2000 Top 50",
        "Dividend Aristocrats",
    ]
    db = get_db(db_path)
    result = db.apply_builtin_deprecations(deprecated)
    removed = int(result.get("removed", 0))
    if removed == 0:
        return {"removed": 0, "message": "No deprecated watchlists found", "removed_names": []}
    return {
        "removed": removed,
        "message": "Deprecated watchlists removed",
        "removed_names": result.get("removed_names", []),
    }


@app.get("/api/screening/presets")
def get_screening_presets() -> Dict[str, object]:
    """Get available screening presets from config."""
    presets = DEFAULT_CONFIG.get("screening", {}).get("presets", {})
    return {
        name: {
            "description": p.get("description", ""),
            "weights": p.get("weights", {}),
            "operator_visible": bool(p.get("operator_visible", True)),
        }
        for name, p in presets.items()
    }


@app.get("/api/investment-profiles")
def get_investment_profiles() -> Dict[str, object]:
    """Get available investment profiles from config."""
    profiles = DEFAULT_CONFIG.get("investment_profiles", {})
    return {
        key: {
            "display_name": p.get("display_name", key),
            "company_context": p.get("company_context", ""),
        }
        for key, p in profiles.items()
    }


@app.get("/api/screening/default-weights")
def get_screening_default_weights() -> Dict[str, float]:
    """Get the default signal weights from config."""
    return DEFAULT_CONFIG.get("screening", {}).get("signal_weights", {})


@app.get("/api/screening/adaptive-weights")
def get_adaptive_weights(db_path: str = "research.db") -> Dict[str, object]:
    """Compute adaptive signal weights from historical backtested results."""
    try:
        from tradingagents.screening.engine import ScreeningEngine
        db = get_db(db_path)
        engine = ScreeningEngine(config=DEFAULT_CONFIG, db=db)
        weights = engine.compute_adaptive_weights(min_samples=30)
        if weights is None:
            return {"available": False, "reason": "Insufficient backtested screening data", "weights": {}, "sample_count": 0}
        sample_count = 0
        if hasattr(db, "get_backtested_screening_results"):
            sample_count = len(db.get_backtested_screening_results(limit=2000))
        return {
            "available": True,
            "weights": weights,
            "sample_count": sample_count,
            "thin_sample": sample_count < 80,
        }
    except Exception as e:
        return {"available": False, "reason": f"Error computing adaptive weights: {e}", "weights": {}}


@app.get("/api/screening/signal-performance")
def get_signal_performance(db_path: str = "research.db") -> Dict[str, object]:
    """Get per-signal hit rates and correlations with forward returns.

    If no cached data exists, triggers a fresh computation.
    Uses a 10-minute in-memory response cache to avoid repeated DB + compute work.
    """
    cache_key = f"signal_perf:{db_path}"
    hit = _cached_response(cache_key)
    if hit is not None and hit.get("available"):
        return hit

    db = get_db(db_path)
    cached = db.get_signal_performance()
    if cached:
        # Group by signal name for easier UI consumption
        by_signal: Dict[str, Dict] = {}
        for row in cached:
            sig = row["signal_name"]
            if sig not in by_signal:
                by_signal[sig] = {"signal": sig, "periods": {}}
            by_signal[sig]["periods"][row["period"]] = {
                "hit_rate": row["hit_rate"],
                "avg_return": row["avg_return"],
                "correlation": row["correlation"],
                "sample_count": row["sample_count"],
            }
            by_signal[sig]["last_updated"] = row["last_updated"]
        result = {"available": True, "signals": by_signal}
        _set_response_cache(cache_key, result)
        return result

    # Try computing fresh
    try:
        from tradingagents.screening.engine import ScreeningEngine
        engine = ScreeningEngine(config=DEFAULT_CONFIG, db=db)
        perf = engine.compute_signal_performance(min_samples=30)
        if not perf:
            return {"available": False, "reason": "Insufficient backtested data", "signals": {}}
        # Re-read from DB for consistent format
        cached = db.get_signal_performance()
        by_signal = {}
        for row in cached:
            sig = row["signal_name"]
            if sig not in by_signal:
                by_signal[sig] = {"signal": sig, "periods": {}}
            by_signal[sig]["periods"][row["period"]] = {
                "hit_rate": row["hit_rate"],
                "avg_return": row["avg_return"],
                "correlation": row["correlation"],
                "sample_count": row["sample_count"],
            }
            by_signal[sig]["last_updated"] = row["last_updated"]
        result = {"available": True, "signals": by_signal}
        _set_response_cache(cache_key, result)
        return result
    except Exception as e:
        return {"available": False, "reason": f"Error: {e}", "signals": {}}


@app.get("/api/screening/signal-performance-by-factor")
def get_signal_performance_by_factor(
    db_path: str = "research.db",
    period: str = "7d",
) -> Dict[str, object]:
    """Signal performance grouped by factor family (Value, Quality, Momentum, etc.).

    Aggregates per-signal hit rates / correlations into the 6 institutional
    factor families.  Returns the same 10-minute in-memory cache as the per-signal
    endpoint to avoid duplicate compute work.
    """
    cache_key = f"signal_perf_by_factor:{db_path}:{period}"
    hit = _cached_response(cache_key)
    if hit is not None:
        return hit
    try:
        from tradingagents.screening.engine import ScreeningEngine
        db = get_db(db_path)
        engine = ScreeningEngine(config=DEFAULT_CONFIG, db=db)
        families = engine.compute_signal_performance_by_factor(min_samples=30, period=period)
        family_weights = engine.compute_adaptive_weights_by_factor(min_samples=50)
        result = {
            "available": bool(families),
            "period": period,
            "by_factor": families,
            "adaptive_family_weights": family_weights,
        }
        _set_response_cache(cache_key, result)
        return result
    except Exception as e:
        return {"available": False, "reason": f"Error: {e}", "by_factor": {}, "adaptive_family_weights": {}}


@app.get("/api/screening/calibration-by-factor")
def get_calibration_by_factor(db_path: str = "research.db") -> Dict[str, object]:
    """Confidence-accuracy calibration curves sliced by dominant factor family.

    Provides per-factor confidence calibration data (how well the model's confidence
    score maps to empirical accuracy, within each factor regime).
    """
    cache_key = f"cal_by_factor:{db_path}"
    hit = _cached_response(cache_key)
    if hit is not None and hit.get("available"):
        return hit
    try:
        from tradingagents.backtesting.calibration import (
            compute_calibration_by_factor,
            preset_performance_families,
        )
        data = compute_calibration_by_factor(db_path=db_path)
        families = preset_performance_families(data)
        all_meta = (data.get("_all") or {}).get("meta") or {}
        result = {
            "available": True,
            "calibration": data,
            "family_count": len(families),
            "global_samples": int(all_meta.get("total_samples") or 0),
            "global_valid_bins": int(all_meta.get("valid_bins") or 0),
        }
        _set_response_cache(cache_key, result)
        return result
    except Exception as e:
        return {"available": False, "reason": f"Error: {e}", "calibration": {}}


@app.get("/api/screening/calibration-by-decision")
def get_calibration_by_decision(db_path: str = "research.db") -> Dict[str, object]:
    """Confidence-accuracy calibration sliced by model decision (BUY/HOLD/SELL).

    Exposes per-decision-class calibration curves to detect systematic biases
    such as BUY-side overconfidence, which ties back to LLM base-rate skew.
    """
    cache_key = f"cal_by_decision:{db_path}"
    hit = _cached_response(cache_key)
    if hit is not None:
        return hit
    try:
        from tradingagents.backtesting.calibration import compute_calibration_by_decision
        data = compute_calibration_by_decision(db_path=db_path)
        result = {"available": True, "calibration": data}
        _set_response_cache(cache_key, result)
        return result
    except Exception as e:
        return {"available": False, "reason": f"Error: {e}", "calibration": {}}


@app.get("/api/screening/reverse-dcf-accuracy")
def get_reverse_dcf_accuracy(
    db_path: str = "research.db",
    lookback_limit: int = 200,
) -> Dict[str, object]:
    """Reverse-DCF accuracy: compare historical implied_growth_rate to analyst consensus.

    For each analysis with a stored implied_growth_rate, fetches the current analyst
    consensus long-term growth estimate and computes the absolute error distribution.
    This is a practical proxy for 'implied vs realized' accuracy since full
    realized multi-year FCF growth is not available in real-time.

    Returns:
        mean_abs_error, median_abs_error, sample_count, per-ticker rows, and a
        distribution histogram of error buckets.
    """
    cache_key = f"rdcf_accuracy:{db_path}:{lookback_limit}"
    hit = _cached_response(cache_key)
    if hit is not None:
        return hit
    try:
        import json as _json
        db = get_db(db_path)
        # Pull recent analyses that have intrinsic_value_data with implied_growth_rate
        analyses = db.get_backtested_analyses(limit=lookback_limit)
        rows = []
        errors = []
        for a in analyses:
            try:
                iv_raw = getattr(a, "intrinsic_value_data", "") or ""
                if not iv_raw:
                    continue
                iv = _json.loads(iv_raw)
                implied = iv.get("implied_growth_rate")
                if implied is None:
                    continue
                # Fetch analyst consensus forward growth for comparison
                from tradingagents.dataflows.yfinance_extended import get_ticker_info
                info = get_ticker_info(a.ticker)
                consensus_growth = info.get("earningsGrowth") or info.get("revenueGrowth")
                if consensus_growth is None:
                    continue
                err = abs(float(implied) - float(consensus_growth))
                errors.append(err)
                rows.append({
                    "ticker": a.ticker,
                    "analysis_date": a.analysis_date,
                    "implied_growth_rate": round(float(implied), 4),
                    "consensus_growth": round(float(consensus_growth), 4),
                    "abs_error": round(err, 4),
                })
            except Exception:
                pass

        if not errors:
            result = {"available": False, "reason": "Insufficient data with implied_growth_rate", "rows": []}
        else:
            import statistics as _stats
            buckets = {"0-5%": 0, "5-10%": 0, "10-20%": 0, "20%+": 0}
            for e in errors:
                pct = e * 100
                if pct < 5:
                    buckets["0-5%"] += 1
                elif pct < 10:
                    buckets["5-10%"] += 1
                elif pct < 20:
                    buckets["10-20%"] += 1
                else:
                    buckets["20%+"] += 1
            result = {
                "available": True,
                "sample_count": len(errors),
                "mean_abs_error": round(_stats.mean(errors), 4),
                "median_abs_error": round(_stats.median(errors), 4),
                "error_distribution": buckets,
                "rows": sorted(rows, key=lambda r: r["analysis_date"], reverse=True)[:50],
            }
        _set_response_cache(cache_key, result)
        return result
    except Exception as e:
        return {"available": False, "reason": f"Error: {e}", "rows": []}


@app.get("/api/screening/upcoming-events")
def get_upcoming_events_endpoint(
    db_path: str = "research.db",
    ticker: Optional[str] = None,
    days_ahead: int = 60,
    limit: int = 50,
) -> Dict[str, object]:
    """Return upcoming events from the event_calendar table.

    Can be filtered to a single ticker or returned across all tracked tickers,
    sorted by proximity (soonest first).
    """
    try:
        db = get_db(db_path)
        events = db.get_upcoming_events(ticker=ticker, max_days=days_ahead, limit=limit)
        return {"available": True, "events": events, "count": len(events)}
    except Exception as e:
        return {"available": False, "reason": str(e), "events": []}


@app.get("/api/screening/scheduler/status")
def get_screening_scheduler_status() -> Dict[str, object]:
    """Get the screening scheduler status."""
    return _screening_scheduler.get_status()


@app.post("/api/screening/auto-discovery/run")
def run_auto_discovery_manual() -> Dict[str, object]:
    """Trigger an on-demand auto-discovery run in the background."""
    if not os.getenv("PERPLEXITY_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="Perplexity API key not configured. Set PERPLEXITY_API_KEY in .env",
        )

    run = _screening_scheduler.trigger_auto_discovery()
    if not run.get("started"):
        raise HTTPException(
            status_code=409,
            detail=f"Auto-discovery already running (run_id={run.get('run_id')})",
        )
    return run


@app.get("/api/screening/auto-discovery/preview")
def preview_auto_discovery() -> Dict[str, object]:
    """Detect matching themes without spending Perplexity or writing watchlists."""
    try:
        from tradingagents.screening.macro_overlay import compute_sector_momentum
        from tradingagents.dataflows.yfinance_extended import get_macro_snapshot
        from tradingagents.screening.theme_detector import ThemeDetector

        ad_cfg = DEFAULT_CONFIG.get("screening", {}).get("scheduler", {}).get("auto_discovery", {})
        detector = ThemeDetector(max_themes=int(ad_cfg.get("max_themes", 5)))
        sector = compute_sector_momentum()
        macro = get_macro_snapshot()
        index_regime = _index_regime_payload(macro)
        themes = detector.detect_themes(sector, macro)
        return {
            "available": True,
            "perplexity_spent": False,
            "regime": index_regime.get("index_trend"),
            "regime_label": index_regime.get("index_regime_label"),
            "index_regime": index_regime,
            "vix": macro.get("vix_value"),
            "theme_count": len(themes),
            "themes": [t.to_dict() for t in themes],
        }
    except Exception as exc:
        return {
            "available": False,
            "perplexity_spent": False,
            "reason": str(exc)[:300],
            "themes": [],
        }


@app.get("/api/screening/auto-discovery/status")
def get_auto_discovery_status() -> Dict[str, object]:
    """Return current/last manual auto-discovery run state plus budget info."""
    from tradingagents.dataflows.usage_tracker import get_tracker

    ad_cfg = DEFAULT_CONFIG.get("screening", {}).get("scheduler", {}).get("auto_discovery", {})
    budget_limit = int(ad_cfg.get("monthly_budget", 20))
    auto_discovery_enabled = bool(ad_cfg.get("enabled", False))

    usage = get_tracker().get_usage("perplexity")
    monthly_remaining = int(usage.get("monthly_remaining", 0))
    auto_used = int(usage.get("monthly_auto_discovery", 0))
    auto_remaining = max(0, budget_limit - auto_used)
    budget_remaining = max(0, min(auto_remaining, monthly_remaining))

    return {
        **_screening_scheduler.get_auto_discovery_status(),
        "auto_discovery_enabled": auto_discovery_enabled,
        "perplexity_available": bool(os.getenv("PERPLEXITY_API_KEY")),
        "budget_limit": budget_limit,
        "budget_used": auto_used,
        "budget_remaining": budget_remaining,
        "monthly_remaining": monthly_remaining,
    }


@app.post("/api/screening/auto-alerts")
def create_screening_auto_alerts(
    run_id: int,
    top_n: int = Query(5, ge=1, le=50),
    db_path: str = "research.db",
) -> Dict[str, object]:
    """
    Auto-create watch alerts for top N tickers from a screening run.
    Creates price_cross (breakout) and volume_spike alerts.
    """
    db = get_db(db_path)
    run_meta = db.get_screening_run(run_id)
    run_criteria = _parse_run_criteria((run_meta or {}).get("criteria"))
    movers_details = db.get_movers_run_details(run_id) if hasattr(db, "get_movers_run_details") else {}
    results = db.get_screening_results(run_id)
    if not results:
        raise HTTPException(status_code=404, detail="Screening run not found")

    if _run_strategy(run_meta) == "reversal_buildup":
        from tradingagents.screening.reversal_buildup import rank_reversal_rows
        for row in results:
            if isinstance(row.get("signals"), str):
                try:
                    row["signals"] = json.loads(row["signals"])
                except (json.JSONDecodeError, TypeError):
                    row["signals"] = {}
            _lift_reversal_buildup(row)
        top_results = rank_reversal_rows(results, top_n=top_n)
    else:
        top_results = sorted(results, key=lambda r: r.get("composite_score", 0), reverse=True)[:top_n]
    alerts_created = []
    errors = []
    skipped_duplicates = 0

    # Load existing active rules to prevent duplicates
    existing_rules = db.get_active_rules()
    existing_set = {(r["ticker"], r["alert_type"]) for r in existing_rules}

    for r in top_results:
        ticker = r.get("ticker", "")
        if not ticker:
            continue
        movers_ctx = movers_details.get(str(ticker).upper(), {})
        alert_context = {
            "run_id": run_id,
            "strategy": run_criteria.get("strategy"),
            "horizon": run_criteria.get("horizon"),
            "source_tags": movers_ctx.get("source_tags", []),
            "primary_bucket": movers_ctx.get("primary_bucket"),
        }

        # Get current price for price-cross alert target (uses shared cached helper)
        try:
            info = _get_ticker_info_cached(ticker)
            current_price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        except Exception as e:
            current_price = 0
            errors.append(f"{ticker}: price fetch failed ({e})")

        # 1. Volume spike alert (2.5x average)
        if (ticker.upper(), "volume_spike") in existing_set:
            skipped_duplicates += 1
        else:
            try:
                db.create_alert_rule(
                    ticker=ticker,
                    alert_type="volume_spike",
                    condition={"multiplier": 2.5, **alert_context},
                )
                alerts_created.append({"ticker": ticker, "type": "volume_spike"})
                existing_set.add((ticker.upper(), "volume_spike"))
            except Exception as e:
                errors.append(f"{ticker}: volume_spike alert failed ({e})")

        # 2. Price breakout alert (5% above current)
        if current_price > 0:
            if (ticker.upper(), "price_cross") in existing_set:
                skipped_duplicates += 1
            else:
                target = round(current_price * 1.05, 2)
                try:
                    db.create_alert_rule(
                        ticker=ticker,
                        alert_type="price_cross",
                        condition={"direction": "above", "target_price": target, **alert_context},
                    )
                    alerts_created.append({"ticker": ticker, "type": "price_cross", "target": target})
                    existing_set.add((ticker.upper(), "price_cross"))
                except Exception as e:
                    errors.append(f"{ticker}: price_cross alert failed ({e})")

    return {
        "run_id": run_id,
        "alerts_created": len(alerts_created),
        "skipped_duplicates": skipped_duplicates,
        "details": alerts_created,
        "errors": errors,
    }


@app.post("/api/screen/runs/{run_id}/backtest")
def backtest_screening(run_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Backtest a screening run to measure signal accuracy."""
    from tradingagents.backtesting.engine import backtest_screening_run
    result = backtest_screening_run(run_id, db_path=db_path)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/screen/runs/{run_id}/risk-metrics")
def screening_risk_metrics(run_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Compute risk metrics from backtested forward returns for a screening run.

    Returns Sharpe, Sortino, max drawdown, win rate, avg return, best/worst for
    each period (7d, 14d, 30d).
    """
    import math
    db = get_db(db_path)
    results = db.get_screening_results(run_id)
    if not results:
        raise HTTPException(status_code=404, detail="No results for this screening run")

    def _compute_metrics(returns: List[float], label: str) -> Dict[str, object]:
        if not returns:
            return {"period": label, "sample_count": 0}
        n = len(returns)
        avg_ret = sum(returns) / n
        wins = sum(1 for r in returns if r > 0)
        win_rate = wins / n * 100
        best = max(returns)
        worst = min(returns)
        # Sharpe: mean / std (annualized assuming 252 trading days)
        if n > 1:
            variance = sum((r - avg_ret) ** 2 for r in returns) / (n - 1)
            std = math.sqrt(variance) if variance > 0 else 0.001
            sharpe = (avg_ret / std) * math.sqrt(252) if std > 0 else 0
            # Sortino: mean / downside std
            downside = [r for r in returns if r < 0]
            if downside:
                down_var = sum(r ** 2 for r in downside) / len(downside)
                down_std = math.sqrt(down_var)
                sortino = (avg_ret / down_std) * math.sqrt(252) if down_std > 0 else 0
            else:
                sortino = sharpe * 1.5  # All positive returns
        else:
            sharpe = 0
            sortino = 0
        # Max drawdown (from cumulative returns)
        cumulative = 0
        peak = 0
        max_dd = 0
        for r in returns:
            cumulative += r
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        # CVaR (95%): mean of returns below the 5th percentile
        sorted_returns = sorted(returns)
        cutoff_idx = max(1, int(math.ceil(n * 0.05)))
        tail = sorted_returns[:cutoff_idx]
        cvar_95_pct = round(sum(tail) / len(tail) * 100, 2) if tail else 0

        return {
            "period": label,
            "sample_count": n,
            "avg_return_pct": round(avg_ret * 100, 2),
            "win_rate_pct": round(win_rate, 1),
            "best_return_pct": round(best * 100, 2),
            "worst_return_pct": round(worst * 100, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "cvar_95_pct": cvar_95_pct,
        }

    metrics = {}
    for period, col in [("7d", "return_7d"), ("14d", "return_14d"), ("30d", "return_30d")]:
        returns = [r[col] for r in results if r.get(col) is not None]
        metrics[period] = _compute_metrics(returns, period)

    # Top 20 by composite score (how the "picks" performed)
    top20 = sorted(results, key=lambda r: r.get("composite_score", 0), reverse=True)[:20]
    for period, col in [("7d", "return_7d"), ("14d", "return_14d"), ("30d", "return_30d")]:
        returns = [r[col] for r in top20 if r.get(col) is not None]
        metrics[f"top20_{period}"] = _compute_metrics(returns, f"top20_{period}")

    metrics["run_id"] = run_id
    metrics["total_results"] = len(results)
    metrics["backtested_count"] = sum(1 for r in results if r.get("return_7d") is not None)
    return metrics


# =========================================================================
# Alerts Page Route
# =========================================================================

@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    """Render the Alerts UI page."""
    return templates.TemplateResponse("alerts.html", {"request": request})


# =========================================================================
# Alert API Endpoints (Phase 3)
# =========================================================================

@app.get("/api/alerts")
def get_unread_alerts(db_path: str = "research.db") -> Dict[str, object]:
    """Get unread alerts (for badge count + dropdown)."""
    db = get_db(db_path)
    alerts = db.get_unread_alerts()
    return {"unread_count": len(alerts), "alerts": alerts}


@app.get("/api/alerts/history")
def get_alert_history(
    limit: int = 50, offset: int = 0, db_path: str = "research.db"
) -> Dict[str, object]:
    """Get paginated alert history."""
    db = get_db(db_path)
    history = db.get_alert_history(limit=limit, offset=offset)
    return {"alerts": history, "limit": limit, "offset": offset}


@app.post("/api/alerts/rules")
def create_alert_rule(req: AlertRuleRequest, db_path: str = "research.db") -> Dict[str, object]:
    """Create a new alert rule."""
    from tradingagents.screening.alert_evaluator import AlertEvaluator

    valid_types = AlertEvaluator.ALERT_TYPES
    if req.alert_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid alert_type '{req.alert_type}'. Valid types: {valid_types}",
        )

    # Merge with defaults for the alert type
    defaults = AlertEvaluator.CONDITION_DEFAULTS.get(req.alert_type, {})
    condition = {**defaults, **req.condition}

    db = get_db(db_path)
    rule_id = db.create_alert_rule(
        ticker=req.ticker.upper(),
        alert_type=req.alert_type,
        condition=condition,
    )
    return {"id": rule_id, "ticker": req.ticker.upper(), "alert_type": req.alert_type, "condition": condition}


@app.get("/api/alerts/rules")
def list_alert_rules(db_path: str = "research.db") -> Dict[str, object]:
    """List all alert rules (active and inactive)."""
    db = get_db(db_path)
    rules = db.get_all_rules()
    return {"rules": rules, "count": len(rules)}


@app.delete("/api/alerts/rules/{rule_id}")
def delete_alert_rule(rule_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Delete an alert rule and its history."""
    db = get_db(db_path)
    success = db.delete_rule(rule_id)
    if not success:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"deleted": True, "id": rule_id}


@app.put("/api/alerts/rules/{rule_id}/toggle")
def toggle_alert_rule(rule_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Toggle an alert rule active/inactive."""
    db = get_db(db_path)
    # Check current state
    rules = db.get_all_rules()
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")

    if rule["is_active"]:
        db.deactivate_rule(rule_id)
        new_state = False
    else:
        db.activate_rule(rule_id)
        new_state = True

    return {"id": rule_id, "is_active": new_state}


@app.put("/api/alerts/{alert_id}/read")
def mark_alert_read(alert_id: int, db_path: str = "research.db") -> Dict[str, object]:
    """Mark a single alert as read."""
    db = get_db(db_path)
    success = db.mark_alert_read(alert_id)
    if not success:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"marked": True, "id": alert_id}


@app.put("/api/alerts/read-all")
def mark_all_alerts_read(db_path: str = "research.db") -> Dict[str, object]:
    """Mark all alerts as read."""
    db = get_db(db_path)
    count = db.mark_all_alerts_read()
    return {"marked_count": count}


def _get_ticker_info_cached(symbol: str) -> dict:
    """Fetch yf.Ticker.info via the centralized get_ticker_info() cache.

    Returns the full cached .info dict (6-hour TTL).  All backend code
    shares the same ``ticker_info_full`` cache entry, eliminating
    redundant yfinance calls across webapp and dataflow layers.
    """
    from tradingagents.dataflows.yfinance_extended import get_ticker_info
    return get_ticker_info(symbol)


@app.get("/api/ticker/{symbol}/summary")
def get_ticker_summary(symbol: str) -> Dict[str, object]:
    """Get lightweight ticker summary for inline expansion (price, range, cap, sector).

    Uses DataCache (15-min TTL) so repeated expand/collapse of the same
    ticker is instant.
    """
    try:
        info = _get_ticker_info_cached(symbol)
        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose") or 0
        change = price - prev_close if price and prev_close else 0
        change_pct = (change / prev_close * 100) if prev_close else 0
        exchange = exchange_from_info(info)
        tv_symbol = tradingview_symbol_token(ticker=symbol.upper(), exchange=exchange)
        tv_url = tradingview_symbol_url(ticker=symbol.upper(), exchange=exchange)
        tv_chart_url = tradingview_chart_url(
            ticker=symbol.upper(),
            exchange=exchange,
            chart_id=_TRADINGVIEW_CHART_ID,
        )
        # Truncate business summary to ~2 sentences for quick context
        raw_summary = info.get("longBusinessSummary") or ""
        brief = raw_summary
        if len(raw_summary) > 300:
            # Cut at the nearest sentence boundary within 300 chars
            cut = raw_summary[:300].rfind(". ")
            brief = raw_summary[: cut + 1] if cut > 100 else raw_summary[:300] + "…"

        result = {
            "ticker": symbol.upper(),
            "company_name": info.get("longName") or info.get("shortName") or "",
            "description": brief,
            "website": info.get("website") or "",
            "price": round(price, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "day_low": round(info.get("dayLow") or 0, 2),
            "day_high": round(info.get("dayHigh") or 0, 2),
            "fifty_two_week_low": round(info.get("fiftyTwoWeekLow") or 0, 2),
            "fifty_two_week_high": round(info.get("fiftyTwoWeekHigh") or 0, 2),
            "market_cap": info.get("marketCap") or 0,
            "sector": info.get("sector") or "-",
            "industry": info.get("industry") or "-",
            "avg_volume": info.get("averageVolume") or 0,
            "pe_ratio": round(info.get("trailingPE") or 0, 2),
            "dividend_yield": round((info.get("dividendYield") or 0) * 100, 2),
            "beta": round(info.get("beta") or 0, 2),
            "target_mean_price": round(info.get("targetMeanPrice") or 0, 2),
            "recommendation": info.get("recommendationKey") or "",
            "num_analysts": info.get("numberOfAnalystOpinions") or 0,
            "exchange": exchange,
            "tradingview_symbol": tv_symbol,
            "tradingview_url": tv_url,
            "tradingview_symbol_url": tv_url,
            "tradingview_chart_url": tv_chart_url,
        }

        try:
            eq = get_earnings_quality(symbol)
            if eq.get("grade"):
                result["_earnings_quality"] = eq
        except Exception:
            pass

        try:
            iv = compute_intrinsic_value(symbol)
            if iv.get("fair_value"):
                result["_intrinsic_value"] = iv
        except Exception:
            pass

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch {symbol}: {e}")


@app.get("/api/ticker/{symbol}/sparkline")
def get_ticker_sparkline(
    symbol: str,
    days: int = Query(365, ge=7, le=730),
) -> Dict[str, object]:
    """Return daily close prices for sparkline rendering.

    Returns a lightweight JSON array of {date, close} for the last N trading days.
    Uses DataCache (1-hour TTL, memory + disk) so repeated views are instant
    and data survives server restarts.
    """
    from tradingagents.dataflows.cache import get_cache
    import yfinance as yf
    from datetime import datetime, timedelta

    sym = symbol.upper().strip()
    if not sym or len(sym) > 10:
        raise HTTPException(status_code=400, detail="Invalid symbol")

    cache = get_cache()
    cached = cache.get("sparkline", sym, str(days))
    if cached is not None:
        logger.debug("sparkline cache HIT for %s (%dd)", sym, days)
        return cached

    logger.debug("sparkline cache MISS for %s (%dd) — calling yfinance", sym, days)
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days + 10)  # buffer for weekends/holidays
        ticker = yf.Ticker(sym)
        hist = ticker.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
        if hist.empty:
            return {"ticker": sym, "points": [], "days": days}

        # Keep last N trading days
        hist = hist.tail(days)
        points = [
            {"d": idx.strftime("%Y-%m-%d"), "c": round(float(row["Close"]), 2)}
            for idx, row in hist.iterrows()
        ]
        result = {"ticker": sym, "points": points, "days": days}
        cache.set("sparkline", sym, str(days), data=result)
        return result
    except Exception as e:
        logger.warning("Sparkline fetch failed for %s: %s", sym, e)
        return {"ticker": sym, "points": [], "days": days, "error": str(e)}


@app.get("/api/ticker/{symbol}/metadata")
def get_ticker_metadata(symbol: str, db_path: str = "research.db") -> Dict[str, object]:
    """Get auto-resolved metadata for a single ticker (sector, cap tier, profile, preset).

    If not cached, resolves on-the-fly and caches for future use.
    """
    from tradingagents.screening.ticker_resolver import resolve_and_cache
    db = get_db(db_path)
    result = resolve_and_cache([symbol.upper()], db, max_workers=1, max_age_days=7)
    meta = result.get(symbol.upper())
    if not meta:
        raise HTTPException(status_code=404, detail=f"Cannot resolve metadata for {symbol}")
    return meta


@app.get("/api/ticker/{symbol}/estimate-revisions")
async def get_ticker_estimate_revisions(symbol: str):
    """Get estimate revision data for deferred loading."""
    try:
        data = get_estimate_revisions(symbol)
        return data
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ticker/{symbol}/rating-changes")
async def get_ticker_rating_changes(symbol: str):
    """Get analyst rating changes for deferred loading."""
    try:
        data = get_rating_changes(symbol)
        return data
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ticker/{symbol}/earnings-quality")
async def get_ticker_earnings_quality(symbol: str):
    """Get earnings quality metrics for deferred loading."""
    try:
        data = get_earnings_quality(symbol)
        return data
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ticker/{symbol}/intrinsic-value")
async def get_ticker_intrinsic_value(symbol: str):
    """Get DCF intrinsic value for deferred loading."""
    try:
        data = compute_intrinsic_value(symbol)
        return data
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ticker/{symbol}/scenario-analysis")
async def get_ticker_scenario_analysis(symbol: str):
    """Get bull/base/bear scenario analysis for deferred loading."""
    try:
        data = compute_scenario_analysis(symbol)
        return data
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ticker/{symbol}/peer-comps")
async def get_ticker_peer_comps(symbol: str):
    """Get peer comparable analysis (P/E, P/S, EV/EBITDA vs 10 sector peers) for deferred loading."""
    try:
        from tradingagents.dataflows.yfinance_extended import compute_peer_comps
        data = compute_peer_comps(symbol)
        return data
    except Exception as e:
        return {"error": str(e), "peers": [], "subject": {}}


@app.get("/api/macro/status")
async def get_macro_status(as_of_date: Optional[str] = None):
    """Get current macro environment status including credit stress and sector breadth."""
    try:
        macro = get_macro_snapshot(as_of_date=as_of_date)
        payload = _index_regime_payload(macro)
        payload.update({
            "vix_level": macro.get("vix_level", "unknown"),
            "yield_curve": macro.get("yield_curve", "unknown"),
            "dollar_trend": macro.get("dollar_trend", "unknown"),
            "credit_stress": macro.get("credit_stress", "unknown"),
            "sector_breadth": macro.get("sector_breadth", "unknown"),
            "sectors_above_sma50": macro.get("sectors_above_sma50", 0),
            "sectors_total": macro.get("sectors_total", 0),
            "index_regime_evidence": macro.get("index_regime_evidence", {}),
            "error": macro.get("error"),
        })
        return payload
    except Exception as e:
        return {"error": str(e)}


class BulkMetadataRequest(BaseModel):
    tickers: List[str] = Field(..., min_length=1)


@app.post("/api/ticker/metadata/bulk")
def get_ticker_metadata_bulk(body: BulkMetadataRequest, db_path: str = "research.db") -> Dict[str, object]:
    """Get auto-resolved metadata for multiple tickers.

    Returns {ticker: metadata_dict} for all resolved tickers.
    Triggers background resolution for any stale/missing entries.
    """
    from tradingagents.screening.ticker_resolver import resolve_and_cache
    db = get_db(db_path)
    result = resolve_and_cache(body.tickers, db, max_workers=8, max_age_days=7)
    return result


@app.get("/api/alerts/types")
def get_alert_types() -> Dict[str, object]:
    """Get available alert types and their descriptions/default conditions."""
    from tradingagents.screening.alert_evaluator import AlertEvaluator
    return {
        "types": [
            {
                "type": t,
                "description": AlertEvaluator.ALERT_TYPE_DESCRIPTIONS.get(t, ""),
                "default_condition": AlertEvaluator.CONDITION_DEFAULTS.get(t, {}),
            }
            for t in AlertEvaluator.ALERT_TYPES
        ]
    }


@app.get("/api/alerts/monitor/status")
def get_alert_monitor_status() -> Dict[str, object]:
    """Get status of the alert monitoring background thread."""
    return {
        "is_running": _alert_monitor.is_running,
        "check_interval": _alert_monitor.active_interval,
        "is_market_hours": _alert_monitor.is_market_hours,
        "market_hours_interval": _alert_monitor.market_hours_interval,
        "off_hours_interval": _alert_monitor.off_hours_interval,
    }


# =========================================================================
# Storage Management
# =========================================================================

@app.get("/api/storage")
def get_storage_report(db_path: str = "research.db") -> Dict[str, object]:
    """Return per-directory file counts and disk sizes."""
    from tradingagents.utils.cleanup_manager import CleanupManager
    cm = CleanupManager(db_path=_safe_db_path(db_path))
    return cm.get_storage_report()


@app.delete("/api/storage/cleanup")
def storage_cleanup(
    max_age_days: int = Query(90, ge=1),
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Run age-based cleanup across reports, eval_results, cache CSVs, and DB backups."""
    from tradingagents.utils.cleanup_manager import CleanupManager
    cm = CleanupManager(db_path=_safe_db_path(db_path))
    return cm.cleanup_all(max_age_days=max_age_days, dry_run=False)


@app.post("/api/storage/vacuum")
def storage_vacuum(db_path: str = "research.db") -> Dict[str, object]:
    """Run VACUUM + ANALYZE on the database."""
    db = get_db(_safe_db_path(db_path))
    return db.vacuum()


# =========================================================================
# Feature Flags & Usage
# =========================================================================

@app.get("/api/features")
def get_features() -> Dict[str, object]:
    """Report which optional features are available based on configuration."""
    return {
        "perplexity_available": bool(os.getenv("PERPLEXITY_API_KEY")),
    }


@app.get("/api/usage")
def get_api_usage() -> Dict[str, object]:
    """Return current API usage statistics across all tracked vendors."""
    from tradingagents.dataflows.usage_tracker import get_perplexity_monthly_limit, get_tracker
    tracker = get_tracker()
    usage = tracker.get_usage()
    vendors = usage.get("vendors") or {}
    px = vendors.get("perplexity") or {}
    monthly = int(px.get("monthly") or 0)
    limit = get_perplexity_monthly_limit()
    usage["perplexity"] = {
        "used": monthly,
        "limit": limit,
        "monthly": monthly,
        "monthly_auto_discovery": int(px.get("monthly_auto_discovery") or 0),
        "monthly_discover": int(px.get("monthly_discover") or 0),
        "monthly_methods": dict(px.get("monthly_methods") or {}),
    }
    return usage


# =========================================================================
# Perplexity Ticker Discovery
# =========================================================================

def _snapshot_discovery_tickers(tickers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Persist full discovery rows so saved runs replay like screening history."""
    snapshot: List[Dict[str, Any]] = []
    for row in tickers or []:
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        snapshot.append(dict(row))
    return snapshot


def _enrich_discovery_run_rows(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from tradingagents.screening.discovery import get_discovery_template

    enriched: List[Dict[str, Any]] = []
    for run in runs:
        row = dict(run)
        tmpl = get_discovery_template(row.get("template_id") or "")
        row["template_label"] = tmpl.label if tmpl else (row.get("template_id") or "")
        enriched.append(row)
    return enriched


@app.get("/api/discover/templates")
def list_discover_templates() -> Dict[str, object]:
    """Operator-facing discovery chips aligned to screening books."""
    from tradingagents.screening.discovery import list_discovery_templates
    return {"templates": list_discovery_templates()}


@app.post("/api/discover")
def discover_tickers(body: DiscoverRequest) -> Dict[str, object]:
    """Discover tickers matching an investment theme via Perplexity AI."""
    if not os.getenv("PERPLEXITY_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="Perplexity API key not configured. Set PERPLEXITY_API_KEY in .env",
        )

    from tradingagents.dataflows.interface import discover_opportunities
    from tradingagents.dataflows.perplexity_api import PerplexityRateLimitError, PerplexityAPIError
    from tradingagents.screening.discovery import (
        annotate_desk_overlap,
        annotate_momentum_hopper_flags,
        build_watchlist_ticker_index,
        get_discovery_template,
        is_momentum_discovery_context,
    )

    tmpl = get_discovery_template(body.template_id)
    prompt_pack = body.prompt_pack or (tmpl.prompt_pack if tmpl else "equity_search")
    asset_policy = body.asset_policy or (tmpl.asset_policy if tmpl else "equity")
    watchlist_preset = body.watchlist_preset or (tmpl.preset if tmpl else None)
    watchlist_profile = body.watchlist_profile or (tmpl.profile if tmpl else None)

    try:
        result = discover_opportunities(
            theme=body.theme,
            criteria=body.criteria,
            max_results=body.max_results,
            market_cap_filter=body.market_cap_filter,
            prompt_pack=prompt_pack,
            asset_policy=asset_policy,
            template_id=body.template_id or "",
        )
    except PerplexityRateLimitError as e:
        from tradingagents.dataflows.usage_tracker import get_tracker
        tracker = get_tracker()
        usage = tracker.get_usage()
        raise HTTPException(
            status_code=429,
            detail={
                "message": str(e),
                "usage": usage.get("perplexity", {}),
            },
        )
    except PerplexityAPIError as e:
        raise HTTPException(
            status_code=502,
            detail=str(e),
        )

    db = get_db(_safe_db_path(body.db_path))
    tickers = annotate_desk_overlap(
        result.get("tickers", []),
        build_watchlist_ticker_index(db.get_watchlists()),
    )
    fit_policy = str(result.get("fit_policy") or (tmpl.fit_policy if tmpl else "") or "")
    if is_momentum_discovery_context(
        body.template_id,
        fit_policy=fit_policy,
        prompt_pack=prompt_pack,
    ):
        tickers = annotate_momentum_hopper_flags(tickers)

    watchlist_id = None
    if body.auto_create_watchlist and body.watchlist_name:
        valid_tickers = [t["ticker"] for t in tickers if t.get("validated")]
        if valid_tickers:
            tickers_csv = ",".join(valid_tickers)
            description = f"Discovered via Perplexity: {body.theme}"
            try:
                watchlist_id = db.create_watchlist(
                    name=body.watchlist_name,
                    description=description,
                    tickers=tickers_csv,
                    default_preset=watchlist_preset,
                    default_investment_profile=watchlist_profile,
                )
            except Exception as e:
                error_msg = str(e)
                if "UNIQUE" in error_msg.upper():
                    raise HTTPException(
                        status_code=409,
                        detail=f"A watchlist named '{body.watchlist_name}' already exists.",
                    )
                raise

    valid_count = sum(1 for t in tickers if t.get("validated"))
    new_count = sum(1 for t in tickers if t.get("validated") and t.get("novelty") == "new")
    response = {
        "tickers": tickers,
        "theme": result.get("theme", body.theme),
        "source": result.get("source", "perplexity"),
        "raw_response": result.get("raw_response", ""),
        "watchlist_id": watchlist_id,
        "valid_count": valid_count,
        "total_count": len(tickers),
        "new_count": new_count,
        "on_desk_count": sum(1 for t in tickers if t.get("on_desk")),
        "used_regex_fallback": result.get("used_regex_fallback", False),
        "template_id": body.template_id or result.get("template_id"),
        "preset": watchlist_preset,
        "prompt_pack": result.get("prompt_pack", prompt_pack),
        "asset_policy": result.get("asset_policy", asset_policy),
        "fit_policy": result.get("fit_policy"),
    }
    try:
        run_id = db.save_discovery_run(
            source="manual",
            template_id=body.template_id or "",
            theme=response["theme"],
            criteria=body.criteria,
            market_cap_filter=body.market_cap_filter,
            max_results=body.max_results,
            preset=watchlist_preset,
            profile=watchlist_profile,
            ticker_count=len(tickers),
            validated_count=valid_count,
            new_count=new_count,
            watchlist_id=watchlist_id,
            tickers=_snapshot_discovery_tickers(tickers),
            summary={
                "used_regex_fallback": result.get("used_regex_fallback", False),
                "prompt_pack": result.get("prompt_pack", prompt_pack),
                "asset_policy": result.get("asset_policy", asset_policy),
                "fit_policy": result.get("fit_policy"),
            },
        )
        response["discovery_run_id"] = run_id
    except Exception as exc:
        logger.warning("Failed to persist discovery run: %s", exc)
    return response


@app.get("/api/discover/history")
def list_discovery_history(
    limit: int = 20,
    offset: int = 0,
    source: Optional[str] = None,
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Recent manual and auto-discovery runs."""
    db = get_db(_safe_db_path(db_path))
    runs = _enrich_discovery_run_rows(
        db.get_discovery_runs(limit=limit, offset=offset, source=source)
    )
    return {"runs": runs, "limit": limit, "offset": offset}


@app.get("/api/discover/history/{run_id}")
def get_discovery_history_run(
    run_id: int,
    db_path: str = "research.db",
) -> Dict[str, object]:
    """Return one discovery run, including stored tickers for replay."""
    from tradingagents.screening.discovery import (
        annotate_desk_overlap,
        build_watchlist_ticker_index,
    )

    db = get_db(_safe_db_path(db_path))
    run = db.get_discovery_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Discovery run {run_id} not found")
    if run.get("tickers"):
        run["tickers"] = annotate_desk_overlap(
            run["tickers"],
            build_watchlist_ticker_index(db.get_watchlists()),
        )
    enriched = _enrich_discovery_run_rows([run])[0]
    return {"run": enriched}
