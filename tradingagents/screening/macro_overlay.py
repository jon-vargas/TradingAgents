"""
Macro-Tactical Overlay for Screening Engine.

Three independent scoring layers producing a per-ticker "Macro Fit" score (0-100):
  Layer 1 — Market Environment  (global, same for all tickers)
  Layer 2 — Sector Momentum     (per-sector, looked up per ticker)
  Layer 3 — Profile-Specific Regime Fit (per-ticker, tailored scorecard)

Composite:
  macro_fit = market_env * 0.30 + sector_momentum * 0.40 + regime_fit * 0.30
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import yfinance as yf

from tradingagents.dataflows.cache import CacheConfig, get_cache

logger = logging.getLogger("tradingagents.macro_overlay")

# =========================================================================
# Constants
# =========================================================================

COMPOSITE_WEIGHTS = {"market": 0.30, "sector": 0.40, "regime": 0.30}

# GICS sector name → SPDR sector ETF ticker
SECTOR_ETF_MAP: Dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Consumer Defensive": "XLP",
    "Consumer Cyclical": "XLY",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
}

# All ETFs fetched in a single yf.download() call
_SECTOR_ETFS = list(SECTOR_ETF_MAP.values())
_ALL_SYMBOLS = _SECTOR_ETFS + ["IWM", "SPY"]

# Sector momentum score range (10-100). Scores are a continuous min-max
# normalization of blended relative returns across sectors with data — see
# compute_sector_momentum() — rather than a discrete rank bucket, so two
# sectors with nearly identical momentum don't land 25+ points apart just
# for being on opposite sides of a rank cutoff.
_SECTOR_SCORE_MIN = 10.0
_SECTOR_SCORE_MAX = 100.0
_SECTOR_SCORE_NEUTRAL = 50.0
# Sentinel rank for a sector with no usable ETF data — deliberately worse
# than any real rank (1-11) so "top N sector" checks elsewhere naturally
# exclude it without needing None-aware comparisons.
_MISSING_SECTOR_RANK = 12

# Profile names that map to dedicated scorecards
SUPPORTED_PROFILES = {"high_growth", "momentum_speculative", "dividend_income", "commodity_cyclical", "large_cap_core"}


# =========================================================================
# Layer 1 — Market Environment Score (global)
# =========================================================================

def compute_market_environment_score(macro: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute the global market environment score (0-100) from macro snapshot data.

    Args:
        macro: Dict returned by get_macro_snapshot()

    Returns:
        {"score": float, "breakdown": {sub_factor: {"score": X, "max": Y, "detail": str}}}
    """
    indices = macro.get("indices", {})
    spy = indices.get("sp500", {})
    vix = indices.get("vix", {})
    dxy = indices.get("dollar_index", {})

    vix_level = macro.get("vix_level", "unknown")
    vix_trend = macro.get("vix_trend", "unknown")
    yc_shape = macro.get("yield_curve", "unknown")
    yc_trend = macro.get("yield_curve_trend", "unknown")
    dollar_trend = macro.get("dollar_trend", "unknown")
    dollar_pct = macro.get("dollar_pct_from_sma50", 0.0)

    breakdown = {}

    # --- Equity Trend (0-25) ---
    eq_score = 10
    eq_detail = "unknown"
    spy_cur = spy.get("current")
    spy_50 = spy.get("sma_50")
    spy_200 = spy.get("sma_200")
    if spy_cur and spy_200:
        pct_from_200 = abs(spy_cur / spy_200 - 1) * 100
        if spy_50 and spy_cur > spy_50 and spy_cur > spy_200:
            eq_score, eq_detail = 25, "Above 50 & 200 SMA"
        elif spy_cur > spy_200 and (spy_50 is None or spy_cur <= spy_50):
            eq_score, eq_detail = 15, "Above 200 SMA, below 50 SMA"
        elif pct_from_200 <= 2:
            eq_score, eq_detail = 10, "Within 2% of 200 SMA (no-mans-land)"
        else:
            eq_score, eq_detail = 5, "Below both SMAs"
    breakdown["equity_trend"] = {"score": eq_score, "max": 25, "detail": eq_detail}

    # --- Volatility Regime (0-25) ---
    vol_score = 12
    vol_detail = "unknown"
    vix_cur = vix.get("current")
    if vix_cur is not None:
        if vix_cur < 15:
            vol_score = 25 if vix_trend == "falling" else 20
            vol_detail = f"VIX {vix_cur:.1f}, {vix_trend}"
        elif vix_cur < 25:
            vol_score = 18 if vix_trend == "falling" else 12
            vol_detail = f"VIX {vix_cur:.1f} normal, {vix_trend}"
        elif vix_cur < 35:
            vol_score = 10 if vix_trend == "falling" else 5
            vol_detail = f"VIX {vix_cur:.1f} elevated, {vix_trend}"
        else:
            vol_score, vol_detail = 3, f"VIX {vix_cur:.1f} panic"
    breakdown["volatility_regime"] = {"score": vol_score, "max": 25, "detail": vol_detail}

    # --- Yield Curve (0-25) ---
    yc_score = 10
    yc_detail = f"{yc_shape}/{yc_trend}"
    if yc_shape == "normal":
        yc_score = {"steepening": 25, "stable": 22, "flattening": 16}.get(yc_trend, 20)
    elif yc_shape == "flat":
        yc_score = 15 if yc_trend == "steepening" else 10
    elif yc_shape == "inverted":
        yc_score = {"steepening": 12, "stable": 6, "flattening": 3}.get(yc_trend, 6)
    breakdown["yield_curve"] = {"score": yc_score, "max": 25, "detail": yc_detail}

    # --- Dollar / Liquidity (0-25) ---
    dl_score = 15
    dl_detail = f"{dollar_trend}, {dollar_pct:+.1f}% from SMA50"
    if dollar_trend == "weakening":
        dl_score = 25 if dollar_pct < -2 else 20
    elif dollar_trend == "strengthening":
        dl_score = 10 if dollar_pct < 2 else 5
    else:
        dl_score = 15
    breakdown["dollar_liquidity"] = {"score": dl_score, "max": 25, "detail": dl_detail}

    total = eq_score + vol_score + yc_score + dl_score
    return {"score": round(total, 2), "breakdown": breakdown}


