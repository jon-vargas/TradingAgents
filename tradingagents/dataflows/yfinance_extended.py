"""
Extended yfinance data fetching for Tier 2 enhancements.

Builds on top of y_finance.py — does NOT duplicate existing functionality.
All ticker.info fields accessed via .get() with sensible defaults (defensive access).

Functions:
- get_analyst_ratings(ticker) — Wall Street consensus, price targets
- get_analyst_ratings_batch(tickers) — Batch variant using yf.Tickers()
- get_earnings_profile(ticker) — Calendar, surprise history, computed metrics
- get_ownership_summary(ticker) — Institutional holders, short interest, insider activity
- get_valuation_metrics(ticker) — P/E, P/S, P/B, EV/EBITDA, margins, growth
- get_macro_snapshot() — Market indices, regime, VIX, yield curve
- get_options_summary(ticker) — IV, P/C ratios, max pain, unusual activity
"""

import os
import yfinance as yf
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Any, TypeVar

from .cache import get_cache, CacheConfig
from .yfinance_limiter import get_yfinance_limiter

import logging

logger = logging.getLogger("tradingagents.dataflows.yfinance_extended")

_FUNDAMENTALS_MISSING_TTL_SECONDS = 12 * 60 * 60  # 12h cooldown

# ---------------------------------------------------------------------------
# yfinance call watchdog
# ---------------------------------------------------------------------------
# yfinance's `Ticker(sym).info`, `.option_chain()`, `.calendar`,
# `.institutional_holders`, `.upgrades_downgrades`, `.income_stmt`, etc. do
# NOT accept a timeout argument and rely on the underlying urllib socket
# default — which in practice can stall indefinitely on a slow yahoo
# endpoint. A single stuck call inside a ThreadPoolExecutor worker can
# leak that thread forever and, repeated across post-close runs, will
# eventually saturate uvicorn's default threadpool / pin our scheduler.
#
# `_call_with_timeout` runs the callable on a dedicated single-worker
# executor and enforces a wall-clock deadline. On timeout we return the
# caller-supplied default and shut down the executor without waiting on
# the (potentially permanently blocked) worker thread, so the caller
# always makes progress.

_T = TypeVar("_T")

# Default per-call wall-clock budget for yfinance helpers that can't be
# bounded any other way. Tunable via env so we can crank it down in
# production without a code change.
_YF_DEFAULT_CALL_TIMEOUT = float(os.environ.get("YFINANCE_CALL_TIMEOUT_SECONDS", "20"))


def _call_with_timeout(
    fn: Callable[..., _T],
    *args,
    timeout: Optional[float] = None,
    default: Optional[_T] = None,
    op: str = "yfinance_call",
    **kwargs,
) -> _T:
    """Run a yfinance helper with a hard wall-clock deadline.

    The underlying call cannot actually be cancelled (yfinance/urllib doesn't
    expose a hook), but the calling code is guaranteed to return within
    ``timeout`` seconds. Leaked worker threads will eventually die when the
    socket hits its OS-level keepalive timeout.
    """
    t = float(timeout if timeout is not None else _YF_DEFAULT_CALL_TIMEOUT)
    from tradingagents.dataflows.screening_ohlcv_cache import is_screening_ohlcv_session_active

    if is_screening_ohlcv_session_active():
        logger.debug("%s skipped; screening OHLCV bulk session active", op)
        return default
    limiter = get_yfinance_limiter()
    if limiter.is_open():
        logger.debug("%s skipped; yfinance breaker open (%.1fs remaining)", op, limiter.cooldown_remaining())
        return default
    # Every Tier-2/enhanced yfinance helper funnels through here, so this is
    # the single choke point for smoothing concurrent bursts (min inter-call
    # spacing) and for letting a sustained 401/429 storm actually trip the
    # shared breaker instead of silently retrying with no backoff.
    limiter.acquire()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yf-watchdog")
    try:
        future = executor.submit(fn, *args, **kwargs)
        try:
            result = future.result(timeout=t)
            limiter.record_success()
            return result
        except FuturesTimeoutError:
            logger.warning("%s timed out after %.1fs; returning default", op, t)
            return default
        except Exception as e:
            if not limiter.record_if_rate_limited(e):
                logger.debug("%s raised %s; returning default", op, e)
            return default
    finally:
        # Don't wait on the (possibly stuck) worker — let it leak rather
        # than block the caller. `cancel_futures=True` cancels any queued
        # work; the in-flight future cannot be cancelled but the executor
        # itself is GC-eligible once the worker thread finally exits.
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)


def _safe_float(val) -> Optional[float]:
    """Safely convert a value to float, returning None for NaN/None."""
    if val is None:
        return None
    try:
        # yfinance occasionally returns a 1-row Series/DataFrame where a bare
        # float(...) raises "cannot convert the series to <class 'float'>".
        if isinstance(val, pd.Series):
            if val.empty:
                return None
            val = val.iloc[-1]
        elif isinstance(val, pd.DataFrame):
            if val.empty:
                return None
            val = val.squeeze()
            if isinstance(val, pd.Series):
                if val.empty:
                    return None
                val = val.iloc[-1]
        if hasattr(val, "item"):
            try:
                val = val.item()
            except ValueError:
                pass
        f = float(val)
        return None if (f != f) else f  # NaN check
    except (TypeError, ValueError):
        return None


def _extract_close_series(df: pd.DataFrame, symbol: Optional[str] = None) -> pd.Series:
    """Normalize yfinance OHLCV output to a 1-D close-price Series."""
    if df is None or df.empty:
        return pd.Series(dtype=float)

    sym = (symbol or "").upper()
    try:
        if isinstance(df.columns, pd.MultiIndex):
            if sym and ("Close", sym) in df.columns:
                close = df[("Close", sym)]
            else:
                close = df["Close"]
        else:
            close = df["Close"]
    except (KeyError, TypeError):
        return pd.Series(dtype=float)

    if isinstance(close, pd.DataFrame):
        if close.shape[1] == 1:
            close = close.iloc[:, 0]
        else:
            close = close.squeeze()
    if not isinstance(close, pd.Series):
        return pd.Series(dtype=float)
    return close.dropna()


def _is_fundamentals_temporarily_unavailable(ticker: str) -> bool:
    """Return True when this ticker is under a short fundamentals cooldown.

    We use this to avoid hammering yfinance quoteSummary endpoints for symbols
    that repeatedly return sparse/absent fundamentals payloads.
    """
    cache = get_cache()
    marker = cache.get("fundamentals_missing", ticker.upper().strip())
    return bool(marker)


def _mark_fundamentals_temporarily_unavailable(ticker: str, reason: str) -> None:
    """Set a short-lived marker indicating sparse fundamentals availability."""
    cache = get_cache()
    sym = ticker.upper().strip()
    cache.set(
        "fundamentals_missing",
        sym,
        data={"ticker": sym, "reason": reason, "marked_at": datetime.now().isoformat()},
        ttl=_FUNDAMENTALS_MISSING_TTL_SECONDS,
    )


# =========================================================================
# Centralized yf.Ticker.info accessor — single cache entry per ticker
# =========================================================================

def get_ticker_info(ticker: str) -> dict:
    """Fetch and cache the full yf.Ticker().info dict.

    All functions that need .info should call this instead of constructing
    their own yf.Ticker instance.  The cache key is ``ticker_info_full``
    so it does not collide with the webapp's slimmed ``ticker_info`` key.

    Returns an empty dict on failure (never raises).
    """
    sym = ticker.upper().strip()
    cache = get_cache()
    cached = cache.get("ticker_info_full", sym)
    if cached is not None:
        return cached

    def _fetch_info():
        return yf.Ticker(sym).info or {}

    info = _call_with_timeout(
        _fetch_info,
        default={},
        op=f"get_ticker_info({sym})",
    ) or {}

    cache.set("ticker_info_full", sym, data=info)
    return info


# =========================================================================
# Feature 1: Analyst Ratings & Price Targets
# =========================================================================

def get_analyst_ratings(ticker: str) -> Dict[str, Any]:
    """
    Get Wall Street analyst ratings and price targets for a ticker.
    
    All fields accessed defensively via .get() with sensible defaults.
    Returns partial results gracefully if data is missing.
    
    Args:
        ticker: Stock ticker symbol
        
    Returns:
        Dict with rating, targets, upside %, and computed fields
    """
    cache = get_cache()
    cached = cache.get("analyst_ratings", ticker)
    if cached is not None:
        return cached
    
    info = get_ticker_info(ticker)
    if not info:
        return _empty_analyst_ratings(ticker)
    
    current_price = info.get("currentPrice")
    target_mean = info.get("targetMeanPrice")
    target_high = info.get("targetHighPrice")
    target_low = info.get("targetLowPrice")
    target_median = info.get("targetMedianPrice")
    
    # Fallback: use regularMarketPrice or previousClose if currentPrice missing
    if current_price is None:
        current_price = info.get("regularMarketPrice") or info.get("previousClose")
    
    # Computed fields (only if we have the required data)
    upside_pct = None
    target_spread_pct = None
    consensus_score = None
    
    rec_mean = info.get("recommendationMean")
    
    if current_price and target_mean:
        try:
            upside_pct = round((target_mean - current_price) / current_price * 100, 2)
        except (ZeroDivisionError, TypeError):
            pass
    
    if current_price and target_high and target_low:
        try:
            target_spread_pct = round((target_high - target_low) / current_price * 100, 2)
        except (ZeroDivisionError, TypeError):
            pass
    
    if rec_mean is not None:
        try:
            # Map 1-5 scale to 0-100 (1=Strong Buy=100, 5=Strong Sell=0)
            consensus_score = round(max(0, min(100, (5 - rec_mean) / 4 * 100)), 1)
        except (TypeError, ValueError):
            pass
    
    result = {
        "ticker": ticker.upper(),
        "recommendation_mean": rec_mean,
        "recommendation_key": info.get("recommendationKey"),
        "number_of_analysts": info.get("numberOfAnalystOpinions", 0),
        "target_mean_price": target_mean,
        "target_high_price": target_high,
        "target_low_price": target_low,
        "target_median_price": target_median,
        "current_price": current_price,
        "upside_pct": upside_pct,
        "target_spread_pct": target_spread_pct,
        "consensus_score": consensus_score,
        "fetched_at": datetime.now().isoformat(),
    }
    
    cache.set("analyst_ratings", ticker, data=result, ttl=CacheConfig.ANALYST_RATINGS)
    return result


