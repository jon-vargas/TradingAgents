"""
Screening Engine — Algorithmic watchlist scanner with advanced scoring signals.

This is a purely computational engine: no LLM calls, no Perplexity.
All data from yfinance (free, no rate limits).

Safeguards & performance:
- Batch yfinance downloads (1 API call per scan, not per ticker)
- Disk + memory caching with 1-hour TTL for screening data
- Chunked processing for large watchlists (>60 tickers per chunk)
- Exponential backoff retry on network failures (up to 3 attempts)
- Funnel scoring: cheap signals first, expensive data only for top % of tickers
- Signal momentum deltas: captures direction-of-change, not just current state
- Cross-watchlist percentile ranking: scores relative to historical baseline
- Graceful degradation: individual ticker failures produce partial results
- Usage tracking via UsageTracker for rate-limit visibility

Core Signals (each normalized to 0.0–1.0):
 1. volume_surge      — Current volume vs 20-day average
 2. rsi_oversold      — RSI below 50 (bullish: stronger as RSI nears 30)
 3. rsi_overbought    — RSI above 50 (bearish: stronger as RSI nears 70)
 4. ma_crossover      — 10 EMA vs 50 SMA position and direction
 5. price_vs_target   — Current price vs mean analyst target
 6. earnings_proximity — Days until next earnings (closer = higher)
 7. insider_buying    — Net insider activity signal
 8. relative_strength — Price performance vs S&P 500 over 20 days
 9. bollinger_squeeze — Bollinger bandwidth compression
10. trend_strength    — ADX value (strong trend = higher)

Tier 2 Signals (from .info; identity books can skip the T1 funnel):
11. valuation_gap     — Blended P/E, P/S, EV/EBITDA vs sector medians (cheaper = higher)
    Enhanced may overwrite with richer multiples when that tier runs.
12. estimate_momentum / rating_momentum / quality_factor / income_factor

Enhanced Signals (top funnel survivors):
13. options_sentiment — IV rank + P/C ratio composite
14. smart_money       — Institutional flow + short interest change
15. pead_drift        — Post-earnings announcement drift
16. weekly_trend_alignment — Weekly chart confluence (funnel-gated)

Also computed where applicable: pead_drift, rsi_overbought (paired with rsi_oversold).
Direction: "bullish" or "bearish" based on weighted signal polarity.
"""

import json
import logging
import math
import threading
import time
import numpy as np
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("tradingagents.screening")

from tradingagents.dataflows.cache import get_cache, CacheConfig
from tradingagents.dataflows.screening_ohlcv_cache import (
    SCREENING_OHLCV_CHUNK_SIZE as _CHUNK_SIZE,
    ScreeningOhlcvSession,
)
from tradingagents.dataflows.yfinance_limiter import get_yfinance_limiter
from tradingagents.dataflows.yfinance_extended import (
    get_valuation_metrics, get_options_summary, get_ownership_summary,
    get_earnings_profile, get_macro_snapshot,
    get_estimate_revisions, get_rating_changes,
)

# ---------------------------------------------------------------------------
# Factor Scorecard
# ---------------------------------------------------------------------------

# Maps each of the 16 screening signals to one of 6 institutional factor families.
# Signals in _EQ_OVERLAP_SIGNALS are categorised as Momentum and are averaged
# within the family — they are not double-counted.
FACTOR_FAMILIES: Dict[str, List[str]] = {
    "Value":      ["valuation_gap", "price_vs_target"],
    "Quality":    ["quality_factor"],
    "Flow":       ["smart_money", "insider_buying", "short_pressure"],
    "Income":     ["income_factor"],
    "Momentum":   ["volume_surge", "rsi_oversold", "rsi_overbought", "ma_crossover",
                   "relative_strength", "bollinger_squeeze", "trend_strength", "pead_drift",
                   "residual_momentum_12_1"],
    "Revisions":  ["estimate_momentum", "rating_momentum"],
    "Catalyst":   ["earnings_proximity"],
    "Risk":       ["options_sentiment"],
    "Macro-Fit":  [],
}

# Composite (long rank) skips these keys. Direction still uses the shared
# signal_weights entry — rsi_overbought is the only −1 polarity signal.
_COMPOSITE_SKIP_SIGNALS = frozenset({"rsi_overbought"})
_TIER2_ZERO_FILL_EXCLUDE = frozenset({
    "quality_factor", "income_factor", "valuation_gap",
    "earnings_proximity", "short_pressure",
})
# In-window catalyst only. Outside this, proximity is None (not a fake 0.0).
_EARNINGS_WINDOW_DAYS = 21
_VALUE_MEGA_GROWTH_SECTORS = frozenset({
    "technology",
    "communication services",
    "consumer cyclical",
    "consumer discretionary",
})
# 252 trading days ≈ 365 calendar days; 420 covers holidays and Yahoo gaps
# so residual_momentum_12_1 can actually compute.
_OHLCV_LOOKBACK_DAYS = 420
# Book-defining sleeves. Missing (None) → sort last so a growth name with
# no yield cannot win dividend_income via composite renormalization.
# 0.0 is a computed reading and still ranks. Any listed key is sufficient.
# These keys also 0-fill in the composite so missing identity cannot be
# renormalized away. Do not add flow/event sleeves here — that would
# 0-fill smart_money / options on every book and skip the T1 funnel on
# small identity lists (earnings/smart-money need enhanced coverage).
PRIMARY_SLEEVE_REQUIRED: Dict[str, tuple] = {
    "dividend_income": ("income_factor",),
    "value_fisher": ("valuation_gap",),
    "quality_compounder": ("quality_factor",),
    # Quality is the identity — valuation_gap stays in the composite but
    # VG=0.0 must not pass a name that never computed quality_factor.
    "long_horizon_12to36m": ("quality_factor",),
    "long_horizon_6to12m": ("quality_factor",),
}
# Sort-last only. Missing flow/event identity cannot win via insider-only
# or volume/RS renormalization. Keys are NOT 0-filled globally.
FLOW_SLEEVE_REQUIRED: Dict[str, tuple] = {
    "smart_money_tracker": ("smart_money", "options_sentiment"),
    "earnings_play": ("pead_drift", "earnings_proximity"),
    "short_squeeze": ("short_pressure",),
}
# Optional floors on flow/event sleeves. 0.0 still counts as present unless
# listed here — squeeze identity is high SI, not a 1% print.
FLOW_SLEEVE_MIN: Dict[str, Dict[str, float]] = {
    "short_squeeze": {"short_pressure": 0.20},
}
# Modest quality floor so a 0.0 print does not satisfy "quality required".
PRIMARY_SLEEVE_MIN: Dict[str, Dict[str, float]] = {
    "long_horizon_6to12m": {"quality_factor": 0.15},
    "long_horizon_12to36m": {"quality_factor": 0.20},
}
_PRIMARY_SLEEVE_KEYS = frozenset(
    key for keys in PRIMARY_SLEEVE_REQUIRED.values() for key in keys
)
_IDENTITY_T2_PRESETS = frozenset(PRIMARY_SLEEVE_REQUIRED.keys())
_DEFAULT_ZSCORE_KEYS = (
    "quality_factor",
    "income_factor",
    "valuation_gap",
    "relative_strength",
    "residual_momentum_12_1",
)


def primary_sleeve_present(
    preset: Optional[str],
    signals: Optional[Dict[str, Any]],
    tier_reached: Optional[str] = None,
) -> bool:
    """True when the preset has no gate, or at least one required sleeve is computed.

    ``tier_reached`` is accepted for call-site compatibility; a missing sleeve
    still fails the gate. Funnel/enhanced skips must not win the book via
    composite renormalization (the original My Watchlist income failure).
    """
    name = str(preset or "")
    payload = signals or {}
    if name == "value_fisher" and _is_mega_growth(payload):
        return False
    keys = FLOW_SLEEVE_REQUIRED.get(name) or PRIMARY_SLEEVE_REQUIRED.get(name)
    if not keys:
        return True
    mins = FLOW_SLEEVE_MIN.get(name) or PRIMARY_SLEEVE_MIN.get(name) or {}
    return any(_sleeve_value_ok(payload.get(k), mins.get(k, 0.0)) for k in keys)


def _is_mega_growth(signals: Optional[Dict[str, Any]]) -> bool:
    """True for mega-cap growth — tier when present, else Discover household list."""
    from tradingagents.screening.discovery import HOUSEHOLD_MEGA_TICKERS

    meta = (signals or {}).get("_screening_meta") or {}
    tier = str(meta.get("market_cap_tier") or "").strip().lower()
    sector = str(meta.get("sector") or "").strip().lower()
    sym = str(meta.get("ticker") or "").strip().upper()
    mega = tier == "mega" or (not tier and sym in HOUSEHOLD_MEGA_TICKERS)
    return mega and sector in _VALUE_MEGA_GROWTH_SECTORS


def _sleeve_value_ok(value: Any, minimum: float) -> bool:
    if value is None:
        return False
    try:
        return float(value) >= float(minimum)
    except (TypeError, ValueError):
        return False


def apply_identity_funnel_policy(
    preset: Optional[str],
    n_scored: int,
    enable_funnel: bool,
    funnel_cutoff: float,
    bypass_below: int = 150,
    min_cut: float = 0.85,
) -> Tuple[bool, float]:
    """Identity books skip the T1 funnel on small lists; raise cutoff on large ones."""
    if not enable_funnel or (preset or "") not in _IDENTITY_T2_PRESETS:
        return enable_funnel, funnel_cutoff
    if n_scored <= bypass_below:
        return False, funnel_cutoff
    return True, max(funnel_cutoff, min(0.95, min_cut))


def compute_factor_scorecard(
    signals: Dict[str, float],
    macro_fit_score: Optional[float] = None,
) -> Dict[str, Optional[float]]:
    """Map screening signals to institutional factor-family scores (0-100).

    Missing families are ``None`` (sort-last / no-data), not a fake 0.0.
    """
    result: Dict[str, Optional[float]] = {}
    for family, members in FACTOR_FAMILIES.items():
        if family == "Macro-Fit":
            result[family] = (
                round(min(100.0, max(0.0, float(macro_fit_score))), 1)
                if macro_fit_score is not None
                else None
            )
            continue
        present = [signals[m] for m in members if m in signals and signals[m] is not None]
        if present:
            result[family] = round(sum(present) / len(present) * 100, 1)
        else:
            result[family] = None
    return result