# =========================================================================
# Shared context — resilient fetch for screening overlay
# =========================================================================

def get_macro_overlay_context(as_of_date: Optional[str] = None) -> tuple:
    """
    Return ``(macro_snapshot, market_env, sector_data)`` for overlay scoring.

    Never raises. Uses cached data when live fetches fail (e.g. yfinance
    breaker open after a long Scan All batch).
    """
    from tradingagents.dataflows.yfinance_extended import get_macro_snapshot

    neutral_sector = {
        "sector_ranks": {s: _MISSING_SECTOR_RANK for s in SECTOR_ETF_MAP},
        "sector_scores": {s: _SECTOR_SCORE_NEUTRAL for s in SECTOR_ETF_MAP},
        "sector_data_quality": {s: "missing" for s in SECTOR_ETF_MAP},
        "etf_returns_20d": {etf: 0.0 for etf in _SECTOR_ETFS},
        "etf_returns_60d": {etf: 0.0 for etf in _SECTOR_ETFS},
        "iwm_vs_spy_20d": 0.0,
    }
    macro_snapshot: Dict[str, Any] = {}
    market_env: Dict[str, Any] = {"score": 50.0, "breakdown": {}}
    sector_data: Dict[str, Any] = dict(neutral_sector)

    try:
        macro_snapshot = get_macro_snapshot(as_of_date=as_of_date) or {}
        market_env = compute_market_environment_score(macro_snapshot)
    except Exception as exc:
        logger.warning(
            "Macro overlay context: snapshot/market-env failed (%s) — neutral baseline",
            exc,
        )

    try:
        sector_data = compute_sector_momentum()
    except Exception as exc:
        logger.warning(
            "Macro overlay context: sector momentum failed (%s) — neutral sectors",
            exc,
        )

    return macro_snapshot, market_env, sector_data


# =========================================================================
# Layer 2 — Sector Momentum Score
# =========================================================================

def _score_sectors_continuous(sector_blended: Dict[str, float]) -> Dict[str, float]:
    """Min-max normalize blended relative returns to a continuous 10-100 score.

    Preferred over a discrete rank->bucket table: two sectors separated by a
    hair of relative return no longer land 25+ points apart just because they
    fall on opposite sides of a rank cutoff, and a wide spread of returns
    isn't compressed into the same handful of buckets as a tight one.
    """
    if not sector_blended:
        return {}
    values = list(sector_blended.values())
    lo, hi = min(values), max(values)
    spread = hi - lo
    scores: Dict[str, float] = {}
    for sector, value in sector_blended.items():
        norm = 0.5 if spread <= 1e-9 else (value - lo) / spread
        scores[sector] = round(_SECTOR_SCORE_MIN + (_SECTOR_SCORE_MAX - _SECTOR_SCORE_MIN) * norm, 1)
    return scores