def get_analyst_ratings_batch(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Batch fetch analyst ratings for multiple tickers.
    
    Uses yf.Tickers() for efficiency over individual calls.
    
    Args:
        tickers: List of ticker symbols
        
    Returns:
        Dict mapping ticker -> analyst ratings dict
    """
    results = {}
    
    # Check cache first for each ticker
    cache = get_cache()
    uncached = []
    for t in tickers:
        cached = cache.get("analyst_ratings", t.upper())
        if cached is not None:
            results[t.upper()] = cached
        else:
            uncached.append(t.upper())
    
    if not uncached:
        return results
    
    # Batch fetch uncached tickers
    try:
        batch = yf.Tickers(" ".join(uncached))
        for t in uncached:
            try:
                info = batch.tickers[t].info or {}
                # Seed the centralized ticker_info_full cache with batch data
                cache.set("ticker_info_full", t, data=info)
                current_price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
                target_mean = info.get("targetMeanPrice")
                rec_mean = info.get("recommendationMean")
                
                upside_pct = None
                if current_price and target_mean:
                    try:
                        upside_pct = round((target_mean - current_price) / current_price * 100, 2)
                    except (ZeroDivisionError, TypeError):
                        pass
                
                consensus_score = None
                if rec_mean is not None:
                    try:
                        consensus_score = round(max(0, min(100, (5 - rec_mean) / 4 * 100)), 1)
                    except (TypeError, ValueError):
                        pass
                
                target_high = info.get("targetHighPrice")
                target_low = info.get("targetLowPrice")
                target_spread_pct = None
                if current_price and target_high and target_low:
                    try:
                        target_spread_pct = round((target_high - target_low) / current_price * 100, 2)
                    except (ZeroDivisionError, TypeError):
                        pass
                
                result = {
                    "ticker": t,
                    "recommendation_mean": rec_mean,
                    "recommendation_key": info.get("recommendationKey"),
                    "number_of_analysts": info.get("numberOfAnalystOpinions", 0),
                    "target_mean_price": target_mean,
                    "target_high_price": target_high,
                    "target_low_price": target_low,
                    "target_median_price": info.get("targetMedianPrice"),
                    "current_price": current_price,
                    "upside_pct": upside_pct,
                    "target_spread_pct": target_spread_pct,
                    "consensus_score": consensus_score,
                    "fetched_at": datetime.now().isoformat(),
                }
                
                cache.set("analyst_ratings", t, data=result, ttl=CacheConfig.ANALYST_RATINGS)
                results[t] = result
            except Exception as e:
                logger.warning("Failed to get analyst ratings for %s in batch: %s", t, e)
                results[t] = _empty_analyst_ratings(t)
    except Exception as e:
        logger.warning("Batch fetch failed, falling back to individual: %s", e)
        for t in uncached:
            results[t] = get_analyst_ratings(t)
    
    return results


def _empty_analyst_ratings(ticker: str) -> Dict[str, Any]:
    """Return an empty analyst ratings dict for graceful degradation."""
    return {
        "ticker": ticker.upper(),
        "recommendation_mean": None,
        "recommendation_key": None,
        "number_of_analysts": 0,
        "target_mean_price": None,
        "target_high_price": None,
        "target_low_price": None,
        "target_median_price": None,
        "current_price": None,
        "upside_pct": None,
        "target_spread_pct": None,
        "consensus_score": None,
        "fetched_at": datetime.now().isoformat(),
    }


# =========================================================================
# Feature 8: Earnings Calendar & Event Risk
# =========================================================================

def get_earnings_profile(ticker: str) -> Dict[str, Any]:
    """
    Get earnings calendar, surprise history, and computed metrics.
    
    Args:
        ticker: Stock ticker symbol
        
    Returns:
        Dict with next earnings date, surprise history, beat rate, etc.
    """
    cache = get_cache()
    cached = cache.get("earnings_calendar_v2", ticker)
    if cached is not None and cached.get("surprise_history"):
        return cached
    
    info = get_ticker_info(ticker)
    t = yf.Ticker(ticker.upper())  # needed for .calendar / .earnings_history
    sym = ticker.upper()

    # Calendar data
    next_earnings_date = None
    days_until_earnings = None
    ex_dividend_date = None

    try:
        calendar = _call_with_timeout(
            lambda: t.calendar,
            default=None,
            op=f"earnings_profile.calendar({sym})",
        )
        if calendar is not None:
            if isinstance(calendar, dict):
                earnings_date_raw = calendar.get("Earnings Date")
                if earnings_date_raw:
                    if isinstance(earnings_date_raw, list) and len(earnings_date_raw) > 0:
                        next_earnings_date = str(earnings_date_raw[0])
                    else:
                        next_earnings_date = str(earnings_date_raw)
                ex_div = calendar.get("Ex-Dividend Date")
                if ex_div:
                    ex_dividend_date = str(ex_div)
    except Exception as e:
        logger.debug("Failed to parse earnings calendar for %s: %s", ticker, e)
    
    # Compute days until earnings
    if next_earnings_date:
        try:
            earnings_dt = pd.to_datetime(next_earnings_date)
            days_until_earnings = (earnings_dt - pd.Timestamp.now()).days
        except Exception as e:
            logger.debug("Failed to compute days until earnings for %s: %s", ticker, e)
    
    # Earnings history (surprise data)
    avg_surprise = None
    beat_rate = None
    surprise_history = []
    
    try:
        earnings_hist = _call_with_timeout(
            lambda: t.earnings_history,
            default=None,
            op=f"earnings_profile.history({sym})",
        )
        if earnings_hist is not None and not getattr(earnings_hist, "empty", True):
            surprise_history = _parse_surprise_history(earnings_hist)
            surprises = [row["surprise_pct"] for row in surprise_history if row.get("surprise_pct") is not None]
            if surprises:
                avg_surprise = round(float(np.mean(surprises)), 2)
                beat_rate = round(sum(1 for s in surprises if s > 0) / len(surprises) * 100, 1)
        if not surprise_history:
            dates_df = _call_with_timeout(
                lambda: t.get_earnings_dates(limit=8),
                default=None,
                op=f"earnings_profile.dates({sym})",
            )
            if dates_df is not None and not getattr(dates_df, "empty", True):
                surprise_history = _parse_surprise_history(dates_df)
                surprises = [row["surprise_pct"] for row in surprise_history if row.get("surprise_pct") is not None]
                if surprises:
                    avg_surprise = round(float(np.mean(surprises)), 2)
                    beat_rate = round(sum(1 for s in surprises if s > 0) / len(surprises) * 100, 1)
    except Exception as e:
        logger.debug("Earnings history not available for %s: %s", ticker, e)
    
    # Dividend info — Yahoo may send a fraction (0.018) or a percent (1.8).
    dividend_rate = info.get("dividendRate")
    from tradingagents.screening.discovery import normalize_dividend_yield

    dividend_yield_frac = normalize_dividend_yield(info.get("dividendYield"))
    payout_ratio = info.get("payoutRatio")
    payout_frac = None
    if payout_ratio is not None and not (isinstance(payout_ratio, float) and np.isnan(payout_ratio)):
        try:
            payout_raw = float(payout_ratio)
            payout_frac = payout_raw / 100.0 if payout_raw > 1.0 else payout_raw
            if payout_frac < 0 or payout_frac > 2.0:
                payout_frac = None
        except (TypeError, ValueError):
            payout_frac = None
    
    result = {
        "ticker": ticker.upper(),
        "next_earnings_date": next_earnings_date,
        "days_until_earnings": days_until_earnings,
        "ex_dividend_date": ex_dividend_date,
        "avg_earnings_surprise_pct": avg_surprise,
        "earnings_beat_rate_pct": beat_rate,
        "surprise_history": surprise_history,
        "dividend_rate": dividend_rate,
        "dividend_yield": round(dividend_yield_frac * 100, 2) if dividend_yield_frac is not None else None,
        "payout_ratio": round(payout_frac * 100, 1) if payout_frac is not None else None,
        "fetched_at": datetime.now().isoformat(),
    }
    
    cache.set("earnings_calendar_v2", ticker, data=result, ttl=CacheConfig.EARNINGS_CALENDAR)
    return result


def _safe_frame_num(row: Any, *keys: str) -> Optional[float]:
    """Read the first finite numeric column from a yfinance earnings row."""
    for key in keys:
        if hasattr(row, "get"):
            raw = row.get(key)
        else:
            raw = None
        if raw is None and hasattr(row, "__getitem__"):
            try:
                raw = row[key]
            except (KeyError, IndexError, TypeError):
                raw = None
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if pd.isna(val):
            continue
        return val
    return None


def _parse_surprise_history(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    """Normalize Yahoo earnings-history / earnings-dates frames to PEAD rows.

    Column names drifted across yfinance versions (``surprisePercent``,
    ``Surprise(%)``, ``epsEstimate`` vs ``EPS Estimate``).
    """
    if frame is None or getattr(frame, "empty", True):
        return []
    recent = frame.head(8)
    rows: List[Dict[str, Any]] = []
    for idx, row in recent.iterrows():
        surprise_pct = _safe_frame_num(
            row,
            "surprisePercent",
            "surprise_pct",
            "Surprise(%)",
            "Surprise (%)",
            "surprise",
        )
        estimate = _safe_frame_num(row, "epsEstimate", "EPS Estimate", "eps_estimate")
        actual = _safe_frame_num(row, "epsActual", "Reported EPS", "eps_actual")
        if surprise_pct is None and estimate is not None and actual is not None and estimate != 0:
            surprise_pct = (actual - estimate) / abs(estimate) * 100.0
        if surprise_pct is None:
            continue
        date_raw = None
        for key in ("reportDate", "Earnings Date", "startdatetime"):
            if hasattr(row, "get"):
                date_raw = row.get(key)
            if date_raw is not None:
                break
        if date_raw is None:
            date_raw = idx
        rows.append({
            "date": str(date_raw).split(" ")[0].split("T")[0],
            "eps_estimate": estimate,
            "eps_actual": actual,
            "surprise_pct": float(surprise_pct),
        })
    return rows


def _empty_earnings_profile(ticker: str) -> Dict[str, Any]:
    """Return an empty earnings profile for graceful degradation."""
    return {
        "ticker": ticker.upper(),
        "next_earnings_date": None,
        "days_until_earnings": None,
        "ex_dividend_date": None,
        "avg_earnings_surprise_pct": None,
        "earnings_beat_rate_pct": None,
        "surprise_history": [],
        "dividend_rate": None,
        "dividend_yield": None,
        "payout_ratio": None,
        "fetched_at": datetime.now().isoformat(),
    }


# =========================================================================
# Feature 6: Institutional Ownership & Fund Flow
# =========================================================================

def get_ownership_summary(ticker: str) -> Dict[str, Any]:
    """
    Get institutional ownership, short interest, and insider activity.
    
    Reuses existing insider data from y_finance.py where possible.
    
    Args:
        ticker: Stock ticker symbol
        
    Returns:
        Dict with institutional %, short interest, insider activity
    """
    cache = get_cache()
    cached = cache.get("ownership", ticker)
    if cached is not None:
        return cached
    
    info = get_ticker_info(ticker)
    sym = ticker.upper()
    t = yf.Ticker(sym)  # needed for .institutional_holders
    
    # Institutional ownership
    held_by_institutions = info.get("heldPercentInstitutions")
    held_by_insiders = info.get("heldPercentInsiders")
    
    # Short interest
    short_ratio = info.get("shortRatio")
    short_pct_float = info.get("shortPercentOfFloat")
    shares_short = info.get("sharesShort")
    shares_short_prior = info.get("sharesShortPriorMonth")
    
    # Computed: short interest change
    short_interest_change = None
    if shares_short is not None and shares_short_prior is not None and shares_short_prior > 0:
        try:
            short_interest_change = round(
                (shares_short - shares_short_prior) / shares_short_prior * 100, 2
            )
        except (ZeroDivisionError, TypeError):
            pass
    
    # Days to cover
    days_to_cover = None
    avg_vol = info.get("averageDailyVolume10Day") or info.get("averageVolume10days")
    if shares_short and avg_vol and avg_vol > 0:
        try:
            days_to_cover = round(shares_short / avg_vol, 1)
        except (ZeroDivisionError, TypeError):
            pass
    
    # Top institutional holders
    top_holders = []
    try:
        holders_df = _call_with_timeout(
            lambda: t.institutional_holders,
            default=None,
            op=f"ownership.institutional_holders({sym})",
        )
        if holders_df is not None and not holders_df.empty:
            for _, row in holders_df.head(5).iterrows():
                top_holders.append({
                    "holder": str(row.get("Holder", "")),
                    "shares": int(row["Shares"]) if not pd.isna(row.get("Shares")) else 0,
                    "pct_out": float(row["% Out"]) if not pd.isna(row.get("% Out")) else 0,
                })
    except Exception as e:
        logger.debug("Failed to fetch institutional holders for %s: %s", ticker, e)
    
    result = {
        "ticker": ticker.upper(),
        "held_by_institutions_pct": round(held_by_institutions * 100, 1) if (held_by_institutions is not None and not (isinstance(held_by_institutions, float) and np.isnan(held_by_institutions))) else None,
        "held_by_insiders_pct": round(held_by_insiders * 100, 1) if (held_by_insiders is not None and not (isinstance(held_by_insiders, float) and np.isnan(held_by_insiders))) else None,
        "short_ratio": short_ratio,
        "short_pct_of_float": round(short_pct_float * 100, 2) if (short_pct_float is not None and not (isinstance(short_pct_float, float) and np.isnan(short_pct_float))) else None,
        "shares_short": shares_short,
        "shares_short_prior_month": shares_short_prior,
        "short_interest_change_pct": short_interest_change,
        "days_to_cover": days_to_cover,
        "top_institutional_holders": top_holders,
        "fetched_at": datetime.now().isoformat(),
    }
    
    cache.set("ownership", ticker, data=result, ttl=CacheConfig.OWNERSHIP)
    return result


def get_insider_net_buy(ticker: str, lookback_days: int = 90) -> Dict[str, Any]:
    """Return a structured insider buy/sell summary for use in smart-money signal enrichment.

    Fetches yfinance insider_transactions, parses recent buys vs sells, and returns:
        buy_count, sell_count, net_buy_count, net_buy_value, net_value_cluster_score (0-1)

    cluster_score is 1.0 when ≥3 net-buy transactions with total net-buy value
    significantly positive; scales linearly below that.
    """
    cache = get_cache()
    sym = ticker.upper()
    cached = cache.get("insider_net_buy", sym)
    if cached is not None:
        return cached

    result: Dict[str, Any] = {
        "ticker": sym, "buy_count": 0, "sell_count": 0,
        "net_buy_count": 0, "net_buy_value": 0.0, "cluster_score": 0.0,
        "fetched_at": datetime.now().isoformat(),
    }
    try:
        t = yf.Ticker(sym)
        txns = _call_with_timeout(
            lambda: t.insider_transactions,
            default=None,
            op=f"insider_net_buy.txns({sym})",
        )
        if txns is None or (hasattr(txns, "empty") and txns.empty):
            cache.set("insider_net_buy", sym, data=result)
            return result

        # Parse date column
        date_col = next((c for c in txns.columns if c.lower() in ("start date", "date", "transaction date")), None)
        if date_col:
            txns = txns.copy()
            txns["_date"] = pd.to_datetime(txns[date_col], errors="coerce")
            cutoff = datetime.now() - timedelta(days=lookback_days)
            txns = txns[txns["_date"] >= pd.Timestamp(cutoff)]

        text_col = next((c for c in txns.columns if c.lower() in ("text", "transaction")), None)
        shares_col = next((c for c in txns.columns if "shares" in c.lower()), None)
        value_col = next((c for c in txns.columns if "value" in c.lower()), None)

        buy_count = sell_count = 0
        net_value = 0.0
        for _, row in txns.iterrows():
            label = str(row.get(text_col, "")).lower() if text_col else ""
            is_buy = any(kw in label for kw in ("purchase", "buy", "acquisition"))
            is_sell = any(kw in label for kw in ("sale", "sell", "disposition"))
            val = abs(float(row[value_col])) if value_col and pd.notna(row.get(value_col)) else 0.0
            if is_buy:
                buy_count += 1
                net_value += val
            elif is_sell:
                sell_count += 1
                net_value -= val

        net_buy_count = buy_count - sell_count
        # cluster_score: ramp 0→1 as net_buy_count goes from 0→3 when net value > 0
        if net_buy_count >= 3 and net_value > 0:
            cluster_score = 1.0
        elif net_buy_count > 0 and net_value > 0:
            cluster_score = min(net_buy_count / 3, 1.0)
        else:
            cluster_score = 0.0

        result.update({
            "buy_count": buy_count,
            "sell_count": sell_count,
            "net_buy_count": net_buy_count,
            "net_buy_value": round(net_value, 0),
            "cluster_score": round(cluster_score, 2),
        })

    except Exception as e:
        logger.debug("insider_net_buy failed for %s: %s", sym, e)

    cache.set("insider_net_buy", sym, data=result)
    return result


def get_corporate_actions(ticker: str, lookback_days: int = 365, forward_days: int = 90) -> Dict[str, Any]:
    """Return structured dividends and splits from yfinance Ticker.actions / Ticker.splits.

    Does NOT depend on yfin_utils.py (which was removed in Plan A).

    Returns:
        Dict with keys:
            ticker, recent_dividends (list), recent_splits (list),
            next_ex_div_date (str or None), ex_div_days_to_event (int or None),
            fetched_at
    """
    cache = get_cache()
    sym = ticker.upper()
    cached = cache.get("corporate_actions", sym)
    if cached is not None:
        return cached

    result: Dict[str, Any] = {
        "ticker": sym, "recent_dividends": [], "recent_splits": [],
        "next_ex_div_date": None, "ex_div_days_to_event": None,
        "fetched_at": datetime.now().isoformat(),
    }
    try:
        t = yf.Ticker(sym)
        info = get_ticker_info(sym)
        today = datetime.now()
        lookback_cutoff = today - timedelta(days=lookback_days)

        # --- Dividends ---
        div_df = _call_with_timeout(
            lambda: t.dividends,
            default=None,
            op=f"corporate_actions.dividends({sym})",
        )
        if div_df is not None and not (hasattr(div_df, "empty") and div_df.empty):
            for idx_date, amount in div_df.items():
                try:
                    dt = pd.Timestamp(idx_date).tz_localize(None)
                    if dt >= pd.Timestamp(lookback_cutoff):
                        result["recent_dividends"].append({
                            "date": dt.strftime("%Y-%m-%d"),
                            "amount": round(float(amount), 4),
                        })
                except Exception:
                    pass

        # Next ex-dividend from .info
        ex_div_raw = info.get("exDividendDate")
        if ex_div_raw:
            try:
                ex_div_dt = pd.Timestamp(ex_div_raw, unit="s") if isinstance(ex_div_raw, (int, float)) else pd.Timestamp(ex_div_raw)
                ex_div_dt = ex_div_dt.tz_localize(None) if ex_div_dt.tzinfo is not None else ex_div_dt
                days_away = (ex_div_dt - pd.Timestamp(today)).days
                result["next_ex_div_date"] = ex_div_dt.strftime("%Y-%m-%d")
                result["ex_div_days_to_event"] = int(days_away)
            except Exception:
                pass

        # --- Splits ---
        split_df = _call_with_timeout(
            lambda: t.splits,
            default=None,
            op=f"corporate_actions.splits({sym})",
        )
        if split_df is not None and not (hasattr(split_df, "empty") and split_df.empty):
            for idx_date, ratio in split_df.items():
                try:
                    dt = pd.Timestamp(idx_date).tz_localize(None)
                    if dt >= pd.Timestamp(lookback_cutoff):
                        result["recent_splits"].append({
                            "date": dt.strftime("%Y-%m-%d"),
                            "ratio": float(ratio),
                        })
                except Exception:
                    pass

    except Exception as e:
        logger.warning("Failed to get corporate actions for %s: %s", sym, e)

    cache.set("corporate_actions", sym, data=result)
    return result


def _empty_ownership_summary(ticker: str) -> Dict[str, Any]:
    """Return an empty ownership summary for graceful degradation."""
    return {
        "ticker": ticker.upper(),
        "held_by_institutions_pct": None,
        "held_by_insiders_pct": None,
        "short_ratio": None,
        "short_pct_of_float": None,
        "shares_short": None,
        "shares_short_prior_month": None,
        "short_interest_change_pct": None,
        "days_to_cover": None,
        "top_institutional_holders": [],
        "fetched_at": datetime.now().isoformat(),
    }


# =========================================================================
# Feature 3: Relative Valuation & Peer Comparison
# =========================================================================

def get_valuation_metrics(ticker: str) -> Dict[str, Any]:
    """
    Get valuation multiples, margins, and growth metrics.
    
    Args:
        ticker: Stock ticker symbol
        
    Returns:
        Dict with P/E, P/S, P/B, EV/EBITDA, PEG, margins, growth, beta
    """
    cache = get_cache()
    cached = cache.get("fundamentals", "valuation", ticker)
    if cached is not None:
        return cached
    
    info = get_ticker_info(ticker)
    
    result = {
        "ticker": ticker.upper(),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        # Valuation multiples
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "price_to_sales": info.get("priceToSalesTrailing12Months"),
        "price_to_book": info.get("priceToBook"),
        "ev_to_ebitda": info.get("enterpriseToEbitda"),
        "peg_ratio": info.get("pegRatio"),
        # Growth
        "revenue_growth": _pct(info.get("revenueGrowth")),
        "earnings_growth": _pct(info.get("earningsGrowth")),
        # Margins
        "gross_margins": _pct(info.get("grossMargins")),
        "operating_margins": _pct(info.get("operatingMargins")),
        "profit_margins": _pct(info.get("profitMargins")),
        # Risk
        "beta": info.get("beta"),
        "fetched_at": datetime.now().isoformat(),
    }
    
    cache.set("fundamentals", "valuation", ticker, data=result, ttl=CacheConfig.FUNDAMENTALS)
    return result


def _pct(value) -> Optional[float]:
    """Convert decimal to percentage, handling None/NaN."""
    if value is None:
        return None
    try:
        v = float(value)
        return round(v * 100, 2) if not np.isnan(v) else None
    except (TypeError, ValueError):
        return None


# =========================================================================
# Feature 7: Macro Snapshot & Regime Detection
# =========================================================================

def _normalize_macro_as_of_date(as_of_date: Optional[str]) -> tuple:
    """Return (normalized YYYY-MM-DD or None, error reason or None)."""
    if not as_of_date:
        return None, None
    as_of_dt = pd.to_datetime(as_of_date, errors="coerce")
    if pd.isna(as_of_dt):
        return None, "invalid_as_of_date"
    as_of_dt = as_of_dt.normalize()
    if as_of_dt > pd.Timestamp.now().normalize():
        return None, "future_as_of_date"
    return as_of_dt.strftime("%Y-%m-%d"), None


def get_macro_snapshot(as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch market indices and compute regime, VIX level, yield curve.

    Indices: S&P 500, VIX, 10Y Treasury, 13-Week T-Bill, Dollar Index, Gold

    Args:
        as_of_date: Optional YYYY-MM-DD for historical/as-of research context.
            Invalid or future dates return an explicit unknown snapshot (no live fallback).

    Note:
        Yahoo historical bars can be revised after the fact. As-of snapshots are
        intended as a desk overlay for research context, not institutional
        backtest attribution.

    Returns:
        Dict with index values, trend/stress regime fields, VIX level/trend,
        yield curve shape/trend, dollar trend/magnitude
    """
    from .index_regime import (
        build_index_regime_fields,
        classify_index_stress,
        classify_index_trend,
        unknown_macro_snapshot,
    )

    normalized_as_of, as_of_error = _normalize_macro_as_of_date(as_of_date)
    if as_of_error:
        return unknown_macro_snapshot(reason=as_of_error, as_of_date=as_of_date)

    cache_key = normalized_as_of or "global"
    cache = get_cache()
    cached = cache.get("macro_snapshot", cache_key)
    if cached is not None:
        return cached
    
    indices = {
        "sp500": "^GSPC",
        "vix": "^VIX",
        "treasury_10y": "^TNX",
        "treasury_3m": "^IRX",  # 13-week T-bill (Fed's preferred yield curve proxy)
        "dollar_index": "DX-Y.NYB",
        "gold": "GC=F",
        "hyg": "HYG",   # iShares High Yield Corp Bond ETF
        "lqd": "LQD",   # iShares Investment Grade Corp Bond ETF
        "tlt": "TLT",   # iShares 20+ Year Treasury Bond ETF
        # Sector ETFs for breadth analysis
        "xlk": "XLK",   # Technology
        "xlf": "XLF",   # Financials
        "xlv": "XLV",   # Healthcare
        "xle": "XLE",   # Energy
        "xli": "XLI",   # Industrials
        "xlc": "XLC",   # Communications
        "xlp": "XLP",   # Consumer Staples
        "xly": "XLY",   # Consumer Discretionary
        "xlu": "XLU",   # Utilities
        "xlb": "XLB",   # Materials
        "xlre": "XLRE", # Real Estate
    }
    
    index_data = {}
    
    as_of_dt = None
    try:
        # Fetch last ~250 trading days for regime computation (200 SMA)
        if normalized_as_of:
            as_of_dt = pd.to_datetime(normalized_as_of).normalize()
            end_date = (as_of_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            start_date = (as_of_dt - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
        else:
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        tickers_str = " ".join(indices.values())
        data = yf.download(
            tickers_str,
            start=start_date,
            end=end_date,
            group_by="ticker",
            progress=False,
            auto_adjust=True,
            timeout=30,
        )

        for name, symbol in indices.items():
            try:
                if len(indices) > 1:
                    close = data[symbol]["Close"].dropna()
                else:
                    close = data["Close"].dropna()

                if as_of_dt is not None and len(close) > 0:
                    close = close[close.index.normalize() <= as_of_dt]

                if len(close) > 0:
                    current = float(close.iloc[-1])
                    sma_200 = float(close.tail(200).mean()) if len(close) >= 200 else None
                    sma_50 = float(close.tail(50).mean()) if len(close) >= 50 else None
                    
                    index_data[name] = {
                        "current": round(current, 2),
                        "sma_200": round(sma_200, 2) if sma_200 else None,
                        "sma_50": round(sma_50, 2) if sma_50 else None,
                    }
            except Exception as e:
                logger.debug("Failed to process %s (%s): %s", name, symbol, e)
    except Exception as e:
        logger.warning("Failed to fetch macro indices: %s", e)
    
    sp500 = index_data.get("sp500", {})
    index_trend = classify_index_trend(sp500)

    # VIX level
    vix_level = "unknown"
    vix = index_data.get("vix", {})
    if vix.get("current"):
        v = vix["current"]
        if v < 15:
            vix_level = "low_fear"
        elif v < 25:
            vix_level = "normal"
        elif v < 35:
            vix_level = "elevated"
        else:
            vix_level = "panic"
    
    # VIX trend (direction relative to 50-day SMA)
    vix_trend = "unknown"
    if vix.get("current") and vix.get("sma_50"):
        vix_trend = "rising" if vix["current"] > vix["sma_50"] else "falling"
    
    # Yield curve
    yield_curve = "unknown"
    t10 = index_data.get("treasury_10y", {})
    t2 = index_data.get("treasury_3m", {})
    if t10.get("current") is not None and t2.get("current") is not None:
        spread = t10["current"] - t2["current"]
        if spread > 0:
            yield_curve = "normal"
        elif spread > -0.5:
            yield_curve = "flat"
        else:
            yield_curve = "inverted"
    
    # Yield curve trend (steepening/flattening based on spread change vs 50d avg)
    yield_curve_trend = "unknown"
    if (t10.get("sma_50") is not None and t2.get("sma_50") is not None
            and t10.get("current") is not None and t2.get("current") is not None):
        spread_now = t10["current"] - t2["current"]
        spread_50d = t10["sma_50"] - t2["sma_50"]
        diff = spread_now - spread_50d
        if diff > 0.1:
            yield_curve_trend = "steepening"
        elif diff < -0.1:
            yield_curve_trend = "flattening"
        else:
            yield_curve_trend = "stable"
    
    # Dollar trend
    dollar_trend = "unknown"
    dxy = index_data.get("dollar_index", {})
    if dxy.get("current") and dxy.get("sma_50"):
        if dxy["current"] > dxy["sma_50"]:
            dollar_trend = "strengthening"
        else:
            dollar_trend = "weakening"
    
    # Dollar magnitude (% distance from 50-day SMA)
    dollar_pct_from_sma50 = 0.0
    if dxy.get("current") and dxy.get("sma_50") and dxy["sma_50"] > 0:
        dollar_pct_from_sma50 = round((dxy["current"] / dxy["sma_50"] - 1) * 100, 2)
    
    # Credit stress: HYG/LQD spread indicates market stress
    credit_stress = "unknown"
    hyg_data = index_data.get("hyg", {})
    lqd_data = index_data.get("lqd", {})
    if hyg_data.get("current") and lqd_data.get("current"):
        hyg_current = hyg_data["current"]
        lqd_current = lqd_data["current"]
        hyg_sma50 = hyg_data.get("sma_50")
        lqd_sma50 = lqd_data.get("sma_50")

        if hyg_sma50 and lqd_sma50:
            hyg_vs_sma = (hyg_current / hyg_sma50 - 1) * 100
            lqd_vs_sma = (lqd_current / lqd_sma50 - 1) * 100
            spread_delta = hyg_vs_sma - lqd_vs_sma

            if spread_delta < -3:
                credit_stress = "high"
            elif spread_delta < -1:
                credit_stress = "elevated"
            elif spread_delta < 1:
                credit_stress = "normal"
            else:
                credit_stress = "benign"

    # Sector breadth: count sectors above their 50-day SMA
    sector_etfs = ["xlk", "xlf", "xlv", "xle", "xli", "xlc", "xlp", "xly", "xlu", "xlb", "xlre"]
    sectors_above_sma50 = 0
    sectors_total = 0
    for etf_key in sector_etfs:
        etf_data = index_data.get(etf_key, {})
        if etf_data.get("current") and etf_data.get("sma_50"):
            sectors_total += 1
            if etf_data["current"] > etf_data["sma_50"]:
                sectors_above_sma50 += 1

    sector_breadth = "unknown"
    if sectors_total >= 8:
        breadth_pct = sectors_above_sma50 / sectors_total
        if breadth_pct >= 0.8:
            sector_breadth = "strong"
        elif breadth_pct >= 0.6:
            sector_breadth = "healthy"
        elif breadth_pct >= 0.4:
            sector_breadth = "mixed"
        elif breadth_pct >= 0.2:
            sector_breadth = "weak"
        else:
            sector_breadth = "very_weak"

    vix_value = vix.get("current") if vix else None
    index_stress, stress_evidence = classify_index_stress(
        vix_level=vix_level,
        vix_value=vix_value,
        credit_stress=credit_stress,
        sector_breadth=sector_breadth,
    )
    regime_fields = build_index_regime_fields(index_trend, index_stress, stress_evidence)

    result = {
        "indices": index_data,
        "vix_level": vix_level,
        "vix_trend": vix_trend,
        "yield_curve": yield_curve,
        "yield_curve_trend": yield_curve_trend,
        "dollar_trend": dollar_trend,
        "dollar_pct_from_sma50": dollar_pct_from_sma50,
        "credit_stress": credit_stress,
        "sector_breadth": sector_breadth,
        "sectors_above_sma50": sectors_above_sma50,
        "sectors_total": sectors_total,
        "fetched_at": datetime.now().isoformat(),
        "as_of_date": normalized_as_of,
        **regime_fields,
    }

    # Only cache when trend could be resolved. An "unknown" trend means the
    # S&P 500 download failed (intermittent yfinance outage). Caching that
    # failure for a full hour would poison every analysis in the window.
    if result.get("index_trend", "unknown") != "unknown":
        cache.set("macro_snapshot", cache_key, data=result, ttl=CacheConfig.MACRO_SNAPSHOT)
    return result


# =========================================================================
# Estimate Revisions (EPS / Revenue estimates + trend)
# =========================================================================

def get_estimate_revisions(ticker: str) -> Dict[str, Any]:
    """Get earnings and revenue estimate revision data for a ticker."""
    cache = get_cache()
    sym = ticker.upper()

    cached = cache.get("estimate_revisions", sym)
    if cached is not None:
        return cached

    result = {"ticker": sym, "fetched_at": datetime.now().isoformat()}

    if _is_fundamentals_temporarily_unavailable(sym):
        result["note"] = "sparse_fundamentals_cooldown"
        cache.set("estimate_revisions", sym, data=result)
        return result

    try:
        t = yf.Ticker(sym)

        # Earnings estimates (columns: avg, low, high, yearAgoEps, numberOfAnalysts, growth)
        try:
            ee = _call_with_timeout(
                lambda: t.earnings_estimate,
                default=None,
                op=f"estimate_revisions.earnings_estimate({sym})",
            )
            if ee is not None and not ee.empty:
                for period_label in ee.index:
                    key = str(period_label).lower().replace(" ", "_").replace("+", "plus")
                    row = ee.loc[period_label]
                    result[f"eps_{key}"] = {
                        "avg": _safe_float(row.get("avg")),
                        "low": _safe_float(row.get("low")),
                        "high": _safe_float(row.get("high")),
                        "year_ago": _safe_float(row.get("yearAgoEps")),
                        "num_analysts": int(row.get("numberOfAnalysts") or 0),
                        "growth": _safe_float(row.get("growth")),
                    }
        except Exception:
            pass

        # Revenue estimates
        try:
            re_df = _call_with_timeout(
                lambda: t.revenue_estimate,
                default=None,
                op=f"estimate_revisions.revenue_estimate({sym})",
            )
            if re_df is not None and not re_df.empty:
                for period_label in re_df.index:
                    key = str(period_label).lower().replace(" ", "_").replace("+", "plus")
                    row = re_df.loc[period_label]
                    result[f"rev_{key}"] = {
                        "avg": _safe_float(row.get("avg")),
                        "low": _safe_float(row.get("low")),
                        "high": _safe_float(row.get("high")),
                        "year_ago": _safe_float(row.get("yearAgoRevenue")),
                        "num_analysts": int(row.get("numberOfAnalysts") or 0),
                        "growth": _safe_float(row.get("growth")),
                    }
        except Exception:
            pass

        # EPS trend (shows revisions over time: 7d, 30d, 60d, 90d ago)
        try:
            eps_trend = _call_with_timeout(
                lambda: t.eps_trend,
                default=None,
                op=f"estimate_revisions.eps_trend({sym})",
            )
            if eps_trend is not None and not eps_trend.empty:
                trend_data = {}
                for period_label in eps_trend.index:
                    key = str(period_label).lower().replace(" ", "_").replace("+", "plus")
                    row = eps_trend.loc[period_label]
                    trend_data[key] = {
                        "current": _safe_float(row.get("current")),
                        "7d_ago": _safe_float(row.get("7daysAgo")),
                        "30d_ago": _safe_float(row.get("30daysAgo")),
                        "60d_ago": _safe_float(row.get("60daysAgo")),
                        "90d_ago": _safe_float(row.get("90daysAgo")),
                    }
                result["eps_trend"] = trend_data
        except Exception:
            pass

    except Exception as e:
        logger.warning("Failed to fetch estimate revisions for %s: %s", sym, e)
        _mark_fundamentals_temporarily_unavailable(sym, f"estimate_revisions_error:{type(e).__name__}")

    has_estimate_payload = any(
        k == "eps_trend" or k.startswith("eps_") or k.startswith("rev_")
        for k in result.keys()
    )
    if not has_estimate_payload:
        result["note"] = "sparse_fundamentals"
        _mark_fundamentals_temporarily_unavailable(sym, "estimate_revisions_empty")

    cache.set("estimate_revisions", sym, data=result)
    return result


# =========================================================================
# Rating Changes (analyst upgrade / downgrade history)
# =========================================================================

def get_rating_changes(ticker: str) -> Dict[str, Any]:
    """Get analyst upgrade/downgrade history for a ticker."""
    cache = get_cache()
    sym = ticker.upper()

    cached = cache.get("rating_changes", sym)
    if cached is not None:
        return cached

    result = {"ticker": sym, "actions": [], "fetched_at": datetime.now().isoformat()}

    if _is_fundamentals_temporarily_unavailable(sym):
        result["note"] = "sparse_fundamentals_cooldown"
        cache.set("rating_changes", sym, data=result)
        return result

    try:
        t = yf.Ticker(sym)

        try:
            ud = _call_with_timeout(
                lambda: t.upgrades_downgrades,
                default=None,
                op=f"rating_changes.upgrades_downgrades({sym})",
            )
            if ud is not None and not ud.empty:
                # Take last 90 days of actions (most recent first)
                recent = ud.head(50)
                actions = []
                net_upgrades = 0
                for idx, row in recent.iterrows():
                    action_type = str(row.get("Action", "")).lower()
                    entry = {
                        "date": str(idx.date()) if hasattr(idx, 'date') else str(idx),
                        "firm": str(row.get("Firm", "")),
                        "to_grade": str(row.get("ToGrade", "")),
                        "from_grade": str(row.get("FromGrade", "")),
                        "action": str(row.get("Action", "")),
                    }
                    actions.append(entry)
                    if "up" in action_type:
                        net_upgrades += 1
                    elif "down" in action_type:
                        net_upgrades -= 1

                result["actions"] = actions[:20]
                result["total_actions_90d"] = len(recent)
                result["net_upgrades_90d"] = net_upgrades
        except Exception:
            pass

    except Exception as e:
        logger.warning("Failed to fetch rating changes for %s: %s", sym, e)
        _mark_fundamentals_temporarily_unavailable(sym, f"rating_changes_error:{type(e).__name__}")

    if not result.get("actions"):
        result["note"] = "sparse_fundamentals"
        _mark_fundamentals_temporarily_unavailable(sym, "rating_changes_empty")

    cache.set("rating_changes", sym, data=result)
    return result


# =========================================================================
# Earnings Quality (accruals ratio, cash conversion, grade)
# =========================================================================

def get_earnings_quality(ticker: str) -> Dict[str, Any]:
    """Compute earnings quality metrics: accruals ratio, cash conversion, grade."""
    cache = get_cache()
    sym = ticker.upper()

    cached = cache.get("earnings_quality", sym)
    if cached is not None:
        return cached

    result = {"ticker": sym, "grade": "", "fetched_at": datetime.now().isoformat()}

    try:
        t = yf.Ticker(sym)

        # Get income statement and cashflow (most recent annual)
        try:
            inc = _call_with_timeout(
                lambda: t.income_stmt,
                default=None,
                op=f"earnings_quality.income_stmt({sym})",
            )
            cf = _call_with_timeout(
                lambda: t.cashflow,
                default=None,
                op=f"earnings_quality.cashflow({sym})",
            )
            bs = _call_with_timeout(
                lambda: t.balance_sheet,
                default=None,
                op=f"earnings_quality.balance_sheet({sym})",
            )
        except Exception:
            inc = cf = bs = None

        if inc is not None and not inc.empty and cf is not None and not cf.empty:
            # Use most recent period (first column)
            latest_inc = inc.iloc[:, 0]
            latest_cf = cf.iloc[:, 0]

            net_income = _safe_float(latest_inc.get("Net Income"))
            op_cashflow = _safe_float(
                latest_cf.get("Operating Cash Flow")
                or latest_cf.get("Total Cash From Operating Activities")
            )

            # Accruals ratio = (Net Income - Operating Cash Flow) / Total Assets
            accruals_ratio = None
            if net_income is not None and op_cashflow is not None:
                total_assets = None
                if bs is not None and not bs.empty:
                    latest_bs = bs.iloc[:, 0]
                    total_assets = _safe_float(latest_bs.get("Total Assets"))

                if total_assets and total_assets > 0:
                    accruals_ratio = (net_income - op_cashflow) / total_assets
                    result["accruals_ratio"] = round(accruals_ratio, 4)

            # Cash conversion = Operating Cash Flow / Net Income
            cash_conversion = None
            if op_cashflow is not None and net_income is not None and net_income != 0:
                cash_conversion = op_cashflow / net_income
                result["cash_conversion"] = round(cash_conversion, 4)

            # Revenue quality: check if revenue is growing while receivables aren't growing faster
            result["has_positive_net_income"] = net_income is not None and net_income > 0
            result["has_positive_ocf"] = op_cashflow is not None and op_cashflow > 0

            # Grade computation: A-F scale
            score = 0
            checks = 0

            if accruals_ratio is not None:
                checks += 1
                if accruals_ratio < 0.05:
                    score += 2  # Low accruals (good)
                elif accruals_ratio < 0.10:
                    score += 1  # Moderate

            if cash_conversion is not None:
                checks += 1
                if cash_conversion > 1.0:
                    score += 2  # OCF exceeds net income (excellent)
                elif cash_conversion > 0.7:
                    score += 1  # Decent conversion

            if op_cashflow is not None and net_income is not None:
                checks += 1
                if op_cashflow > 0 and net_income > 0:
                    score += 1
                elif op_cashflow > 0 > net_income:
                    score += 1

            if checks > 0:
                normalized = score / (checks * 2)  # Max 2 points per check
                if normalized >= 0.8:
                    result["grade"] = "A"
                elif normalized >= 0.6:
                    result["grade"] = "B"
                elif normalized >= 0.4:
                    result["grade"] = "C"
                elif normalized >= 0.2:
                    result["grade"] = "D"
                else:
                    result["grade"] = "F"

    except Exception as e:
        logger.warning("Failed to compute earnings quality for %s: %s", sym, e)

    cache.set("earnings_quality", sym, data=result)
    return result


# =========================================================================
# Intrinsic Value (DCF), Weekly Technicals, Scenario Analysis
# =========================================================================

def _dcf_equity_value_per_share(
    fcf: float,
    growth_rate: float,
    wacc: float,
    terminal_growth: float,
    net_debt: float,
    shares_outstanding: float,
) -> float:
    """Return DCF equity fair value per share for given assumptions."""
    dcf_value = sum(
        fcf * ((1 + growth_rate) ** year) / ((1 + wacc) ** year)
        for year in range(1, 11)
    )
    terminal_fcf = fcf * ((1 + growth_rate) ** 10)
    terminal_value = terminal_fcf * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal_value / ((1 + wacc) ** 10)
    enterprise_value = dcf_value + pv_terminal
    equity_value = enterprise_value - net_debt
    return equity_value / shares_outstanding


def _reverse_dcf(
    fcf: float,
    wacc: float,
    terminal_growth: float,
    net_debt: float,
    shares_outstanding: float,
    current_price: float,
    lo: float = -0.20,
    hi: float = 0.50,
    iterations: int = 50,
) -> Optional[float]:
    """Binary-search for the implied growth rate that equates DCF fair value to current price.

    Returns the implied growth rate, or None if the price is outside the achievable range or
    if the DCF denominator would go negative (wacc <= terminal_growth edge cases).
    """
    if wacc <= terminal_growth:
        return None

    def _obj(g: float) -> float:
        try:
            return _dcf_equity_value_per_share(fcf, g, wacc, terminal_growth, net_debt, shares_outstanding) - current_price
        except (ZeroDivisionError, OverflowError):
            return float("nan")

    lo_val, hi_val = _obj(lo), _obj(hi)
    if lo_val != lo_val or hi_val != hi_val:
        return None
    if lo_val * hi_val > 0:
        return None

    for _ in range(iterations):
        mid = (lo + hi) / 2
        mid_val = _obj(mid)
        if mid_val != mid_val:
            return None
        if abs(mid_val) < 0.01 or (hi - lo) < 1e-6:
            return mid
        if lo_val * mid_val < 0:
            hi = mid
        else:
            lo = mid
            lo_val = mid_val
    return (lo + hi) / 2


def compute_intrinsic_value(ticker: str) -> Dict[str, Any]:
    """Compute intrinsic value via simple DCF model using FCF, growth, and WACC.

    Extended outputs (Plan B Phase 1):
    - implied_growth_rate: growth rate implied by current price (reverse DCF)
    - implied_vs_consensus: delta between implied and consensus growth (implied - consensus)
    - dcf_sensitivity_grid: 3x3 WACC x terminal_growth fair-value grid
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    cache = get_cache()
    sym = ticker.upper()

    cached = cache.get("intrinsic_value", sym)
    if cached is not None:
        return cached

    result = {"ticker": sym, "fetched_at": datetime.now().isoformat()}

    try:
        t = yf.Ticker(sym)
        info = get_ticker_info(sym)
        cf = _call_with_timeout(
            lambda: t.cashflow,
            default=None,
            op=f"intrinsic_value.cashflow({sym})",
        )

        if cf is None or cf.empty:
            cache.set("intrinsic_value", sym, data=result)
            return result

        latest_cf = cf.iloc[:, 0]
        fcf = _safe_float(latest_cf.get("Free Cash Flow"))
        if fcf is None:
            op_cf = _safe_float(latest_cf.get("Operating Cash Flow") or latest_cf.get("Total Cash From Operating Activities"))
            capex = _safe_float(latest_cf.get("Capital Expenditure"))
            if op_cf is not None and capex is not None:
                fcf = op_cf + capex  # capex is typically negative

        if fcf is None or fcf <= 0:
            result["note"] = "Negative or unavailable FCF"
            cache.set("intrinsic_value", sym, data=result)
            return result

        earnings_growth = _safe_float(info.get("earningsGrowth"))
        revenue_growth = _safe_float(info.get("revenueGrowth"))
        consensus_growth = earnings_growth or revenue_growth or 0.05
        growth_rate = max(0.0, min(consensus_growth, 0.30))

        beta = _safe_float(info.get("beta")) or 1.0
        risk_free = 0.042  # ~10-yr Treasury
        equity_premium = 0.055
        wacc = risk_free + beta * equity_premium
        wacc = max(0.06, min(wacc, 0.15))

        terminal_growth = 0.025

        dcf_value = 0
        for year in range(1, 11):
            projected_fcf = fcf * ((1 + growth_rate) ** year)
            dcf_value += projected_fcf / ((1 + wacc) ** year)

        terminal_fcf = fcf * ((1 + growth_rate) ** 10)
        terminal_value = terminal_fcf * (1 + terminal_growth) / (wacc - terminal_growth)
        pv_terminal = terminal_value / ((1 + wacc) ** 10)

        enterprise_value = dcf_value + pv_terminal

        total_debt = _safe_float(info.get("totalDebt")) or 0
        total_cash = _safe_float(info.get("totalCash")) or 0
        net_debt = total_debt - total_cash
        equity_value = enterprise_value - net_debt

        shares_outstanding = _safe_float(info.get("sharesOutstanding"))
        if shares_outstanding and shares_outstanding > 0:
            fair_value = equity_value / shares_outstanding
            current_price = _safe_float(info.get("currentPrice")) or _safe_float(info.get("regularMarketPrice"))

            result["fair_value"] = round(fair_value, 2)
            result["current_price"] = current_price
            if current_price and current_price > 0:
                result["margin_of_safety_pct"] = round((fair_value - current_price) / current_price * 100, 1)

            result["assumptions"] = {
                "fcf": round(fcf, 0),
                "growth_rate": round(growth_rate, 4),
                "wacc": round(wacc, 4),
                "terminal_growth": terminal_growth,
                "beta": round(beta, 2),
            }

            # --- Reverse DCF: implied growth rate ---
            if current_price and current_price > 0:
                implied_g = _reverse_dcf(
                    fcf=fcf,
                    wacc=wacc,
                    terminal_growth=terminal_growth,
                    net_debt=net_debt,
                    shares_outstanding=shares_outstanding,
                    current_price=current_price,
                )
                if implied_g is not None:
                    result["implied_growth_rate"] = round(implied_g, 4)
                    result["implied_vs_consensus"] = round(implied_g - consensus_growth, 4)

            # --- DCF sensitivity grid (3x3 WACC x terminal_growth) ---
            try:
                dcf_cfg = DEFAULT_CONFIG.get("valuation", {}).get("dcf_sensitivity", {})
                wacc_offsets = [x / 100 for x in dcf_cfg.get("wacc_offsets_pp", [-1.0, 0.0, 1.0])]
                tg_offsets = [x / 100 for x in dcf_cfg.get("terminal_growth_offsets_pp", [-0.5, 0.0, 0.5])]
                grid: Dict[str, float] = {}
                for dw in wacc_offsets:
                    for dt in tg_offsets:
                        w = round(wacc + dw, 4)
                        tg = round(terminal_growth + dt, 4)
                        if w <= tg or tg < 0:
                            continue
                        cell_label = f"wacc{int(round(dw * 100)):+d}pp_tg{round(dt * 100, 1):+.1f}pp"
                        try:
                            cell_fv = _dcf_equity_value_per_share(
                                fcf, growth_rate, w, tg, net_debt, shares_outstanding
                            )
                            grid[cell_label] = round(cell_fv, 2)
                        except Exception:
                            pass
                if grid:
                    result["dcf_sensitivity_grid"] = grid
            except Exception as _ge:
                logger.debug("DCF sensitivity grid skipped for %s: %s", sym, _ge)

    except Exception as e:
        logger.warning("Failed to compute intrinsic value for %s: %s", sym, e)

    cache.set("intrinsic_value", sym, data=result)
    return result


def _weekly_technicals_is_complete(result: Dict[str, Any]) -> bool:
    """True when the payload has enough data for MTF scoring."""
    return (
        result.get("weekly_close") is not None
        and result.get("confluence_score") is not None
    )


def get_weekly_technicals(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """Get weekly timeframe technical indicators for multi-timeframe confluence."""
    cache = get_cache()
    sym = ticker.upper()
    as_of_key = (as_of_date or "latest")[:10]

    cached = cache.get("weekly_technicals", sym, as_of_key)
    if cached is not None and _weekly_technicals_is_complete(cached):
        return cached

    result = {"ticker": sym, "fetched_at": datetime.now().isoformat(), "as_of_date": as_of_key}

    try:
        if as_of_date:
            as_of_dt = pd.to_datetime(as_of_date, errors="coerce")
            if pd.isna(as_of_dt):
                as_of_dt = pd.Timestamp.now()
            end_date = (as_of_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            start_date = (as_of_dt - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
            df = yf.download(
                sym,
                start=start_date,
                end=end_date,
                interval="1wk",
                progress=False,
                auto_adjust=True,
            )
        else:
            df = yf.download(sym, period="1y", interval="1wk", progress=False, auto_adjust=True)

        if df is None or df.empty or len(df) < 14:
            cache.set("weekly_technicals", sym, as_of_key, data=result)
            return result

        close = _extract_close_series(df, sym)
        if len(close) < 14:
            cache.set("weekly_technicals", sym, as_of_key, data=result)
            return result

        # Weekly RSI (14-period)
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta.clip(upper=0))
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, float('nan'))
        rsi = 100 - (100 / (1 + rs))
        latest_rsi = _safe_float(rsi.iloc[-1]) if not rsi.empty else None
        result["weekly_rsi"] = round(latest_rsi, 2) if latest_rsi is not None else None

        if len(close) >= 10:
            sma_10 = _safe_float(close.rolling(10).mean().iloc[-1])
            if sma_10 is not None:
                result["weekly_sma10"] = round(sma_10, 2)
        if len(close) >= 20:
            sma_20 = _safe_float(close.rolling(20).mean().iloc[-1])
            if sma_20 is not None:
                result["weekly_sma20"] = round(sma_20, 2)

        latest_close = _safe_float(close.iloc[-1])
        if latest_close is None:
            return result
        result["weekly_close"] = round(latest_close, 2)

        # Weekly MACD (12, 26, 9)
        if len(close) >= 26:
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_val = _safe_float(macd_line.iloc[-1])
            signal_val = _safe_float(signal_line.iloc[-1])
            hist_val = _safe_float((macd_line - signal_line).iloc[-1])
            if macd_val is not None:
                result["weekly_macd"] = round(macd_val, 4)
            if signal_val is not None:
                result["weekly_macd_signal"] = round(signal_val, 4)
            if hist_val is not None:
                result["weekly_macd_histogram"] = round(hist_val, 4)

        bullish_count = 0
        total_checks = 0

        if latest_rsi is not None:
            total_checks += 1
            if 30 < latest_rsi < 70:
                bullish_count += 0.5
            elif latest_rsi <= 30:
                bullish_count += 0.7

        if result.get("weekly_sma10") is not None:
            total_checks += 1
            if latest_close > result["weekly_sma10"]:
                bullish_count += 1

        if result.get("weekly_macd_histogram") is not None:
            total_checks += 1
            if result["weekly_macd_histogram"] > 0:
                bullish_count += 1

        if total_checks > 0:
            result["confluence_score"] = round(bullish_count / total_checks, 2)

    except Exception as e:
        logger.warning("Failed to get weekly technicals for %s: %s", sym, e)
        return result

    if _weekly_technicals_is_complete(result):
        cache.set("weekly_technicals", sym, as_of_key, data=result)
    elif result.get("weekly_rsi") is None and result.get("weekly_close") is None:
        # Insufficient history / empty fetch — cache to avoid hammering, but never
        # cache partial payloads (e.g. RSI only from an old float() failure).
        cache.set("weekly_technicals", sym, as_of_key, data=result)
    return result


def _finalize_scenario_valuation(result: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Cap extreme blended upside and mark pre-profit P/S books as low reliability."""
    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = config or DEFAULT_CONFIG
    valuation_cfg = (cfg or {}).get("valuation") or {}
    try:
        max_upside = float(valuation_cfg.get("max_blended_upside_pct", 200.0))
    except (TypeError, ValueError):
        max_upside = 200.0

    method = str(result.get("valuation_method") or "")
    if "P/S" in method:
        result["valuation_reliability"] = "low"

    raw_upside = result.get("blended_upside_pct")
    try:
        upside = float(raw_upside) if raw_upside is not None else None
    except (TypeError, ValueError):
        upside = None

    if upside is not None and abs(upside) > max_upside:
        result["blended_upside_pct_raw"] = upside
        result["blended_upside_pct"] = None
        result["blended_upside_pct_capped"] = True
        result["valuation_reliability"] = "unreliable"
    return result


def compute_scenario_analysis(ticker: str) -> Dict[str, Any]:
    """Compute bull/base/bear price scenarios using PE x EPS multiples.

    Extended outputs (Plan B Phase 1):
    - blended_fair_value: probability-weighted blend using config scenario_weights
    - scenario_weights: the weights used (from valuation.scenario_weights in config)
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    cache = get_cache()
    sym = ticker.upper()

    cached = cache.get("scenario_analysis", sym)
    if cached is not None:
        return cached

    result = {"ticker": sym, "scenarios": {}, "fetched_at": datetime.now().isoformat()}

    try:
        info = get_ticker_info(sym)
        current_price = _safe_float(info.get("currentPrice")) or _safe_float(info.get("regularMarketPrice"))
        forward_pe = _safe_float(info.get("forwardPE"))
        forward_eps = _safe_float(info.get("forwardEps"))
        trailing_pe = _safe_float(info.get("trailingPE"))

        if not current_price:
            cache.set("scenario_analysis", sym, data=result)
            return result

        # Guard: EPS below $0.10 produces astronomically high (>200x) P/E multiples
        # that are meaningless for pre-profit or barely-profitable companies.
        # Fall through to P/S-based valuation in that case.
        if forward_eps and forward_eps >= 0.10:
            sector_avg_pe = forward_pe or trailing_pe or 18

            bull_pe = sector_avg_pe * 1.3
            base_pe = sector_avg_pe
            bear_pe = sector_avg_pe * 0.7

            earnings_growth = _safe_float(info.get("earningsGrowth")) or 0.10
            bull_eps = forward_eps * (1 + max(earnings_growth * 1.5, 0.15))
            base_eps = forward_eps * (1 + earnings_growth)
            bear_eps = forward_eps * (1 + min(earnings_growth * 0.3, 0))

            bull_price = round(bull_eps * bull_pe, 2)
            base_price = round(base_eps * base_pe, 2)
            bear_price = round(bear_eps * bear_pe, 2)

            result["scenarios"] = {
                "bull": {
                    "price": bull_price,
                    "eps": round(bull_eps, 2),
                    "pe": round(bull_pe, 1),
                    "upside_pct": round((bull_price - current_price) / current_price * 100, 1),
                },
                "base": {
                    "price": base_price,
                    "eps": round(base_eps, 2),
                    "pe": round(base_pe, 1),
                    "upside_pct": round((base_price - current_price) / current_price * 100, 1),
                },
                "bear": {
                    "price": bear_price,
                    "eps": round(bear_eps, 2),
                    "pe": round(bear_pe, 1),
                    "upside_pct": round((bear_price - current_price) / current_price * 100, 1),
                },
            }
            result["current_price"] = current_price

            # --- Blended fair value using config scenario weights ---
            weights = DEFAULT_CONFIG.get("valuation", {}).get("scenario_weights", {"bull": 0.25, "base": 0.50, "bear": 0.25})
            total_w = sum(weights.values())
            if abs(total_w - 1.0) < 0.01 and total_w > 0:
                blended = (
                    bull_price * weights.get("bull", 0.25)
                    + base_price * weights.get("base", 0.50)
                    + bear_price * weights.get("bear", 0.25)
                )
                result["blended_fair_value"] = round(blended, 2)
                result["blended_upside_pct"] = round((blended - current_price) / current_price * 100, 1)
                result["scenario_weights"] = {k: round(v, 3) for k, v in weights.items()}

        elif current_price:
            # Fallback for pre-profit / near-zero EPS companies: use P/S-based scenarios.
            # We anchor to a sector-typical P/S multiple and vary by ±30% bull/bear.
            revenue = _safe_float(info.get("totalRevenue"))
            shares = _safe_float(info.get("sharesOutstanding"))
            price_to_sales = _safe_float(info.get("priceToSalesTrailing12Months"))
            revenue_growth = _safe_float(info.get("revenueGrowth")) or 0.10

            if revenue and shares and price_to_sales and shares > 0:
                revenue_per_share = revenue / shares
                base_ps = price_to_sales
                bull_price = round(revenue_per_share * (1 + max(revenue_growth * 1.5, 0.15)) * base_ps * 1.2, 2)
                base_price = round(revenue_per_share * (1 + revenue_growth) * base_ps, 2)
                bear_price = round(revenue_per_share * (1 + min(revenue_growth * 0.3, 0)) * base_ps * 0.7, 2)

                result["scenarios"] = {
                    "bull": {"price": bull_price, "method": "P/S", "ps": round(base_ps * 1.2, 1),
                             "upside_pct": round((bull_price - current_price) / current_price * 100, 1)},
                    "base": {"price": base_price, "method": "P/S", "ps": round(base_ps, 1),
                             "upside_pct": round((base_price - current_price) / current_price * 100, 1)},
                    "bear": {"price": bear_price, "method": "P/S", "ps": round(base_ps * 0.7, 1),
                             "upside_pct": round((bear_price - current_price) / current_price * 100, 1)},
                }
                result["current_price"] = current_price
                result["valuation_method"] = "P/S (pre-profit fallback)"

                weights = DEFAULT_CONFIG.get("valuation", {}).get("scenario_weights", {"bull": 0.25, "base": 0.50, "bear": 0.25})
                total_w = sum(weights.values())
                if abs(total_w - 1.0) < 0.01 and total_w > 0:
                    blended = (
                        bull_price * weights.get("bull", 0.25)
                        + base_price * weights.get("base", 0.50)
                        + bear_price * weights.get("bear", 0.25)
                    )
                    result["blended_fair_value"] = round(blended, 2)
                    result["blended_upside_pct"] = round((blended - current_price) / current_price * 100, 1)
                    result["scenario_weights"] = {k: round(v, 3) for k, v in weights.items()}

    except Exception as e:
        logger.warning("Failed to compute scenario analysis for %s: %s", sym, e)

    result = _finalize_scenario_valuation(result, DEFAULT_CONFIG)
    cache.set("scenario_analysis", sym, data=result)
    return result


def compute_peer_comps(ticker: str, peers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Compute peer comparison table for P/E, P/S, and EV/EBITDA.

    Assembles up to max_peers sector peers using the ticker_metadata DB cache
    (populated by ticker_resolver), fetches valuation metrics for each, and
    returns median + percentile rank for the target ticker on each multiple.

    Args:
        ticker: Target ticker symbol.
        peers: Optional explicit peer list. When None, sector peers are auto-selected
               from the ticker_metadata DB using sector + market_cap_tier matching.

    Returns:
        Dict with keys:
            ticker, sector, market_cap_tier, multiples (per-metric dict with
            target_value, peer_median, percentile_rank, peer_values), fetched_at.
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    cache = get_cache()
    sym = ticker.upper()

    cached = cache.get("peer_comps", sym)
    if cached is not None:
        return cached

    result: Dict[str, Any] = {"ticker": sym, "fetched_at": datetime.now().isoformat()}
    metrics_to_compare = ["forward_pe", "price_to_sales", "ev_to_ebitda"]
    metric_labels = {"forward_pe": "Forward P/E", "price_to_sales": "P/S", "ev_to_ebitda": "EV/EBITDA"}

    try:
        cfg_comps = DEFAULT_CONFIG.get("valuation", {}).get("peer_comps", {})
        min_peers = int(cfg_comps.get("min_peers", 3))
        max_peers = int(cfg_comps.get("max_peers", 10))

        # Resolve target ticker metadata for sector/cap-tier
        info = get_ticker_info(sym)
        sector = str(info.get("sector") or "")
        industry = str(info.get("industry") or "")
        market_cap = _safe_float(info.get("marketCap"))
        from tradingagents.screening.ticker_resolver import classify_market_cap
        cap_tier = classify_market_cap(market_cap)
        result["sector"] = industry or sector
        result["peer_universe"] = "industry" if industry else "sector"
        result["market_cap_tier"] = cap_tier

        # Build peer list if not explicitly provided
        if not peers:
            # Attempt to load from ticker_metadata DB
            try:
                from tradingagents.reporting.database import get_db
                db = get_db()
                all_meta = {}
                if industry:
                    with db._connect() as conn:
                        rows = conn.execute(
                            "SELECT ticker, market_cap_tier FROM ticker_metadata WHERE industry=? AND ticker!=? LIMIT 50",
                            (industry, sym),
                        ).fetchall()
                    all_meta = {row["ticker"]: dict(row) for row in rows}
                if not all_meta:
                    all_meta = db.get_ticker_metadata_bulk_by_sector(sector) if hasattr(db, "get_ticker_metadata_bulk_by_sector") else {}
                # Fall back: query DB directly for sector matches
                if not all_meta:
                    with db._connect() as conn:
                        rows = conn.execute(
                            "SELECT ticker, market_cap_tier FROM ticker_metadata WHERE sector=? AND ticker!=? LIMIT 50",
                            (sector, sym),
                        ).fetchall()
                    all_meta = {row["ticker"]: dict(row) for row in rows}
                    result["peer_universe"] = "sector"
            except Exception as _db_err:
                logger.debug("Peer lookup from DB failed for %s: %s", sym, _db_err)
                all_meta = {}

            # Prefer same market_cap_tier peers, then expand to neighboring tiers
            tier_order = ["mega", "large", "mid", "small", "micro", "unknown"]
            same_tier = [t for t, m in all_meta.items() if m.get("market_cap_tier") == cap_tier]
            other_tier = [t for t, m in all_meta.items() if m.get("market_cap_tier") != cap_tier]
            candidate_peers = (same_tier + other_tier)[:max_peers * 2]
            peers = candidate_peers[:max_peers]

        if not peers:
            result["note"] = "No sector peers found in metadata cache"
            cache.set("peer_comps", sym, data=result)
            return result

        # Fetch valuation metrics for target + peers
        all_tickers = [sym] + [p.upper() for p in peers if p.upper() != sym]

        def _fetch_val(t: str) -> Optional[Dict[str, Any]]:
            try:
                v = get_valuation_metrics(t)
                return {"ticker": t, **v}
            except Exception:
                return None

        # Parallel fetch with short timeout
        val_data: Dict[str, Dict] = {}
        with ThreadPoolExecutor(max_workers=min(len(all_tickers), 8)) as pool:
            futs = {pool.submit(_fetch_val, t): t for t in all_tickers}
            for fut in futs:
                try:
                    res = fut.result(timeout=20)
                    if res:
                        val_data[res["ticker"]] = res
                except Exception:
                    pass

        if sym not in val_data:
            result["note"] = "Could not fetch target ticker valuation metrics"
            cache.set("peer_comps", sym, data=result)
            return result

        peer_tickers = [t for t in val_data if t != sym]
        if len(peer_tickers) < min_peers:
            result["note"] = f"Insufficient peers: {len(peer_tickers)} < {min_peers}"

        multiples: Dict[str, Any] = {}
        for metric in metrics_to_compare:
            target_val = _safe_float(val_data[sym].get(metric))
            peer_vals = [_safe_float(val_data[t].get(metric)) for t in peer_tickers]
            peer_vals_clean = [v for v in peer_vals if v is not None and v > 0]

            if not peer_vals_clean:
                multiples[metric] = {
                    "label": metric_labels[metric],
                    "target_value": target_val,
                    "peer_median": None,
                    "percentile_rank": None,
                    "peer_count": 0,
                }
                continue

            peer_vals_sorted = sorted(peer_vals_clean)
            n = len(peer_vals_sorted)
            median = (
                peer_vals_sorted[n // 2]
                if n % 2 == 1
                else (peer_vals_sorted[n // 2 - 1] + peer_vals_sorted[n // 2]) / 2
            )

            percentile_rank: Optional[float] = None
            if target_val is not None and target_val > 0:
                below = sum(1 for v in peer_vals_sorted if v < target_val)
                percentile_rank = round(below / n * 100, 1)

            multiples[metric] = {
                "label": metric_labels[metric],
                "target_value": round(target_val, 2) if target_val is not None else None,
                "peer_median": round(median, 2),
                "percentile_rank": percentile_rank,
                "peer_count": n,
                "peer_values": {t: round(v, 2) for t, v in zip(peer_tickers, peer_vals) if v is not None},
            }

        result["multiples"] = multiples
        result["peers_used"] = peer_tickers

    except Exception as e:
        logger.warning("Failed to compute peer comps for %s: %s", sym, e)

    cache.set("peer_comps", sym, data=result)
    return result


# =========================================================================
# Formatting helpers for analyst prompt injection
# =========================================================================

def format_analyst_context(ticker: str) -> str:
    """
    Fetch and format analyst ratings, earnings, and ownership data
    as a text block for injection into the fundamentals analyst prompt.
    
    Args:
        ticker: Stock ticker symbol
        
    Returns:
        Formatted text block ready for prompt injection
    """
    sections = []
    
    # Analyst ratings
    ratings = get_analyst_ratings(ticker)
    if ratings.get("recommendation_key"):
        rec = ratings["recommendation_key"].upper()
        sections.append(
            f"\n--- ANALYST CONSENSUS ---\n"
            f"Rating: {ratings['recommendation_mean']} ({rec}), "
            f"Analysts: {ratings['number_of_analysts']}\n"
            f"Target: ${ratings['target_low_price']}-${ratings['target_high_price']} "
            f"(mean ${ratings['target_mean_price']})\n"
            f"Upside: {ratings['upside_pct']}%, Consensus Score: {ratings['consensus_score']}/100"
        )
    
    # Earnings profile
    earnings = get_earnings_profile(ticker)
    if earnings.get("next_earnings_date") or earnings.get("earnings_beat_rate_pct") is not None:
        parts = []
        if earnings.get("next_earnings_date"):
            days = earnings.get("days_until_earnings")
            parts.append(f"Next Earnings: {earnings['next_earnings_date']} ({days} days)")
        if earnings.get("earnings_beat_rate_pct") is not None:
            parts.append(f"Beat Rate: {earnings['earnings_beat_rate_pct']}%")
        avg_surp = earnings.get("avg_earnings_surprise_pct")
        if avg_surp is not None and not (isinstance(avg_surp, float) and np.isnan(avg_surp)):
            parts.append(f"Avg Surprise: {avg_surp:+.1f}%")
        div_yield = earnings.get("dividend_yield")
        if div_yield is not None and not (isinstance(div_yield, float) and np.isnan(div_yield)):
            parts.append(f"Dividend Yield: {div_yield}%")
        sections.append("\n--- EARNINGS PROFILE ---\n" + ", ".join(parts))
    
    # Ownership
    ownership = get_ownership_summary(ticker)
    if ownership.get("held_by_institutions_pct") is not None:
        parts = [f"Institutional: {ownership['held_by_institutions_pct']}%"]
        if ownership.get("short_pct_of_float") is not None:
            parts.append(f"Short Interest: {ownership['short_pct_of_float']}%")
        if ownership.get("days_to_cover") is not None:
            parts.append(f"Days to Cover: {ownership['days_to_cover']}")
        si_change = ownership.get("short_interest_change_pct")
        if si_change is not None and not (isinstance(si_change, float) and np.isnan(si_change)):
            parts.append(f"Short Change MoM: {si_change:+.1f}%")
        sections.append("\n--- OWNERSHIP & SHORT INTEREST ---\n" + ", ".join(parts))
    
    # Earnings quality
    try:
        eq = get_earnings_quality(ticker)
        if eq.get("grade"):
            eq_parts = [f"Grade {eq['grade']}"]
            if eq.get("accruals_ratio") is not None:
                eq_parts.append(f"Accruals Ratio: {eq['accruals_ratio']:.4f}")
            if eq.get("cash_conversion") is not None:
                eq_parts.append(f"Cash Conversion: {eq['cash_conversion']:.4f}")
            eq_parts.append(f"Positive OCF: {'Yes' if eq.get('has_positive_ocf') else 'No'}")
            sections.append("\n--- EARNINGS QUALITY ---\n" + ", ".join(eq_parts))
    except Exception as e:
        logger.debug("Earnings quality unavailable for %s: %s", ticker, e)

    # Intrinsic value (DCF)
    try:
        iv = compute_intrinsic_value(ticker)
        if iv.get("fair_value"):
            margin = iv.get("margin_of_safety_pct", 0)
            label = "UNDERVALUED" if margin > 0 else "OVERVALUED"
            iv_parts = [f"${iv['fair_value']:.2f} ({label} by {abs(margin):.1f}%)"]
            assumptions = iv.get("assumptions", {})
            if assumptions:
                iv_parts.append(
                    f"FCF: ${assumptions.get('fcf', 0):,.0f}, "
                    f"Growth: {assumptions.get('growth_rate', 0):.1%}, "
                    f"WACC: {assumptions.get('wacc', 0):.1%}"
                )
            sections.append("\n--- DCF INTRINSIC VALUE ---\n" + " | ".join(iv_parts))
    except Exception as e:
        logger.debug("Intrinsic value unavailable for %s: %s", ticker, e)

    return "\n".join(sections) if sections else ""


def format_risk_context(ticker: str) -> str:
    """
    Format risk metrics as a text block for the risk manager prompt.
    
    Imports risk_metrics lazily to avoid circular imports.
    """
    try:
        from .risk_metrics import compute_risk_metrics
        metrics = compute_risk_metrics(ticker)
        if metrics.get("error"):
            return ""
        
        parts = []
        if metrics.get("beta") is not None:
            parts.append(f"Beta: {metrics['beta']:.2f}")
        if metrics.get("sharpe_ratio") is not None:
            parts.append(f"Sharpe: {metrics['sharpe_ratio']:.2f}")
        if metrics.get("max_drawdown_pct") is not None:
            parts.append(f"Max Drawdown: {metrics['max_drawdown_pct']:.1f}%")
        if metrics.get("volatility_annual_pct") is not None:
            parts.append(f"Volatility: {metrics['volatility_annual_pct']:.1f}%")
        if metrics.get("var_95_pct") is not None:
            parts.append(f"VaR (95%): {metrics['var_95_pct']:.2f}%")
        if metrics.get("cvar_95_pct") is not None:
            parts.append(f"CVaR (95%): {metrics['cvar_95_pct']:.2f}%")
        
        if parts:
            return "\n--- QUANTITATIVE RISK METRICS ---\n" + ", ".join(parts)
    except Exception as e:
        logger.debug("Failed to compute risk metrics for %s: %s", ticker, e)
    
    return ""


def format_macro_context(
    macro_snapshot: Optional[Dict[str, Any]] = None,
    as_of_date: Optional[str] = None,
) -> str:
    """
    Format macro snapshot as a text block for analyst prompt injection.

    Prefer a graph-owned ``macro_snapshot`` when available so the report stays
    aligned to ``trade_date``; non-graph callers may pass ``as_of_date`` instead.
    """
    try:
        macro = macro_snapshot if macro_snapshot is not None else get_macro_snapshot(as_of_date=as_of_date)
        parts = []

        from .index_regime import index_regime_display_label

        label = index_regime_display_label(macro)
        if label and label != "Unknown":
            parts.append(f"Index Regime: {label} (index-level; not a buy ticket)")

        if macro.get("index_stress") == "stressed":
            vix_val = macro.get("indices", {}).get("vix", {}).get("current")
            credit = macro.get("credit_stress", "unknown")
            stress_bits = []
            if vix_val is not None:
                stress_bits.append(f"VIX {vix_val}")
            if credit not in ("unknown", ""):
                stress_bits.append(f"credit {credit}")
            if stress_bits:
                parts.append("Index stress elevated: " + ", ".join(stress_bits))

        vix = macro.get("vix_level", "unknown")
        if vix != "unknown":
            vix_val = macro.get("indices", {}).get("vix", {}).get("current")
            parts.append(f"VIX: {vix_val} ({vix.replace('_', ' ').title()})" if vix_val else f"VIX: {vix}")

        yc = macro.get("yield_curve", "unknown")
        if yc != "unknown":
            parts.append(f"Yield Curve: {yc.title()}")

        dollar = macro.get("dollar_trend", "unknown")
        if dollar != "unknown":
            parts.append(f"Dollar: {dollar.title()}")

        if parts:
            return "\n--- MARKET ENVIRONMENT ---\n" + " | ".join(parts)
    except Exception as e:
        logger.debug("Failed to get macro context: %s", e)

    return ""


# =========================================================================
# Feature 3 (Step 2.4): Peer Comparison
# =========================================================================

def get_sector_peers(ticker: str, limit: int = 10) -> List[str]:
    """
    Get peers for a ticker using industry-level matching first, sector-level fallback.

    Resolution order:
    1. Curated sub-sector map keyed on yfinance ``industry`` field — ensures
       specialty companies (uranium, gold, lithium, etc.) get domain-relevant comps.
    2. Curated sector map keyed on yfinance ``sector`` field — broad-brush fallback
       for industries not yet in the sub-sector map.

    Args:
        ticker: Target ticker symbol
        limit: Max number of peers to return

    Returns:
        List of peer ticker symbols (excluding the target ticker)
    """
    cache = get_cache()
    cached = cache.get("sector_peers", ticker, str(limit))
    if cached is not None:
        return cached

    info = get_ticker_info(ticker)
    industry = (info.get("industry") or "").strip()
    sector = (info.get("sector") or "").strip()

    sym = ticker.upper()

    # --- 1. Industry-level lookup (highest precision) ---
    industry_map = _get_industry_tickers()
    if industry and industry in industry_map:
        candidates = industry_map[industry]
        peers = [p for p in candidates if p.upper() != sym][:limit]
        cache.set("sector_peers", ticker, str(limit), data=peers, ttl=CacheConfig.FUNDAMENTALS)
        return peers

    # --- 2. Sector-level fallback ---
    if not sector:
        return []

    sector_map = _get_sector_tickers()
    sector_tickers = sector_map.get(sector, [])
    peers = [p for p in sector_tickers if p.upper() != sym][:limit]

    cache.set("sector_peers", ticker, str(limit), data=peers, ttl=CacheConfig.FUNDAMENTALS)
    return peers


def _get_industry_tickers() -> Dict[str, List[str]]:
    """Curated tickers by yfinance industry string for high-precision peer matching.

    These override the broad sector map for specialty sub-industries where sector-level
    peers would be misleading (e.g., comparing a uranium miner against oil majors).
    Keys must match the exact string returned by ``yf.Ticker(sym).info["industry"]``.
    """
    return {
        # ── Uranium & Nuclear ───────────────────────────────────────────────────────
        "Uranium": ["CCJ", "DNN", "NXE", "LEU", "UEC", "URG", "UUUU", "PALAF", "PDN.AX"],
        # ── Gold Mining ─────────────────────────────────────────────────────────────
        "Gold": ["NEM", "AEM", "GOLD", "AU", "AGI", "KGC", "EQX", "WPM", "OR", "FNV"],
        "Gold Mining": ["NEM", "AEM", "GOLD", "AU", "AGI", "KGC", "EQX", "WPM", "OR", "FNV"],
        # ── Silver Mining ───────────────────────────────────────────────────────────
        "Silver": ["WPM", "PAAS", "AG", "SVM", "MAG", "SILV", "SSRM"],
        # ── Copper Mining ───────────────────────────────────────────────────────────
        "Copper": ["FCX", "SCCO", "AA", "HBM", "TECK", "CMMC", "IVN.TO"],
        # ── Lithium ─────────────────────────────────────────────────────────────────
        "Lithium & Battery Tech": ["ALB", "SQM", "LTHM", "LAC", "PLL", "SGML", "LI.TO"],
        # ── Rare Earth Metals ───────────────────────────────────────────────────────
        "Other Industrial Metals & Mining": [
            "MP", "UUUU", "NXE", "LYEL", "REE", "MVST", "TMC",
        ],
        # ── Steel / Iron ────────────────────────────────────────────────────────────
        "Steel": ["NUE", "STLD", "CLF", "X", "CMC", "RS", "ZEUS"],
        # ── Aluminum ────────────────────────────────────────────────────────────────
        "Aluminum": ["AA", "CENX", "KALU", "ARNC"],
        # ── Oil & Gas E&P ───────────────────────────────────────────────────────────
        "Oil & Gas E&P": ["XOM", "CVX", "COP", "OXY", "EOG", "FANG", "DVN", "MRO", "HES", "CTRA"],
        "Oil & Gas Exploration & Production": [
            "XOM", "CVX", "COP", "OXY", "EOG", "FANG", "DVN", "MRO", "HES", "CTRA"
        ],
        # ── Oil & Gas Refining ──────────────────────────────────────────────────────
        "Oil & Gas Refining & Marketing": ["MPC", "PSX", "VLO", "DK", "PARR", "PBF"],
        # ── Oil & Gas Equipment & Services ─────────────────────────────────────────
        "Oil & Gas Equipment & Services": ["SLB", "HAL", "BKR", "OII", "WFRD", "LBRT", "PTEN"],
        # ── Solar ───────────────────────────────────────────────────────────────────
        "Solar": ["ENPH", "SEDG", "FSLR", "RUN", "ARRY", "NOVA", "SHLS"],
        # ── Semiconductors ──────────────────────────────────────────────────────────
        "Semiconductors": ["NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "MCHP", "ON", "MRVL", "ADI"],
        "Semiconductor Equipment": ["AMAT", "LRCX", "KLAC", "ASML", "TER", "UCTT", "ONTO"],
        # ── Biotechnology ───────────────────────────────────────────────────────────
        "Biotechnology": ["MRNA", "BNTX", "REGN", "VRTX", "GILD", "BIIB", "SGEN", "ALNY", "INCY"],
        # ── Drug Manufacturers ──────────────────────────────────────────────────────
        "Drug Manufacturers—General": ["JNJ", "PFE", "MRK", "ABBV", "BMY", "LLY", "AZN", "NVS"],
        # ── Banks ───────────────────────────────────────────────────────────────────
        "Banks—Diversified": ["JPM", "BAC", "WFC", "C", "USB", "PNC", "TFC", "KEY"],
        "Banks—Regional": ["FITB", "HBAN", "RF", "CFG", "ZION", "WAL", "FHB", "FFIN"],
        # ── Insurance ───────────────────────────────────────────────────────────────
        "Insurance—Property & Casualty": ["PGR", "CB", "TRV", "ALL", "MKL", "RLI", "WRB"],
        # ── Electric Vehicles ───────────────────────────────────────────────────────
        "Auto Manufacturers": ["TSLA", "GM", "F", "RIVN", "LCID", "NIO", "LI", "XPEV"],
        # ── Airlines ────────────────────────────────────────────────────────────────
        "Airlines": ["DAL", "UAL", "AAL", "LUV", "ALK", "JBLU", "SAVE"],
        # ── REITs ───────────────────────────────────────────────────────────────────
        "REIT—Industrial": ["PLD", "REXR", "EGP", "STAG", "FR", "LXP"],
        "REIT—Office": ["BXP", "VNO", "SLG", "HIW", "PDM", "DEI"],
        "REIT—Retail": ["SPG", "O", "NNN", "ROIC", "KIM", "REG"],
        "REIT—Residential": ["AVB", "EQR", "MAA", "UDR", "CPT", "NMD"],
        # ── Cloud / Software ────────────────────────────────────────────────────────
        "Software—Application": ["CRM", "NOW", "ADBE", "INTU", "WDAY", "TEAM", "SNOW", "ZS", "DDOG"],
        "Software—Infrastructure": ["MSFT", "ORCL", "VMW", "PANW", "FTNT", "NET", "CRWD"],
    }


def _get_sector_tickers() -> Dict[str, List[str]]:
    """Curated large-cap tickers by GICS sector for peer comparison.

    Used as a fallback when the ticker's ``industry`` field does not match any
    entry in ``_get_industry_tickers()``.
    """
    return {
        "Technology": [
            "AAPL", "MSFT", "NVDA", "AVGO", "ADBE", "CRM", "CSCO", "ORCL",
            "ACN", "INTC", "AMD", "TXN", "QCOM", "INTU", "AMAT", "ADI",
            "LRCX", "KLAC", "SNPS", "CDNS", "MCHP", "ON", "MU",
        ],
        "Healthcare": [
            "UNH", "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "PFE",
            "AMGN", "DHR", "ISRG", "SYK", "GILD", "VRTX", "REGN", "CI",
            "ELV", "BDX", "ZTS", "DXCM", "IDXX", "BSX", "MDT",
        ],
        "Financial Services": [
            "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "BLK", "SCHW",
            "AXP", "SPGI", "CB", "CME", "MCO", "MMC", "AON", "PGR",
            "PNC", "USB", "TFC", "ICE", "FIS", "FISV",
        ],
        "Consumer Cyclical": [
            "AMZN", "TSLA", "HD", "MCD", "LOW", "NKE", "SBUX", "TJX",
            "BKNG", "MAR", "ROST", "ORLY", "CMG", "DHI", "LEN", "YUM",
            "DG", "DLTR", "LULU", "ABNB", "DASH",
        ],
        "Communication Services": [
            "META", "GOOGL", "GOOG", "NFLX", "DIS", "CMCSA", "TMUS",
            "T", "VZ", "CHTR", "EA", "TTWO", "WBD", "MTCH", "RBLX",
        ],
        "Industrials": [
            "GE", "CAT", "HON", "UNP", "UPS", "RTX", "BA", "DE", "LMT",
            "MMM", "GD", "ITW", "EMR", "PCAR", "CSX", "NSC", "WM",
            "FAST", "CTAS", "PAYX", "FDX",
        ],
        "Consumer Defensive": [
            "PG", "COST", "KO", "PEP", "WMT", "PM", "MO", "CL", "MDLZ",
            "KDP", "GIS", "KHC", "STZ", "EL", "CLX", "HSY", "KMB",
        ],
        "Energy": [
            "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO",
            "OXY", "WMB", "KMI", "HES", "FANG", "BKR", "HAL",
        ],
        "Utilities": [
            "NEE", "DUK", "SO", "AEP", "SRE", "D", "EXC", "XEL",
            "WEC", "ES", "ED", "AES", "PCG", "CEG", "PEG",
        ],
        "Real Estate": [
            "PLD", "AMT", "CCI", "EQIX", "PSA", "WELL", "SPG", "O",
            "DLR", "AVB", "EQR", "VICI", "ARE", "SBAC", "INVH",
        ],
        "Basic Materials": [
            "LIN", "APD", "SHW", "ECL", "NEM", "FCX", "DOW", "DD",
            "NUE", "VMC", "MLM", "PPG", "IFF", "CE", "CTVA",
        ],
    }


def compute_peer_percentiles(ticker: str, peers: List[str] = None) -> Dict[str, Any]:
    """
    Compute percentile rank of a ticker vs its peers on key valuation metrics.
    
    Args:
        ticker: Target ticker symbol
        peers: Optional peer list (auto-detected if None)
        
    Returns:
        Dict with metric_name -> {"value": X, "percentile": Y, "peer_median": Z}
    """
    cache = get_cache()
    cached = cache.get("peer_percentiles", ticker)
    if cached is not None:
        return cached
    
    if peers is None:
        peers = get_sector_peers(ticker)
    
    if not peers:
        return {"ticker": ticker.upper(), "peers": [], "percentiles": {}}
    
    all_tickers = [ticker.upper()] + peers
    metrics_data: Dict[str, Dict] = {}
    
    try:
        batch = yf.Tickers(" ".join(all_tickers))
        for t in all_tickers:
            try:
                info = batch.tickers[t].info or {}
                # Seed centralized ticker_info_full cache with batch data
                cache.set("ticker_info_full", t, data=info)
                metrics_data[t] = {
                    "trailing_pe": info.get("trailingPE"),
                    "forward_pe": info.get("forwardPE"),
                    "price_to_sales": info.get("priceToSalesTrailing12Months"),
                    "price_to_book": info.get("priceToBook"),
                    "ev_to_ebitda": info.get("enterpriseToEbitda"),
                    "revenue_growth": info.get("revenueGrowth"),
                    "profit_margins": info.get("profitMargins"),
                    "beta": info.get("beta"),
                }
            except Exception as e:
                logger.debug("Failed to fetch peer metrics for %s: %s", t, e)
                continue
    except Exception as e:
        logger.warning("Peer metrics batch failed for %s: %s", ticker, e)
        return {"ticker": ticker.upper(), "peers": peers, "percentiles": {}}
    
    target_metrics = metrics_data.get(ticker.upper(), {})
    if not target_metrics:
        return {"ticker": ticker.upper(), "peers": peers, "percentiles": {}}
    
    percentiles: Dict[str, Dict] = {}
    metric_names = ["trailing_pe", "forward_pe", "price_to_sales", "price_to_book",
                    "ev_to_ebitda", "revenue_growth", "profit_margins", "beta"]
    
    for metric in metric_names:
        target_val = target_metrics.get(metric)
        if target_val is None:
            continue
        
        peer_vals = []
        for p in peers:
            pval = metrics_data.get(p, {}).get(metric)
            if pval is not None and not (isinstance(pval, float) and np.isnan(pval)):
                peer_vals.append(pval)
        
        if not peer_vals:
            continue
        
        count_below = sum(1 for v in peer_vals if v < target_val)
        percentile = round(count_below / len(peer_vals) * 100, 1)
        peer_median = round(float(np.median(peer_vals)), 2)
        
        percentiles[metric] = {
            "value": round(target_val, 2) if isinstance(target_val, float) else target_val,
            "percentile": percentile,
            "peer_median": peer_median,
            "peer_count": len(peer_vals),
        }
    
    result = {
        "ticker": ticker.upper(),
        "peers": peers,
        "percentiles": percentiles,
        "computed_at": datetime.now().isoformat(),
    }
    
    cache.set("peer_percentiles", ticker, data=result, ttl=CacheConfig.FUNDAMENTALS)
    return result


def format_peer_context(ticker: str) -> str:
    """
    Format peer comparison data as a text block for the fundamentals analyst prompt.
    """
    try:
        data = compute_peer_percentiles(ticker)
        percs = data.get("percentiles", {})
        if not percs:
            return ""
        
        peers = data.get("peers", [])
        lines = [f"\n--- PEER COMPARISON ({len(peers)} sector peers) ---"]
        
        for metric, info in percs.items():
            label = metric.replace("_", " ").title()
            lines.append(
                f"{label}: {info['value']} (P{info['percentile']:.0f} vs peers, "
                f"median={info['peer_median']})"
            )
        
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Peer comparison failed for %s: %s", ticker, e)
        return ""


# =========================================================================
# Feature 5 (Step 3.1): Options Flow & Implied Volatility
# =========================================================================

def get_options_summary(ticker: str) -> Dict[str, Any]:
    """
    Get options intelligence: IV, IV Rank, P/C ratios, max pain, unusual activity.
    
    All computation is local from yfinance options chains — no external API.
    
    Args:
        ticker: Stock ticker symbol
        
    Returns:
        Dict with IV, IV rank, P/C ratios, max pain, unusual activity flags
    """
    cache = get_cache()
    cached = cache.get("options", ticker)
    if cached is not None:
        return cached
    
    info = get_ticker_info(ticker)
    sym = ticker.upper()
    t = yf.Ticker(sym)  # needed for .options / .option_chain()
    current_price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")

    if not current_price:
        return _empty_options_summary(ticker, "No current price available")

    # Get options expirations
    try:
        expirations = _call_with_timeout(
            lambda: t.options,
            default=None,
            op=f"options.expirations({sym})",
        )
    except Exception as e:
        logger.warning("Failed to fetch options expirations for %s: %s", ticker, e)
        return _empty_options_summary(ticker, "No options data available")

    if expirations is None:
        return _empty_options_summary(ticker, "No options data available")
    
    if not expirations:
        return _empty_options_summary(ticker, "No options expirations found")
    
    # Use nearest expiration for ATM IV, and aggregate across first 3 for volume/OI
    total_call_volume = 0
    total_put_volume = 0
    total_call_oi = 0
    total_put_oi = 0
    atm_iv = None
    unusual_activity = []
    
    # For max pain calculation
    all_strikes_oi = []  # (strike, call_oi, put_oi)
    
    exps_to_scan = expirations[:min(3, len(expirations))]
    
    for exp in exps_to_scan:
        try:
            chain = _call_with_timeout(
                t.option_chain,
                exp,
                default=None,
                op=f"options.option_chain({sym},{exp})",
            )
            if chain is None:
                continue
            calls = chain.calls
            puts = chain.puts
            
            if calls.empty and puts.empty:
                continue
            
            # Aggregate volume and OI
            total_call_volume += int(calls["volume"].sum()) if "volume" in calls.columns else 0
            total_put_volume += int(puts["volume"].sum()) if "volume" in puts.columns else 0
            total_call_oi += int(calls["openInterest"].sum()) if "openInterest" in calls.columns else 0
            total_put_oi += int(puts["openInterest"].sum()) if "openInterest" in puts.columns else 0
            
            # ATM IV from nearest expiration only
            if atm_iv is None and exp == exps_to_scan[0]:
                atm_iv = _compute_atm_iv(calls, puts, current_price)
            
            # Collect strike OI for max pain (first expiration only)
            if exp == exps_to_scan[0]:
                for _, row in calls.iterrows():
                    strike = row.get("strike", 0)
                    c_oi = row.get("openInterest", 0) or 0
                    all_strikes_oi.append((strike, c_oi, 0))
                for _, row in puts.iterrows():
                    strike = row.get("strike", 0)
                    p_oi = row.get("openInterest", 0) or 0
                    # Merge with existing or add
                    found = False
                    for i, (s, c, p) in enumerate(all_strikes_oi):
                        if s == strike:
                            all_strikes_oi[i] = (s, c, p + p_oi)
                            found = True
                            break
                    if not found:
                        all_strikes_oi.append((strike, 0, p_oi))
            
            # Unusual activity: contracts where volume >> OI (>3x)
            for _, row in calls.iterrows():
                vol = row.get("volume", 0) or 0
                oi = row.get("openInterest", 0) or 0
                if oi > 0 and vol > oi * 3 and vol > 100:
                    unusual_activity.append({
                        "type": "call",
                        "strike": row.get("strike"),
                        "expiry": exp,
                        "volume": int(vol),
                        "oi": int(oi),
                        "ratio": round(vol / oi, 1),
                    })
            for _, row in puts.iterrows():
                vol = row.get("volume", 0) or 0
                oi = row.get("openInterest", 0) or 0
                if oi > 0 and vol > oi * 3 and vol > 100:
                    unusual_activity.append({
                        "type": "put",
                        "strike": row.get("strike"),
                        "expiry": exp,
                        "volume": int(vol),
                        "oi": int(oi),
                        "ratio": round(vol / oi, 1),
                    })
                    
        except Exception as e:
            logger.debug("Options chain error for %s exp=%s: %s", ticker, exp, e)
            continue
    
    # Compute P/C ratios
    pc_volume_ratio = None
    if total_call_volume > 0:
        pc_volume_ratio = round(total_put_volume / total_call_volume, 3)
    
    pc_oi_ratio = None
    if total_call_oi > 0:
        pc_oi_ratio = round(total_put_oi / total_call_oi, 3)
    
    # Max pain
    max_pain = _compute_max_pain(all_strikes_oi, current_price) if all_strikes_oi else None
    
    # IV Rank (simplified — compare ATM IV to implied vol from info)
    iv_rank = None
    implied_vol = info.get("impliedVolatility")  # 52-week annualized
    if atm_iv is not None and implied_vol is not None and implied_vol > 0:
        # Rough rank: current ATM IV vs annualized IV
        iv_rank = round(min(atm_iv / implied_vol * 50, 100), 1)
    
    # Sort unusual activity by volume/OI ratio
    unusual_activity.sort(key=lambda x: x.get("ratio", 0), reverse=True)
    
    result = {
        "ticker": ticker.upper(),
        "current_price": current_price,
        "expirations_scanned": len(exps_to_scan),
        "atm_iv": round(atm_iv * 100, 2) if atm_iv else None,  # As percentage
        "iv_rank": iv_rank,
        "put_call_volume_ratio": pc_volume_ratio,
        "put_call_oi_ratio": pc_oi_ratio,
        "total_call_volume": total_call_volume,
        "total_put_volume": total_put_volume,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "max_pain": max_pain,
        "unusual_activity_count": len(unusual_activity),
        "unusual_activity": unusual_activity[:5],  # Top 5 most unusual
        "fetched_at": datetime.now().isoformat(),
    }
    result = sanitize_options_snapshot(result)
    
    cache.set("options", ticker, data=result, ttl=CacheConfig.OPTIONS)
    return result


def sanitize_options_snapshot(opts: Dict[str, Any], spot: Optional[float] = None) -> Dict[str, Any]:
    """Drop implausible ATM IV / max-pain prints before prompt or storage."""
    out = dict(opts or {})
    price = spot if spot is not None else out.get("current_price")
    pain = out.get("max_pain")
    try:
        if pain is not None and price not in (None, ""):
            pf = float(pain)
            pr = float(price)
            if pr > 0 and (pf < pr * 0.4 or pf > pr * 2.5):
                out["max_pain"] = None
    except (TypeError, ValueError):
        out["max_pain"] = None
    iv = out.get("atm_iv")
    try:
        if iv is not None:
            ivf = float(iv)
            # Stored as percent. Values < 1 are leftover decimals or junk.
            out["atm_iv"] = None if ivf < 1.0 else round(ivf, 2)
    except (TypeError, ValueError):
        out["atm_iv"] = None
    return out


def _compute_atm_iv(calls: 'pd.DataFrame', puts: 'pd.DataFrame', current_price: float) -> Optional[float]:
    """Compute ATM implied volatility as average of nearest call + put IV."""
    try:
        # Find strikes nearest to current price
        if "strike" not in calls.columns or "impliedVolatility" not in calls.columns:
            return None
        
        calls_valid = calls[calls["impliedVolatility"].notna() & (calls["impliedVolatility"] > 0)]
        puts_valid = puts[puts["impliedVolatility"].notna() & (puts["impliedVolatility"] > 0)]
        
        if calls_valid.empty and puts_valid.empty:
            return None
        
        ivs = []
        if not calls_valid.empty:
            nearest_call = calls_valid.iloc[(calls_valid["strike"] - current_price).abs().argsort()[:1]]
            ivs.append(float(nearest_call["impliedVolatility"].iloc[0]))
        
        if not puts_valid.empty:
            nearest_put = puts_valid.iloc[(puts_valid["strike"] - current_price).abs().argsort()[:1]]
            ivs.append(float(nearest_put["impliedVolatility"].iloc[0]))
        
        return sum(ivs) / len(ivs) if ivs else None
    except Exception as e:
        logger.debug("Failed to compute ATM IV: %s", e)
        return None


def _compute_max_pain(strikes_oi: list, current_price: float) -> Optional[float]:
    """
    Compute max pain: the strike where total losses for option holders is maximized
    (i.e., where most options expire worthless).
    """
    try:
        if not strikes_oi:
            return None
        
        # For each candidate strike, compute total $ at risk
        min_pain = float("inf")
        max_pain_strike = None
        
        unique_strikes = sorted(set(s for s, _, _ in strikes_oi))
        
        for candidate in unique_strikes:
            total_pain = 0
            for strike, call_oi, put_oi in strikes_oi:
                # Calls: holders lose if stock is below strike
                if candidate < strike:
                    total_pain += call_oi * (strike - candidate) * 0  # calls expire worthless (no pain to writers)
                else:
                    total_pain += call_oi * (candidate - strike)  # ITM calls cost writers
                
                # Puts: holders lose if stock is above strike
                if candidate > strike:
                    total_pain += put_oi * 0  # puts expire worthless
                else:
                    total_pain += put_oi * (strike - candidate)  # ITM puts cost writers
            
            if total_pain < min_pain:
                min_pain = total_pain
                max_pain_strike = candidate
        
        return round(max_pain_strike, 2) if max_pain_strike is not None else None
    except Exception as e:
        logger.debug("Failed to compute max pain: %s", e)
        return None


def _empty_options_summary(ticker: str, reason: str = "") -> Dict[str, Any]:
    """Return empty options summary for graceful degradation."""
    return {
        "ticker": ticker.upper(),
        "current_price": None,
        "expirations_scanned": 0,
        "atm_iv": None,
        "iv_rank": None,
        "put_call_volume_ratio": None,
        "put_call_oi_ratio": None,
        "total_call_volume": 0,
        "total_put_volume": 0,
        "total_call_oi": 0,
        "total_put_oi": 0,
        "max_pain": None,
        "unusual_activity_count": 0,
        "unusual_activity": [],
        "fetched_at": datetime.now().isoformat(),
        "note": reason,
    }


def format_options_context(ticker: str) -> str:
    """Format options data as text block for risk manager prompt injection."""
    try:
        opts = get_options_summary(ticker)
        if not opts.get("atm_iv"):
            return ""
        
        parts = []
        if opts["atm_iv"] is not None:
            parts.append(f"IV: {opts['atm_iv']}%")
        if opts["iv_rank"] is not None:
            parts.append(f"IV Rank: {opts['iv_rank']}")
        if opts["put_call_volume_ratio"] is not None:
            parts.append(f"P/C Vol: {opts['put_call_volume_ratio']}")
        if opts["put_call_oi_ratio"] is not None:
            parts.append(f"P/C OI: {opts['put_call_oi_ratio']}")
        if opts["max_pain"] is not None:
            parts.append(f"Max Pain: ${opts['max_pain']}")
        if opts["unusual_activity_count"] > 0:
            parts.append(f"Unusual Activity: {opts['unusual_activity_count']} contracts")
        
        if parts:
            return "\n--- OPTIONS INTELLIGENCE ---\n" + ", ".join(parts)
    except Exception as e:
        logger.debug("Options context failed for %s: %s", ticker, e)
    return ""


def format_estimate_revisions_context(ticker: str) -> str:
    """Format EPS estimate revision data for analyst prompt injection."""
    try:
        data = get_estimate_revisions(ticker)
        if not data:
            return ""

        parts = []

        eps_trend = data.get("eps_trend", {})
        for period_key, trend in eps_trend.items():
            current = trend.get("current")
            ago_30 = trend.get("30d_ago")
            if current is not None and ago_30 is not None and ago_30 != 0:
                revision_pct = ((current - ago_30) / abs(ago_30)) * 100
                direction = "UP" if revision_pct > 1 else "DOWN" if revision_pct < -1 else "FLAT"
                parts.append(f"{period_key}: EPS {direction} {revision_pct:+.1f}% over 30d (current: {current:.2f})")

        eps_keys = [k for k in data if k.startswith("eps_") and k != "eps_trend"]
        for k in eps_keys[:2]:
            ep = data[k]
            num = ep.get("num_analysts", 0)
            growth = ep.get("growth")
            if num > 0:
                growth_str = f", growth: {growth:.1%}" if growth is not None else ""
                parts.append(f"{k}: avg EPS {ep.get('avg', 'N/A')} ({num} analysts{growth_str})")

        if parts:
            return "\n--- ESTIMATE REVISIONS ---\n" + "\n".join(parts)
    except Exception as e:
        logger.debug("Estimate revisions context failed for %s: %s", ticker, e)
    return ""


def format_rating_changes_context(ticker: str) -> str:
    """Format analyst upgrade/downgrade history for prompt injection."""
    try:
        data = get_rating_changes(ticker)
        if not data or not data.get("actions"):
            return ""

        parts = []
        net = data.get("net_upgrades_90d", 0)
        total = data.get("total_actions_90d", 0)
        direction = "NET UPGRADES" if net > 0 else "NET DOWNGRADES" if net < 0 else "NEUTRAL"
        parts.append(f"90-Day Summary: {direction} ({net:+d} net, {total} total actions)")

        for action in data["actions"][:5]:
            firm = action.get("firm", "Unknown")
            to_g = action.get("to_grade", "")
            from_g = action.get("from_grade", "")
            act = action.get("action", "")
            dt = action.get("date", "")
            change = f"{from_g} -> {to_g}" if from_g and to_g else to_g or from_g
            parts.append(f"  {dt}: {firm} {act} {change}")

        if parts:
            return "\n--- RATING CHANGES ---\n" + "\n".join(parts)
    except Exception as e:
        logger.debug("Rating changes context failed for %s: %s", ticker, e)
    return ""


def format_weekly_technicals_context(ticker: str, as_of_date: Optional[str] = None) -> str:
    """Format weekly timeframe technicals for multi-timeframe analysis."""
    try:
        data = get_weekly_technicals(ticker, as_of_date=as_of_date)
        if not data or not data.get("weekly_close"):
            return ""

        parts = []
        close = data.get("weekly_close")
        rsi = data.get("weekly_rsi")
        sma10 = data.get("weekly_sma10")
        macd_hist = data.get("weekly_macd_histogram")
        confluence = data.get("confluence_score")

        trend = "NEUTRAL"
        if sma10 and close:
            trend = "ABOVE SMA10 (bullish)" if close > sma10 else "BELOW SMA10 (bearish)"
        parts.append(f"Weekly Close: ${close:.2f} ({trend})")

        if rsi is not None:
            rsi_label = "overbought" if rsi > 70 else "oversold" if rsi < 30 else "neutral"
            parts.append(f"Weekly RSI: {rsi:.1f} ({rsi_label})")

        if macd_hist is not None:
            macd_dir = "positive (bullish)" if macd_hist > 0 else "negative (bearish)"
            parts.append(f"Weekly MACD Histogram: {macd_hist:.4f} ({macd_dir})")

        if confluence is not None:
            conf_label = "bullish" if confluence > 0.6 else "bearish" if confluence < 0.4 else "mixed"
            parts.append(f"Weekly Confluence Score: {confluence:.2f} ({conf_label})")

        if parts:
            return "\n--- WEEKLY TECHNICALS ---\n" + "\n".join(parts)
    except Exception as e:
        logger.debug("Weekly technicals context failed for %s: %s", ticker, e)
    return ""
