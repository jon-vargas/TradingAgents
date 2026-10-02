"""
Shared OHLCV cache helpers for screening and prewarm.

Single source of truth for chunk size, cache keys, JSON bar payloads, and SPY
benchmark caching. Used by ScreeningEngine and scripts/prewarm_screen_all.py.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from tradingagents.dataflows.cache import CacheConfig, get_cache

logger = logging.getLogger("tradingagents.dataflows.screening_ohlcv_cache")

SCREENING_OHLCV_CHUNK_SIZE = 60
DEFAULT_OHLCV_LOOKBACK_DAYS = 420
MIN_OHLCV_BARS = 20

_ohlcv_session_depth = 0
_ohlcv_session_lock = threading.Lock()


class ScreeningOhlcvSession:
    """While active, yfinance_extended helpers skip Yahoo calls."""

    def __enter__(self) -> "ScreeningOhlcvSession":
        global _ohlcv_session_depth
        with _ohlcv_session_lock:
            _ohlcv_session_depth += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        global _ohlcv_session_depth
        with _ohlcv_session_lock:
            _ohlcv_session_depth = max(0, _ohlcv_session_depth - 1)


def is_screening_ohlcv_session_active() -> bool:
    with _ohlcv_session_lock:
        return _ohlcv_session_depth > 0


def clamp_lookback_days(raw: Optional[int]) -> int:
    lookback = int(raw or DEFAULT_OHLCV_LOOKBACK_DAYS)
    return max(400, min(lookback, 800))


def lookback_from_config(screening_config: Optional[Dict[str, Any]]) -> int:
    cfg = screening_config or {}
    return clamp_lookback_days(cfg.get("ohlcv_lookback_days"))


def session_tag_for(scan_date: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    from tradingagents.screening.reversal_buildup import session_complete_tag

    return session_complete_tag(scan_date, cfg=cfg)


def date_range_for_scan(scan_date: str, lookback_days: int) -> Tuple[datetime, datetime]:
    end_date = datetime.strptime(scan_date, "%Y-%m-%d") + timedelta(days=1)
    start_date = end_date - timedelta(days=lookback_days)
    return start_date, end_date


def chunk_cache_key(scan_date: str, session_tag: str, tickers: List[str]) -> str:
    tickers_key = ",".join(sorted(t.upper() for t in tickers))
    return f"screening_ohlcv:{scan_date}:{session_tag}:{tickers_key}"


def _series_to_bars(close, volume, high, low) -> Optional[Dict[str, Any]]:
    if close is None or len(close) < MIN_OHLCV_BARS:
        return None

    def _idx_to_str(idx) -> List[str]:
        out: List[str] = []
        for ts in idx:
            try:
                out.append(pd.Timestamp(ts).strftime("%Y-%m-%d"))
            except Exception:
                out.append(str(ts))
        return out

    idx = close.index
    return {
        "dates": _idx_to_str(idx),
        "close": [float(x) for x in close.tolist()],
        "volume": [float(x) for x in volume.tolist()] if volume is not None else [],
        "high": [float(x) for x in high.tolist()] if high is not None else [],
        "low": [float(x) for x in low.tolist()] if low is not None else [],
    }


def bars_to_series(bars: Dict[str, Any]) -> Tuple[Any, Any, Any, Any]:
    dates = bars.get("dates") or []
    idx = pd.to_datetime(dates)
    close = pd.Series(bars.get("close") or [], index=idx, dtype=float)
    volume = pd.Series(bars.get("volume") or [], index=idx, dtype=float)
    high = pd.Series(bars.get("high") or [], index=idx, dtype=float)
    low = pd.Series(bars.get("low") or [], index=idx, dtype=float)
    return close, volume, high, low


def extract_ticker_bars_from_download(
    ohlcv: pd.DataFrame,
    ticker: str,
    is_single: bool,
) -> Optional[Dict[str, Any]]:
    if ohlcv is None or ohlcv.empty:
        return None
    try:
        if is_single:
            close = ohlcv["Close"].dropna()
            volume = ohlcv["Volume"].dropna()
            high = ohlcv["High"].dropna()
            low = ohlcv["Low"].dropna()
        else:
            sym = ticker.upper()
            try:
                ticker_data = ohlcv[sym]
            except KeyError:
                return None
            close = ticker_data["Close"].dropna()
            volume = ticker_data["Volume"].dropna()
            high = ticker_data["High"].dropna()
            low = ticker_data["Low"].dropna()
        close = close.squeeze() if hasattr(close, "squeeze") else close
        volume = volume.squeeze() if hasattr(volume, "squeeze") else volume
        high = high.squeeze() if hasattr(high, "squeeze") else high
        low = low.squeeze() if hasattr(low, "squeeze") else low
        return _series_to_bars(close, volume, high, low)
    except Exception:
        return None


def persist_ticker_bars(
    cache,
    ticker: str,
    scan_date: str,
    session_tag: str,
    lookback_days: int,
    bars: Dict[str, Any],
) -> None:
    sym = ticker.upper()
    cache.set(
        "screening_ohlcv_ticker",
        sym,
        scan_date,
        session_tag,
        str(lookback_days),
        data=bars,
        ttl=CacheConfig.SCREENING_PRICES,
    )


def load_ticker_bars(
    cache,
    tickers: List[str],
    scan_date: str,
    session_tag: str,
    lookback_days: int,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    hits: Dict[str, Dict[str, Any]] = {}
    misses: List[str] = []
    for t in tickers:
        sym = t.upper()
        cached = cache.get(
            "screening_ohlcv_ticker",
            sym,
            scan_date,
            session_tag,
            str(lookback_days),
        )
        if isinstance(cached, dict) and len(cached.get("close") or []) >= MIN_OHLCV_BARS:
            hits[sym] = cached
        else:
            misses.append(sym)
    return hits, misses


def persist_bars_from_download(
    cache,
    ohlcv: pd.DataFrame,
    tickers: List[str],
    scan_date: str,
    session_tag: str,
    lookback_days: int,
    is_single: bool,
) -> Dict[str, Dict[str, Any]]:
    stored: Dict[str, Dict[str, Any]] = {}
    for t in tickers:
        bars = extract_ticker_bars_from_download(ohlcv, t, is_single and len(tickers) == 1)
        if bars:
            persist_ticker_bars(cache, t, scan_date, session_tag, lookback_days, bars)
            stored[t.upper()] = bars
    return stored


def _spy_payload_from_series(spy_close) -> Dict[str, Any]:
    if spy_close is None or len(spy_close) == 0:
        return {}
    idx = spy_close.index
    dates = [pd.Timestamp(ts).strftime("%Y-%m-%d") for ts in idx]
    vals = spy_close.tolist()
    return {"dates": dates, "close": [float(x) for x in vals]}


def _spy_series_from_payload(payload: Dict[str, Any]):
    if not payload or not payload.get("close"):
        return None
    idx = pd.to_datetime(payload.get("dates") or [])
    close = pd.Series(payload["close"], index=idx, dtype=float)
    return close.squeeze() if hasattr(close, "squeeze") else close


def get_cached_spy_close(
    cache,
    scan_date: str,
    session_tag: str,
    lookback_days: int,
    download_fn: Callable[[], Any],
) -> Any:
    cached = cache.get("screening_spy", scan_date, session_tag, str(lookback_days))
    if isinstance(cached, dict) and cached.get("close"):
        series = _spy_series_from_payload(cached)
        if series is not None and len(series):
            return series
    spy_close = download_fn()
    if spy_close is not None and len(spy_close):
        payload = _spy_payload_from_series(spy_close)
        if payload:
            cache.set(
                "screening_spy",
                scan_date,
                session_tag,
                str(lookback_days),
                data=payload,
                ttl=CacheConfig.SCREENING_PRICES,
            )
    return spy_close


def synthesize_ohlcv_frame(
    bars_by_ticker: Dict[str, Dict[str, Any]],
    tickers: List[str],
) -> Optional[pd.DataFrame]:
    """Build a group_by=ticker-like frame for the engine parse loop."""
    if not bars_by_ticker:
        return None
    if len(tickers) == 1:
        sym = tickers[0].upper()
        bars = bars_by_ticker.get(sym)
        if not bars:
            return None
        close, volume, high, low = bars_to_series(bars)
        return pd.DataFrame(
            {"Close": close, "Volume": volume, "High": high, "Low": low},
        )
    frames: Dict[str, pd.DataFrame] = {}
    for t in tickers:
        sym = t.upper()
        bars = bars_by_ticker.get(sym)
        if not bars:
            continue
        close, volume, high, low = bars_to_series(bars)
        frames[sym] = pd.DataFrame(
            {"Close": close, "Volume": volume, "High": high, "Low": low},
        )
    if not frames:
        return None
    return pd.concat(frames, axis=1, keys=list(frames.keys()))


def warm_chunk_ohlcv(
    tickers: List[str],
    scan_date: str,
    screening_config: Optional[Dict[str, Any]],
    download_fn: Callable[[List[str], datetime, datetime, bool], Optional[pd.DataFrame]],
    cache=None,
) -> int:
    """Download and cache OHLCV for one chunk. Returns count of tickers stored."""
    if not tickers:
        return 0
    cache = cache or get_cache()
    lookback = lookback_from_config(screening_config)
    session_tag = session_tag_for(scan_date, screening_config)
    start_date, end_date = date_range_for_scan(scan_date, lookback)
    is_single = len(tickers) == 1
    ohlcv = download_fn(tickers, start_date, end_date, is_single)
    if ohlcv is None or ohlcv.empty:
        return 0
    stored = persist_bars_from_download(
        cache, ohlcv, tickers, scan_date, session_tag, lookback, is_single,
    )
    chunk_key = chunk_cache_key(scan_date, session_tag, tickers)
    cache.set(
        "screening_prices",
        chunk_key,
        data={"ohlcv": ohlcv, "spy_close": None},
        ttl=CacheConfig.SCREENING_PRICES,
    )
    return len(stored)


SCREENING_CACHE_TYPES = (
    "screening_prices",
    "screening_ohlcv_ticker",
    "screening_spy",
)


def invalidate_screening_price_caches(cache) -> int:
    total = 0
    for dtype in SCREENING_CACHE_TYPES:
        total += cache.invalidate_type(dtype)
    return total