def compute_sector_momentum() -> Dict[str, Any]:
    """
    Fetch sector ETFs + IWM + SPY, compute relative returns, and rank sectors.

    Sectors whose ETF fetch fails entirely (both 20d and 60d relative return
    unavailable) are excluded from the ranking/scoring pool rather than
    silently blended in as a 0.0 ("in-line with SPY") return — a failed
    fetch is not the same claim as "this sector tracked the market exactly."
    They get a neutral score (50) and a sentinel rank of 12 (worse than any
    real 1-11 rank, so they're naturally excluded from "top sector" checks
    elsewhere without those callers needing None-handling), and are flagged
    in ``sector_data_quality`` so callers/UI can distinguish "no edge" from
    "no data".

    Returns:
        {
            "sector_ranks": {sector_name: rank (1-11, or 12 if no data)},
            "sector_scores": {sector_name: score (10-100)},
            "sector_data_quality": {sector_name: "full"|"partial"|"missing"},
            "etf_returns_20d": {etf: float},
            "etf_returns_60d": {etf: float},
            "iwm_vs_spy_20d": float,
        }
    """
    cache = get_cache()
    cached = cache.get("sector_momentum", "global")
    if cached is not None:
        logger.debug("sector_momentum cache HIT")
        return cached

    logger.info("Fetching sector momentum data for %d symbols", len(_ALL_SYMBOLS))
    t0 = time.monotonic()

    try:
        import pandas as pd
        from datetime import datetime, timedelta

        end = datetime.now()
        start = end - timedelta(days=120)
        data = yf.download(
            " ".join(_ALL_SYMBOLS),
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            group_by="ticker",
            progress=False,
            auto_adjust=True,
        )

        def _rel_return(etf: str, days: int) -> Optional[float]:
            """Compute ETF return minus SPY return over `days` trading days."""
            try:
                etf_close = data[etf]["Close"].dropna()
                spy_close = data["SPY"]["Close"].dropna()
                if len(etf_close) < days or len(spy_close) < days:
                    return None
                etf_ret = (etf_close.iloc[-1] / etf_close.iloc[-days] - 1) * 100
                spy_ret = (spy_close.iloc[-1] / spy_close.iloc[-days] - 1) * 100
                return round(float(etf_ret - spy_ret), 3)
            except Exception as e:
                logger.debug("Relative return calc failed for %s (%dd): %s", etf, days, e)
                return None

        etf_returns_20d: Dict[str, float] = {}
        etf_returns_60d: Dict[str, float] = {}
        sector_blended: Dict[str, float] = {}
        sector_data_quality: Dict[str, str] = {}

        for sector, etf in SECTOR_ETF_MAP.items():
            r20 = _rel_return(etf, 20)
            r60 = _rel_return(etf, 60)
            # These raw per-ETF fields stay 0.0-defaulted for backward
            # compatibility with the Layer 3 scorecards below, which each
            # read a specific ETF's return as one of several inputs — a
            # missing single ETF there is a minor, tolerable approximation.
            # The ranking/scoring pool below is stricter (see docstring).
            etf_returns_20d[etf] = r20 if r20 is not None else 0.0
            etf_returns_60d[etf] = r60 if r60 is not None else 0.0

            if r20 is None and r60 is None:
                sector_data_quality[sector] = "missing"
                continue
            elif r20 is None:
                blended = r60
                sector_data_quality[sector] = "partial"
            elif r60 is None:
                blended = r20
                sector_data_quality[sector] = "partial"
            else:
                blended = r20 * 0.6 + r60 * 0.4
                sector_data_quality[sector] = "full"
            sector_blended[sector] = blended

        # Rank + score only sectors with at least partial data. Sectors with
        # no data at all don't distort the ranking of sectors that do.
        sorted_sectors = sorted(sector_blended.items(), key=lambda x: x[1], reverse=True)
        sector_ranks: Dict[str, int] = {
            sector: rank for rank, (sector, _) in enumerate(sorted_sectors, 1)
        }
        sector_scores = _score_sectors_continuous(sector_blended)
        for sector in SECTOR_ETF_MAP:
            if sector not in sector_scores:
                sector_ranks[sector] = _MISSING_SECTOR_RANK
                sector_scores[sector] = _SECTOR_SCORE_NEUTRAL

        # IWM vs SPY (used by momentum_speculative scorecard)
        iwm_vs_spy_20d = _rel_return("IWM", 20) or 0.0

        elapsed = time.monotonic() - t0
        logger.info("Sector momentum computed in %.1fs — top: %s, bottom: %s%s",
                     elapsed, sorted_sectors[0][0] if sorted_sectors else "?",
                     sorted_sectors[-1][0] if sorted_sectors else "?",
                     f" (missing: {sorted(s for s, q in sector_data_quality.items() if q == 'missing')})"
                     if "missing" in sector_data_quality.values() else "")

    except Exception as e:
        logger.warning("Sector momentum fetch failed: %s — using neutral scores", e)
        sector_ranks = {s: _MISSING_SECTOR_RANK for s in SECTOR_ETF_MAP}
        sector_scores = {s: _SECTOR_SCORE_NEUTRAL for s in SECTOR_ETF_MAP}
        sector_data_quality = {s: "missing" for s in SECTOR_ETF_MAP}
        etf_returns_20d = {etf: 0.0 for etf in _SECTOR_ETFS}
        etf_returns_60d = {etf: 0.0 for etf in _SECTOR_ETFS}
        iwm_vs_spy_20d = 0.0

    result = {
        "sector_ranks": sector_ranks,
        "sector_scores": sector_scores,
        "sector_data_quality": sector_data_quality,
        "etf_returns_20d": etf_returns_20d,
        "etf_returns_60d": etf_returns_60d,
        "iwm_vs_spy_20d": iwm_vs_spy_20d,
    }
    cache.set("sector_momentum", "global", data=result)
    return result