def _numeric_or_zero(value: Any) -> float:
    """Coerce None / non-numeric to 0.0 for funnel math only."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MAX_RETRIES = 3  # Retry attempts on transient failures
_RETRY_BASE_DELAY = 2  # Seconds (doubles each retry)
_DEFAULT_FUNNEL_CUTOFF_PCT = 0.60  # Top 60% of Tier 1 scores proceed to Tier 2

# Process-global serialization lock for yfinance bulk downloads.
# yfinance's internal urllib/requests session is not thread-safe for concurrent
# bulk downloads — simultaneous calls can corrupt each other's responses and
# produce empty DataFrames or raise ambiguous-truth-value errors.  This lock
# ensures only one OHLCV/SPY download request is in-flight at a time while
# still allowing other (non-download) engine work to proceed concurrently.
_DOWNLOAD_LOCK = threading.Lock()


@dataclass
class ScreeningResult:
    """Result for a single ticker in a screening run."""
    ticker: str
    composite_score: float  # 0–100
    direction: str  # "bullish", "bearish", or "neutral"
    signals: Dict[str, Optional[float]]  # signal_name → normalized 0.0-1.0, or None when no data
    signal_deltas: Dict[str, float] = field(default_factory=dict)  # signal_name → delta (-1 to +1)
    percentile: Optional[float] = None  # Cross-watchlist percentile (0–100)
    rank: int = 0
    macro_fit: Optional[float] = None  # Macro-Tactical Overlay score (0–100)
    macro_breakdown: Optional[Dict[str, Any]] = field(default=None)  # Layer scores + sub-factor details
    index_regime: Optional[str] = None
    index_regime_label: Optional[str] = None
    index_trend: Optional[str] = None
    index_stress: Optional[str] = None
    entry_quality: Optional[float] = None  # Entry Quality Score (0–100)
    entry_quality_signals: Optional[Dict[str, float]] = field(default=None)  # Sub-signal breakdown
    composite_fundamental: Optional[float] = None  # Composite recomputed excluding signals that overlap Entry Quality (RSI, MACD proxy, Bollinger squeeze, volume, trend strength, MA crossover). Used by the opportunity blender to prevent double-counting of technical setups when Composite and EQ would otherwise both push the row higher from the same price-action story.
    risk_score: Optional[float] = None  # Standalone risk dimension (0-100, higher=riskier). Not used in composite_score or opportunity_score — exposed separately so operators can filter/sort by risk independently of "is this a good opportunity". See ScreeningEngine._compute_risk_score for the component breakdown.
    risk_components: Optional[Dict[str, float]] = field(default=None)  # Per-sub-component risk breakdown (vol, drawdown, liquidity, beta, leverage, profitability, short_pressure, asset_class_baseline). Each value is the same 0-100 scale; None if insufficient data.
    asset_class: Optional[str] = None  # "equity" | "etf" | "adr" | "unknown"; snapshotted at screen time so later analysis doesn't need to cross-join ticker_metadata (which may rotate).
    factor_scorecard: Optional[Dict[str, float]] = field(default=None)  # 7-family scorecard
    signal_coverage_pct: Optional[float] = None
    tier_reached: Optional[str] = None


class ScreeningEngine:
    """
    Algorithmic screening engine for watchlist scanning.

    Usage:
        engine = ScreeningEngine(config, db)
        results = engine.scan(tickers=["AAPL", "MSFT", ...], date="2026-01-27")
    """

    # Tier 1 signals — computed from batch OHLCV data (no extra API calls)
    TIER1_SIGNALS = [
        "volume_surge", "rsi_oversold", "rsi_overbought", "ma_crossover",
        "relative_strength", "bollinger_squeeze", "trend_strength",
        "residual_momentum_12_1",
    ]

    # Tier 2 signals — require .info or individual API calls (expensive)
    TIER2_SIGNALS = [
        "price_vs_target", "earnings_proximity", "insider_buying",
        "estimate_momentum", "rating_momentum",
        "quality_factor", "income_factor", "valuation_gap",
        "short_pressure",
    ]

    # Enhanced signals — from yfinance_extended (only for funnel survivors)
    ENHANCED_SIGNALS = [
        "options_sentiment", "smart_money",
        "pead_drift", "weekly_trend_alignment",
    ]

    FUNNEL_GATED_SIGNALS = ["weekly_trend_alignment"]

    SIGNAL_NAMES = TIER1_SIGNALS + TIER2_SIGNALS + ENHANCED_SIGNALS

    # Signals whose information content is also captured by the Entry Quality
    # sub-signals (RSI sweet spot, Bollinger position, volatility contraction,
    # MACD phase, volume dryup). Excluding these from ``composite_fundamental``
    # prevents the Opportunity Score from double-counting the same technical
    # setup once via Composite and again via Entry Quality, which previously
    # caused high-tech-momentum names to dominate the leaderboard even when
    # fundamentals/catalyst signals were average.
    _EQ_OVERLAP_SIGNALS = frozenset({
        "rsi_oversold",
        "rsi_overbought",
        "bollinger_squeeze",
        "ma_crossover",
        "trend_strength",
        "volume_surge",
    })

    def __init__(self, config: dict = None, db=None):
        from tradingagents.default_config import DEFAULT_CONFIG
        self.config = config or DEFAULT_CONFIG
        self.db = db
        self.cache = get_cache()
        self._screening_config = self.config.get("screening", {})
        self._lock = threading.Lock()
        self._skipped: List[Dict[str, str]] = []  # tracks {ticker, reason}
        self._breaker_wait_used = 0.0
        self.last_run_id: Optional[int] = None

    def scan(
        self,
        tickers: List[str],
        date: str = None,
        weights: Dict[str, float] = None,
        preset: str = None,
        watchlist_id: int = None,
        enable_funnel: bool = True,
        enable_enhanced: bool = True,
        funnel_cutoff_pct: Optional[float] = None,
        enhanced_top_pct: Optional[float] = None,
        enhanced_min_count: Optional[int] = None,
        chunk_sleep_seconds: Optional[float] = None,
        info_chunk_sleep_seconds: Optional[float] = None,
        criteria_meta: Optional[Dict[str, Any]] = None,
        time_budget_seconds: Optional[float] = None,
        progress_cb=None,
    ) -> List[ScreeningResult]:
        """
        Run a screening scan on a list of tickers.

        Args:
            tickers: List of ticker symbols to screen
            date: Reference date (default: today)
            weights: Custom signal weights (overrides config)
            preset: Named preset
            watchlist_id: Optional watchlist ID (for tracking)
            enable_funnel: Use funnel scoring (skip expensive signals for low-tier tickers)
            enable_enhanced: Compute enhanced signals for top tickers

        Returns:
            List of ScreeningResult, sorted by composite_score descending
            (or reversal score when criteria_meta.strategy is reversal_buildup)
        """
        if not tickers:
            return []
        self.last_run_id = None

        with self._lock:
            self._skipped = []
        self._breaker_wait_used = 0.0
        t_start = time.monotonic()

        from tradingagents.screening.discovery import parse_watchlist_tickers

        valid, rejected = parse_watchlist_tickers(tickers)
        for bad in rejected:
            self._skipped.append({"ticker": str(bad), "reason": "invalid_ticker"})
        tickers = valid
        if not tickers:
            return []

        def _emit_progress(phase: str, current: int, total: int) -> None:
            if not progress_cb:
                return
            try:
                progress_cb({"phase": phase, "current": int(current), "total": max(1, int(total))})
            except Exception:
                logger.debug("scan progress callback failed", exc_info=True)
        date = date or datetime.now().strftime("%Y-%m-%d")

        strategy_name = str((criteria_meta or {}).get("strategy") or "").strip()
        is_reversal = strategy_name == "reversal_buildup"
        is_early_momentum = strategy_name in {"early_momentum", "early_momentum_union"}
        is_base = strategy_name in {"base_coil", "base_coil_union"}
        is_multi = strategy_name == "multi_sleeve"
        if is_reversal or is_base:
            # Force inside scan() so API, CLI, and tests cannot leave enhanced
            # Yahoo fetches on. Reversal and Base ranking are OHLCV-only.
            enable_enhanced = False
        # Early momentum keeps enhanced on (unlike reversal and base).

        # ---------------------------------------------------------------
        # Deadline / time-budget guard
        # ---------------------------------------------------------------
        # A single hung yfinance worker can pin this scan for hours. The
        # caller (scheduler) sets a wall-clock budget; we check it at
        # cheap phase boundaries and bail out with whatever partial
        # results we have so the scheduler can move on.
        budget = float(time_budget_seconds) if time_budget_seconds else 0.0
        deadline = t_start + budget if budget > 0 else None

        def _deadline_exceeded(stage: str) -> bool:
            if deadline is None:
                return False
            if time.monotonic() < deadline:
                return False
            logger.warning(
                "scan: time budget (%.0fs) exhausted at stage=%s; bailing out",
                budget,
                stage,
            )
            return True

        # Resolve weights  (priority: explicit weights > explicit preset > watchlist default_preset > global)
        presets_cfg = self._screening_config.get("presets", {})
        if preset and preset in presets_cfg:
            weights = presets_cfg[preset]["weights"]
        elif weights is None:
            # Check if the watchlist defines a default preset
            wl_preset = None
            if watchlist_id is not None and self.db:
                try:
                    wl = self.db.get_watchlist(watchlist_id)
                    if wl:
                        wl_preset = wl.get("default_preset")
                except Exception as e:
                    logger.debug("Watchlist DB lookup failed: %s", e)
            if wl_preset and wl_preset in presets_cfg:
                weights = presets_cfg[wl_preset]["weights"]
                preset = wl_preset  # So logs show the resolved preset name
            else:
                weights = self._screening_config.get("signal_weights", {})

        # Normalize weights to sum to 1.0
        total_weight = sum(weights.values()) or 1.0
        weights = {k: v / total_weight for k, v in weights.items()}
        base_weights = dict(weights)

        # Optional adaptive blend (Phase 4)
        run_criteria_meta: Dict[str, Any] = dict(criteria_meta or {})
        blend_cfg = self._screening_config.get("adaptive_blend", {})
        if blend_cfg.get("enabled"):
            adaptive = self.compute_adaptive_weights(min_samples=int(blend_cfg.get("min_samples", 30)))
            if adaptive:
                ratio = float(blend_cfg.get("ratio", 0.25))
                merged = {}
                all_keys = set(weights) | set(adaptive)
                for k in all_keys:
                    bw = weights.get(k, 0.0)
                    aw = adaptive.get(k, 0.0)
                    merged[k] = (1.0 - ratio) * bw + ratio * aw
                tw = sum(merged.values()) or 1.0
                weights = {k: v / tw for k, v in merged.items()}
                run_criteria_meta["adaptive_blend_applied"] = True
                run_criteria_meta["adaptive_blend_ratio"] = ratio
            else:
                run_criteria_meta["adaptive_blend_applied"] = False
        else:
            run_criteria_meta["adaptive_blend_applied"] = False

        # Enforce max watchlist size before any expensive metadata work.
        max_size = self._screening_config.get("hard_max_watchlist_size", 2000)
        tickers = [t.upper().strip() for t in tickers[:max_size] if t.strip()]

        # Skip tickers known to be evicted by ticker_health (e.g. delisted via
        # M&A like ANSS→SNPS). These would otherwise burn yfinance rate-limit
        # budget on every scan only to fail. The filter is opt-in via
        # ``ticker_health.skip_evicted`` (default True) and respects an
        # optional freshness window so stale evictions can age out naturally.
        health_cfg = self._screening_config.get("ticker_health", {}) or {}
        skip_evicted = bool(health_cfg.get("skip_evicted", True))
        if skip_evicted and self.db and tickers:
            try:
                evict_since = health_cfg.get("skip_evicted_within_days")
                evicted_set = self.db.get_evicted_tickers(
                    since_days=int(evict_since) if evict_since else None
                )
                if evicted_set:
                    before = len(tickers)
                    tickers = [t for t in tickers if t not in evicted_set]
                    skipped = before - len(tickers)
                    if skipped:
                        logger.info(
                            "ticker_health: skipped %d evicted ticker(s) before scan",
                            skipped,
                        )
            except Exception:
                logger.debug("ticker_health: evicted-set lookup failed; proceeding", exc_info=True)

        # --- Per-ticker weight resolution via metadata ---
        # If no explicit preset/weights override was provided, resolve per-ticker
        # weights from cached metadata so mixed watchlists get per-ticker treatment.
        per_ticker_weights: Dict[str, Dict[str, float]] = {}  # ticker → weights
        meta_map: Dict[str, Dict[str, Any]] = {}  # ticker → metadata (reused by Phase 4.5 macro overlay)
        use_per_ticker = (preset is None) and self.db
        if use_per_ticker:
            try:
                from tradingagents.screening.discovery import canonical_preset_name
                from tradingagents.screening.ticker_resolver import resolve_and_cache
                # Large watchlists need a longer wall-clock budget so metadata
                # resolution completes before per-ticker weights are applied.
                meta_budget = min(120.0, max(30.0, len(tickers) * 0.35))
                meta_mode = str(
                    self._screening_config.get("metadata_resolve_during_scan") or "use_cached"
                ).strip().lower()
                meta_cached_days = int(
                    self._screening_config.get("metadata_cached_max_age_days") or 30
                )
                meta_map = resolve_and_cache(
                    tickers,
                    self.db,
                    max_workers=8,
                    max_age_days=7,
                    total_budget_seconds=meta_budget,
                    refresh_stale=(meta_mode == "refresh_stale"),
                    cached_max_age_days=meta_cached_days,
                )
                for t, meta in meta_map.items():
                    resolved_preset = canonical_preset_name(meta.get("resolved_preset"))
                    if resolved_preset and resolved_preset in presets_cfg:
                        tw = dict(presets_cfg[resolved_preset]["weights"])
                        tw_total = sum(tw.values())
                        if tw_total <= 0:
                            tw_total = 1.0
                        per_ticker_weights[t.upper()] = {k: v / tw_total for k, v in tw.items()}
                if per_ticker_weights:
                    profiles_used = {}
                    for t, meta in meta_map.items():
                        p = canonical_preset_name(meta.get("resolved_preset") or "default") or "default"
                        profiles_used[p] = profiles_used.get(p, 0) + 1
                    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(profiles_used.items()))
                    logger.info("Per-ticker weights: %d resolved (%s)", len(per_ticker_weights), breakdown)
            except Exception as e:
                logger.warning("Per-ticker weight resolution failed: %s", e)
                per_ticker_weights = {}

        per_ticker_preset: Dict[str, str] = {}
        if use_per_ticker:
            from tradingagents.screening.discovery import canonical_preset_name

            for t, meta in meta_map.items():
                resolved = canonical_preset_name(meta.get("resolved_preset"))
                if resolved and resolved in presets_cfg:
                    per_ticker_preset[t.upper()] = str(resolved)

        def _get_weights(ticker: str) -> Dict[str, float]:
            """Return per-ticker weights if available, otherwise global weights."""
            return per_ticker_weights.get(ticker.upper(), weights)

        def _resolved_preset_name(ticker: str) -> str:
            if preset:
                return str(preset)
            return per_ticker_preset.get(ticker.upper()) or "default"

        # Runtime performance knobs (config-default, per-call override)
        funnel_cutoff = float(
            self._screening_config.get("funnel_cutoff_pct", _DEFAULT_FUNNEL_CUTOFF_PCT)
            if funnel_cutoff_pct is None
            else funnel_cutoff_pct
        )
        funnel_cutoff = max(0.10, min(0.95, funnel_cutoff))
        enhanced_top = float(
            self._screening_config.get("enhanced_top_pct", 0.30)
            if enhanced_top_pct is None
            else enhanced_top_pct
        )
        enhanced_top = max(0.05, min(0.95, enhanced_top))
        enhanced_min = int(
            self._screening_config.get("enhanced_min_count", 15)
            if enhanced_min_count is None
            else enhanced_min_count
        )
        enhanced_min = max(1, enhanced_min)
        chunk_sleep = float(
            self._screening_config.get("chunk_sleep_seconds", 1.0)
            if chunk_sleep_seconds is None
            else chunk_sleep_seconds
        )
        chunk_sleep = max(0.0, chunk_sleep)
        info_chunk_sleep = float(
            self._screening_config.get("info_chunk_sleep_seconds", 0.5)
            if info_chunk_sleep_seconds is None
            else info_chunk_sleep_seconds
        )
        info_chunk_sleep = max(0.0, info_chunk_sleep)
        tier2_max_tickers = max(0, int(self._screening_config.get("tier2_max_tickers", 700) or 0))
        tier2_aux_max_tickers = max(0, int(self._screening_config.get("tier2_aux_max_tickers", 300) or 0))
        # Like tier2_max_tickers, bound the absolute enhanced-signal pool so
        # runtime/API-call volume doesn't scale linearly with universe size
        # (enhanced_top_pct alone made this 597->960 tickers when the union
        # universe grew 2000->3220). 0 disables the cap.
        enhanced_max_tickers = max(0, int(self._screening_config.get("enhanced_max_tickers", 600) or 0))
        skip_sparse_fundamentals_aux = bool(
            self._screening_config.get("skip_sparse_fundamentals_aux", True)
        )
        sparse_fundamentals_skipped = 0

        logger.info("Scanning %d tickers (date=%s, preset=%s)", len(tickers), date, preset or "custom")

        funnel_tickers: set = set()
        aux_tickers: set = set()
        eliminated: set = set()
        enhanced_tickers_set: set = set()

        # Fetch macro regime for weight adjustments
        regime = self._get_regime(date)

        # Regime-adjusted weight modifiers — applied after macro overlay (Phase 0F)
        regime_cfg = self._screening_config.get("regime_adjustments", {})
        regime_strength_default = float(regime_cfg.get("strength", 0.15))
        regime_strength = regime_strength_default

        def _regime_adjust(w, boost_signals, dampen_signals, strength):
            """Apply regime-based weight adjustment and re-normalize."""
            adj = dict(w)
            for s in boost_signals:
                if s in adj:
                    adj[s] = adj[s] * (1 + strength)
            for s in dampen_signals:
                if s in adj:
                    adj[s] = adj[s] * (1 - strength)
            total = sum(adj.values())
            if total <= 0:
                total = 1.0
            return {k: v / total for k, v in adj.items()}

        _defensive = {"trend_strength", "insider_buying", "valuation_gap", "price_vs_target", "smart_money"}
        _momentum = {"volume_surge", "relative_strength", "bollinger_squeeze", "ma_crossover"}

        # --- Chunked batch fetch for large watchlists ---
        # The YFinance limiter is re-checked between every chunk; if the
        # breaker tripped during a previous chunk, remaining tickers are
        # deferred (marked skipped) rather than burning quota on guaranteed
        # failures. This is what makes a 3k-ticker scan complete-or-degrade
        # gracefully instead of hanging for 20+ minutes.
        batch_data: Dict[str, Dict[str, Any]] = {}
        chunks = [tickers[i:i + _CHUNK_SIZE] for i in range(0, len(tickers), _CHUNK_SIZE)]
        limiter = get_yfinance_limiter()
        with ScreeningOhlcvSession():
            for ci, chunk in enumerate(chunks):
                if _deadline_exceeded(f"ohlcv_chunk_{ci+1}/{len(chunks)}"):
                    for t in chunk:
                        self._skipped.append({"ticker": t, "reason": "deadline_exceeded"})
                    continue
                if len(chunks) > 1:
                    logger.info("Fetching chunk %d/%d (%d tickers)", ci + 1, len(chunks), len(chunk))
                _emit_progress("ohlcv", ci + 1, len(chunks))
                if limiter.is_open() and self._wait_for_yfinance_breaker(limiter, f"ohlcv_chunk_{ci+1}/{len(chunks)}"):
                    logger.warning(
                        "Deferring OHLCV chunk %d/%d: yfinance breaker still open after wait budget",
                        ci + 1,
                        len(chunks),
                    )
                    for t in chunk:
                        self._skipped.append({"ticker": t, "reason": "deferred_breaker_open"})
                    continue
                chunk_data = self._fetch_batch_ohlcv(chunk, date)
                batch_data.update(chunk_data)
                if chunk_sleep > 0 and ci < len(chunks) - 1:
                    time.sleep(chunk_sleep)

        if is_reversal and batch_data:
            from tradingagents.screening.reversal_buildup import truncate_batch_data
            truncate_batch_data(batch_data, date)
        if is_early_momentum and batch_data:
            from tradingagents.screening.early_momentum import truncate_batch_data as _em_truncate
            _em_truncate(batch_data, date, cfg=self._screening_config.get("early_momentum"))
            self._attach_momentum_benchmarks(batch_data, date)
        if is_multi and batch_data:
            self._attach_momentum_benchmarks(batch_data, date)

        # Record usage for visibility
        self._record_usage(len(tickers))

        # =====================================================================
        # PHASE 1: Tier 1 signals (OHLCV-only, no extra API calls)
        # =====================================================================
        tier1_scores: Dict[str, Dict] = {}  # ticker → {signals, score}
        for ticker in tickers:
            data = batch_data.get(ticker)
            if data is None:
                self._skipped.append({"ticker": ticker, "reason": "no_data"})
                continue
            try:
                signals = self._compute_tier1_signals(ticker, data, regime)
                # Compute a preliminary score using only Tier 1 weights (per-ticker aware)
                tw = _get_weights(ticker)
                t1_weights = {k: tw.get(k, 0.0) for k in self.TIER1_SIGNALS}
                t1_total = sum(t1_weights.values()) or 1.0
                t1_weights_norm = {k: v / t1_total for k, v in t1_weights.items()}
                t1_score = sum(
                    _numeric_or_zero(signals.get(s)) * t1_weights_norm.get(s, 0.0)
                    for s in self.TIER1_SIGNALS
                )
                tier1_scores[ticker] = {"signals": signals, "score": t1_score, "data": data}
            except Exception as e:
                self._skipped.append({"ticker": ticker, "reason": str(e)[:80]})
                logger.warning("%s Tier 1 failed: %s", ticker, e, exc_info=True)

        # =====================================================================
        # PHASE 1.5: Macro-Tactical Overlay (before Tier 2 / enhanced fetches)
        # =====================================================================
        # Runs immediately after Tier 1 so scheduler time budgets cannot exhaust
        # during OHLCV+.info work before macro_fit is computed. Global macro
        # data is cached (1 h); per-ticker scoring is pure Python.
        ticker_macro: Dict[str, Dict[str, Any]] = {}
        if _deadline_exceeded("macro_overlay"):
            logger.warning("Skipping macro overlay: deadline exceeded")
        else:
            ticker_macro = self._compute_macro_overlay(tier1_scores, meta_map, as_of_date=date)

        # Phase 0F: reduce regime shift when macro_fit overlay is materially present.
        #
        # macro_fit_present/regime_strength below stay as an aggregate (any-ticker)
        # signal purely for the `weights` global fallback (used only for tickers
        # that didn't resolve a per-ticker profile) and for telemetry. For the
        # dominant per_ticker_weights path, strength is now decided per ticker
        # from *that ticker's own* macro_fit deviation — previously a single
        # outlier ticker's macro_fit tripped the reduced-strength branch for
        # every ticker in the batch, which at Scan All scale (hundreds/thousands
        # of tickers spanning many sectors) made "reduced strength" the de facto
        # permanent state rather than a real presence/absence switch.
        macro_dev_threshold = float(regime_cfg.get("macro_fit_deviation_threshold", 5))
        regime_strength_reduced = float(regime_cfg.get("strength_when_macro_present", 0.08))

        def _macro_fit_deviates(ticker: str) -> bool:
            entry = ticker_macro.get(ticker.upper())
            if not entry:
                return False
            mf = entry.get("macro_fit")
            return mf is not None and abs(float(mf) - 50.0) >= macro_dev_threshold

        def _ticker_regime_strength(ticker: str) -> float:
            return regime_strength_reduced if _macro_fit_deviates(ticker) else regime_strength_default

        macro_fit_present = any(_macro_fit_deviates(t) for t in ticker_macro)
        if macro_fit_present:
            regime_strength = regime_strength_reduced
        reduced_count = sum(1 for t in per_ticker_weights if _macro_fit_deviates(t))
        run_criteria_meta["macro_fit_present"] = macro_fit_present
        run_criteria_meta["regime_strength_applied"] = regime_strength
        run_criteria_meta["regime_strength_reduced_ticker_count"] = reduced_count
        run_criteria_meta["regime_strength_full_ticker_count"] = max(0, len(per_ticker_weights) - reduced_count)

        if regime in ("bear", "risk_off") and regime_strength_default > 0:
            weights = _regime_adjust(weights, _defensive, _momentum, regime_strength)
            per_ticker_weights = {
                t: _regime_adjust(w, _defensive, _momentum, _ticker_regime_strength(t))
                for t, w in per_ticker_weights.items()
            }
            logger.info(
                "Regime: %s — weight shift applied (defensive↑, momentum↓); "
                "%d/%d tickers at reduced strength (macro_fit present)",
                regime, reduced_count, len(per_ticker_weights),
            )
        elif regime in ("bull", "risk_on") and regime_strength_default > 0:
            _momentum_bull = _momentum | {"options_sentiment"}
            _defensive_bull = {"trend_strength", "insider_buying", "valuation_gap", "price_vs_target"}
            weights = _regime_adjust(weights, _momentum_bull, _defensive_bull, regime_strength)
            per_ticker_weights = {
                t: _regime_adjust(w, _momentum_bull, _defensive_bull, _ticker_regime_strength(t))
                for t, w in per_ticker_weights.items()
            }
            logger.info(
                "Regime: %s — weight shift applied (momentum↑, defensive↓); "
                "%d/%d tickers at reduced strength (macro_fit present)",
                regime, reduced_count, len(per_ticker_weights),
            )
        else:
            logger.info("Regime: %s — no weight adjustment", regime)

        # =====================================================================
        # PHASE 2: Funnel — determine which tickers get expensive signals
        # =====================================================================
        # Income/value/quality books cannot rank on T1 technicals. On small
        # lists send everyone to T2; on large lists raise the cutoff.
        prev_funnel = enable_funnel
        prev_cut = funnel_cutoff
        enable_funnel, funnel_cutoff = apply_identity_funnel_policy(
            preset,
            len(tier1_scores),
            enable_funnel,
            funnel_cutoff,
            bypass_below=int(self._screening_config.get("identity_funnel_bypass_below", 150) or 150),
            min_cut=float(self._screening_config.get("identity_funnel_min_cutoff", 0.85) or 0.85),
        )
        if prev_funnel and not enable_funnel:
            logger.info(
                "Funnel: identity preset %s n=%d ≤ bypass — T2 for all",
                preset, len(tier1_scores),
            )
        elif prev_funnel and funnel_cutoff != prev_cut:
            logger.info(
                "Funnel: identity preset %s n=%d — cutoff raised to %.2f",
                preset, len(tier1_scores), funnel_cutoff,
            )

        sorted_t1 = sorted(tier1_scores.items(), key=lambda x: x[1]["score"], reverse=True)
        if enable_funnel and len(tier1_scores) > 10:
            # If all Tier 1 scores are identical, skip funnel (no meaningful differentiation)
            if sorted_t1[0][1]["score"] == sorted_t1[-1][1]["score"]:
                funnel_ranked = [t for t, _ in sorted_t1]
                funnel_tickers = set(funnel_ranked)
                eliminated = set()
                logger.info("Funnel: bypassed (all Tier 1 scores equal)")
            else:
                cutoff_idx = max(5, int(len(sorted_t1) * funnel_cutoff))
                funnel_ranked = [t for t, _ in sorted_t1[:cutoff_idx]]
                funnel_tickers = set(funnel_ranked)
                eliminated = set(t for t, _ in sorted_t1[cutoff_idx:])
                logger.info("Funnel: %d proceed to Tier 2, %d fast-tracked", len(funnel_tickers), len(eliminated))
        else:
            funnel_ranked = [t for t, _ in sorted_t1]
            funnel_tickers = set(funnel_ranked)
            eliminated = set()

        if tier2_max_tickers > 0 and len(funnel_ranked) > tier2_max_tickers:
            downgraded = set(funnel_ranked[tier2_max_tickers:])
            eliminated.update(downgraded)
            funnel_ranked = funnel_ranked[:tier2_max_tickers]
            funnel_tickers = set(funnel_ranked)
            logger.info(
                "Tier 2 cap applied: %d/%d funnel survivors enriched with info",
                len(funnel_ranked),
                len(funnel_ranked) + len(downgraded),
            )

        # =====================================================================
        # PHASE 3: Tier 2 signals (requires .info — only for funnel survivors)
        # =====================================================================
        # Fetch .info only for funnel tickers (saves ~40% of API calls)
        funnel_list = list(funnel_ranked)
        aux_tickers = set(funnel_list)
        aux_data: Dict[str, Dict[str, Any]] = {}
        if tier2_aux_max_tickers > 0 and len(funnel_list) > tier2_aux_max_tickers:
            aux_tickers = set(funnel_list[:tier2_aux_max_tickers])
            logger.info(
                "Tier 2 auxiliary cap applied: estimate/rating fetch limited to %d tickers",
                len(aux_tickers),
            )
        if funnel_list:
            info_chunks = [funnel_list[i:i + _CHUNK_SIZE] for i in range(0, len(funnel_list), _CHUNK_SIZE)]
            for ic, info_chunk in enumerate(info_chunks):
                if _deadline_exceeded(f"info_chunk_{ic+1}/{len(info_chunks)}"):
                    break
                _emit_progress("fundamentals", ic + 1, len(info_chunks))
                info_data = self._fetch_info_cached(info_chunk, date)
                for t in info_chunk:
                    if t in batch_data:
                        info = info_data.get(t, {}) or {}
                        batch_data[t]["info"] = info
                        sparse = skip_sparse_fundamentals_aux and self._has_sparse_fundamentals(info)
                        if t in aux_tickers and sparse:
                            sparse_fundamentals_skipped += 1
                if info_chunk_sleep > 0 and ic < len(info_chunks) - 1:
                    time.sleep(info_chunk_sleep)

        eligible_aux_tickers = [
            ticker
            for ticker in aux_tickers
            if ticker in batch_data
            and not (
                skip_sparse_fundamentals_aux
                and self._has_sparse_fundamentals(batch_data[ticker].get("info", {}))
            )
        ]
        if eligible_aux_tickers:
            aux_data = self._fetch_tier2_auxiliary(eligible_aux_tickers)
        for ticker in funnel_list:
            if ticker not in batch_data:
                continue
            auxiliary = aux_data.get(ticker, {})
            batch_data[ticker]["estimate_revisions"] = auxiliary.get("estimate_revisions", {})
            batch_data[ticker]["rating_changes"] = auxiliary.get("rating_changes", {})

        if sparse_fundamentals_skipped > 0:
            logger.info(
                "Sparse-fundamentals guard: skipped estimate/rating fetch for %d tickers",
                sparse_fundamentals_skipped,
            )
            if self.db and hasattr(self.db, "record_runtime_metric"):
                try:
                    self.db.record_runtime_metric(
                        "scan_sparse_fundamentals_skipped",
                        float(sparse_fundamentals_skipped),
                        context={
                            "date": date,
                            "preset": preset or "custom",
                            "watchlist_id": watchlist_id,
                        },
                    )
                except Exception as e:
                    logger.debug("Failed to record sparse fundamentals metric: %s", e)

        t2_live_medians = self._live_medians_from_info_batch(batch_data, funnel_list)
        persist_min = int(self._screening_config.get("sector_median_persist_min_tickers", 80) or 80)
        if t2_live_medians and len(funnel_list) >= persist_min:
            self._persist_sector_medians(t2_live_medians, universe_size=len(funnel_list))

        for ticker in funnel_list:
            data = batch_data.get(ticker)
            if data is None:
                continue
            try:
                t2_signals = self._compute_tier2_signals(
                    ticker, data, regime, sector_medians_all=t2_live_medians,
                )
                tier1_scores[ticker]["signals"].update(t2_signals)
            except Exception as e:
                logger.warning("%s Tier 2 failed: %s", ticker, e)

        # For eliminated tickers, set most Tier 2 signals to 0 (neutral).
        # Quality/income stay None — a fake 0.0 looks like real low quality.
        for ticker in eliminated:
            for sig in self.TIER2_SIGNALS:
                if sig in _TIER2_ZERO_FILL_EXCLUDE:
                    tier1_scores[ticker]["signals"].setdefault(sig, None)
                else:
                    tier1_scores[ticker]["signals"][sig] = 0.0

        # =====================================================================
        # PHASE 4: Enhanced signals (only for top candidates)
        # =====================================================================
        enhanced_data: Optional[Dict[str, Any]] = None
        weekly_computed_tickers: set = set()
        if enable_enhanced and not _deadline_exceeded("enhanced_signals"):
            # Top-N enhanced set is configurable (percent + minimum floor)
            all_sorted = sorted(tier1_scores.items(), key=lambda x: x[1]["score"], reverse=True)
            enhanced_count = max(enhanced_min, int(len(all_sorted) * enhanced_top))
            enhanced_count = min(len(all_sorted), enhanced_count)
            if enhanced_max_tickers > 0:
                enhanced_count = min(enhanced_count, enhanced_max_tickers)
            enhanced_tickers = [t for t, _ in all_sorted[:enhanced_count]]
            enhanced_tickers_set = set(enhanced_tickers)
            if enhanced_tickers:
                logger.info("Enhanced signals for top %d tickers", len(enhanced_tickers))
                enhanced_data = self._fetch_enhanced_data(enhanced_tickers)

                # Build dynamic sector medians (fwd P/E, P/S, EV/EBITDA) from this batch
                live_sector_medians_all = self._compute_live_sector_medians(enhanced_data)
                live_sector_medians = live_sector_medians_all.get("forward_pe", {})

                from tradingagents.dataflows.yfinance_extended import get_weekly_technicals
                from tradingagents.screening.weekly_alignment import score_weekly_alignment

                weekly_tickers = enhanced_tickers
                if (criteria_meta or {}).get("source_mode") == "union_buckets":
                    weekly_mtf_cfg = (
                        self._screening_config.get("scan_all", {}).get("weekly_mtf", {})
                    )
                    weekly_cap = max(0, int(weekly_mtf_cfg.get("hydrate_top_n_cap", 60)))
                    weekly_tickers = enhanced_tickers[:weekly_cap]
                    logger.info(
                        "Union Scan All: weekly MTF precompute limited to %d/%d candidates",
                        len(weekly_tickers),
                        len(enhanced_tickers),
                    )
                weekly_tickers_set = set(weekly_tickers)

                for ticker in enhanced_tickers:
                    enh = enhanced_data.get(ticker, {})
                    try:
                        enh_signals, vg_detail = self._compute_enhanced_signals(
                            ticker, enh,
                            batch_data.get(ticker, {}).get("info", {}),
                            sector_medians=live_sector_medians,
                            sector_medians_all=live_sector_medians_all,
                        )
                        tier1_scores[ticker]["signals"].update(enh_signals)
                        tier1_scores[ticker]["valuation_gap_detail"] = vg_detail
                    except Exception as e:
                        logger.warning("%s enhanced signals failed: %s", ticker, e)
                    if ticker not in weekly_tickers_set:
                        continue
                    try:
                        weekly_data = get_weekly_technicals(ticker, as_of_date=date)
                        # A non-throwing fetch can still return no weekly
                        # history (e.g. a recent IPO or delisted ticker).
                        # Keep unknown distinct from a genuinely bearish 0.0.
                        if (
                            weekly_data.get("weekly_close") is not None
                            and weekly_data.get("confluence_score") is not None
                        ):
                            tier1_scores[ticker]["signals"]["weekly_trend_alignment"] = (
                                score_weekly_alignment(weekly_data)
                            )
                            weekly_computed_tickers.add(ticker)
                    except Exception as e:
                        logger.debug("Weekly alignment for %s failed: %s", ticker, e)

        # Uncomputed enhanced signals stay None — same contract as weekly,
        # quality, and income. A fake 0.0 looks like "not cheap / no flow /
        # no PEAD" and still enters the composite denominator.
        for ticker in tier1_scores:
            for sig in self.ENHANCED_SIGNALS:
                tier1_scores[ticker]["signals"].setdefault(sig, None)

        # =====================================================================
        # PHASE 4.1: Entry Quality Score (must run before batch_data cleanup)
        # =====================================================================
        entry_quality_map: Dict[str, tuple] = {}
        for ticker, entry in tier1_scores.items():
            data = entry.get("data")
            if data:
                try:
                    eq_score, eq_signals = self._compute_entry_quality(
                        data["close"], data["high"], data["low"], data["volume"]
                    )
                    entry_quality_map[ticker] = (eq_score, eq_signals)
                except Exception as e:
                    logger.debug("Entry quality for %s failed: %s", ticker, e)

        if entry_quality_map:
            logger.info("Entry quality: scored %d/%d tickers", len(entry_quality_map), len(tier1_scores))

        # =====================================================================
        # PHASE 4.2: Risk Score (must run before batch_data cleanup — uses OHLCV + .info)
        # =====================================================================
        # Independent dimension from composite / entry_quality / macro_fit.
        # Wrapped in per-ticker try/except so one bad symbol can't mask the
        # risk column on the rest of the batch.
        # Import classifier lazily so unit tests that call _compute_risk_score
        # in isolation don't need ticker_resolver on the import path.
        from tradingagents.screening.ticker_resolver import classify_asset_class

        risk_map: Dict[str, tuple] = {}
        _adv_write_count = 0  # IMP-1/IMP-7: tracks non-null avg_dollar_volume_usd writes for null_adv_rate_pct
        for ticker, entry in tier1_scores.items():
            data = entry.get("data")
            if data is None:
                continue
            try:
                t_meta = meta_map.get(ticker.upper(), meta_map.get(ticker, {})) or {}
                info = batch_data.get(ticker, {}).get("info", {}) if ticker in batch_data else {}
                # Prefer cached asset_class from ticker_metadata (fast path). If
                # the metadata row is pre-backfill (asset_class IS NULL) we
                # classify on-the-fly from the live yfinance .info payload we
                # already have in batch_data. This makes the new dimension
                # correct on every scan regardless of whether ticker_metadata
                # has been refreshed since the universe expansion landed — the
                # alternative (relying purely on the 7-day TTL) would silently
                # mis-route ETFs through the equity risk path for up to a week
                # after deployment, which is the correctness bug this block
                # guards against.
                asset_class = t_meta.get("asset_class")
                if not asset_class and info:
                    asset_class = classify_asset_class(info)
                if not asset_class:
                    asset_class = "equity"
                rs, rc = self._compute_risk_score(data.get("close"), info, asset_class)
                risk_map[ticker] = (rs, rc, asset_class)
                if self.db and info:
                    avg_vol = info.get("averageVolume") or info.get("averageDailyVolume10Day")
                    close_s = data.get("close")
                    if avg_vol and close_s is not None and len(close_s) > 0:
                        price = float(close_s.iloc[-1])
                        if price > 0:
                            dollar_adv = float(avg_vol) * price
                            try:
                                self.db.save_ticker_metadata(
                                    ticker,
                                    sector=info.get("sector") or t_meta.get("sector"),
                                    avg_dollar_volume_usd=round(dollar_adv, 2),
                                    asset_class=asset_class,
                                )
                                _adv_write_count += 1
                            except Exception as adv_err:
                                logger.debug("ADV metadata save failed for %s: %s", ticker, adv_err)
            except Exception as e:
                logger.debug("Risk score for %s failed: %s", ticker, e)

        if risk_map:
            logger.info("Risk score: scored %d/%d tickers", len(risk_map), len(tier1_scores))

        reversal_map: Dict[str, Dict[str, Any]] = {}
        momentum_map: Dict[str, Dict[str, Any]] = {}
        base_map: Dict[str, Dict[str, Any]] = {}
        run_reversal = is_reversal or is_multi
        run_momentum = is_early_momentum or is_multi
        run_base = is_base or is_multi
        if run_reversal:
            from tradingagents.screening.reversal_buildup import score_ticker as score_reversal
            from tradingagents.screening.multi_sleeve import reversal_ohlcv_for_score
            for ticker, entry in tier1_scores.items():
                data = entry.get("data") or batch_data.get(ticker) or {}
                if is_multi:
                    data = reversal_ohlcv_for_score(data, date, config=self.config)
                risk_entry = risk_map.get(ticker)
                risk_components_val = risk_entry[1] if risk_entry is not None else None
                try:
                    risk_score_val = risk_entry[0] if risk_entry is not None else None
                    reversal_map[ticker] = score_reversal(
                        data.get("close"),
                        data.get("high"),
                        data.get("low"),
                        data.get("volume"),
                        spy_close=data.get("spy_close"),
                        risk_components=risk_components_val,
                        risk_score=risk_score_val,
                        config=self.config,
                    )
                except Exception as rev_err:
                    logger.debug("Reversal buildup for %s failed: %s", ticker, rev_err)
            logger.info("Reversal buildup: scored %d/%d tickers", len(reversal_map), len(tier1_scores))
        if run_momentum:
            from tradingagents.screening.early_momentum import score_ticker as score_momentum
            for ticker, entry in tier1_scores.items():
                data = entry.get("data") or batch_data.get(ticker) or {}
                t_meta = meta_map.get(ticker.upper(), meta_map.get(ticker, {})) or {}
                risk_entry = risk_map.get(ticker)
                asset_class = risk_entry[2] if risk_entry is not None else t_meta.get("asset_class") or "equity"
                macro = ticker_macro.get(ticker, {})
                try:
                    momentum_map[ticker] = score_momentum(
                        data.get("close"),
                        data.get("high"),
                        data.get("low"),
                        data.get("volume"),
                        scan_date=date,
                        signals=entry.get("signals") or {},
                        meta=t_meta,
                        asset_class=str(asset_class or "equity"),
                        index_trend=macro.get("index_trend"),
                        index_stress=bool(macro.get("index_stress")),
                        config=self.config,
                        phase="pre",
                        ticker=ticker,
                        spy_close=data.get("spy_close"),
                        iwm_close=data.get("iwm_close"),
                        ufo_close=data.get("ufo_close"),
                        peer_closes=data.get("peer_closes"),
                    )
                except Exception as em_err:
                    logger.debug("Early momentum pre-score for %s failed: %s", ticker, em_err)
            logger.info("Early momentum: pre-scored %d/%d tickers", len(momentum_map), len(tier1_scores))
            if is_early_momentum or is_multi:
                self._last_batch_data = {
                    t: {
                        k: (entry.get("data") or batch_data.get(t) or {}).get(k)
                        for k in ("close", "high", "low", "volume", "spy_close", "iwm_close", "ufo_close", "peer_closes")
                    }
                    for t, entry in tier1_scores.items()
                }
                sample = next(iter(self._last_batch_data.values()), {}) or {}
                self._last_batch_data["_benchmarks"] = {
                    "spy_close": sample.get("spy_close"),
                    "iwm_close": sample.get("iwm_close"),
                    "ufo_close": sample.get("ufo_close"),
                    "peer_closes": sample.get("peer_closes") or {},
                }
        if run_base:
            from tradingagents.screening.base_coil import score_base
            for ticker, entry in tier1_scores.items():
                data = entry.get("data") or batch_data.get(ticker) or {}
                try:
                    base_map[ticker] = score_base(
                        data.get("close"),
                        data.get("high"),
                        data.get("low"),
                        data.get("volume"),
                        spy_close=data.get("spy_close"),
                        config=self.config,
                    )
                except Exception as base_err:
                    logger.debug("Base coil for %s failed: %s", ticker, base_err)
            logger.info("Base coil: scored %d/%d tickers", len(base_map), len(tier1_scores))

        self._apply_cross_sectional_zscore(tier1_scores, meta_map)

        # Free large DataFrames — no longer needed after signal computation
        del batch_data
        for entry in tier1_scores.values():
            entry.pop("data", None)

        # =====================================================================
        # PHASE 5: Signal momentum deltas
        # =====================================================================
        prior_signals = self._load_prior_signals(tickers)

        # =====================================================================
        # PHASE 6: Assemble final results
        # =====================================================================
        results: List[ScreeningResult] = []
        for ticker, entry in tier1_scores.items():
            signals = entry["signals"]
            tw = _get_weights(ticker)
            composite = self._compute_composite_score(signals, tw)
            composite_fundamental = self._compute_composite_fundamental_score(signals, tw)
            weekly_align = signals.get("weekly_trend_alignment")
            direction = self._determine_direction(
                signals, tw, weekly_alignment=weekly_align,
            )
            coverage_pct = self._compute_signal_coverage(
                signals,
                tw,
                presence_signals={
                    "weekly_trend_alignment",
                    "ma_crossover",
                    "trend_strength",
                    "residual_momentum_12_1",
                },
            )
            tier = self._resolve_tier_reached(
                ticker,
                enhanced_tickers=enhanced_tickers_set,
                funnel_tickers=funnel_tickers,
                aux_tickers=aux_tickers,
            )

            # Compute deltas from prior screening data
            # None = no prior data available; 0.0 = signal unchanged
            deltas = {}
            prior = prior_signals.get(ticker, {})
            has_prior = len(prior) > 0
            for sig_name, sig_val in signals.items():
                prev = prior.get(sig_name)
                if prev is not None and sig_val is not None:
                    deltas[sig_name] = round(sig_val - prev, 4)
                elif has_prior:
                    # Prior run existed but didn't have this signal (new signal, or no-data)
                    deltas[sig_name] = None
                # else: no prior run at all → omit from deltas (empty dict)

            # Macro-Tactical Overlay data (from Phase 4.5)
            macro_overlay = ticker_macro.get(ticker, {})

            # Entry Quality data (from Phase 4.1)
            eq_data = entry_quality_map.get(ticker, (None, None))

            # Risk score + per-component breakdown + asset class (from Phase 4.2).
            # Defaults to (None, None, resolved-class-or-equity) when risk
            # computation wasn't possible (no OHLCV + no .info) — still records
            # the asset_class so the UI can render the badge even when numerics
            # are absent.
            risk_entry = risk_map.get(ticker)
            if risk_entry is not None:
                risk_score_val, risk_components_val, risk_asset_class = risk_entry
            else:
                t_meta_fallback = meta_map.get(ticker.upper(), meta_map.get(ticker, {})) or {}
                risk_score_val, risk_components_val = None, None
                risk_asset_class = t_meta_fallback.get("asset_class") or "equity"

            scorecard = compute_factor_scorecard(signals, macro_fit_score=macro_overlay.get("macro_fit"))
            sig_out = {k: (round(v, 4) if isinstance(v, (int, float)) and v is not None else v)
                       for k, v in signals.items()}
            resolved_name = _resolved_preset_name(ticker)
            sleeve_ok = primary_sleeve_present(resolved_name, signals, tier_reached=tier)
            t_meta = meta_map.get(ticker.upper(), meta_map.get(ticker, {})) or {}
            sig_out["_screening_meta"] = {
                "signal_coverage_pct": coverage_pct,
                "tier_reached": tier,
                "valuation_gap_detail": entry.get("valuation_gap_detail"),
                "weekly_computed": ticker in weekly_computed_tickers,
                "resolved_preset": resolved_name,
                "weight_preset": resolved_name,
                "primary_sleeve_present": sleeve_ok,
                "market_cap_tier": t_meta.get("market_cap_tier"),
                "sector": t_meta.get("sector"),
                "ticker": ticker.upper(),
            }
            if ticker in reversal_map:
                sig_out["_reversal_buildup"] = reversal_map[ticker]
            if ticker in momentum_map:
                sig_out["_early_momentum"] = momentum_map[ticker]
            if ticker in base_map:
                sig_out["_base_coil"] = base_map[ticker]
            results.append(ScreeningResult(
                ticker=ticker,
                composite_score=round(composite, 2),
                direction=direction,
                signals=sig_out,
                signal_deltas=deltas,
                macro_fit=macro_overlay.get("macro_fit"),
                macro_breakdown=macro_overlay.get("macro_breakdown"),
                index_regime=macro_overlay.get("index_regime"),
                index_regime_label=macro_overlay.get("index_regime_label"),
                index_trend=macro_overlay.get("index_trend"),
                index_stress=macro_overlay.get("index_stress"),
                entry_quality=eq_data[0],
                entry_quality_signals=eq_data[1],
                composite_fundamental=round(composite_fundamental, 2),
                risk_score=risk_score_val,
                risk_components=risk_components_val,
                asset_class=risk_asset_class,
                factor_scorecard=scorecard,
                signal_coverage_pct=coverage_pct,
                tier_reached=tier,
            ))

        # Sort and rank
        if is_reversal:
            from tradingagents.screening.reversal_buildup import (
                apply_reversal_context,
                reversal_sort_key,
            )
            for r in results:
                payload = (r.signals or {}).get("_reversal_buildup")
                if isinstance(payload, dict):
                    apply_reversal_context(
                        payload,
                        composite_score=r.composite_score,
                        macro_fit=r.macro_fit,
                        direction=r.direction,
                        config=self.config,
                    )
            results.sort(key=lambda r: reversal_sort_key(
                (r.signals or {}).get("_reversal_buildup"), r.ticker
            ))
        elif is_early_momentum:
            from tradingagents.screening.early_momentum import momentum_sort_key
            results.sort(key=lambda r: momentum_sort_key(
                (r.signals or {}).get("_early_momentum"), r.ticker
            ))
        elif is_base:
            from tradingagents.screening.base_coil import base_sort_key
            results = [
                r for r in results
                if ((r.signals or {}).get("_base_coil") or {}).get("on_board")
            ]
            results.sort(key=lambda r: base_sort_key(
                (r.signals or {}).get("_base_coil"), r.ticker
            ))
        elif is_multi:
            from tradingagents.screening.reversal_buildup import apply_reversal_context
            from tradingagents.screening.multi_sleeve import attach_multi_sleeve_meta, multi_sleeve_sort_key
            for r in results:
                payload = (r.signals or {}).get("_reversal_buildup")
                if isinstance(payload, dict):
                    apply_reversal_context(
                        payload,
                        composite_score=r.composite_score,
                        macro_fit=r.macro_fit,
                        direction=r.direction,
                        config=self.config,
                    )
            attach_multi_sleeve_meta(results)
            results.sort(key=multi_sleeve_sort_key)
        else:
            results.sort(
                key=lambda r: (
                    0 if primary_sleeve_present(
                        ((r.signals or {}).get("_screening_meta") or {}).get("resolved_preset"),
                        r.signals,
                        tier_reached=((r.signals or {}).get("_screening_meta") or {}).get("tier_reached"),
                    ) else 1,
                    -r.composite_score,
                )
            )
        for i, r in enumerate(results, 1):
            r.rank = i

        # =====================================================================
        # PHASE 7: Cross-watchlist percentile ranking
        # =====================================================================
        self._apply_percentile_rankings(results)

        # IMP-1/IMP-7: liquidity ADV write-coverage honesty. Logs what fraction
        # of this scan's tickers did NOT get a fresh avg_dollar_volume_usd
        # write during Phase 4.2 (e.g. no .info payload available), so
        # operators can see when the Phase 3 liquidity gate is running on
        # stale/missing ADV for a large slice of the universe.
        if tier1_scores:
            run_criteria_meta["null_adv_rate_pct"] = round(
                100.0 * (1.0 - (_adv_write_count / len(tier1_scores))), 1
            )
            run_criteria_meta["adv_write_count"] = _adv_write_count

        # IMP-4: surface whether the Opp Score's macro penalty fired for any
        # ticker in this scan, alongside the regime-weight flags already
        # logged above (macro_fit_present / regime_strength_applied). This is
        # a *different* channel from the regime weight shift (which acts on
        # signal weights inside scan()) — the Opp Score penalty is applied
        # downstream by webapp/CLI callers of compute_opportunity_score(). We
        # recompute it here (cheap, pure function) purely for criteria-JSON
        # visibility so "no triple stacking" is verifiable from one place.
        try:
            from tradingagents.screening.opportunity_score import compute_opportunity_score as _opp_score_fn
            opp_macro_penalty_applied = False
            for r in results:
                weekly_align_val = (r.signals or {}).get("weekly_trend_alignment")
                _, opp_meta = _opp_score_fn(
                    r.composite_score, r.entry_quality, r.macro_fit,
                    composite_fundamental=r.composite_fundamental,
                    weekly_alignment=weekly_align_val,
                )
                if opp_meta.get("opp_macro_penalty_applied"):
                    opp_macro_penalty_applied = True
                    break
            run_criteria_meta["opp_macro_penalty_applied"] = opp_macro_penalty_applied
        except Exception as opp_flag_err:
            logger.debug("opp_macro_penalty_applied criteria flag failed: %s", opp_flag_err)

        elapsed = time.monotonic() - t_start
        per_ticker = elapsed / len(tickers) if tickers else 0
        logger.info("Completed: %d scored, %d skipped, %.1fs total (%.2fs/ticker)",
                    len(results), len(self._skipped), elapsed, per_ticker)
        if self._skipped:
            reasons: Dict[str, int] = {}
            for s in self._skipped:
                r = s["reason"]
                reasons[r] = reasons.get(r, 0) + 1
            for reason, count in reasons.items():
                logger.info("Skip reason: %s (%d)", reason, count)

        if (is_early_momentum or is_multi) and (criteria_meta or {}).get("run_enrich", True):
            try:
                results = self.finalize_early_momentum(
                    results,
                    scan_date=date,
                    cancel_check=(criteria_meta or {}).get("_cancel_check"),
                    progress_cb=(criteria_meta or {}).get("_progress_cb"),
                    enrich_cache=(criteria_meta or {}).get("_enrich_cache"),
                )
            except RuntimeError as em_err:
                if "cancelled" in str(em_err).lower():
                    logger.warning("Early momentum enrich cancelled during scan")
                    return []
                raise
            if is_multi:
                from tradingagents.screening.multi_sleeve import attach_multi_sleeve_meta, multi_sleeve_sort_key
                attach_multi_sleeve_meta(results)
                results.sort(key=multi_sleeve_sort_key)
                for i, r in enumerate(results, 1):
                    r.rank = i

        # Persist to database if available
        if self.db and watchlist_id is not None:
            try:
                # Include macro environment context for audit trail
                macro_ctx = {}
                index_fields: Dict[str, Any] = {}
                if ticker_macro:
                    sample = next(iter(ticker_macro.values()), {})
                    me = sample.get("macro_breakdown", {}).get("market_environment", {})
                    macro_ctx = {"market_env_score": me.get("score"), "regime": regime}
                try:
                    from tradingagents.dataflows.index_regime import index_regime_storage_fields

                    macro_snap = get_macro_snapshot(as_of_date=date)
                    index_fields = index_regime_storage_fields(macro_snap)
                    macro_ctx.update(index_fields)
                except Exception:
                    pass
                criteria_payload = {
                    "weights": weights,
                    "preset": preset,
                    "regime": regime,
                    "macro": macro_ctx,
                    **index_fields,
                }
                if criteria_meta:
                    criteria_payload.update(criteria_meta)
                if run_criteria_meta:
                    criteria_payload.update(run_criteria_meta)
                criteria = json.dumps(criteria_payload)
                run_id = self.db.save_screening_run(
                    watchlist_id=watchlist_id,
                    criteria=criteria,
                    ticker_count=len(tickers),
                    results_count=len(results),
                )
                self.db.save_screening_results(run_id, [asdict(r) for r in results])
                self.last_run_id = run_id
                logger.info("Saved run #%d with %d results", run_id, len(results))
            except Exception as e:
                logger.error("Failed to save results: %s", e)

        # ---------------------------------------------------------------
        # Persist corporate-action events into event_calendar (Plan B P3)
        # ---------------------------------------------------------------
        # Union Scan All persists projected leaderboard rows after this method
        # returns. Fetching corporate actions for every enhanced candidate here
        # is non-essential and can block that job for many minutes when Yahoo is
        # rate-limited; per-watchlist scans retain the event-calendar refresh.
        if (
            enhanced_data
            and self.db
            and (criteria_meta or {}).get("source_mode") != "union_buckets"
        ):
            try:
                from tradingagents.dataflows.yfinance_extended import get_corporate_actions
                for ticker, edata in enhanced_data.items():
                    try:
                        actions = get_corporate_actions(ticker)
                        days_away = actions.get("ex_div_days_to_event")
                        if days_away is not None:
                            self.db.upsert_event_calendar(
                                ticker=ticker,
                                event_type="ex_dividend",
                                event_date=actions.get("next_ex_div_date"),
                                days_to_event=int(days_away),
                                description=f"Ex-dividend date approaching in {days_away}d",
                            )
                        ep = edata.get("earnings_profile") or {}
                        ep_date = ep.get("next_earnings_date")
                        ep_days = ep.get("days_until_earnings")
                        if ep_date and ep_days is not None:
                            self.db.upsert_event_calendar(
                                ticker=ticker,
                                event_type="earnings",
                                event_date=ep_date,
                                days_to_event=int(ep_days),
                                description=f"Earnings in {ep_days}d",
                            )
                    except Exception as _ev_err:
                        logger.debug("event_calendar persist skipped for %s: %s", ticker, _ev_err)
            except Exception as _ec_err:
                logger.debug("event_calendar block failed: %s", _ec_err)

        return results

    def finalize_early_momentum(
        self,
        results: List[ScreeningResult],
        *,
        scan_date: str,
        cancel_check=None,
        progress_cb=None,
        enrich_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[ScreeningResult]:
        """Run bounded enrich pass and recompute score_final on scan results."""
        from dataclasses import replace
        from tradingagents.screening.early_momentum import momentum_sort_key
        from tradingagents.screening.early_momentum_enrich import enrich_and_finalize_rows

        rows = [asdict(r) for r in results]
        batch_data = getattr(self, "_last_batch_data", None) or {}
        finalized = enrich_and_finalize_rows(
            rows,
            scan_date=scan_date,
            batch_data=batch_data,
            config=self.config,
            cache=enrich_cache,
            cancel_check=cancel_check,
            progress_cb=progress_cb,
        )
        by_ticker = {str(r.get("ticker") or "").upper(): r for r in finalized}
        out: List[ScreeningResult] = []
        for orig in results:
            row = by_ticker.get(orig.ticker.upper())
            if not row:
                out.append(orig)
                continue
            sig = dict(orig.signals or {})
            em = (row.get("signals") or {}).get("_early_momentum")
            if isinstance(em, dict):
                sig["_early_momentum"] = em
            out.append(replace(orig, signals=sig))
        out.sort(key=lambda r: momentum_sort_key((r.signals or {}).get("_early_momentum"), r.ticker))
        reranked: List[ScreeningResult] = []
        for i, r in enumerate(out, 1):
            reranked.append(replace(r, rank=i))
        return reranked

    def _wait_for_yfinance_breaker(self, limiter, context: str) -> bool:
        """Sleep through an open Yahoo breaker if wait budget remains.

        Returns True when the breaker is still open and the caller should skip
        the current work. Returns False when it is safe to fetch.
        """
        if limiter is None:
            return False
        try:
            still_open = bool(limiter.is_open())
        except Exception:
            return False
        if not still_open:
            return False

        cfg = self._screening_config or {}
        max_total = float(cfg.get("breaker_wait_max_seconds", 900) or 0)
        max_trip = float(cfg.get("breaker_wait_max_per_trip", 360) or 0)
        used = float(getattr(self, "_breaker_wait_used", 0.0) or 0.0)

        while True:
            try:
                if not limiter.is_open():
                    return False
            except Exception:
                return False
            remaining_budget = max(0.0, max_total - used)
            if remaining_budget <= 0 or max_trip <= 0:
                return True
            try:
                cooldown = float(limiter.cooldown_remaining() or 0.0)
            except Exception:
                cooldown = 0.0
            sleep_for = min(max(cooldown, 1.0), max_trip, remaining_budget)
            logger.warning(
                "Waiting %.1fs for yfinance breaker (%s); budget left %.1fs",
                sleep_for,
                context,
                remaining_budget - sleep_for,
            )
            time.sleep(sleep_for)
            used += sleep_for
            self._breaker_wait_used = used

    # =========================================================================
    # Data Fetching — OHLCV (Tier 1: batch, cached, retried)
    # =========================================================================

    def _fetch_batch_ohlcv(self, tickers: List[str], date: str) -> Dict[str, Dict[str, Any]]:
        """
        Batch-fetch price data for all tickers. Info is NOT fetched here
        (deferred to funnel phase to save API calls).
        """
        batch_data: Dict[str, Dict[str, Any]] = {}
        end_date = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
        lookback = int(
            (self._screening_config or {}).get("ohlcv_lookback_days", _OHLCV_LOOKBACK_DAYS)
            or _OHLCV_LOOKBACK_DAYS
        )
        lookback = max(400, min(lookback, 800))
        start_date = end_date - timedelta(days=lookback)
        is_single = len(tickers) == 1
        tickers_key = ",".join(sorted(tickers))

        from tradingagents.dataflows import screening_ohlcv_cache as ohlcv_cache
        from tradingagents.screening.reversal_buildup import truncate_batch_data

        session_tag = ohlcv_cache.session_tag_for(date, self._screening_config)
        cache_key_ohlcv = ohlcv_cache.chunk_cache_key(date, session_tag, tickers)
        cached_ohlcv = self.cache.get("screening_prices", cache_key_ohlcv)

        if cached_ohlcv is not None:
            logger.debug("OHLCV cache HIT (%d tickers)", len(tickers))
            ohlcv = cached_ohlcv.get("ohlcv")
            spy_close = cached_ohlcv.get("spy_close")
            if spy_close is None:
                spy_close = ohlcv_cache.get_cached_spy_close(
                    self.cache,
                    date,
                    session_tag,
                    lookback,
                    lambda: self._download_spy_with_retry(start_date, end_date),
                )
        else:
            hits, misses = ohlcv_cache.load_ticker_bars(
                self.cache, tickers, date, session_tag, lookback,
            )
            ohlcv = None
            if not misses:
                ohlcv = ohlcv_cache.synthesize_ohlcv_frame(hits, tickers)
            else:
                download_list = misses
                dl_single = len(download_list) == 1
                ohlcv_dl = self._download_ohlcv_with_retry(
                    download_list, start_date, end_date, dl_single,
                )
                if ohlcv_dl is None:
                    if len(hits) < len(tickers):
                        return batch_data
                else:
                    new_bars = ohlcv_cache.persist_bars_from_download(
                        self.cache,
                        ohlcv_dl,
                        download_list,
                        date,
                        session_tag,
                        lookback,
                        dl_single,
                    )
                    hits = {**hits, **new_bars}
                ohlcv = ohlcv_cache.synthesize_ohlcv_frame(hits, tickers)
            if ohlcv is None or (hasattr(ohlcv, "empty") and ohlcv.empty):
                return batch_data
            spy_close = ohlcv_cache.get_cached_spy_close(
                self.cache,
                date,
                session_tag,
                lookback,
                lambda: self._download_spy_with_retry(start_date, end_date),
            )
            self.cache.set(
                "screening_prices",
                cache_key_ohlcv,
                data={"ohlcv": ohlcv, "spy_close": spy_close},
                ttl=CacheConfig.SCREENING_PRICES,
            )

        # Parse per-ticker data. Each per-ticker outcome is also recorded to
        # ticker_health so stale/delisted symbols can be evicted later.
        health_track_enabled = bool(self._screening_config.get("ticker_health", {}).get("track", True))

        def _health_fail(sym: str, reason: str) -> None:
            if not health_track_enabled or not self.db:
                return
            from tradingagents.dataflows.vendor_errors import should_persist_ticker_health_failure

            if not should_persist_ticker_health_failure(reason):
                return
            try:
                self.db.record_ticker_failure(sym, reason=reason)
            except Exception:
                logger.debug("ticker_health failure record skipped for %s", sym, exc_info=True)

        def _health_ok(sym: str) -> None:
            if not health_track_enabled or not self.db:
                return
            try:
                self.db.record_ticker_success(sym)
            except Exception:
                logger.debug("ticker_health success record skipped for %s", sym, exc_info=True)

        for t in tickers:
            try:
                if is_single:
                    close = ohlcv["Close"].dropna()
                    volume = ohlcv["Volume"].dropna()
                    high = ohlcv["High"].dropna()
                    low = ohlcv["Low"].dropna()
                else:
                    try:
                        ticker_data = ohlcv[t]
                    except KeyError:
                        self._skipped.append({"ticker": t, "reason": "missing_from_bulk_download"})
                        _health_fail(t, "missing_from_bulk_download")
                        continue
                    close = ticker_data["Close"].dropna()
                    volume = ticker_data["Volume"].dropna()
                    high = ticker_data["High"].dropna()
                    low = ticker_data["Low"].dropna()

                # Ensure Series (not DataFrame with single column)
                close = close.squeeze() if hasattr(close, 'squeeze') else close
                volume = volume.squeeze() if hasattr(volume, 'squeeze') else volume
                high = high.squeeze() if hasattr(high, 'squeeze') else high
                low = low.squeeze() if hasattr(low, 'squeeze') else low

                if len(close) < 20:
                    self._skipped.append({"ticker": t, "reason": "insufficient_data (<20 days)"})
                    _health_fail(t, "insufficient_ohlcv_history")
                    continue

                batch_data[t] = {
                    "close": close, "volume": volume, "high": high, "low": low,
                    "info": {},  # Deferred to Tier 2 funnel phase
                    "spy_close": spy_close,
                }
                _health_ok(t)
            except Exception as e:
                self._skipped.append({"ticker": t, "reason": f"data_extraction: {str(e)[:60]}"})
                _health_fail(t, f"data_extraction: {str(e)[:60]}")
                continue

        truncate_batch_data(batch_data, date)
        return batch_data

    def _fetch_info_cached(self, tickers: List[str], date: str) -> Dict[str, dict]:
        """Fetch .info data with caching and parallel fetching."""
        tickers_key = ",".join(sorted(tickers))
        cache_key_info = f"screening_info:{date}:{tickers_key}"
        cached_info = self.cache.get("screening_info", cache_key_info)
        if cached_info is not None:
            logger.debug("Info cache HIT (%d tickers)", len(tickers))
            return cached_info
        info_data = self._fetch_info_with_retry(tickers)
        self.cache.set("screening_info", cache_key_info, data=info_data, ttl=CacheConfig.SCREENING_PRICES)
        return info_data

    # =========================================================================
    # Retry Helpers
    # =========================================================================

    def _download_ohlcv_with_retry(self, tickers, start_date, end_date, is_single):
        """Download OHLCV data with exponential backoff retry.

        Uses _DOWNLOAD_LOCK to prevent concurrent yfinance downloads from
        corrupting each other's responses (yfinance urllib session is not
        thread-safe for overlapping bulk requests).
        """
        tickers_str = " ".join(tickers)
        limiter = get_yfinance_limiter()
        for attempt in range(_MAX_RETRIES):
            if limiter.is_open() and self._wait_for_yfinance_breaker(limiter, "ohlcv_download"):
                logger.warning(
                    "OHLCV download skipped: yfinance breaker still open after wait budget"
                )
                return None
            if not limiter.acquire(block=True):
                return None
            try:
                with _DOWNLOAD_LOCK:
                    if is_single:
                        ohlcv = yf.download(
                            tickers_str, start=start_date.strftime("%Y-%m-%d"),
                            end=end_date.strftime("%Y-%m-%d"), progress=False,
                            auto_adjust=True, multi_level_index=False,
                            timeout=30,
                        )
                    else:
                        ohlcv = yf.download(
                            tickers_str, start=start_date.strftime("%Y-%m-%d"),
                            end=end_date.strftime("%Y-%m-%d"), group_by="ticker",
                            progress=False, auto_adjust=True,
                            timeout=30,
                        )
                if ohlcv is not None and not ohlcv.empty:
                    limiter.record_success()
                    return ohlcv
                logger.warning("OHLCV download returned empty (attempt %d)", attempt + 1)
                limiter.record_success()
            except Exception as e:
                if limiter.record_if_rate_limited(e):
                    return None
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("OHLCV download failed (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, e)
                if attempt < _MAX_RETRIES - 1:
                    logger.info("Retrying in %ds...", delay)
                    time.sleep(delay)
        logger.error("All OHLCV download attempts failed")
        return None

    def _attach_momentum_benchmarks(self, batch_data: Dict[str, Dict[str, Any]], date: str) -> None:
        """Stamp IWM/UFO + versioned space-peer closes onto each momentum row."""
        from tradingagents.screening.early_momentum import DEFAULT_SPACE_BASKET, get_momentum_config
        from tradingagents.screening.reversal_buildup import session_complete_tag

        cfg = get_momentum_config(self.config)
        space = [str(t).upper() for t in (cfg.get("space_basket") or DEFAULT_SPACE_BASKET)]
        iwm_sym = str(cfg.get("iwm_ticker") or "IWM").upper()
        ufo_sym = str(cfg.get("ufo_ticker") or "UFO").upper()
        session_tag = session_complete_tag(date)
        cache_key = f"momentum_bench:{date}:{session_tag}:{cfg.get('peer_basket_version') or 'v1'}"
        cached = self.cache.get("screening_prices", cache_key) if self.cache else None

        peer_closes: Dict[str, Any] = {}
        iwm_close = None
        ufo_close = None
        if isinstance(cached, dict):
            iwm_close = cached.get("iwm_close")
            ufo_close = cached.get("ufo_close")
            peer_closes = dict(cached.get("peer_closes") or {})

        for t in space:
            if t in batch_data and batch_data[t].get("close") is not None:
                peer_closes[t] = batch_data[t]["close"]

        missing = []
        if iwm_close is None:
            missing.append(iwm_sym)
        if ufo_close is None:
            missing.append(ufo_sym)
        for t in space:
            if t not in peer_closes:
                missing.append(t)

        if missing:
            end_date = datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)
            lookback = int((self._screening_config or {}).get("ohlcv_lookback_days", _OHLCV_LOOKBACK_DAYS) or _OHLCV_LOOKBACK_DAYS)
            start_date = end_date - timedelta(days=max(400, min(lookback, 800)))
            fetched = self._download_closes_with_retry(list(dict.fromkeys(missing)), start_date, end_date)
            iwm_close = iwm_close if iwm_close is not None else fetched.get(iwm_sym)
            ufo_close = ufo_close if ufo_close is not None else fetched.get(ufo_sym)
            for t in space:
                if t not in peer_closes and fetched.get(t) is not None:
                    peer_closes[t] = fetched[t]
            if self.cache is not None:
                try:
                    self.cache.set(
                        "screening_prices",
                        cache_key,
                        data={"iwm_close": iwm_close, "ufo_close": ufo_close, "peer_closes": peer_closes},
                        ttl=CacheConfig.SCREENING_PRICES,
                    )
                except Exception:
                    pass

        for row in batch_data.values():
            if not isinstance(row, dict):
                continue
            row["iwm_close"] = iwm_close
            row["ufo_close"] = ufo_close
            row["peer_closes"] = peer_closes

    def _download_closes_with_retry(self, symbols: List[str], start_date, end_date) -> Dict[str, Any]:
        """Download Close series for a small benchmark set (IWM/UFO/space peers)."""
        out: Dict[str, Any] = {}
        symbols = [str(s).upper() for s in symbols if str(s).strip()]
        if not symbols:
            return out
        limiter = get_yfinance_limiter()
        for attempt in range(_MAX_RETRIES):
            if limiter.is_open() and self._wait_for_yfinance_breaker(limiter, "momentum_bench"):
                return out
            if not limiter.acquire(block=True):
                return out
            try:
                with _DOWNLOAD_LOCK:
                    raw = yf.download(
                        symbols if len(symbols) > 1 else symbols[0],
                        start=start_date.strftime("%Y-%m-%d"),
                        end=end_date.strftime("%Y-%m-%d"),
                        progress=False,
                        auto_adjust=True,
                        group_by="ticker" if len(symbols) > 1 else None,
                        timeout=30,
                    )
                if raw is None or getattr(raw, "empty", True):
                    limiter.record_success()
                    return out
                limiter.record_success()
                if len(symbols) == 1:
                    close = raw["Close"].dropna() if "Close" in raw.columns else raw.dropna()
                    close = close.squeeze() if hasattr(close, "squeeze") else close
                    if close is not None and len(close):
                        out[symbols[0]] = close
                    return out
                for sym in symbols:
                    try:
                        if isinstance(raw.columns, pd.MultiIndex):
                            if (sym, "Close") in raw.columns:
                                close = raw[(sym, "Close")].dropna()
                            elif ("Close", sym) in raw.columns:
                                close = raw[("Close", sym)].dropna()
                            else:
                                close = raw[sym]["Close"].dropna()
                        else:
                            close = raw["Close"].dropna()
                        close = close.squeeze() if hasattr(close, "squeeze") else close
                        if close is not None and len(close):
                            out[sym] = close
                    except Exception:
                        continue
                return out
            except Exception as e:
                if limiter.record_if_rate_limited(e):
                    return out
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                else:
                    logger.warning("Momentum benchmark download failed: %s", e)
        return out

    def _download_spy_with_retry(self, start_date, end_date):
        """Download SPY benchmark data with retry.

        Also protected by _DOWNLOAD_LOCK to prevent concurrent yfinance requests
        from interfering with the OHLCV download in another thread.
        """
        limiter = get_yfinance_limiter()
        for attempt in range(_MAX_RETRIES):
            if limiter.is_open() and self._wait_for_yfinance_breaker(limiter, "spy_download"):
                logger.warning(
                    "SPY benchmark skipped: yfinance breaker still open after wait budget"
                )
                return None
            if not limiter.acquire(block=True):
                return None
            try:
                with _DOWNLOAD_LOCK:
                    spy_data = yf.download(
                        "SPY", start=start_date.strftime("%Y-%m-%d"),
                        end=end_date.strftime("%Y-%m-%d"), progress=False,
                        auto_adjust=True, multi_level_index=False,
                        timeout=30,
                    )
                if not spy_data.empty:
                    limiter.record_success()
                    spy_close = spy_data["Close"].dropna()
                    if hasattr(spy_close, 'squeeze'):
                        spy_close = spy_close.squeeze()
                    return spy_close
                limiter.record_success()
            except Exception as e:
                if limiter.record_if_rate_limited(e):
                    return None
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                else:
                    logger.warning("SPY download failed after %d attempts: %s", _MAX_RETRIES, e)
        return None

    def _fetch_info_with_retry(self, tickers: List[str]) -> Dict[str, dict]:
        """Fetch ticker .info data with concurrent fetching for speed.

        Routes through centralized get_ticker_info() (6-hour DataCache TTL)
        so repeated screening runs don't re-fetch from yfinance.
        """
        from tradingagents.dataflows.yfinance_extended import get_ticker_info
        info_data: Dict[str, dict] = {}

        def _get_single_info(ticker: str) -> tuple:
            for attempt in range(_MAX_RETRIES):
                try:
                    return (ticker, get_ticker_info(ticker))
                except Exception as e:
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                    else:
                        logger.debug("Info fetch failed for %s after %d attempts: %s", ticker, _MAX_RETRIES, e)
            return (ticker, {})

        max_workers = min(8, len(tickers))
        executor: Optional[ThreadPoolExecutor] = None
        futures: Dict[Any, str] = {}
        try:
            executor = ThreadPoolExecutor(max_workers=max_workers)
            futures = {executor.submit(_get_single_info, t): t for t in tickers}
            for future in as_completed(futures, timeout=60):
                try:
                    ticker, info = future.result()
                    info_data[ticker] = info
                except Exception as e:
                    logger.debug("Info future failed for %s: %s", futures[future], e)
                    info_data[futures[future]] = {}
        except FuturesTimeoutError:
            logger.warning("Concurrent info fetch timed out globally; cancelling unfinished tasks")
        except Exception as e:
            logger.warning("Concurrent info fetch failed: %s", e)
        finally:
            if futures:
                for future in futures:
                    if not future.done():
                        future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

        # Fill in any missing tickers with empty dicts — no sequential fallback
        # to avoid potential cascading hangs when the thread pool already failed
        for t in tickers:
            if t not in info_data:
                info_data[t] = {}
        return info_data

    # =========================================================================
    # Enhanced Data Fetching (Tier 2+ only — valuation, options, ownership)
    # =========================================================================

    def _fetch_enhanced_data(self, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch valuation, options, ownership data for top candidates."""
        enhanced: Dict[str, Dict[str, Any]] = {}

        def _fetch_one(ticker: str) -> tuple:
            result: Dict[str, Any] = {}
            try:
                result["valuation"] = get_valuation_metrics(ticker)
                result["options"] = get_options_summary(ticker)
                result["ownership"] = get_ownership_summary(ticker)
                result["earnings_profile"] = get_earnings_profile(ticker)
                try:
                    from tradingagents.dataflows.yfinance_extended import get_insider_net_buy
                    result["insider_net_buy"] = get_insider_net_buy(ticker)
                except Exception as _ib_err:
                    logger.debug("insider_net_buy fetch skipped for %s: %s", ticker, _ib_err)
            except Exception as e:
                logger.warning("Enhanced data failed for %s: %s", ticker, e)
            return (ticker, result)

        max_workers = min(6, len(tickers))
        executor: Optional[ThreadPoolExecutor] = None
        futures = []
        try:
            executor = ThreadPoolExecutor(max_workers=max_workers)
            futures = [executor.submit(_fetch_one, t) for t in tickers]
            # yfinance can leave an individual quoteSummary request blocked
            # after a rate-limit/404 sequence. Do not let one enhanced row
            # indefinitely pin a full Scan All job.
            for future in as_completed(futures, timeout=180):
                try:
                    ticker, data = future.result(timeout=45)
                    enhanced[ticker] = data
                except Exception as e:
                    logger.debug("Enhanced future failed: %s", e)
        except FuturesTimeoutError:
            logger.warning(
                "Enhanced data batch timed out after 180s; returning partial enrichment"
            )
        except Exception as e:
            logger.warning("Enhanced batch fetch failed: %s", e)
        finally:
            for future in futures:
                if not future.done():
                    future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

        return enhanced

    def _fetch_tier2_auxiliary(self, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch estimates and rating changes with a bounded wall-clock budget."""
        auxiliary: Dict[str, Dict[str, Any]] = {}

        def _fetch_one(ticker: str) -> tuple:
            try:
                return (
                    ticker,
                    {
                        "estimate_revisions": get_estimate_revisions(ticker),
                        "rating_changes": get_rating_changes(ticker),
                    },
                )
            except Exception as e:
                logger.debug("Tier 2 auxiliary fetch failed for %s: %s", ticker, e)
                return ticker, {}

        executor: Optional[ThreadPoolExecutor] = None
        futures = []
        try:
            executor = ThreadPoolExecutor(max_workers=min(6, len(tickers)))
            futures = [executor.submit(_fetch_one, ticker) for ticker in tickers]
            for future in as_completed(futures, timeout=120):
                ticker, data = future.result()
                auxiliary[ticker] = data
        except FuturesTimeoutError:
            logger.warning(
                "Tier 2 auxiliary batch timed out after 120s; returning partial data"
            )
        except Exception as e:
            logger.warning("Tier 2 auxiliary batch failed: %s", e)
        finally:
            for future in futures:
                if not future.done():
                    future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

        return auxiliary

    # =========================================================================
    # Usage Tracking
    # =========================================================================

    def _record_usage(self, ticker_count: int):
        """Record yfinance API usage for visibility."""
        try:
            from tradingagents.dataflows.usage_tracker import get_tracker
            tracker = get_tracker()
            estimated_calls = 3 + ticker_count
            tracker.record_use("yfinance", count=estimated_calls)
        except Exception as e:
            logger.debug("Usage tracking failed: %s", e)

    def _has_sparse_fundamentals(self, info: Dict[str, Any]) -> bool:
        """Detect symbols that usually lack analyst/fundamentals payloads.

        This is intentionally conservative: we only mark sparse when market-cap
        exists (the symbol is real) but both valuation and analyst-coverage
        fields are absent. Those names tend to trigger repeated quoteSummary
        failures for estimate/rating endpoints and add runtime without adding
        signal quality.

        ETFs / mutual funds / closed-end funds are treated as sparse by
        construction: they don't have company-level PE, EPS, analyst targets,
        or estimate revisions (the underlying basket does, but the ETF ticker
        itself doesn't). Flagging them here is what routes them away from the
        Tier 2 auxiliary fetch (estimate_revisions, rating_changes) and the
        enhanced fundamentals path (valuation_gap, smart_money, pead_drift).
        """
        if not info:
            return False
        quote_type = str(info.get("quoteType") or "").strip().upper()
        if quote_type in {"ETF", "MUTUALFUND", "CLOSEDENDFUND"}:
            return True
        has_market_cap = info.get("marketCap") is not None
        has_valuation = (
            info.get("trailingPE") is not None
            or info.get("forwardPE") is not None
        )
        has_analyst = (
            info.get("numberOfAnalystOpinions") not in (None, 0)
            or info.get("recommendationMean") is not None
            or info.get("targetMeanPrice") is not None
        )
        return has_market_cap and (not has_valuation) and (not has_analyst)

    # =========================================================================
    # Signal Computations — Tier 1 (OHLCV only, zero extra API calls)
    # =========================================================================

    def _compute_tier1_signals(self, ticker: str, data: Dict, regime: str = "unknown") -> Dict[str, float]:
        """Compute Tier 1 signals from batch OHLCV data only.
        regime is captured for future regime-adjusted signal weights."""
        close = data["close"]
        volume = data["volume"]
        high = data["high"]
        low = data["low"]
        spy_close = data.get("spy_close")

        signals = {}
        signals["volume_surge"] = self._signal_volume_surge(volume)
        signals["rsi_oversold"] = self._signal_rsi_oversold(close)
        signals["rsi_overbought"] = self._signal_rsi_overbought(close)
        signals["ma_crossover"] = self._signal_ma_crossover(close)
        signals["relative_strength"] = self._signal_relative_strength(close, spy_close)
        signals["bollinger_squeeze"] = self._signal_bollinger_squeeze(close)
        signals["trend_strength"] = self._signal_trend_strength(close, high, low)
        signals["residual_momentum_12_1"] = self._signal_residual_momentum_12_1(close, spy_close)
        return signals

    # =========================================================================
    # Signal Computations — Tier 2 (requires .info data)
    # =========================================================================

    def _compute_tier2_signals(
        self,
        ticker: str,
        data: Dict,
        regime: str = "unknown",
        sector_medians_all: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> Dict[str, float]:
        """Compute Tier 2 signals that require .info or individual API calls.
        regime is captured for future regime-adjusted signal weights."""
        close = data["close"]
        info = data.get("info", {})
        signals = {}
        signals["price_vs_target"] = self._signal_price_vs_target(close, info)
        signals["earnings_proximity"] = self._signal_earnings_proximity(ticker)
        signals["insider_buying"] = self._signal_insider_buying(info)

        est_data = data.get("estimate_revisions", {})
        signals["estimate_momentum"] = self._signal_estimate_momentum(ticker, info, est_data)

        rating_data = data.get("rating_changes", {})
        signals["rating_momentum"] = self._signal_rating_momentum(ticker, info, rating_data)
        signals["quality_factor"] = self._signal_quality_factor(info)
        signals["income_factor"] = self._signal_income_factor(info)
        signals["short_pressure"] = self._signal_short_pressure(info)
        vg_score, _vg_detail = self._signal_valuation_gap_multi(
            info.get("forwardPE"),
            info.get("priceToSalesTrailing12Months"),
            info.get("enterpriseToEbitda"),
            info.get("sector"),
            sector_medians_all if sector_medians_all is not None else self._stored_sector_medians(),
        )
        signals["valuation_gap"] = vg_score

        return signals

    # =========================================================================
    # Signal Computations — Enhanced (valuation, options, smart money)
    # =========================================================================

    def _compute_enhanced_signals(
        self, ticker: str, enhanced: Dict[str, Any], info: dict,
        sector_medians: Optional[Dict[str, float]] = None,
        sector_medians_all: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> Tuple[Dict[str, float], Optional[Dict[str, Any]]]:
        """Compute enhanced signals from yfinance_extended data.

        Returns ``(signals, valuation_gap_detail)`` — the detail dict
        (``{pe, ps, ev_ebitda, weights_used}``) is kept out of the numeric
        ``signals`` map (which composite-score math iterates as
        ``value * weight``) and surfaced separately for the expand-panel UI.
        """
        signals = {}

        val = enhanced.get("valuation", {})
        fwd_pe = val.get("forward_pe")
        ps = val.get("price_to_sales")
        ev_ebitda = val.get("ev_to_ebitda")
        sector = val.get("sector")
        vg_score, vg_detail = self._signal_valuation_gap_multi(
            fwd_pe, ps, ev_ebitda, sector, sector_medians_all or {},
        )
        signals["valuation_gap"] = vg_score

        # 11. Options Sentiment — IV rank + bullish P/C ratio
        opts = enhanced.get("options", {})
        signals["options_sentiment"] = self._signal_options_sentiment(opts)

        # 12. Smart Money — Institutional flow + decreasing short interest + insider net-buy cluster
        own = enhanced.get("ownership", {})
        insider_nb = enhanced.get("insider_net_buy", {})
        signals["smart_money"] = self._signal_smart_money(own, info, insider_net_buy=insider_nb)

        # Squeeze identity from the same ownership block (SI% / DTC).
        sp = self._signal_short_pressure(info, own)
        if sp is not None:
            signals["short_pressure"] = sp

        # 13. PEAD — Post-earnings announcement drift
        signals["pead_drift"] = self._signal_pead_drift(ticker, info, enhanced, None)

        return signals, vg_detail

    # Metrics supported by _compute_live_sector_medians.
    # Each entry maps a logical name to the valuation dict key returned by get_valuation_metrics.
    _SECTOR_MEDIAN_METRICS: Dict[str, str] = {
        "forward_pe": "forward_pe",
        "price_to_sales": "price_to_sales",
        "ev_to_ebitda": "ev_to_ebitda",
    }

    def _compute_live_sector_medians(
        self,
        enhanced_data: Dict[str, Dict[str, Any]],
        metrics: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, float]]:
        """Compute sector medians for one or more valuation multiples from the current batch.

        Args:
            enhanced_data: Per-ticker enhanced signal data (includes a 'valuation' sub-dict).
            metrics: Subset of _SECTOR_MEDIAN_METRICS keys to compute. Defaults to all three
                     (forward_pe, price_to_sales, ev_to_ebitda).

        Returns:
            Nested dict: {metric: {sector: median_value}}. Sectors with fewer than
            ``live_median_min_peers`` (default 8) valid data points are omitted;
            callers should fall back to static medians for those.

        Backward-compat: callers that only need forward_pe can call
            `result["forward_pe"]` on the returned dict.
        """
        from collections import defaultdict
        from tradingagents.screening.ticker_resolver import _safe_num

        if metrics is None:
            metrics = list(self._SECTOR_MEDIAN_METRICS.keys())

        # {metric: {sector: [values]}}
        buckets: Dict[str, Dict[str, list]] = {m: defaultdict(list) for m in metrics}

        for _ticker, data in enhanced_data.items():
            val = data.get("valuation", {})
            sector = val.get("sector")
            if not sector or not isinstance(sector, str):
                continue
            for metric in metrics:
                val_key = self._SECTOR_MEDIAN_METRICS.get(metric, metric)
                raw = _safe_num(val.get(val_key))
                if raw is not None and raw > 0:
                    buckets[metric][sector].append(raw)

        result: Dict[str, Dict[str, float]] = {}
        for metric, sector_vals in buckets.items():
            medians: Dict[str, float] = {}
            min_peers = 8
            vg_cfg = (getattr(self, "_screening_config", None) or {}).get("valuation_gap_metrics", {})
            try:
                min_peers = int(vg_cfg.get("live_median_min_peers", 8) or 8)
            except (TypeError, ValueError):
                min_peers = 8
            min_peers = max(3, min_peers)
            for sector, vals in sector_vals.items():
                if len(vals) >= min_peers:
                    s = sorted(vals)
                    mid = len(s) // 2
                    medians[sector] = (s[mid - 1] + s[mid]) / 2 if len(s) % 2 == 0 else s[mid]
            if medians:
                result[metric] = medians

        if result.get("forward_pe"):
            logger.info(
                "Live sector medians (fwd P/E): %s",
                {s: round(v, 1) for s, v in result["forward_pe"].items()},
            )
        return result

    def _stored_sector_medians(self) -> Dict[str, Dict[str, float]]:
        """Latest persisted broad-universe medians, or empty."""
        db = getattr(self, "db", None)
        if db is None or not hasattr(db, "get_latest_sector_medians"):
            return {}
        try:
            stored = db.get_latest_sector_medians()
            return stored if isinstance(stored, dict) else {}
        except Exception:
            logger.debug("sector median load failed", exc_info=True)
            return {}

    def _persist_sector_medians(
        self, medians: Dict[str, Dict[str, float]], universe_size: int
    ) -> None:
        db = getattr(self, "db", None)
        if db is None or not hasattr(db, "save_sector_medians"):
            return
        try:
            db.save_sector_medians(medians, universe_size=universe_size)
        except Exception:
            logger.debug("sector median persist failed", exc_info=True)

    def _live_medians_from_info_batch(
        self,
        batch_data: Dict[str, Dict[str, Any]],
        tickers: List[str],
    ) -> Dict[str, Dict[str, float]]:
        """Build live medians from already-fetched .info, merged over persisted."""
        payload: Dict[str, Dict[str, Any]] = {}
        for ticker in tickers:
            info = (batch_data.get(ticker) or {}).get("info") or {}
            if not info:
                continue
            payload[ticker] = {
                "valuation": {
                    "forward_pe": info.get("forwardPE"),
                    "price_to_sales": info.get("priceToSalesTrailing12Months"),
                    "ev_to_ebitda": info.get("enterpriseToEbitda"),
                    "sector": info.get("sector"),
                }
            }
        stored = self._stored_sector_medians()
        live = self._compute_live_sector_medians(payload) if payload else {}
        merged: Dict[str, Dict[str, float]] = {k: dict(v) for k, v in stored.items() if isinstance(v, dict)}
        for metric, sectors in live.items():
            bucket = dict(merged.get(metric) or {})
            bucket.update(sectors or {})
            merged[metric] = bucket
        return merged

    def _merged_static_medians(
        self, builtin: Dict[str, float], metric: str
    ) -> Dict[str, float]:
        stored = self._stored_sector_medians().get(metric) or {}
        out = dict(builtin)
        for sector, value in stored.items():
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if num > 0:
                out[str(sector)] = num
        return out

    # Static sector median forward P/E — used only as fallback when live data
    # has fewer than 3 peers for a sector in the current screening batch.
    _STATIC_SECTOR_MEDIANS: Dict[str, float] = {
        "Technology": 28, "Healthcare": 22, "Financial Services": 14,
        "Consumer Cyclical": 20, "Communication Services": 22,
        "Industrials": 18, "Consumer Defensive": 20, "Energy": 12,
        "Utilities": 16, "Real Estate": 25, "Basic Materials": 15,
    }

    _STATIC_SECTOR_MEDIANS_PS: Dict[str, float] = {
        "Technology": 8, "Healthcare": 5, "Financial Services": 3,
        "Consumer Cyclical": 2, "Communication Services": 4,
        "Industrials": 2, "Consumer Defensive": 2, "Energy": 1.5,
        "Utilities": 3, "Real Estate": 5, "Basic Materials": 1.5,
    }
    _STATIC_SECTOR_MEDIANS_EV: Dict[str, float] = {
        "Technology": 22, "Healthcare": 18, "Financial Services": 12,
        "Consumer Cyclical": 14, "Communication Services": 16,
        "Industrials": 14, "Consumer Defensive": 14, "Energy": 8,
        "Utilities": 12, "Real Estate": 20, "Basic Materials": 10,
    }

    def _metric_gap_score(
        self, value: Optional[float], sector: Optional[str],
        live_medians: Optional[Dict[str, float]], static_medians: Dict[str, float],
        metric_name: str = "forward_pe",
    ) -> Optional[float]:
        from tradingagents.screening.ticker_resolver import _safe_num
        value = _safe_num(value)
        if value is None or value <= 0:
            return None
        static_map = self._merged_static_medians(static_medians, metric_name)
        static = static_map.get(sector or "") if sector else None
        live = None
        if live_medians and sector:
            live = live_medians.get(sector)
        median = static if static and static > 0 else None
        if live is not None and live > 0:
            blend = 0.4
            vg_cfg = (getattr(self, "_screening_config", None) or {}).get("valuation_gap_metrics", {})
            try:
                blend = float(vg_cfg.get("live_median_blend", 0.4))
            except (TypeError, ValueError):
                blend = 0.4
            blend = min(1.0, max(0.0, blend))
            if median is not None:
                median = blend * live + (1.0 - blend) * median
            else:
                median = live
        if median is None:
            median = static_medians.get(sector or "", 20)
        if median <= 0:
            return None
        discount = (median - value) / median
        return float(min(max(discount * 1.5, 0.0), 1.0))

    def _signal_valuation_gap_multi(
        self,
        forward_pe: Optional[float],
        price_to_sales: Optional[float],
        ev_to_ebitda: Optional[float],
        sector: Optional[str],
        live_medians_all: Dict[str, Dict[str, float]],
    ) -> tuple:
        """Blend P/E, P/S, EV/EBITDA valuation gaps (Phase 6A)."""
        cfg = (getattr(self, "_screening_config", None) or {}).get("valuation_gap_metrics", {})
        w_pe = float(cfg.get("forward_pe", 0.5))
        w_ps = float(cfg.get("price_to_sales", 0.25))
        w_ev = float(cfg.get("ev_to_ebitda", 0.25))
        live_medians_all = live_medians_all or {}
        pe_medians = live_medians_all.get("forward_pe", {})
        ps_medians = live_medians_all.get("price_to_sales", {})
        ev_medians = live_medians_all.get("ev_to_ebitda", {})
        pe_s = self._metric_gap_score(
            forward_pe, sector, pe_medians, self._STATIC_SECTOR_MEDIANS, "forward_pe",
        )
        ps_s = self._metric_gap_score(
            price_to_sales, sector, ps_medians, self._STATIC_SECTOR_MEDIANS_PS, "price_to_sales",
        )
        ev_s = self._metric_gap_score(
            ev_to_ebitda, sector, ev_medians, self._STATIC_SECTOR_MEDIANS_EV, "ev_to_ebitda",
        )
        parts = []
        weights_used = {}
        if pe_s is not None:
            parts.append((w_pe, pe_s))
            weights_used["pe"] = w_pe
        if ps_s is not None:
            parts.append((w_ps, ps_s))
            weights_used["ps"] = w_ps
        if ev_s is not None:
            parts.append((w_ev, ev_s))
            weights_used["ev_ebitda"] = w_ev
        if not parts:
            return None, {}
        tw = sum(w for w, _ in parts) or 1.0
        score = sum(w * s for w, s in parts) / tw
        detail = {
            "pe": pe_s,
            "ps": ps_s,
            "ev_ebitda": ev_s,
            "weights_used": weights_used,
        }
        return score, detail

    def _signal_valuation_gap(
        self, forward_pe: Optional[float], sector: Optional[str],
        live_medians: Optional[Dict[str, float]] = None,
    ) -> float:
        """Valuation gap: how cheap is the stock vs sector median forward P/E.

        Uses dynamically computed sector medians from the current screening
        batch when available (8+ peers required, blended toward static). Falls back to static
        benchmarks for sectors with insufficient live data.
        """
        from tradingagents.screening.ticker_resolver import _safe_num
        # Defensive: yfinance occasionally returns forward_pe as a string/NaN.
        forward_pe = _safe_num(forward_pe)
        if forward_pe is None or forward_pe <= 0:
            return 0.0
        # Prefer live median, fall back to static
        median = None
        if live_medians and sector:
            median = live_medians.get(sector)
        if median is None:
            median = self._merged_static_medians(self._STATIC_SECTOR_MEDIANS, "forward_pe").get(
                sector or "", 20,
            )
        if median <= 0:
            return 0.0
        discount = (median - forward_pe) / median
        return float(min(max(discount * 1.5, 0.0), 1.0))

    def _signal_options_sentiment(self, opts: Dict[str, Any]) -> Optional[float]:
        """Options sentiment: IV rank + P/C ratio composite. None if no options data."""
        if not opts:
            return None
        pc = opts.get("put_call_volume_ratio")
        iv_rank = opts.get("iv_rank")
        unusual = opts.get("unusual_activity") or []
        if pc is None and iv_rank is None and not unusual:
            return None
        score = 0.0
        if pc is not None:
            if pc < 0.5:
                score += 0.20
            elif pc < 0.8:
                score += 0.10
        if iv_rank is not None:
            if 20 <= iv_rank <= 60:
                score += 0.3
            elif iv_rank < 20:
                score += 0.15
        call_unusual = sum(1 for u in unusual if isinstance(u, dict) and u.get("type") == "call")
        if call_unusual >= 2:
            score += 0.3
        elif call_unusual >= 1:
            score += 0.15
        return float(min(score, 1.0))

    def _signal_smart_money(
        self,
        ownership: Dict[str, Any],
        info: dict,
        insider_net_buy: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """Positioning / flow: institutional ownership, SI change, DTC, insider cluster.

        This is not a quality (ROE/margin) sleeve — see ``quality_factor``.
        Returns None when ownership/flow inputs are missing (not a fake 0.0).
        """
        ownership = ownership or {}
        inst_pct = ownership.get("held_by_institutions_pct")
        si_change = ownership.get("short_interest_change_pct")
        dtc = ownership.get("days_to_cover")
        insider_pct = ownership.get("held_by_insiders_pct")
        cluster = 0.0
        if insider_net_buy and isinstance(insider_net_buy, dict):
            cluster = insider_net_buy.get("cluster_score", 0.0) or 0.0
        if all(v is None for v in (inst_pct, si_change, dtc, insider_pct)) and cluster <= 0:
            return None
        score = 0.0
        if inst_pct is not None:
            if inst_pct > 80:
                score += 0.25
            elif inst_pct > 60:
                score += 0.12
        if si_change is not None:
            if si_change < -10:
                score += 0.30
            elif si_change < 0:
                score += 0.15
        if dtc is not None and dtc < 2:
            score += 0.12
        if insider_pct is not None and insider_pct > 5:
            score += 0.08
        if cluster > 0:
            score += cluster * 0.25
        return float(min(score, 1.0))

    # =========================================================================
    # Original Tier 1/2 Signal Methods
    # =========================================================================

    def _signal_volume_surge(self, volume: pd.Series) -> float:
        if len(volume) < 21:
            return 0.0
        avg_vol = volume.iloc[-21:-1].mean()
        current_vol = volume.iloc[-1]
        if avg_vol == 0:
            return 0.0
        ratio = current_vol / avg_vol
        if ratio <= 1.0:
            return 0.0
        return float(min(math.log(ratio) / math.log(5), 1.0))

    def _compute_rsi(self, close: pd.Series) -> Optional[float]:
        """Compute 14-period RSI. Returns None if insufficient data."""
        if len(close) < 15:
            return None
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]
        if pd.isna(current_rsi):
            return None
        return float(current_rsi)

    def _signal_rsi_oversold(self, close: pd.Series) -> float:
        """Bullish RSI signal: fires when RSI < 50, strongest near 30."""
        current_rsi = self._compute_rsi(close)
        if current_rsi is None or current_rsi >= 50:
            return 0.0
        # RSI 50 → 0.0, RSI 30 → 0.67, RSI 20 → 1.0
        return float(min((50 - current_rsi) / 30, 1.0))

    def _signal_rsi_overbought(self, close: pd.Series) -> float:
        """Bearish RSI signal: fires when RSI > 50, strongest near 70."""
        current_rsi = self._compute_rsi(close)
        if current_rsi is None or current_rsi <= 50:
            return 0.0
        # RSI 50 → 0.0, RSI 70 → 0.67, RSI 80 → 1.0
        return float(min((current_rsi - 50) / 30, 1.0))

    def _signal_ma_crossover(self, close: pd.Series) -> float:
        if len(close) < 51:
            return 0.0
        ema10 = close.ewm(span=10, adjust=False).mean()
        sma50 = close.rolling(50).mean()
        current_diff = ema10.iloc[-1] - sma50.iloc[-1]
        prev_diff = ema10.iloc[-2] - sma50.iloc[-2]
        if pd.isna(current_diff) or pd.isna(prev_diff) or sma50.iloc[-1] == 0:
            return 0.0
        if current_diff <= 0:
            return 0.0
        pct_diff = current_diff / sma50.iloc[-1]
        fresh_cross = 0.2 if prev_diff <= 0 else 0.0
        signal = pct_diff * 10 + fresh_cross
        return float(min(signal, 1.0))

    def _signal_price_vs_target(self, close: pd.Series, info: dict) -> float:
        current_price = float(close.iloc[-1])
        target_mean = info.get("targetMeanPrice")
        if not target_mean or current_price == 0:
            return 0.0
        upside = (target_mean - current_price) / current_price
        return float(min(max(upside / 0.40, 0.0), 1.0))

    def _signal_earnings_proximity(self, ticker: str) -> Optional[float]:
        """In-window catalyst only. None when undated or outside ~21 days."""
        try:
            profile = get_earnings_profile(ticker)
            days = profile.get("days_until_earnings")
            if days is None or days < 0 or days > _EARNINGS_WINDOW_DAYS:
                return None
            beat_rate = profile.get("earnings_beat_rate_pct")
            proximity = max(0.0, 1.0 - (float(days) / _EARNINGS_WINDOW_DAYS))
            beat_bonus = 0.2 if beat_rate is not None and beat_rate > 70 else 0.0
            return float(min(proximity + beat_bonus, 1.0))
        except Exception as e:
            logger.debug("Earnings proximity check failed for %s: %s", ticker, e)
            return None

    def _signal_short_pressure(
        self,
        info: Optional[Dict[str, Any]],
        ownership: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """Squeeze setup: high SI% of float and/or days-to-cover. None if missing."""
        from tradingagents.screening.ticker_resolver import _safe_num

        info = info or {}
        ownership = ownership or {}
        si = _safe_num(info.get("shortPercentOfFloat"))
        sr = _safe_num(info.get("shortRatio"))
        if si is None:
            pct = _safe_num(ownership.get("short_pct_of_float"))
            if pct is not None:
                si = pct / 100.0 if pct > 1.5 else pct
        if sr is None:
            sr = _safe_num(ownership.get("short_ratio")) or _safe_num(ownership.get("days_to_cover"))
        if si is None and sr is None:
            return None
        parts = []
        if si is not None:
            parts.append(min(max(float(si) / 0.20, 0.0), 1.0))
        if sr is not None:
            parts.append(min(max(float(sr) / 10.0, 0.0), 1.0))
        return float(sum(parts) / len(parts)) if parts else None

    def _signal_insider_buying(self, info: dict) -> float:
        """Insider ownership only. SI / inst % live on ``smart_money`` (flow)."""
        held_insiders = info.get("heldPercentInsiders")
        signal = 0.0
        if held_insiders and held_insiders > 0.05:
            signal += 0.6
        if held_insiders and held_insiders > 0.15:
            signal += 0.4
        return float(min(signal, 1.0))

    def _signal_estimate_momentum(self, ticker: str, info: dict, est_data: dict) -> Optional[float]:
        """Estimate revision momentum: upward revisions = bullish signal.

        Returns None when no EPS trend data is available so the signal is
        excluded from the composite score (and the UI renders '—' instead
        of a misleading amber bar at 50).
        """
        eps_trend = est_data.get("eps_trend", {})
        if not eps_trend:
            return None

        for period_key in eps_trend:
            trend = eps_trend[period_key]
            current = trend.get("current")
            ago_30d = trend.get("30d_ago")
            if current is not None and ago_30d is not None and ago_30d != 0:
                revision_pct = (current - ago_30d) / abs(ago_30d)
                return max(0.0, min(1.0, 0.5 + revision_pct * 5))
            break

        return None

    def _signal_rating_momentum(self, ticker: str, info: dict, rating_data: dict) -> Optional[float]:
        """Analyst upgrade/downgrade momentum: net upgrades = bullish.

        Returns None when no analyst actions were recorded in the past 90 days
        so the signal is excluded from the composite score rather than imputing
        a neutral 0.5 that would look like real coverage in the UI.
        """
        net = rating_data.get("net_upgrades_90d", 0)
        total = rating_data.get("total_actions_90d", 0)

        if total == 0:
            return None

        ratio = net / max(total, 1)
        return max(0.0, min(1.0, 0.5 + ratio * 0.5))

    def _signal_pead_drift(self, ticker: str, info: dict, enhanced: dict, hist: any) -> Optional[float]:
        """Post-earnings announcement drift: surprise direction predicts continuation.

        Returns None when surprise history is missing or unparseable (same
        contract as estimate/rating momentum — do not impute 0.5).
        """
        earnings_profile = enhanced.get("earnings_profile", {})
        surprise_history = earnings_profile.get("surprise_history", [])

        if not surprise_history:
            return None

        latest = surprise_history[0] if isinstance(surprise_history, list) else None
        if not latest:
            return None

        surprise_pct = latest.get("surprise_pct") or latest.get("surprisePercent")
        if surprise_pct is None:
            return None

        try:
            surprise_pct = float(surprise_pct)
        except (TypeError, ValueError):
            return None

        decay = 1.0
        report_date_str = latest.get("date", "")
        if report_date_str:
            try:
                report_dt = datetime.strptime(str(report_date_str).split(" ")[0].split("T")[0], "%Y-%m-%d")
                days_since = (datetime.now() - report_dt).days
                decay = max(0.2, 1.0 - (days_since / 85))
                if days_since > 85:
                    return None
            except (ValueError, TypeError):
                pass

        raw = 0.5 + surprise_pct / 40
        return max(0.0, min(1.0, 0.5 + (raw - 0.5) * decay))

    def _signal_relative_strength(self, close: pd.Series, spy_close: Optional[pd.Series]) -> float:
        if spy_close is None or len(close) < 21 or len(spy_close) < 21:
            return 0.0
        stock_ret = (close.iloc[-1] / close.iloc[-21] - 1) if close.iloc[-21] != 0 else 0
        spy_ret = (spy_close.iloc[-1] / spy_close.iloc[-21] - 1) if spy_close.iloc[-21] != 0 else 0
        excess_return = stock_ret - spy_ret
        if excess_return <= 0:
            return 0.0
        return float(min(math.log(1 + excess_return / 0.05) / math.log(5), 1.0))

    def _signal_bollinger_squeeze(self, close: pd.Series) -> float:
        if len(close) < 21:
            return 0.0
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bandwidth = (2 * std20 / sma20).dropna()
        if len(bandwidth) < 2:
            return 0.0
        current_bw = bandwidth.iloc[-1]
        avg_bw = bandwidth.mean()
        if avg_bw == 0 or pd.isna(avg_bw):
            return 0.0
        squeeze_ratio = 1 - (current_bw / avg_bw)
        return float(min(max(squeeze_ratio, 0.0), 1.0))

    def _signal_trend_strength(self, close: pd.Series, high: pd.Series, low: pd.Series) -> float:
        if len(close) < 15:
            return 0.0
        tr = pd.DataFrame({
            "hl": high - low, "hc": abs(high - close.shift(1)), "lc": abs(low - close.shift(1)),
        }).max(axis=1)
        up = high.diff()
        down = -low.diff()
        plus_dm = up.where((up > down) & (up > 0), 0.0)
        minus_dm = down.where((down > up) & (down > 0), 0.0)
        atr14 = tr.rolling(14).mean()
        plus_di = 100 * (plus_dm.rolling(14).mean() / atr14.replace(0, np.nan))
        minus_di = 100 * (minus_dm.rolling(14).mean() / atr14.replace(0, np.nan))
        dx = abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan) * 100
        adx = dx.rolling(14).mean()
        current_adx = adx.iloc[-1]
        plus_now = plus_di.iloc[-1]
        minus_now = minus_di.iloc[-1]
        if pd.isna(current_adx) or pd.isna(plus_now) or pd.isna(minus_now):
            return 0.0
        if plus_now <= minus_now:
            return 0.0
        return float(min(max((current_adx - 20) / 20, 0.0), 1.0))

    @staticmethod
    def _log_unit_score(raw: Any, scale: float, cap: float, percent_if_gt_one: bool = True) -> Optional[float]:
        """Map a ratio onto [0, 1] with log compression so mid values don't max out."""
        try:
            x = float(raw)
        except (TypeError, ValueError):
            return None
        if percent_if_gt_one and x > 1.0:
            x = x / 100.0
        if x <= 0 or scale <= 0 or cap <= 0:
            return None
        return float(min(1.0, math.log(1.0 + x / scale) / math.log(1.0 + cap / scale)))

    def _signal_quality_factor(self, info: dict) -> Optional[float]:
        """Log-scaled ROE / GM / FCF yield with a leverage haircut. None if no fundamentals."""
        if not info:
            return None
        parts: List[float] = []
        roe_s = self._log_unit_score(info.get("returnOnEquity"), scale=0.08, cap=0.40)
        if roe_s is not None:
            parts.append(roe_s)
        gm_s = self._log_unit_score(info.get("grossMargins"), scale=0.20, cap=0.70)
        if gm_s is not None:
            parts.append(gm_s)
        fcf = info.get("freeCashflow")
        mcap = info.get("marketCap")
        try:
            if fcf is not None and mcap not in (None, 0, 0.0):
                fy = float(fcf) / float(mcap)
                fy_s = self._log_unit_score(fy, scale=0.02, cap=0.08, percent_if_gt_one=False)
                if fy_s is not None:
                    parts.append(fy_s)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        if not parts:
            return None
        score = sum(parts) / len(parts)
        de = info.get("debtToEquity")
        if de is not None:
            try:
                de_f = float(de)
                if de_f > 5:
                    de_f = de_f / 100.0
                if de_f > 2.0:
                    score *= 0.6
                elif de_f > 1.0:
                    score *= 0.8
            except (TypeError, ValueError):
                pass
        return float(max(0.0, min(1.0, score)))

    def _signal_income_factor(self, info: dict) -> Optional[float]:
        """Dividend yield with payout sanity. None if no yield."""
        if not info:
            return None
        yld = info.get("dividendYield")
        if yld is None:
            yld = info.get("trailingAnnualDividendYield")
        if yld is None:
            yld = info.get("yield")
        if yld is None:
            rate = info.get("dividendRate")
            price = (
                info.get("currentPrice")
                or info.get("regularMarketPrice")
                or info.get("previousClose")
            )
            try:
                if rate is not None and price not in (None, 0, 0.0):
                    yld = float(rate) / float(price)
            except (TypeError, ValueError, ZeroDivisionError):
                yld = None
        try:
            yld_f = float(yld)
        except (TypeError, ValueError):
            return None
        if yld_f > 1.0:
            yld_f = yld_f / 100.0
        if yld_f <= 0:
            return None
        if yld_f < 0.02:
            score = (yld_f / 0.02) * 0.6
        elif yld_f <= 0.05:
            score = 0.6 + ((yld_f - 0.02) / 0.03) * 0.4
        else:
            score = max(0.3, 1.0 - (yld_f - 0.05) / 0.10)
        payout = info.get("payoutRatio")
        if payout is not None:
            try:
                p = float(payout)
                if p > 1.0:
                    p = p / 100.0
                if p > 0.90 or p < 0:
                    score *= 0.5
                elif p > 0.75:
                    score *= 0.75
            except (TypeError, ValueError):
                pass
        return float(max(0.0, min(1.0, score)))

    def _signal_residual_momentum_12_1(
        self,
        close: pd.Series,
        spy_close: Optional[pd.Series],
    ) -> Optional[float]:
        """12-month return minus last month, excess vs SPY. None if short history."""
        if close is None or spy_close is None:
            return None
        if len(close) < 252 or len(spy_close) < 252:
            return None
        if close.iloc[-252] == 0 or close.iloc[-21] == 0:
            return None
        if spy_close.iloc[-252] == 0 or spy_close.iloc[-21] == 0:
            return None
        stock_12 = float(close.iloc[-1] / close.iloc[-252] - 1)
        stock_1 = float(close.iloc[-1] / close.iloc[-21] - 1)
        spy_12 = float(spy_close.iloc[-1] / spy_close.iloc[-252] - 1)
        spy_1 = float(spy_close.iloc[-1] / spy_close.iloc[-21] - 1)
        excess = (stock_12 - stock_1) - (spy_12 - spy_1)
        return float(max(0.0, min(1.0, 0.5 + excess / 0.40)))

    # =========================================================================
    # Entry Quality Sub-Signals (each normalized 0.0-1.0)
    # =========================================================================

    _EQ_WEIGHTS = {
        "mean_reversion": 0.25,
        "rsi_sweet_spot": 0.20,
        "bollinger_position": 0.15,
        "volatility_contraction": 0.15,
        "macd_phase": 0.15,
        "volume_dryup": 0.10,
    }

    def _eq_mean_reversion_distance(self, close: pd.Series, high: pd.Series, low: pd.Series) -> float:
        """Price distance from 20 SMA in ATR multiples. Closer = better entry."""
        if len(close) < 21:
            return 0.5
        sma20 = close.rolling(20).mean()
        tr = pd.DataFrame({
            "hl": high - low,
            "hc": abs(high - close.shift(1)),
            "lc": abs(low - close.shift(1)),
        }).max(axis=1)
        atr14 = tr.rolling(14).mean()
        current_sma = sma20.iloc[-1]
        current_atr = atr14.iloc[-1]
        if pd.isna(current_sma) or pd.isna(current_atr) or current_atr == 0:
            return 0.5
        distance_atr = abs(close.iloc[-1] - current_sma) / current_atr
        # Within 0.5 ATR = 1.0, linear decay to 0 at 3 ATR
        if distance_atr <= 0.5:
            return 1.0
        return float(max(1.0 - (distance_atr - 0.5) / 2.5, 0.0))

    def _eq_rsi_sweet_spot(self, close: pd.Series) -> float:
        """RSI proximity to the ideal pullback zone (40-55)."""
        rsi = self._compute_rsi(close)
        if rsi is None:
            return 0.5
        if 40 <= rsi <= 55:
            # Bell curve centered at 47.5: peak = 1.0
            center = 47.5
            return float(1.0 - abs(rsi - center) / 15.0)
        elif rsi < 40:
            # Below 40: decays toward 0.3 at RSI 25, 0.0 at RSI 10
            if rsi >= 25:
                return float(0.3 + (rsi - 25) / 15.0 * 0.2)
            return float(max(rsi / 25.0 * 0.3, 0.0))
        else:
            # Above 55: decays toward 0 at RSI 80+
            return float(max(1.0 - (rsi - 55) / 25.0, 0.0))

    def _eq_bollinger_position(self, close: pd.Series) -> float:
        """Where price sits within Bollinger Bands. Lower half = better entry."""
        if len(close) < 21:
            return 0.5
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        upper = sma20 + 2 * std20
        lower = sma20 - 2 * std20
        band_width = upper.iloc[-1] - lower.iloc[-1]
        if pd.isna(band_width) or band_width == 0:
            return 0.5
        pct_b = (close.iloc[-1] - lower.iloc[-1]) / band_width
        if pd.isna(pct_b):
            return 0.5
        # %B 0.3-0.5 is ideal (lower half but not oversold). Score peaks at 0.35.
        if 0.2 <= pct_b <= 0.5:
            return float(1.0 - abs(pct_b - 0.35) / 0.3)
        elif pct_b < 0.2:
            return float(max(pct_b / 0.2 * 0.5, 0.0))
        else:
            # Above 0.5: decays to 0 at %B 1.0+
            return float(max(1.0 - (pct_b - 0.5) / 0.5, 0.0))

    def _eq_volatility_contraction(self, high: pd.Series, low: pd.Series, close: pd.Series) -> float:
        """ATR(5) / ATR(20) ratio. Contraction = base forming = coiled spring."""
        if len(close) < 21:
            return 0.5
        tr = pd.DataFrame({
            "hl": high - low,
            "hc": abs(high - close.shift(1)),
            "lc": abs(low - close.shift(1)),
        }).max(axis=1)
        atr5 = tr.rolling(5).mean().iloc[-1]
        atr20 = tr.rolling(20).mean().iloc[-1]
        if pd.isna(atr5) or pd.isna(atr20) or atr20 == 0:
            return 0.5
        ratio = atr5 / atr20
        # Ratio 0.5-0.7 = tight consolidation (score 1.0)
        # Ratio 1.0 = normal (score 0.5)
        # Ratio > 1.3 = expanding volatility (score 0.0)
        if ratio <= 0.5:
            return 0.9
        elif ratio <= 0.7:
            return 1.0
        elif ratio <= 1.0:
            return float(1.0 - (ratio - 0.7) / 0.3 * 0.5)
        else:
            return float(max(0.5 - (ratio - 1.0) / 0.6 * 0.5, 0.0))

    def _eq_macd_phase(self, close: pd.Series) -> float:
        """MACD histogram momentum phase. Fresh impulse = best entry."""
        if len(close) < 35:
            return 0.5
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line
        if len(histogram) < 2 or pd.isna(histogram.iloc[-1]) or pd.isna(histogram.iloc[-2]):
            return 0.5
        current = histogram.iloc[-1]
        previous = histogram.iloc[-2]
        # Fresh bullish crossover: histogram just turned positive
        if current > 0 and previous <= 0:
            return 1.0
        # Fresh bearish crossover (could mean beginning of pullback — neutral for entry)
        if current < 0 and previous >= 0:
            return 0.4
        # Positive and rising: mid-move
        if current > 0 and current > previous:
            return 0.5
        # Positive but declining: late in move, momentum fading
        if current > 0 and current <= previous:
            return 0.2
        # Negative and falling: downtrend accelerating — poor entry
        if current < 0 and current < previous:
            return 0.1
        # Negative but rising: potential bottoming — decent entry
        if current < 0 and current >= previous:
            return 0.7
        return 0.5

    def _eq_volume_dryup(self, volume: pd.Series) -> float:
        """Below-average volume = healthy pullback. Expanding volume on decline = distribution."""
        if len(volume) < 21:
            return 0.5
        avg5 = volume.iloc[-5:].mean()
        avg20 = volume.iloc[-20:].mean()
        if avg20 == 0 or pd.isna(avg5) or pd.isna(avg20):
            return 0.5
        ratio = avg5 / avg20
        # Ratio < 0.6 = very low volume (healthy consolidation) → 1.0
        # Ratio ~ 1.0 = normal → 0.5
        # Ratio > 1.5 = high volume (could be distribution) → 0.1
        if ratio <= 0.6:
            return 1.0
        elif ratio <= 1.0:
            return float(1.0 - (ratio - 0.6) / 0.4 * 0.5)
        else:
            return float(max(0.5 - (ratio - 1.0) / 1.0 * 0.4, 0.1))

    def _compute_entry_quality(
        self, close: pd.Series, high: pd.Series, low: pd.Series, volume: pd.Series
    ) -> tuple:
        """Compute Entry Quality Score (0-100) and sub-signal breakdown."""
        eq_signals = {
            "mean_reversion": self._eq_mean_reversion_distance(close, high, low),
            "rsi_sweet_spot": self._eq_rsi_sweet_spot(close),
            "bollinger_position": self._eq_bollinger_position(close),
            "volatility_contraction": self._eq_volatility_contraction(high, low, close),
            "macd_phase": self._eq_macd_phase(close),
            "volume_dryup": self._eq_volume_dryup(volume),
        }
        score = sum(v * self._EQ_WEIGHTS[k] for k, v in eq_signals.items()) * 100
        return (round(score, 2), {k: round(v, 4) for k, v in eq_signals.items()})

    # =========================================================================
    # Composite Score & Direction
    # =========================================================================

    def _composite_signal_names(
        self, signals: Dict[str, float], weights: Dict[str, float]
    ) -> List[str]:
        """Signal keys plus primary sleeves that this book actually weights."""
        names = set(signals.keys())
        for key, weight in (weights or {}).items():
            if key in _PRIMARY_SLEEVE_KEYS and weight > 0:
                names.add(key)
        return list(names)

    @staticmethod
    def _composite_value(
        name: str, value: Any, weight: float
    ) -> Optional[float]:
        """None primary sleeves contribute 0 and still count in the denominator."""
        if value is None:
            if name in _PRIMARY_SLEEVE_KEYS and weight > 0:
                return 0.0
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _compute_composite_score(self, signals: Dict[str, float], weights: Dict[str, float]) -> float:
        """Weighted sum of signals, scaled to 0–100.

        Non-primary None values are skipped and remaining weights renormalized.
        Missing primary sleeves (quality / income / valuation_gap) with weight
        > 0 contribute 0.0 and still count in the denominator so a growth name
        cannot win an income/value book via renormalization.
        """
        score = 0.0
        effective_weight = 0.0
        for name in self._composite_signal_names(signals, weights):
            if name in _COMPOSITE_SKIP_SIGNALS:
                continue
            w = weights.get(name, 0.0)
            value = self._composite_value(name, signals.get(name), w)
            if value is None:
                continue
            score += value * w
            effective_weight += w
        if effective_weight <= 0:
            return 0.0
        return (score / effective_weight) * 100

    def _compute_composite_fundamental_score(
        self, signals: Dict[str, float], weights: Dict[str, float]
    ) -> float:
        """Composite recomputed excluding EQ-overlap technical signals.

        Renormalizes across the remaining weights so presets that are heavily
        technical (e.g. ``momentum_hunter``) don't collapse to a near-zero
        score simply because their dominant signals were excluded. If all
        remaining weights sum to 0 we fall back to the full composite to avoid
        returning NaN — this only happens for pathological preset configs.

        Primary-sleeve Nones still 0-fill; other missing signals are skipped.
        """
        retained_weight = 0.0
        retained_score = 0.0
        for name in self._composite_signal_names(signals, weights):
            if name in self._EQ_OVERLAP_SIGNALS or name in _COMPOSITE_SKIP_SIGNALS:
                continue
            w = weights.get(name, 0.0)
            if w <= 0:
                continue
            value = self._composite_value(name, signals.get(name), w)
            if value is None:
                continue
            retained_weight += w
            retained_score += value * w
        if retained_weight <= 0:
            return self._compute_composite_score(signals, weights)
        return (retained_score / retained_weight) * 100

    # =========================================================================
    # Risk Score (0-100, higher = riskier)
    # =========================================================================
    #
    # Exposed as a separate dimension alongside composite / entry_quality /
    # macro_fit. Deliberately NOT folded into opportunity_score — the point
    # is to let operators sort/filter "high opportunity BUT high risk" vs
    # "high opportunity AND low risk" independently instead of silently
    # collapsing the two. Components are derived from data already on hand
    # (OHLCV + .info) so computing risk_score adds zero API calls.
    #
    # Each sub-component is normalized to 0-100 where 100 = maximum risk on
    # that axis. Missing components are renormalized across present ones so
    # a ticker with partial data still gets a usable score. Weights sum to
    # 1.00 when all eight components are present; for ETFs the
    # profitability/leverage/short_pressure components are skipped and the
    # remaining weights rebalance automatically.
    _RISK_WEIGHTS = {
        "volatility": 0.20,       # 60d realized vol (annualized)
        "drawdown": 0.15,         # Current drawdown from 52w high
        "liquidity": 0.15,        # 1 / log(avg dollar volume)
        "beta": 0.10,             # |beta - 1.0| scaled
        "leverage": 0.15,         # debt/equity (N/A for ETFs)
        "profitability": 0.10,    # -profit_margins (N/A for ETFs)
        "short_pressure": 0.10,   # shortPercentOfFloat (N/A for ETFs)
        "asset_class_baseline": 0.05,  # baseline floor by asset class
    }

    @staticmethod
    def _risk_volatility(close: "pd.Series") -> Optional[float]:
        """60-day realized vol (annualized) → 0-100. <10% → 0, >50% → 100."""
        try:
            if close is None or len(close) < 30:
                return None
            returns = close.pct_change().dropna().tail(60)
            if len(returns) < 10:
                return None
            daily_std = float(returns.std())
            annualized = daily_std * (252 ** 0.5)  # √252 trading days
            # Map 10% → 0, 50% → 100 (clamped)
            pct = max(0.0, min(1.0, (annualized - 0.10) / 0.40))
            return round(pct * 100.0, 1)
        except Exception:
            return None

    @staticmethod
    def _risk_drawdown(close: "pd.Series") -> Optional[float]:
        """Current drawdown from 52w (252d) high → 0-100 as -dd. 0% → 0, 50%+ → 100."""
        try:
            if close is None or len(close) < 20:
                return None
            window = close.tail(252)
            peak = float(window.max())
            current = float(window.iloc[-1])
            if peak <= 0:
                return None
            dd = max(0.0, (peak - current) / peak)
            # Map 0% → 0, 50%+ → 100
            pct = min(1.0, dd / 0.50)
            return round(pct * 100.0, 1)
        except Exception:
            return None

    @staticmethod
    def _risk_liquidity(info: Dict[str, Any], close: "pd.Series") -> Optional[float]:
        """Log-scaled dollar-volume risk. <$1M/day → 100; >$100M/day → 0."""
        try:
            avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day")
            if avg_volume is None or close is None or len(close) == 0:
                return None
            price = float(close.iloc[-1])
            if price <= 0 or avg_volume <= 0:
                return None
            dollar_volume = float(avg_volume) * price
            # log10 scaled: 10^6 ($1M) → 100, 10^8 ($100M) → 0
            import math
            log_dv = math.log10(max(1.0, dollar_volume))
            # Map log_dv=6 → 100, log_dv=8 → 0 (linear interpolation, clamped)
            pct = max(0.0, min(1.0, (8.0 - log_dv) / 2.0))
            return round(pct * 100.0, 1)
        except Exception:
            return None

    @staticmethod
    def _risk_beta(info: Dict[str, Any]) -> Optional[float]:
        """|beta - 1.0| → 0-100. Beta 1.0 → 0; deviation ≥ 1.5 → 100."""
        try:
            beta = info.get("beta")
            if beta is None:
                return None
            b = float(beta)
            deviation = abs(b - 1.0)
            pct = min(1.0, deviation / 1.5)
            return round(pct * 100.0, 1)
        except Exception:
            return None

    @staticmethod
    def _risk_leverage(info: Dict[str, Any]) -> Optional[float]:
        """debtToEquity → 0-100. 0 → 0; 300+ → 100. (yfinance returns D/E as ratio*100.)"""
        try:
            dte = info.get("debtToEquity")
            if dte is None:
                return None
            # yfinance returns debtToEquity as a percentage (e.g. 120 = 1.2x).
            # We treat D/E of 0 → 0 risk, D/E of 300 (3.0x) → 100 risk.
            d = float(dte)
            pct = max(0.0, min(1.0, d / 300.0))
            return round(pct * 100.0, 1)
        except Exception:
            return None

    @staticmethod
    def _risk_profitability(info: Dict[str, Any]) -> Optional[float]:
        """profitMargins inverted → 0-100. +20% margin → 0; -20%+ → 100."""
        try:
            pm = info.get("profitMargins")
            if pm is None:
                return None
            m = float(pm)
            # yfinance returns profitMargins as a decimal (e.g. 0.20 = 20%).
            # Map +20% → 0, -20% → 100, linearly, clamped.
            # Risk = (0.20 - m) / 0.40 clamped to 0-1.
            pct = max(0.0, min(1.0, (0.20 - m) / 0.40))
            return round(pct * 100.0, 1)
        except Exception:
            return None

    @staticmethod
    def _risk_short_pressure(info: Dict[str, Any]) -> Optional[float]:
        """shortPercentOfFloat → 0-100. 0 → 0; 30%+ → 100."""
        try:
            sp = info.get("shortPercentOfFloat")
            if sp is None:
                # Fall back to shortRatio (days-to-cover) if available — 20+ days is elevated.
                sr = info.get("shortRatio")
                if sr is None:
                    return None
                pct = max(0.0, min(1.0, float(sr) / 20.0))
                return round(pct * 100.0, 1)
            # yfinance returns shortPercentOfFloat as a decimal.
            pct = max(0.0, min(1.0, float(sp) / 0.30))
            return round(pct * 100.0, 1)
        except Exception:
            return None

    @staticmethod
    def _risk_asset_class_baseline(asset_class: str) -> float:
        """Baseline floor reflecting structural risk by asset class.

        ETFs are a diversified basket → lower baseline (20). ADRs carry FX +
        geopolitical overlay → medium (40). Plain equities → 50 (neutral).
        Unknown tickers carry uncertainty so they start at 55.
        """
        return {
            "etf": 20.0,
            "adr": 40.0,
            "equity": 50.0,
        }.get(asset_class or "unknown", 55.0)

    def _compute_risk_score(
        self,
        close: "pd.Series",
        info: Dict[str, Any],
        asset_class: str,
    ) -> tuple:
        """Compute risk_score (0-100) + per-component breakdown.

        Returns ``(risk_score, components_dict)`` where components_dict
        contains every sub-component that had data available (missing ones
        are omitted, not set to None, so the frontend can render cleanly).
        The top-level risk_score is a weight-normalized blend across
        whichever components had data. Never returns ``None`` at the
        top level — the asset_class_baseline always provides a floor so
        every row gets a usable risk reading.
        """
        components: Dict[str, float] = {}
        # Probe each component; only include those with data
        vol = self._risk_volatility(close)
        if vol is not None:
            components["volatility"] = vol
        dd = self._risk_drawdown(close)
        if dd is not None:
            components["drawdown"] = dd
        liq = self._risk_liquidity(info or {}, close)
        if liq is not None:
            components["liquidity"] = liq
        beta = self._risk_beta(info or {})
        if beta is not None:
            components["beta"] = beta

        # Equity-only components — ETFs short-circuit these since D/E,
        # profit margins, and short interest don't apply to pooled
        # vehicles (or the values yfinance returns for ETFs are
        # misleading: the SPY "debtToEquity" field is actually the
        # market-cap-weighted average of the underlying basket).
        if (asset_class or "").lower() != "etf":
            lev = self._risk_leverage(info or {})
            if lev is not None:
                components["leverage"] = lev
            prof = self._risk_profitability(info or {})
            if prof is not None:
                components["profitability"] = prof
            sp = self._risk_short_pressure(info or {})
            if sp is not None:
                components["short_pressure"] = sp

        # Asset-class baseline always present — prevents rows with only
        # price data (no .info) from returning None and pushes the score
        # in the right direction based on vehicle type.
        components["asset_class_baseline"] = self._risk_asset_class_baseline(asset_class)

        # Weighted blend across present components (renormalize weights)
        total_weight = 0.0
        score_sum = 0.0
        for name, value in components.items():
            w = self._RISK_WEIGHTS.get(name, 0.0)
            if w <= 0:
                continue
            total_weight += w
            score_sum += value * w
        if total_weight <= 0:
            risk_score = components["asset_class_baseline"]
        else:
            risk_score = score_sum / total_weight
        return (round(max(0.0, min(100.0, risk_score)), 1), components)

    # Signal polarity map: +1 = bullish, -1 = bearish, 0 = neutral/activity
    # Used by _determine_direction to compute a weighted directional score.
    SIGNAL_POLARITY = {
        "volume_surge": 0,         # Activity signal — high volume is ambiguous
        "rsi_oversold": +1,        # Oversold = potential bullish reversal
        "rsi_overbought": -1,      # Overbought = potential bearish reversal
        "ma_crossover": +1,        # EMA above SMA = bullish trend
        "price_vs_target": +1,     # Below analyst target = undervalued (bullish)
        "earnings_proximity": 0,   # Upcoming earnings is a catalyst, not directional
        "insider_buying": +1,      # Insider conviction = bullish
        "relative_strength": +1,   # Outperforming market = bullish momentum
        "bollinger_squeeze": 0,    # Compression precedes a move, but direction unknown
        "trend_strength": 0,       # Strong trend is interesting, but ADX is directionless
        "valuation_gap": +1,       # Cheap vs sector = bullish value
        "options_sentiment": +1,   # Low P/C + call activity = bullish positioning
        "smart_money": +1,         # Institutional accumulation + short covering = bullish
        "estimate_momentum": +1,   # Upward revisions = bullish
        "rating_momentum": +1,     # Net upgrades = bullish
        "pead_drift": +1,
        "weekly_trend_alignment": +1,
        "quality_factor": +1,
        "income_factor": +1,
        "residual_momentum_12_1": +1,
        "short_pressure": +1,      # High SI / DTC is the squeeze setup (bullish)
    }

    def _determine_direction(
        self,
        signals: Dict[str, float],
        weights: Dict[str, float],
        weekly_alignment: Optional[float] = None,
    ) -> str:
        """Determine overall direction using weighted signal polarity.

        Each signal has an inherent polarity (+1 bullish, -1 bearish, 0 neutral).
        The direction score is the weighted sum of (signal_value * polarity * weight),
        normalized by the total directional weight so the magnitude is comparable
        across preset mixes (momentum_hunter vs. dividend_income, etc.).

        The normalized score sits in roughly ``[-1.0, +1.0]``. When its absolute
        value falls below ``direction_conviction_threshold`` (default 0.05) we
        label it ``neutral`` rather than forcing a bullish/bearish call — this
        eliminates the historical bias that came from bullish-polarity signals
        outnumbering bearish ones 12:1 and tripped small positive residuals
        into false "bullish" labels on weakly-scoring defensives.
        """
        net_score = 0.0
        total_directional_weight = 0.0
        for name, value in signals.items():
            if value is None:
                continue  # No-data sentinel — exclude from directional score
            polarity = self.SIGNAL_POLARITY.get(name, 0)
            if polarity == 0:
                continue  # Neutral signals don't affect direction
            w = weights.get(name, 0.0)
            if w <= 0:
                continue
            net_score += value * polarity * w
            total_directional_weight += w

        if total_directional_weight <= 0:
            return "neutral"

        normalized = net_score / total_directional_weight
        threshold = float(
            self._screening_config.get("direction_conviction_threshold", 0.05)
        )
        if normalized > threshold:
            direction = "bullish"
        elif normalized < -threshold:
            direction = "bearish"
        else:
            direction = "neutral"

        if (
            direction == "bullish"
            and weekly_alignment is not None
            and float(weekly_alignment) < 0.35
        ):
            return "neutral"
        return direction

    @staticmethod
    def _compute_signal_coverage(
        signals: Dict[str, Any],
        weights: Dict[str, float],
        presence_signals: Optional[set[str]] = None,
    ) -> float:
        """Return weighted signal availability.

        Most signals count only when positive because a zero historically
        represents no usable contribution. ``presence_signals`` is for
        directional indicators where a computed zero is meaningful data:
        e.g. a bearish weekly alignment should count as observed, while an
        unfetched weekly alignment (None) must remain absent.
        """
        total = sum(w for w in weights.values() if w > 0)
        if total <= 0:
            return 0.0
        presence_signals = presence_signals or set()
        present = 0.0
        for sig, w in weights.items():
            if w <= 0:
                continue
            val = signals.get(sig)
            if val is None:
                continue
            if sig in presence_signals or float(val) > 0:
                present += w
        return round(100.0 * present / total, 1)

    @staticmethod
    def _resolve_tier_reached(
        ticker: str,
        *,
        enhanced_tickers: set,
        funnel_tickers: set,
        aux_tickers: set,
    ) -> str:
        if ticker in enhanced_tickers:
            return "enhanced"
        if ticker in aux_tickers:
            return "tier2_aux"
        if ticker in funnel_tickers:
            return "tier2"
        return "tier1_only"

    def _apply_cross_sectional_zscore(
        self,
        tier1_scores: Dict[str, Dict[str, Any]],
        meta_map: Dict[str, Dict[str, Any]],
    ) -> None:
        """Within-sector z-score eligible signals, then map back to [0, 1].

        Distinct from Scan All ``opportunity_z`` (per-watchlist dedup).
        Disabled unless ``screening.cross_sectional_zscore.enabled`` is true.
        Sectors with fewer than ``min_peers`` observed values are left raw.
        """
        cfg = self._screening_config.get("cross_sectional_zscore", {}) or {}
        if not cfg.get("enabled"):
            return
        min_peers = max(2, int(cfg.get("min_peers", 8) or 8))
        clip = float(cfg.get("clip", 3.0) or 3.0)
        if clip <= 0:
            clip = 3.0
        eligible = list(cfg.get("eligible_keys") or _DEFAULT_ZSCORE_KEYS)
        by_sector: Dict[str, List[str]] = {}
        for ticker in tier1_scores:
            meta = meta_map.get(ticker.upper(), meta_map.get(ticker, {})) or {}
            sector = str(meta.get("sector") or "Unknown")
            by_sector.setdefault(sector, []).append(ticker)
        for _sector, names in by_sector.items():
            if len(names) < min_peers:
                continue
            for key in eligible:
                observed: List[Tuple[str, float]] = []
                for ticker in names:
                    raw = (tier1_scores[ticker].get("signals") or {}).get(key)
                    if raw is None:
                        continue
                    try:
                        observed.append((ticker, float(raw)))
                    except (TypeError, ValueError):
                        continue
                if len(observed) < min_peers:
                    continue
                values = [v for _, v in observed]
                mean = sum(values) / len(values)
                var = sum((v - mean) ** 2 for v in values) / len(values)
                std = math.sqrt(var)
                if std < 1e-9:
                    continue
                for ticker, value in observed:
                    z = max(-clip, min(clip, (value - mean) / std))
                    mapped = (z + clip) / (2.0 * clip)
                    tier1_scores[ticker]["signals"][key] = float(max(0.0, min(1.0, mapped)))

    # =========================================================================
    # Cross-Watchlist Percentile Ranking
    # =========================================================================

    def _apply_percentile_rankings(self, results: List[ScreeningResult]):
        """
        Compute percentile of each result's composite score relative to the
        historical baseline (last 10 screening runs across all watchlists).
        """
        if not self.db or not results:
            return

        try:
            historical_scores = self._get_historical_score_baseline()
            if not historical_scores or len(historical_scores) < 30:
                # Need at least 30 historical data points for meaningful percentiles
                return
            historical_scores_sorted = sorted(historical_scores)
            n = len(historical_scores_sorted)
            for r in results:
                # Count how many historical scores are below this result's score
                count_below = sum(1 for s in historical_scores_sorted if s < r.composite_score)
                r.percentile = round(count_below / n * 100, 1) if n > 0 else None
        except Exception as e:
            logger.warning("Percentile ranking failed: %s", e)

    def _get_historical_score_baseline(self) -> List[float]:
        """Get composite scores from recent screening runs for baseline."""
        try:
            runs = self.db.get_screening_runs(limit=10)
            scores = []
            for run in runs:
                results = self.db.get_screening_results(run["id"])
                for r in results:
                    if r.get("composite_score") is not None:
                        scores.append(float(r["composite_score"]))
            return scores
        except Exception as e:
            logger.debug("Historical score baseline failed: %s", e)
            return []

    # =========================================================================
    # Signal Momentum Deltas — load prior signal values from DB
    # =========================================================================

    def _load_prior_signals(self, tickers: List[str]) -> Dict[str, Dict[str, float]]:
        """
        Load signal values from the most recent prior screening run that
        included each ticker. Used to compute signal deltas (rate of change).
        """
        if not self.db:
            return {}

        prior: Dict[str, Dict[str, float]] = {}
        try:
            # Get the last few runs (we need at least one prior)
            runs = self.db.get_screening_runs(limit=5)
            if len(runs) < 1:
                return {}

            # Build a lookup of the most recent prior signal values per ticker
            for run in runs:
                results = self.db.get_screening_results(run["id"])
                for r in results:
                    t = r.get("ticker", "")
                    if t in tickers and t not in prior:
                        signals_raw = r.get("signals", "{}")
                        if isinstance(signals_raw, str):
                            try:
                                signals = json.loads(signals_raw)
                            except (json.JSONDecodeError, TypeError):
                                signals = {}
                        else:
                            signals = signals_raw or {}
                        if signals:
                            numeric: Dict[str, float] = {}
                            for k, v in signals.items():
                                if str(k).startswith("_") or v is None:
                                    continue
                                if isinstance(v, (int, float)):
                                    numeric[k] = float(v)
                            if numeric:
                                prior[t] = numeric
        except Exception as e:
            logger.warning("Failed to load prior signals: %s", e)

        return prior

    # =========================================================================
    # Macro-Tactical Overlay
    # =========================================================================

    def _compute_macro_overlay(
        self,
        tier1_scores: Dict[str, Dict],
        meta_map: Dict[str, Dict[str, Any]],
        as_of_date: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Score macro_fit for every Tier 1 survivor. Never raises."""
        ticker_macro: Dict[str, Dict[str, Any]] = {}
        if not tier1_scores:
            return ticker_macro

        try:
            from tradingagents.screening.macro_overlay import (
                compute_macro_fit,
                compute_ticker_macro_overlay,
                get_macro_overlay_context,
            )

            macro_snapshot, market_env, sector_data = get_macro_overlay_context(as_of_date=as_of_date)
            market_score = float(market_env.get("score", 50) or 50)
            logger.info(
                "Macro overlay: market_env=%.1f, computing per-ticker regime fit...",
                market_score,
            )

            for ticker in tier1_scores:
                try:
                    t_meta = meta_map.get(ticker.upper(), meta_map.get(ticker, {}))
                    overlay = compute_ticker_macro_overlay(
                        macro=macro_snapshot,
                        sector_data=sector_data,
                        market_env=market_env,
                        profile=t_meta.get("resolved_profile", "unknown"),
                        ticker_sector=t_meta.get("sector", "Unknown"),
                        ticker_meta=t_meta,
                    )
                    ticker_macro[ticker] = overlay
                except Exception as exc:
                    logger.debug("Macro overlay for %s failed: %s", ticker, exc)

            # Market-level baseline beats N/A when per-ticker scoring fails.
            if len(ticker_macro) < len(tier1_scores):
                from tradingagents.dataflows.index_regime import normalize_index_regime

                index_fields = normalize_index_regime(macro_snapshot)
                baseline_fit = compute_macro_fit(market_score, 50.0, 50.0)
                baseline_breakdown = {
                    "market_environment": {
                        "score": market_score,
                        "breakdown": market_env.get("breakdown", {}),
                    },
                    "sector_momentum": {"score": 50, "sector": "Unknown", "rank": 6},
                    "regime_fit": {
                        "score": 50,
                        "profile_used": "baseline",
                        "breakdown": {},
                    },
                }
                for ticker in tier1_scores:
                    if ticker not in ticker_macro:
                        ticker_macro[ticker] = {
                            "macro_fit": baseline_fit,
                            "macro_breakdown": baseline_breakdown,
                            "index_regime": index_fields.get("index_regime"),
                            "index_regime_label": index_fields.get("index_regime_label"),
                            "index_trend": index_fields.get("index_trend"),
                            "index_stress": index_fields.get("index_stress"),
                        }

            logger.info(
                "Macro overlay: scored %d/%d tickers",
                len(ticker_macro),
                len(tier1_scores),
            )
        except Exception as exc:
            logger.warning(
                "Macro overlay failed globally: %s — results will have macro_fit=None",
                exc,
            )
        return ticker_macro

    # =========================================================================
    # Macro Regime
    # =========================================================================

    def _get_regime(self, as_of_date: Optional[str] = None) -> str:
        """Get market regime (trend only) from macro snapshot."""
        try:
            macro = get_macro_snapshot(as_of_date=as_of_date)
            return macro.get("market_regime", "unknown")
        except Exception as e:
            logger.debug("Macro regime fetch failed: %s", e)
            return "unknown"

    # =========================================================================
    # Adaptive Weight Learning (Feature 7)
    # =========================================================================

    @staticmethod
    def _normalize_forward_return_records(
        records: List[Dict[str, Any]],
        columns: Tuple[str, ...] = ("return_7d", "return_14d", "return_30d"),
    ) -> None:
        """Coerce legacy percent-scale screening returns to decimals in-place.

        ``backtest_screening_run`` used to store 2.5 for +2.5%. Analysis
        backtests and the Signal Performance UI use 0.025. If the typical
        absolute value is > 1.0, treat the column as percent.
        """
        for col in columns:
            vals = [abs(float(r[col])) for r in records if r.get(col) is not None]
            if not vals:
                continue
            vals.sort()
            typical = vals[len(vals) // 2]
            if typical <= 1.0:
                continue
            for r in records:
                if r.get(col) is None:
                    continue
                r[col] = float(r[col]) / 100.0

    def compute_adaptive_weights(self, min_samples: int = 50) -> Optional[Dict[str, float]]:
        """
        Compute signal weights optimized by correlation with forward returns.

        Requires historical screening results with populated return_7d.
        Returns None if insufficient data.

        Algorithm:
        1. Pull all screening results with forward returns
        2. Compute Pearson correlation of each signal with 7-day return
        3. Positive correlations become weights (negatives zeroed)
        4. Normalize to sum to 1.0
        """
        if not self.db:
            return None

        try:
            results = []
            if hasattr(self.db, "get_backtested_screening_results"):
                results = self.db.get_backtested_screening_results(limit=2000)
            else:
                for run in self.db.get_screening_runs(limit=50):
                    results.extend(self.db.get_screening_results(run["id"]))

            records = []
            for r in results:
                ret7 = r.get("return_7d")
                if ret7 is None:
                    continue
                signals_raw = r.get("signals", "{}")
                if isinstance(signals_raw, str):
                    try:
                        signals = json.loads(signals_raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                else:
                    signals = signals_raw or {}
                if signals:
                    record = {**signals, "return_7d": float(ret7)}
                    records.append(record)
            self._normalize_forward_return_records(records, ("return_7d",))

            if len(records) < min_samples:
                logger.info("Adaptive weights: insufficient data (%d/%d samples)", len(records), min_samples)
                return None

            df = pd.DataFrame(records)
            signal_cols = [c for c in df.columns if c != "return_7d"]

            correlations = {}
            for col in signal_cols:
                if col in df.columns and df[col].notna().sum() >= min_samples // 2:
                    corr = df[col].corr(df["return_7d"])
                    if not pd.isna(corr):
                        correlations[col] = max(corr, 0.0)  # Only positive correlations

            if not correlations:
                return None

            total = sum(correlations.values())
            if total <= 0:
                logger.info("Adaptive weights: no signal has positive correlation with returns")
                return None
            weights = {k: round(v / total, 4) for k, v in correlations.items()}
            logger.info("Adaptive weights computed from %d samples: %s", len(records), weights)
            return weights

        except Exception as e:
            logger.error("Adaptive weights failed: %s", e)
            return None

    def compute_signal_performance(
        self, min_samples: int = 30, run_limit: int = 50,
    ) -> Dict[str, Dict[str, Any]]:
        """Compute per-signal hit rates and correlations with forward returns.

        For each signal, computes:
        - hit_rate: % of times signal > 0.5 predicted a positive return
        - avg_return: mean return when the signal fired (> 0.5)
        - correlation: Pearson correlation with forward returns

        Results are saved to the signal_performance DB table and returned.
        """
        if not self.db:
            return {}

        try:
            fetch_limit = max(int(run_limit) * 80, 400)
            if hasattr(self.db, "get_backtested_screening_results"):
                results = self.db.get_backtested_screening_results(limit=fetch_limit)
            else:
                results = []
                for run in self.db.get_screening_runs(limit=max(1, int(run_limit))):
                    results.extend(self.db.get_screening_results(run["id"]))

            records = []
            for r in results:
                signals_raw = r.get("signals", "{}")
                if isinstance(signals_raw, str):
                    try:
                        signals = json.loads(signals_raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                else:
                    signals = signals_raw or {}
                if signals:
                    record = dict(signals)
                    for col in ("return_7d", "return_14d", "return_30d"):
                        record[col] = r.get(col)
                    records.append(record)
            self._normalize_forward_return_records(records)

            if len(records) < min_samples:
                return {}

            df = pd.DataFrame(records)
            signal_cols = [c for c in df.columns if c not in ("return_7d", "return_14d", "return_30d")]

            perf: Dict[str, Dict[str, Any]] = {}
            for period, ret_col in [("7d", "return_7d"), ("14d", "return_14d"), ("30d", "return_30d")]:
                valid = df[df[ret_col].notna()]
                if len(valid) < min_samples:
                    continue
                for sig in signal_cols:
                    if sig not in valid.columns or valid[sig].notna().sum() < min_samples // 2:
                        continue
                    fired = valid[valid[sig] > 0.5]
                    hit_rate = (fired[ret_col] > 0).mean() if len(fired) > 0 else 0
                    avg_ret = fired[ret_col].mean() if len(fired) > 0 else 0
                    corr = valid[sig].corr(valid[ret_col])
                    if pd.isna(corr):
                        corr = 0.0

                    key = f"{sig}_{period}"
                    hit_pct = round(float(hit_rate) * 100, 1)
                    perf[key] = {
                        "signal_name": sig,
                        "period": period,
                        "hit_rate": hit_pct,
                        "avg_return": round(float(avg_ret), 6),
                        "correlation": round(float(corr), 4),
                        "sample_count": int(len(fired)),
                    }
                    self.db.save_signal_performance(
                        signal_name=sig, period=period,
                        hit_rate=hit_pct,
                        avg_return=round(float(avg_ret), 6),
                        correlation=round(float(corr), 4),
                        sample_count=int(len(fired)),
                    )

            if perf:
                logger.info("Signal performance computed: %d signal-period combinations from %d records", len(perf), len(records))
            return perf

        except Exception as e:
            logger.error("Signal performance computation failed: %s", e)
            return {}

    def compute_signal_performance_by_factor(
        self,
        min_samples: int = 30,
        period: str = "7d",
    ) -> Dict[str, Dict[str, Any]]:
        """Group per-signal performance metrics by factor family.

        Calls :meth:`compute_signal_performance` then aggregates by FACTOR_FAMILIES,
        returning a dict keyed by family name with avg hit_rate, avg_return,
        avg_correlation, and the contributing signals.

        Args:
            min_samples: Minimum samples required (forwarded to compute_signal_performance).
            period: Return horizon to aggregate ("7d" | "14d" | "30d").

        Returns:
            Dict[family_name, {avg_hit_rate, avg_return, avg_correlation, signals, sample_count}]
        """
        raw = self.compute_signal_performance(min_samples=min_samples)
        if not raw:
            return {}

        # Build a flat signal→metrics map for the requested period
        sig_perf: Dict[str, Dict[str, Any]] = {}
        for key, v in raw.items():
            if v.get("period") == period:
                sig_perf[v["signal_name"]] = v

        # Aggregate by family
        family_results: Dict[str, Dict[str, Any]] = {}
        for family, members in FACTOR_FAMILIES.items():
            if not members:
                continue
            contributing = [sig_perf[m] for m in members if m in sig_perf]
            if not contributing:
                continue
            family_results[family] = {
                "avg_hit_rate": round(sum(c["hit_rate"] for c in contributing) / len(contributing), 4),
                "avg_return": round(sum(c["avg_return"] for c in contributing) / len(contributing), 6),
                "avg_correlation": round(sum(c["correlation"] for c in contributing) / len(contributing), 4),
                "signals": [c["signal_name"] for c in contributing],
                "sample_count": max(c["sample_count"] for c in contributing),
                "period": period,
            }
        return family_results

    def compute_adaptive_weights_by_factor(
        self,
        min_samples: int = 50,
    ) -> Dict[str, float]:
        """Compute normalized adaptive weights grouped by factor family.

        Runs :meth:`compute_adaptive_weights` on individual signals then
        sums weights within each family to produce family-level portfolio tilts.

        Returns:
            Dict[family_name, normalized_weight_0_to_1].  Empty dict if insufficient data.
        """
        signal_weights = self.compute_adaptive_weights(min_samples=min_samples)
        if not signal_weights:
            return {}

        family_weights: Dict[str, float] = {}
        for family, members in FACTOR_FAMILIES.items():
            total = sum(signal_weights.get(m, 0.0) for m in members)
            if total > 0:
                family_weights[family] = total

        # Normalize so family weights sum to 1.0
        grand_total = sum(family_weights.values()) or 1.0
        return {f: round(w / grand_total, 4) for f, w in family_weights.items()}