# =========================================================================
# Layer 3 — Profile-Specific Macro Scorecards
# =========================================================================

def score_profile_scorecard(
    profile: str,
    macro: Dict[str, Any],
    sector_data: Dict[str, Any],
    ticker_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Select the appropriate scorecard for a ticker's profile and compute its score.

    Args:
        profile: resolved_profile from ticker metadata
        macro: Dict returned by get_macro_snapshot()
        sector_data: Dict returned by compute_sector_momentum()
        ticker_meta: Per-ticker metadata dict (sector, beta_tier, etc.)

    Returns:
        {"score": float (0-100), "profile_used": str, "breakdown": {factor: {"score": X, "max": Y, "detail": str}}}
    """
    profile = (profile or "unknown").lower().strip()
    scorecard_fn = _SCORECARD_DISPATCH.get(profile, _scorecard_fallback)
    result = scorecard_fn(macro, sector_data, ticker_meta)
    result["profile_used"] = profile if profile in SUPPORTED_PROFILES else "fallback"
    return result


# ---------------------------------------------------------------------------
# Individual scorecards
# ---------------------------------------------------------------------------

def _get_10y_rate_direction(macro: Dict) -> tuple:
    """Helper: classify 10Y treasury vs SMA50. Returns (score_bucket, detail)."""
    t10 = macro.get("indices", {}).get("treasury_10y", {})
    cur = t10.get("current")
    sma = t10.get("sma_50")
    if cur is None or sma is None or sma == 0:
        return "unknown", "10Y data unavailable"
    pct = (cur / sma - 1) * 100
    if pct < -2:
        return "falling", f"10Y {cur:.2f} well below SMA50"
    elif pct < 0:
        return "falling_mild", f"10Y {cur:.2f} slightly below SMA50"
    elif pct < 2:
        return "stable", f"10Y {cur:.2f} near SMA50"
    else:
        return "rising", f"10Y {cur:.2f} above SMA50"


def _get_spy_trend(macro: Dict) -> tuple:
    """Helper: classify SPY vs SMAs. Returns (label, detail)."""
    spy = macro.get("indices", {}).get("sp500", {})
    cur = spy.get("current")
    sma50 = spy.get("sma_50")
    sma200 = spy.get("sma_200")
    if cur is None or sma200 is None:
        return "unknown", "SPY data unavailable"
    above_200 = cur > sma200
    above_50 = (sma50 is not None and cur > sma50)
    pct_from_200 = abs(cur / sma200 - 1) * 100
    if above_50 and above_200:
        return "strong", f"SPY above 50 & 200 SMA"
    elif above_200:
        return "moderate", f"SPY above 200, below 50 SMA"
    elif pct_from_200 <= 2:
        return "transition", f"SPY within 2% of 200 SMA"
    else:
        return "weak", f"SPY below both SMAs"


def _get_vix_info(macro: Dict) -> tuple:
    """Helper: returns (vix_value, vix_level, vix_trend)."""
    vix = macro.get("indices", {}).get("vix", {})
    return (
        vix.get("current"),
        macro.get("vix_level", "unknown"),
        macro.get("vix_trend", "unknown"),
    )


def _etf_rel_return(sector_data: Dict, etf: str) -> float:
    """Get 20-day relative return for an ETF from sector data."""
    return sector_data.get("etf_returns_20d", {}).get(etf, 0.0)


def _avg_etf_rel_return(sector_data: Dict, etfs: list) -> float:
    """Average 20-day relative return across multiple ETFs."""
    vals = [_etf_rel_return(sector_data, e) for e in etfs]
    return sum(vals) / len(vals) if vals else 0.0


# --- high_growth ---

def _scorecard_high_growth(macro: Dict, sector_data: Dict, ticker_meta: Dict) -> Dict:
    """Rate direction + VIX + Growth factor momentum + Market trend = 100."""
    breakdown = {}

    # Rate direction (0-30)
    rd, rd_detail = _get_10y_rate_direction(macro)
    if rd in ("falling", "falling_mild"):
        rd_score = 30
    elif rd == "stable":
        rd_score = 20
    else:
        rd_score = 5
    breakdown["rate_direction"] = {"score": rd_score, "max": 30, "detail": rd_detail}

    # VIX regime (0-25)
    vix_val, _, vix_trend = _get_vix_info(macro)
    if vix_val is None:
        vix_score, vix_detail = 12, "VIX unavailable"
    elif vix_val < 18:
        vix_score, vix_detail = 25, f"VIX {vix_val:.1f} low"
    elif vix_val < 25:
        vix_score = 18 if vix_trend == "falling" else 10
        vix_detail = f"VIX {vix_val:.1f} normal, {vix_trend}"
    else:
        vix_score = 12 if vix_trend == "falling" else 3
        vix_detail = f"VIX {vix_val:.1f} elevated, {vix_trend}"
    breakdown["vix_regime"] = {"score": vix_score, "max": 25, "detail": vix_detail}

    # Growth factor momentum (0-25) — XLK vs SPY
    xlk_rel = _etf_rel_return(sector_data, "XLK")
    if xlk_rel > 1:
        gf_score, gf_detail = 25, f"XLK outperforming SPY by {xlk_rel:+.1f}%"
    elif xlk_rel >= -1:
        gf_score, gf_detail = 15, f"XLK inline with SPY ({xlk_rel:+.1f}%)"
    else:
        gf_score, gf_detail = 5, f"XLK underperforming SPY by {xlk_rel:+.1f}%"
    breakdown["growth_factor"] = {"score": gf_score, "max": 25, "detail": gf_detail}

    # Market trend (0-20)
    trend, trend_detail = _get_spy_trend(macro)
    mt_score = {"strong": 20, "moderate": 12, "transition": 8, "weak": 3}.get(trend, 10)
    breakdown["market_trend"] = {"score": mt_score, "max": 20, "detail": trend_detail}

    total = rd_score + vix_score + gf_score + mt_score
    return {"score": round(total, 2), "breakdown": breakdown}


# --- momentum_speculative ---

def _scorecard_momentum_speculative(macro: Dict, sector_data: Dict, ticker_meta: Dict) -> Dict:
    """Risk appetite + Small cap momentum + Market trend + Dollar = 100."""
    breakdown = {}

    # Risk appetite (0-30)
    vix_val, _, vix_trend = _get_vix_info(macro)
    if vix_val is None:
        ra_score, ra_detail = 12, "VIX unavailable"
    elif vix_val < 18:
        ra_score = 30 if vix_trend == "falling" else 20
        ra_detail = f"VIX {vix_val:.1f} low, {vix_trend}"
    elif vix_val < 25:
        ra_score = 12 if vix_trend == "falling" else 6
        ra_detail = f"VIX {vix_val:.1f} normal, {vix_trend}"
    else:
        ra_score, ra_detail = 3, f"VIX {vix_val:.1f} — spec risk off"
    breakdown["risk_appetite"] = {"score": ra_score, "max": 30, "detail": ra_detail}

    # Small cap momentum (0-25) — IWM vs SPY
    iwm_rel = sector_data.get("iwm_vs_spy_20d", 0.0)
    if iwm_rel > 1:
        sc_score, sc_detail = 25, f"IWM outperforming SPY by {iwm_rel:+.1f}%"
    elif iwm_rel >= -1:
        sc_score, sc_detail = 15, f"IWM inline ({iwm_rel:+.1f}%)"
    else:
        sc_score, sc_detail = 5, f"IWM underperforming by {iwm_rel:+.1f}%"
    breakdown["small_cap_momentum"] = {"score": sc_score, "max": 25, "detail": sc_detail}

    # Market trend (0-25)
    trend, trend_detail = _get_spy_trend(macro)
    mt_score = {"strong": 25, "moderate": 12, "transition": 6, "weak": 2}.get(trend, 10)
    breakdown["market_trend"] = {"score": mt_score, "max": 25, "detail": trend_detail}

    # Dollar / liquidity (0-20)
    dollar_pct = macro.get("dollar_pct_from_sma50", 0.0)
    if dollar_pct < -1:
        dl_score, dl_detail = 20, f"Dollar weak ({dollar_pct:+.1f}% from SMA50)"
    elif abs(dollar_pct) <= 1:
        dl_score, dl_detail = 12, f"Dollar neutral ({dollar_pct:+.1f}%)"
    else:
        dl_score, dl_detail = 5, f"Dollar strong ({dollar_pct:+.1f}%) — tight conditions"
    breakdown["dollar_liquidity"] = {"score": dl_score, "max": 20, "detail": dl_detail}

    total = ra_score + sc_score + mt_score + dl_score
    return {"score": round(total, 2), "breakdown": breakdown}


# --- dividend_income ---

def _scorecard_dividend_income(macro: Dict, sector_data: Dict, ticker_meta: Dict) -> Dict:
    """Yield curve shape + Rate direction + Defensive demand + Sector momentum = 100."""
    breakdown = {}

    yc_shape = macro.get("yield_curve", "unknown")
    yc_trend = macro.get("yield_curve_trend", "unknown")

    # Yield curve shape (0-30)
    if yc_shape == "normal":
        ycs = {"steepening": 30, "stable": 25, "flattening": 18}.get(yc_trend, 22)
    elif yc_shape == "flat":
        ycs = 15
    elif yc_shape == "inverted":
        ycs = 12 if yc_trend == "steepening" else 5
    else:
        ycs = 12
    yc_detail = f"{yc_shape}/{yc_trend}"
    breakdown["yield_curve_shape"] = {"score": ycs, "max": 30, "detail": yc_detail}

    # Rate direction (0-25)
    rd, rd_detail = _get_10y_rate_direction(macro)
    if rd in ("falling", "falling_mild"):
        rd_score = 25
    elif rd == "stable":
        rd_score = 15
    else:
        rd_score = 5
    breakdown["rate_direction"] = {"score": rd_score, "max": 25, "detail": rd_detail}

    # Defensive demand (0-25) — higher VIX = more demand for safety
    vix_val, _, vix_trend = _get_vix_info(macro)
    if vix_val is None:
        dd_score, dd_detail = 15, "VIX unavailable"
    elif vix_val > 25:
        dd_score = 25 if vix_trend == "rising" else 20
        dd_detail = f"VIX {vix_val:.1f} — flight to quality"
    elif vix_val >= 18:
        dd_score, dd_detail = 15, f"VIX {vix_val:.1f} moderate"
    else:
        dd_score, dd_detail = 10, f"VIX {vix_val:.1f} low — risk-on"
    breakdown["defensive_demand"] = {"score": dd_score, "max": 25, "detail": dd_detail}

    # Sector momentum (0-20) — XLP + XLU average
    def_rel = _avg_etf_rel_return(sector_data, ["XLP", "XLU"])
    if def_rel > 0.5:
        sm_score, sm_detail = 20, f"Defensives outperforming ({def_rel:+.1f}%)"
    elif def_rel >= -0.5:
        sm_score, sm_detail = 12, f"Defensives inline ({def_rel:+.1f}%)"
    else:
        sm_score, sm_detail = 5, f"Defensives underperforming ({def_rel:+.1f}%)"
    breakdown["sector_momentum"] = {"score": sm_score, "max": 20, "detail": sm_detail}

    total = ycs + rd_score + dd_score + sm_score
    return {"score": round(total, 2), "breakdown": breakdown}


# --- commodity_cyclical ---

def _scorecard_commodity_cyclical(macro: Dict, sector_data: Dict, ticker_meta: Dict) -> Dict:
    """Dollar trend + Industrial momentum + Gold trend + Market trend = 100."""
    breakdown = {}

    # Dollar trend (0-35) — granular via dollar_pct_from_sma50
    dollar_pct = macro.get("dollar_pct_from_sma50", 0.0)
    if dollar_pct < -2:
        dt_score, dt_detail = 35, f"Dollar weak ({dollar_pct:+.1f}%) — strong tailwind"
    elif dollar_pct < -1:
        dt_score, dt_detail = 28, f"Dollar mildly weak ({dollar_pct:+.1f}%)"
    elif abs(dollar_pct) <= 1:
        dt_score, dt_detail = 18, f"Dollar neutral ({dollar_pct:+.1f}%)"
    elif dollar_pct <= 2:
        dt_score, dt_detail = 8, f"Dollar mildly strong ({dollar_pct:+.1f}%)"
    else:
        dt_score, dt_detail = 3, f"Dollar strong ({dollar_pct:+.1f}%) — headwind"
    breakdown["dollar_trend"] = {"score": dt_score, "max": 35, "detail": dt_detail}

    # Industrial momentum (0-25) — XLI + XLE + XLB average
    cyc_rel = _avg_etf_rel_return(sector_data, ["XLI", "XLE", "XLB"])
    if cyc_rel > 1:
        im_score, im_detail = 25, f"Cyclicals outperforming ({cyc_rel:+.1f}%)"
    elif cyc_rel >= -1:
        im_score, im_detail = 15, f"Cyclicals inline ({cyc_rel:+.1f}%)"
    else:
        im_score, im_detail = 5, f"Cyclicals underperforming ({cyc_rel:+.1f}%)"
    breakdown["industrial_momentum"] = {"score": im_score, "max": 25, "detail": im_detail}

    # Gold trend (0-20)
    gold = macro.get("indices", {}).get("gold", {})
    gold_cur = gold.get("current")
    gold_sma = gold.get("sma_50")
    if gold_cur and gold_sma and gold_sma > 0:
        gold_pct = (gold_cur / gold_sma - 1) * 100
        if gold_pct > 2:
            gt_score, gt_detail = 20, f"Gold above SMA50 ({gold_pct:+.1f}%) — inflation rising"
        elif gold_pct >= -2:
            gt_score, gt_detail = 14, f"Gold near SMA50 ({gold_pct:+.1f}%)"
        else:
            gt_score, gt_detail = 6, f"Gold below SMA50 ({gold_pct:+.1f}%) — disinflation"
    else:
        gt_score, gt_detail = 12, "Gold data unavailable"
    breakdown["gold_trend"] = {"score": gt_score, "max": 20, "detail": gt_detail}

    # Market trend (0-20) — SPY vs 200 SMA
    spy = macro.get("indices", {}).get("sp500", {})
    spy_cur = spy.get("current")
    spy_200 = spy.get("sma_200")
    if spy_cur and spy_200:
        pct_from_200 = abs(spy_cur / spy_200 - 1) * 100
        if spy_cur > spy_200:
            mt_score, mt_detail = 20, "SPY above 200 SMA — growth backdrop"
        elif pct_from_200 <= 2:
            mt_score, mt_detail = 12, "SPY near 200 SMA — transition"
        else:
            mt_score, mt_detail = 6, "SPY below 200 SMA — weak backdrop"
    else:
        mt_score, mt_detail = 10, "SPY data unavailable"
    breakdown["market_trend"] = {"score": mt_score, "max": 20, "detail": mt_detail}

    total = dt_score + im_score + gt_score + mt_score
    return {"score": round(total, 2), "breakdown": breakdown}


# --- large_cap_core ---

def _scorecard_large_cap_core(macro: Dict, sector_data: Dict, ticker_meta: Dict) -> Dict:
    """Market trend + VIX regime + Yield curve + Dollar stability = 100."""
    breakdown = {}

    # Market trend (0-30)
    trend, trend_detail = _get_spy_trend(macro)
    mt_score = {"strong": 30, "moderate": 18, "transition": 12, "weak": 5}.get(trend, 15)
    breakdown["market_trend"] = {"score": mt_score, "max": 30, "detail": trend_detail}

    # VIX regime (0-25)
    vix_val, _, vix_trend = _get_vix_info(macro)
    if vix_val is None:
        vs_score, vs_detail = 12, "VIX unavailable"
    elif vix_val < 20:
        vs_score, vs_detail = 25, f"VIX {vix_val:.1f} calm"
    elif vix_val < 30:
        vs_score = 18 if vix_trend == "falling" else 10
        vs_detail = f"VIX {vix_val:.1f}, {vix_trend}"
    else:
        vs_score, vs_detail = 5, f"VIX {vix_val:.1f} elevated"
    breakdown["vix_regime"] = {"score": vs_score, "max": 25, "detail": vs_detail}

    # Yield curve (0-25)
    yc_shape = macro.get("yield_curve", "unknown")
    yc_trend = macro.get("yield_curve_trend", "unknown")
    if yc_shape == "normal":
        yc_score = 25 if yc_trend in ("steepening", "stable") else 18
    elif yc_shape == "flat":
        yc_score = 15
    elif yc_shape == "inverted":
        yc_score = 12 if yc_trend == "steepening" else 6
    else:
        yc_score = 12
    yc_detail = f"{yc_shape}/{yc_trend}"
    breakdown["yield_curve"] = {"score": yc_score, "max": 25, "detail": yc_detail}

    # Dollar stability (0-20)
    dollar_pct = macro.get("dollar_pct_from_sma50", 0.0)
    abs_pct = abs(dollar_pct)
    if abs_pct <= 1:
        ds_score, ds_detail = 20, f"Dollar stable ({dollar_pct:+.1f}% from SMA50)"
    elif abs_pct <= 2:
        ds_score, ds_detail = 14, f"Dollar moderate move ({dollar_pct:+.1f}%)"
    else:
        ds_score, ds_detail = 8, f"Dollar volatile ({dollar_pct:+.1f}%)"
    breakdown["dollar_stability"] = {"score": ds_score, "max": 20, "detail": ds_detail}

    total = mt_score + vs_score + yc_score + ds_score
    return {"score": round(total, 2), "breakdown": breakdown}


# --- fallback (unknown profile) ---

def _scorecard_fallback(macro: Dict, sector_data: Dict, ticker_meta: Dict) -> Dict:
    """Balanced average using large_cap_core logic (most generic)."""
    return _scorecard_large_cap_core(macro, sector_data, ticker_meta)


# Dispatch table
_SCORECARD_DISPATCH = {
    "high_growth": _scorecard_high_growth,
    "momentum_speculative": _scorecard_momentum_speculative,
    "dividend_income": _scorecard_dividend_income,
    "commodity_cyclical": _scorecard_commodity_cyclical,
    "large_cap_core": _scorecard_large_cap_core,
}


# =========================================================================
# Composite — Macro Fit Score
# =========================================================================

def compute_macro_fit(
    market_score: float,
    sector_momentum_score: float,
    regime_fit_score: float,
) -> float:
    """
    Compute the final Macro Fit score (0-100) from the three layers.

    Weights: market 30%, sector 40%, regime 30%
    (Sector momentum gets highest weight per Moskowitz-Grinblatt 1999 cross-sectional momentum evidence.)
    """
    fit = (
        market_score * COMPOSITE_WEIGHTS["market"]
        + sector_momentum_score * COMPOSITE_WEIGHTS["sector"]
        + regime_fit_score * COMPOSITE_WEIGHTS["regime"]
    )
    return round(max(0, min(100, fit)), 2)


# =========================================================================
# Convenience — Full per-ticker macro overlay
# =========================================================================

def compute_ticker_macro_overlay(
    macro: Dict[str, Any],
    sector_data: Dict[str, Any],
    market_env: Dict[str, Any],
    profile: str,
    ticker_sector: str,
    ticker_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Compute the full macro overlay for a single ticker.

    Args:
        macro: Raw macro snapshot
        sector_data: Sector momentum data (from compute_sector_momentum())
        market_env: Market environment result (from compute_market_environment_score())
        profile: resolved_profile from ticker metadata
        ticker_sector: Ticker's GICS sector name
        ticker_meta: Full ticker metadata dict

    Returns:
        {
            "macro_fit": float (0-100),
            "macro_breakdown": {
                "market_environment": {"score": X, "breakdown": {...}},
                "sector_momentum": {"score": X, "sector": str, "rank": int},
                "regime_fit": {"score": X, "profile_used": str, "breakdown": {...}},
            }
        }
    """
    market_score = market_env.get("score", 50)

    # Sector momentum score for this ticker's sector
    sector_scores = sector_data.get("sector_scores", {})
    sector_ranks = sector_data.get("sector_ranks", {})
    sec_score = sector_scores.get(ticker_sector, _SECTOR_SCORE_NEUTRAL)
    sec_rank = sector_ranks.get(ticker_sector, _MISSING_SECTOR_RANK)

    # Profile-specific regime fit
    regime_result = score_profile_scorecard(profile, macro, sector_data, ticker_meta)
    regime_score = regime_result.get("score", 50)

    macro_fit = compute_macro_fit(market_score, sec_score, regime_score)

    from tradingagents.dataflows.index_regime import normalize_index_regime

    index_fields = normalize_index_regime(macro)

    macro_breakdown = {
        "market_environment": {
            "score": market_score,
            "breakdown": market_env.get("breakdown", {}),
        },
        "sector_momentum": {
            "score": sec_score,
            "sector": ticker_sector or "Unknown",
            "rank": sec_rank,
        },
        "regime_fit": {
            "score": regime_score,
            "profile_used": regime_result.get("profile_used", "fallback"),
            "breakdown": regime_result.get("breakdown", {}),
        },
    }

    return {
        "macro_fit": macro_fit,
        "macro_breakdown": macro_breakdown,
        "index_regime": index_fields.get("index_regime"),
        "index_regime_label": index_fields.get("index_regime_label"),
        "index_stress": index_fields.get("index_stress"),
        "index_trend": index_fields.get("index_trend"),
    }
