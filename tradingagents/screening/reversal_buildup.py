"""Reversal-buildup classifier — OHLCV-only setup labels and 0–100 score.

Pure functions. No yfinance calls. Config-driven thresholds live under
``screening.reversal_buildup`` in default_config; this module ships the same
defaults so unit tests can run without a full config tree.

Score formula weights remain 0.25×4. Phase gates are v1.2: hist-turn is
required for early/confirmed, volume without a turn stays watching, recency
is a penalty not a floor, and rank is phase-band then score. Overlay
penalties (risk / weak composite / lens conflict) do not mix opportunity
score into the reversal rank. Do not change constants without updating
tests/test_reversal_buildup.py.
"""
from __future__ import annotations

from datetime import datetime, time, date as date_cls
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

STRATEGY = "reversal_buildup"
EMBED_KEY = "_reversal_buildup"
DEFAULT_VISIBLE_PHASES = frozenset({"early_turn", "confirmed"})
HIDDEN_DEFAULT_PHASES = frozenset({"watching", "late", "none"})
# Ascending sort band: early_turn first, then confirmed, watching, late, none.
PHASE_SORT_RANK = {
    "early_turn": 0,
    "confirmed": 1,
    "watching": 2,
    "late": 3,
    "none": 4,
}
EXCLUDED_SCAN_ALL_STRATEGIES = frozenset({
    "reversal_buildup",
    "reversal_all_union",
    "early_momentum",
    "early_momentum_union",
    "movers",
    "long_horizon",
})

NY_TZ = ZoneInfo("America/New_York")
SESSION_CLOSE = time(16, 0)

DEFAULT_REVERSAL_CONFIG: Dict[str, Any] = {
    "rsi_period": 14,
    "rsi_lookback": 60,
    "rsi_oversold": 35.0,
    "rsi_overbought": 70.0,
    "rsi_late_long": 55.0,
    "rsi_late_short": 45.0,
    "range_lookback": 60,
    "range_long_max": 0.25,
    "range_short_min": 0.75,
    "range_mid": 0.50,
    "confirmed_pos60_long_max": 0.35,
    "confirmed_pos60_short_min": 0.65,
    "late_pos60_long_max": 0.40,
    "late_pos60_short_min": 0.60,
    "rsi_min_travel_pts": 5.0,
    "weekly_short_demote_slope": 0.0,
    "weekly_long_demote_slope": -5.0,
    "recency_sessions": 15,
    "min_bars": 60,
    "persist_sessions": 3,
    "volume_spike_mult": 5.0,
    "volume_share_window": 10,
    "volume_confirmed": 0.55,
    "sma_period": 20,
    "breakout_lookback": 10,
    "late_ret_atr": 1.0,
    "atr_period": 14,
    "rsi_recover_cap_pts": 15.0,
    "rsi_recover_peak_pts": 10.0,
    "rsi_recover_decay_pts": 12.0,
    "volume_bias_denom": 0.55,
    "penalty_late": 0.70,
    "penalty_failed_turn": 0.75,
    "penalty_weekly_against": 0.85,
    "penalty_watching": 0.80,
    "penalty_stale": 0.90,
    "penalty_market_dump_short": 0.85,
    "penalty_short_pressure": 0.90,
    "short_pressure_threshold": 0.6,
    "penalty_high_risk": 0.70,
    "high_risk_threshold": 70.0,
    "penalty_weak_composite": 0.75,
    "weak_composite_long": 20.0,
    "lens_conflict_macro_min": 65.0,
    "weights": {
        "dislocation_proximity": 0.25,
        "rsi_recovery": 0.25,
        "macd_hist_turn": 0.25,
        "volume_bias": 0.25,
    },
    "session_timezone": "America/New_York",
    "session_close_hour": 16,
    "session_close_minute": 0,
}


def get_reversal_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge screening.reversal_buildup over module defaults."""
    merged = dict(DEFAULT_REVERSAL_CONFIG)
    merged["weights"] = dict(DEFAULT_REVERSAL_CONFIG["weights"])
    src = config or {}
    block = src.get("screening", src).get("reversal_buildup") if isinstance(src.get("screening", src), dict) else None
    if isinstance(src.get("reversal_buildup"), dict) and block is None:
        block = src["reversal_buildup"]
    if isinstance(block, dict):
        for key, value in block.items():
            if key == "weights" and isinstance(value, dict):
                merged["weights"].update(value)
            else:
                merged[key] = value
    return merged


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(min(hi, max(lo, value)))


def _series(values: Any) -> Optional[pd.Series]:
    if values is None:
        return None
    if isinstance(values, pd.Series):
        return values.dropna()
    try:
        return pd.Series(values).dropna()
    except Exception:
        return None


def is_regular_session_open(scan_date: str, now: Optional[datetime] = None, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True when scan_date is today ET and the regular session has not yet closed (16:00 ET)."""
    cfg = cfg or DEFAULT_REVERSAL_CONFIG
    tz_name = str(cfg.get("session_timezone") or "America/New_York")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = NY_TZ
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    try:
        scan = datetime.strptime(str(scan_date)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    if scan != current.date():
        return False
    close_h = int(cfg.get("session_close_hour", 16))
    close_m = int(cfg.get("session_close_minute", 0))
    return current.time() < time(close_h, close_m)


def session_complete_tag(scan_date: str, now: Optional[datetime] = None, cfg: Optional[Dict[str, Any]] = None) -> str:
    return "open" if is_regular_session_open(scan_date, now=now, cfg=cfg) else "closed"


def _bar_date(ts: Any) -> Optional[date_cls]:
    try:
        stamp = pd.Timestamp(ts)
    except Exception:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(NY_TZ)
    return stamp.date()


def truncate_price_series(
    series: Optional[pd.Series],
    scan_date: str,
    now: Optional[datetime] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[pd.Series]:
    """Drop the incomplete current-session bar when the NYSE session is still open."""
    if series is None or not isinstance(series, pd.Series) or series.empty:
        return series
    if not is_regular_session_open(scan_date, now=now, cfg=cfg):
        return series
    try:
        cutoff = datetime.strptime(str(scan_date)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return series
    mask = [_bar_date(idx) is not None and _bar_date(idx) < cutoff for idx in series.index]
    truncated = series.loc[mask]
    return truncated if not truncated.empty else series.iloc[0:0]


def truncate_batch_data(
    batch_data: Dict[str, Dict[str, Any]],
    scan_date: str,
    now: Optional[datetime] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Truncate every OHLCV series and the shared SPY series in ``batch_data``."""
    if not batch_data:
        return batch_data
    spy_done = False
    truncated_spy = None
    for ticker, payload in list(batch_data.items()):
        if not isinstance(payload, dict):
            continue
        for key in ("close", "high", "low", "volume", "open"):
            if key in payload:
                payload[key] = truncate_price_series(payload.get(key), scan_date, now=now, cfg=cfg)
        if not spy_done and payload.get("spy_close") is not None:
            truncated_spy = truncate_price_series(payload.get("spy_close"), scan_date, now=now, cfg=cfg)
            spy_done = True
        if truncated_spy is not None:
            payload["spy_close"] = truncated_spy
    return batch_data


def rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    """SMA RSI (matches ScreeningEngine._compute_rsi)."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period)
    loss = loss.mean()
    rs = gain / loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def macd_histogram(close: pd.Series) -> pd.Series:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line - signal


def atr_series(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def weekly_rsi_slope(close: pd.Series, period: int = 14) -> Optional[float]:
    """Weekly RSI slope from resampling the same daily bars (no extra Yahoo call)."""
    if close is None or len(close) < period + 5:
        return None
    series = close.copy()
    if not isinstance(series.index, pd.DatetimeIndex):
        series.index = pd.to_datetime(series.index)
    weekly = series.resample("W-FRI").last().dropna()
    if len(weekly) < period + 2:
        return None
    wrsi = rsi_series(weekly, period=period).dropna()
    if len(wrsi) < 2:
        return None
    slope = float(wrsi.iloc[-1] - wrsi.iloc[-2])
    if np.isnan(slope):
        return None
    return slope


def _days_since_extreme(values: pd.Series, which: str) -> Optional[int]:
    window = values.dropna()
    if window.empty:
        return None
    if which == "min":
        loc = int(window.values.argmin())
    else:
        loc = int(window.values.argmax())
    return int(len(window) - 1 - loc)


def _up_volume_share(
    close: pd.Series,
    volume: pd.Series,
    window: int,
    side: str,
    spike_mult: float,
) -> Tuple[float, float, bool]:
    """Return (share_used, raw_share, spike_ignored)."""
    n = min(window, len(close) - 1)
    if n <= 0:
        return 0.0, 0.0, False
    px = close.iloc[-(n + 1):]
    vol = volume.reindex(px.index).fillna(0.0)
    rets = px.diff()
    dollar = (px * vol).iloc[1:]
    rets = rets.iloc[1:]
    avg20 = float(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else float(volume.mean() or 0.0)
    spike = bool(avg20 > 0 and float(vol.iloc[-1]) >= spike_mult * avg20)

    def _share(mask_spike: bool) -> float:
        use = dollar.copy()
        r = rets.copy()
        if mask_spike and spike and len(use) > 1:
            use = use.iloc[:-1]
            r = r.iloc[:-1]
        total = float(use.sum())
        if total <= 0:
            return 0.0
        if side == "long":
            tagged = float(use[r > 0].sum())
        else:
            tagged = float(use[r < 0].sum())
        return tagged / total

    raw = _share(False)
    used = _share(True)
    return used, raw, spike


def compute_features(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    spy_close: Optional[pd.Series] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = cfg or DEFAULT_REVERSAL_CONFIG
    close = close.astype(float)
    high = high.reindex(close.index).ffill().astype(float)
    low = low.reindex(close.index).ffill().astype(float)
    volume = volume.reindex(close.index).fillna(0.0).astype(float)

    lookback = int(cfg["range_lookback"])
    rsi_lb = int(cfg["rsi_lookback"])
    rsi_p = int(cfg["rsi_period"])
    atr_p = int(cfg["atr_period"])
    sma_p = int(cfg["sma_period"])
    vol_w = int(cfg["volume_share_window"])
    brk = int(cfg["breakout_lookback"])

    rsi = rsi_series(close, period=rsi_p)
    hist = macd_histogram(close)
    atr = atr_series(high, low, close, period=atr_p)
    sma20 = close.rolling(sma_p).mean()

    rsi_win = rsi.iloc[-rsi_lb:]
    rsi_min_60 = float(rsi_win.min()) if rsi_win.notna().any() else None
    rsi_max_60 = float(rsi_win.max()) if rsi_win.notna().any() else None
    rsi_now = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None
    days_since_rsi_min = _days_since_extreme(rsi_win, "min")
    days_since_rsi_max = _days_since_extreme(rsi_win, "max")

    low60 = low.iloc[-lookback:]
    high60 = high.iloc[-lookback:]
    range_low = float(low60.min())
    range_high = float(high60.max())
    rng = range_high - range_low
    last_close = float(close.iloc[-1])
    pos60 = ((last_close - range_low) / rng) if rng > 0 else 0.5
    days_since_range_low = _days_since_extreme(low60, "min")
    days_since_range_high = _days_since_extreme(high60, "max")
    pos60_min = None
    pos60_max = None
    if rng > 0:
        pos_path = (close.iloc[-lookback:] - range_low) / rng
        pos60_min = float(pos_path.min())
        pos60_max = float(pos_path.max())

    atr_now = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0
    atr_from_60d_low = ((last_close - range_low) / atr_now) if atr_now > 0 else None

    hist_now = float(hist.iloc[-1]) if pd.notna(hist.iloc[-1]) else 0.0
    hist_prev = float(hist.iloc[-2]) if len(hist) >= 2 and pd.notna(hist.iloc[-2]) else hist_now
    hist_rising = hist_now > hist_prev
    hist_rising_3 = False
    hist_falling_3 = False
    if len(hist) >= 4:
        h1, h2, h3 = float(hist.iloc[-3]), float(hist.iloc[-2]), hist_now
        hist_rising_3 = h1 < h2 < h3
        hist_falling_3 = h1 > h2 > h3
    prior3_min = float(hist.iloc[-4:-1].min()) if len(hist) >= 4 else hist_prev
    prior3_max = float(hist.iloc[-4:-1].max()) if len(hist) >= 4 else hist_prev

    rsi_rising_3 = False
    if rsi_now is not None and len(rsi) >= 3:
        r1, r2 = rsi.iloc[-3], rsi.iloc[-2]
        rsi_rising_3 = bool(pd.notna(r1) and pd.notna(r2) and float(r1) < float(r2) < rsi_now)

    rsi_falling_3 = False
    if rsi_now is not None and len(rsi) >= 3:
        r1, r2 = rsi.iloc[-3], rsi.iloc[-2]
        rsi_falling_3 = bool(pd.notna(r1) and pd.notna(r2) and float(r1) > float(r2) > rsi_now)

    reclaim_sma20 = bool(pd.notna(sma20.iloc[-1]) and last_close >= float(sma20.iloc[-1]))
    high10 = float(high.iloc[-(brk + 1):-1].max()) if len(high) > brk else last_close
    low10 = float(low.iloc[-(brk + 1):-1].min()) if len(low) > brk else last_close
    breakout_10d = last_close > high10
    breakdown_10d = last_close < low10

    up_share, up_share_raw, spike_ignored = _up_volume_share(
        close, volume, vol_w, "long", float(cfg["volume_spike_mult"])
    )
    down_share, down_share_raw, _ = _up_volume_share(
        close, volume, vol_w, "short", float(cfg["volume_spike_mult"])
    )
    vol5 = float(volume.iloc[-5:].mean()) if len(volume) >= 5 else float(volume.mean() or 0)
    vol20 = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else vol5
    vol5_vs_20 = (vol5 / vol20) if vol20 > 0 else None

    ret_5 = float(close.iloc[-1] / close.iloc[-6] - 1.0) if len(close) >= 6 else 0.0
    ret_20 = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 else 0.0
    ret_5d_atr = (ret_5 * last_close / atr_now) if atr_now > 0 else 0.0
    ret_20d_atr = (ret_20 * last_close / atr_now) if atr_now > 0 else 0.0

    failed_turn_long = False
    failed_turn_short = False
    if len(hist) >= 5 and not reclaim_sma20:
        # hist was rising off the low, now falling two sessions
        h = hist.iloc[-5:]
        rising_then = float(h.iloc[0]) < float(h.iloc[1]) < float(h.iloc[2])
        falling_now = float(h.iloc[-1]) < float(h.iloc[-2]) < float(h.iloc[-3])
        failed_turn_long = bool(rising_then and falling_now and hist_now < 0)
        falling_then = float(h.iloc[0]) > float(h.iloc[1]) > float(h.iloc[2])
        rising_now = float(h.iloc[-1]) > float(h.iloc[-2]) > float(h.iloc[-3])
        failed_turn_short = bool(falling_then and rising_now and hist_now > 0)

    w_slope = weekly_rsi_slope(close, period=rsi_p)

    vs_spy = None
    spy = _series(spy_close)
    if spy is not None and len(spy) >= lookback:
        spy_aligned = spy.reindex(close.index).ffill()
        if spy_aligned.notna().sum() >= lookback:
            s_low = float(spy_aligned.iloc[-lookback:].min())
            s_high = float(spy_aligned.iloc[-lookback:].max())
            s_rng = s_high - s_low
            spy_pos = ((float(spy_aligned.iloc[-1]) - s_low) / s_rng) if s_rng > 0 else 0.5
            if pos60 <= float(cfg["range_long_max"]) and spy_pos > 0.40:
                vs_spy = "idiosyncratic"
            elif pos60 <= float(cfg["range_long_max"]) and spy_pos <= float(cfg["range_long_max"]):
                vs_spy = "market_dump"
            elif pos60 >= float(cfg["range_short_min"]) and spy_pos < 0.60:
                vs_spy = "idiosyncratic"
            elif pos60 >= float(cfg["range_short_min"]) and spy_pos >= float(cfg["range_short_min"]):
                vs_spy = "market_dump"
            else:
                vs_spy = "mixed"

    rsi_recover_long = None
    rsi_recover_short = None
    if rsi_now is not None and rsi_min_60 is not None:
        rsi_recover_long = rsi_now - rsi_min_60
    if rsi_now is not None and rsi_max_60 is not None:
        rsi_recover_short = rsi_max_60 - rsi_now

    making_new_rsi_low = bool(
        rsi_now is not None and rsi_min_60 is not None and rsi_now <= rsi_min_60 + 1e-9
    )
    making_new_rsi_high = bool(
        rsi_now is not None and rsi_max_60 is not None and rsi_now >= rsi_max_60 - 1e-9
    )

    return {
        "rsi": rsi_now,
        "rsi_min_60": rsi_min_60,
        "rsi_max_60": rsi_max_60,
        "rsi_recover": rsi_recover_long,
        "rsi_recover_short": rsi_recover_short,
        "days_since_rsi_extreme_min": days_since_rsi_min,
        "days_since_rsi_extreme_max": days_since_rsi_max,
        "pos60": pos60,
        "pos60_min": pos60_min,
        "pos60_max": pos60_max,
        "atr_from_60d_low": atr_from_60d_low,
        "macd_hist": hist_now,
        "macd_hist_prev": hist_prev,
        "macd_rising": hist_rising,
        "macd_rising_3": hist_rising_3,
        "macd_falling_3": hist_falling_3,
        "macd_prior3_min": prior3_min,
        "macd_prior3_max": prior3_max,
        "rsi_rising_3": rsi_rising_3,
        "rsi_falling_3": rsi_falling_3,
        "up_vol_share_10d": up_share,
        "up_vol_share_10d_raw": up_share_raw,
        "down_vol_share_10d": down_share,
        "down_vol_share_10d_raw": down_share_raw,
        "volume_spike_ignored": spike_ignored,
        "vol5_vs_20": vol5_vs_20,
        "reclaim_sma20": reclaim_sma20,
        "breakout_10d": breakout_10d,
        "breakdown_10d": breakdown_10d,
        "ret_5d_atr": ret_5d_atr,
        "ret_20d_atr": ret_20d_atr,
        "weekly_rsi_slope": w_slope,
        "vs_spy": vs_spy,
        "failed_turn_long": failed_turn_long,
        "failed_turn_short": failed_turn_short,
        "making_new_rsi_low": making_new_rsi_low,
        "making_new_rsi_high": making_new_rsi_high,
        "range_low": range_low,
        "range_high": range_high,
        "close": last_close,
        "n_bars": int(len(close)),
        "days_since_range_low": days_since_range_low,
        "days_since_range_high": days_since_range_high,
    }


def _long_dislocation(feat: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    rsi_min = feat.get("rsi_min_60")
    pos_min = feat.get("pos60_min")
    if rsi_min is None or pos_min is None:
        return False
    return rsi_min <= float(cfg["rsi_oversold"]) and pos_min <= float(cfg["range_long_max"])


def _short_dislocation(feat: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    rsi_max = feat.get("rsi_max_60")
    pos_max = feat.get("pos60_max")
    if rsi_max is None or pos_max is None:
        return False
    return rsi_max >= float(cfg["rsi_overbought"]) and pos_max >= float(cfg["range_short_min"])


def _rsi_travel_pts(feat: Dict[str, Any], side: str) -> float:
    rsi = feat.get("rsi")
    if rsi is None:
        return 0.0
    if side == "short":
        rsi_max = feat.get("rsi_max_60")
        if rsi_max is None:
            return 0.0
        return float(rsi_max) - float(rsi)
    rsi_min = feat.get("rsi_min_60")
    if rsi_min is None:
        return 0.0
    return float(rsi) - float(rsi_min)


def _in_confirmed_zone(feat: Dict[str, Any], cfg: Dict[str, Any], side: str) -> bool:
    pos60 = _as_float(feat.get("pos60"), 0.5)
    if side == "short":
        return pos60 >= float(cfg.get("confirmed_pos60_short_min", 0.65))
    return pos60 <= float(cfg.get("confirmed_pos60_long_max", 0.35))


def _recency_ok(feat: Dict[str, Any], cfg: Dict[str, Any], side: str) -> bool:
    gate = int(cfg["recency_sessions"])
    if side == "long":
        rsi_d = feat.get("days_since_rsi_extreme_min")
        rng_d = feat.get("days_since_range_low")
    else:
        rsi_d = feat.get("days_since_rsi_extreme_max")
        rng_d = feat.get("days_since_range_high")
    if rsi_d is None or rng_d is None:
        return False
    return int(rsi_d) <= gate and int(rng_d) <= gate


def evaluate_phase(
    feat: Dict[str, Any],
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, List[str]]:
    """Ordered evaluator: none → late → confirmed → early_turn → watching.

    Confirmed requires a hist turn still on the setup side of zero *and* a
    volume/SMA confirm bit, while still near the 60d extreme. Volume without
    a hist turn stays watching. Recency is tagged, not a phase floor. Shorts
    with a rising weekly RSI, and longs with weekly slope below the demote
    threshold, cannot stay early/confirmed. Returns (side, phase, reasons).
    """
    cfg = cfg or DEFAULT_REVERSAL_CONFIG
    reasons: List[str] = []
    n_bars = int(feat.get("n_bars") or 0)
    if n_bars < int(cfg["min_bars"]):
        return "none", "none", ["insufficient_history"]

    long_d = _long_dislocation(feat, cfg)
    short_d = _short_dislocation(feat, cfg)
    if not long_d and not short_d:
        return "none", "none", ["no_dislocation"]

    # Prefer the side whose extreme is more recent when both fire.
    side = "long"
    if long_d and short_d:
        long_age = int(feat.get("days_since_rsi_extreme_min") or 10**9)
        short_age = int(feat.get("days_since_rsi_extreme_max") or 10**9)
        side = "long" if long_age <= short_age else "short"
    elif short_d:
        side = "short"

    rsi = feat.get("rsi")
    pos60 = _as_float(feat.get("pos60"), 0.5)
    ret20 = _as_float(feat.get("ret_20d_atr"), 0.0)
    late_pos60_long = float(cfg.get("late_pos60_long_max", cfg.get("range_mid", 0.50)))
    late_pos60_short = float(cfg.get("late_pos60_short_min", cfg.get("range_mid", 0.50)))

    late = False
    if side == "long":
        if rsi is not None and rsi >= float(cfg["rsi_late_long"]):
            late = True
            reasons.append("late_rsi")
        if ret20 > float(cfg["late_ret_atr"]):
            late = True
            reasons.append("late_stretched")
        if pos60 > late_pos60_long:
            late = True
            reasons.append("late_mid_band")
    else:
        if rsi is not None and rsi <= float(cfg["rsi_late_short"]):
            late = True
            reasons.append("late_rsi")
        if ret20 < -float(cfg["late_ret_atr"]):
            late = True
            reasons.append("late_stretched")
        if pos60 < late_pos60_short:
            late = True
            reasons.append("late_mid_band")
    if late:
        return side, "late", reasons

    recency = _recency_ok(feat, cfg, side)
    persist = int(cfg["persist_sessions"])
    vol_ok = float(cfg["volume_confirmed"])

    if side == "long":
        vol_flip = _as_float(feat.get("up_vol_share_10d")) >= vol_ok
        confirm_bits = bool(feat.get("reclaim_sma20") or vol_flip or feat.get("breakout_10d"))
        hist_turn = bool(feat.get("macd_rising")) and _as_float(feat.get("macd_hist")) < 0
        persist_ok = bool(feat.get("macd_rising_3") and feat.get("rsi_rising_3")) if persist >= 3 else hist_turn
        in_quartile = pos60 <= float(cfg["range_long_max"])
        if feat.get("reclaim_sma20"):
            reasons.append("reclaim_sma20")
        if vol_flip:
            reasons.append(f"up_vol_10d_{_as_float(feat.get('up_vol_share_10d')):.2f}")
        if feat.get("breakout_10d"):
            reasons.append("breakout_10d")
    else:
        vol_flip = _as_float(feat.get("down_vol_share_10d")) >= vol_ok
        confirm_bits = bool(vol_flip or feat.get("breakdown_10d"))
        hist_turn = bool(not feat.get("macd_rising")) and _as_float(feat.get("macd_hist")) > 0
        persist_ok = bool(feat.get("macd_falling_3") and feat.get("rsi_falling_3")) if persist >= 3 else hist_turn
        in_quartile = pos60 >= float(cfg["range_short_min"])
        if vol_flip:
            reasons.append(f"down_vol_10d_{_as_float(feat.get('down_vol_share_10d')):.2f}")
        if feat.get("breakdown_10d"):
            reasons.append("breakdown_10d")

    if long_d or short_d:
        rsi_now = feat.get("rsi")
        if side == "long" and rsi_now is not None and feat.get("rsi_min_60") is not None:
            reasons.append(
                f"rsi_recover_{feat['rsi_min_60']:.0f}_to_{rsi_now:.0f}"
            )
        elif side == "short" and rsi_now is not None and feat.get("rsi_max_60") is not None:
            reasons.append(
                f"rsi_fade_{feat['rsi_max_60']:.0f}_to_{rsi_now:.0f}"
            )
        if hist_turn:
            reasons.append("macd_hist_rising_neg" if side == "long" else "macd_hist_falling_pos")

    if not recency:
        reasons.append("stale_extreme")

    travel = _rsi_travel_pts(feat, side)
    thin_travel = travel < float(cfg.get("rsi_min_travel_pts", 5.0))
    # Persist is a score bonus (macd_rising_3 sleeve), not a phase gate.
    early_ok = hist_turn and in_quartile and not thin_travel

    if thin_travel and (confirm_bits or hist_turn):
        reasons.append("thin_rsi_travel")
        phase = "watching"
    elif confirm_bits and not hist_turn:
        reasons.append("volume_without_turn")
        phase = "watching"
    elif confirm_bits and hist_turn and _in_confirmed_zone(feat, cfg, side):
        phase = "confirmed"
    elif confirm_bits and hist_turn:
        reasons.append("left_setup_zone")
        phase = "watching"
    elif early_ok:
        phase = "early_turn"
        if not persist_ok:
            reasons.append("persist_pending")
    else:
        phase = "watching"
        if not reasons:
            reasons.append("dislocation_only")

    w_slope = feat.get("weekly_rsi_slope")
    short_demote = float(cfg.get("weekly_short_demote_slope", 0.0))
    long_demote = float(cfg.get("weekly_long_demote_slope", -5.0))
    if (
        side == "short"
        and phase in {"early_turn", "confirmed"}
        and w_slope is not None
        and float(w_slope) > short_demote
    ):
        reasons.append("weekly_against_hard")
        phase = "watching"
    elif (
        side == "long"
        and phase in {"early_turn", "confirmed"}
        and w_slope is not None
        and float(w_slope) < long_demote
    ):
        reasons.append("weekly_against_hard")
        phase = "watching"

    return side, phase, reasons


def _rsi_recovery_score(travel: float, cfg: Dict[str, Any]) -> float:
    """Peak around 8–12 RSI points, then decay. Linear cap is the fallback."""
    if travel <= 0:
        return 0.0
    peak = float(cfg.get("rsi_recover_peak_pts") or 0.0)
    decay = float(cfg.get("rsi_recover_decay_pts") or 0.0)
    if peak <= 0:
        cap = float(cfg.get("rsi_recover_cap_pts", 15.0))
        return _clip(100.0 * min(1.0, travel / max(cap, 1e-6)))
    if travel <= peak:
        return _clip(100.0 * travel / peak)
    if decay <= 0:
        return 100.0
    over = travel - peak
    return _clip(100.0 * max(0.0, 1.0 - over / decay))


def _component_scores(feat: Dict[str, Any], side: str, cfg: Dict[str, Any]) -> Dict[str, float]:
    pos60 = _as_float(feat.get("pos60"), 0.5)
    long_max = float(cfg["range_long_max"])
    short_min = float(cfg["range_short_min"])
    if side == "short":
        # Mirror of long `100 * (1 - pos60 / 0.25)` from the 0.75 band.
        prox = _clip(100.0 * (1.0 - (1.0 - pos60) / (1.0 - short_min)))
        rsi_now = feat.get("rsi")
        rsi_max = feat.get("rsi_max_60")
        if rsi_now is None or rsi_max is None or feat.get("making_new_rsi_high") or rsi_now >= rsi_max:
            rsi_rec = 0.0
        else:
            rsi_rec = _rsi_recovery_score(float(rsi_max) - float(rsi_now), cfg)
        hist = _as_float(feat.get("macd_hist"))
        hist_prev = _as_float(feat.get("macd_hist_prev"))
        macd = 0.0
        if hist < hist_prev and hist > 0 and hist_prev > 0:
            macd += 50.0
        if hist < _as_float(feat.get("macd_prior3_max"), hist_prev):
            macd += 25.0
        if feat.get("macd_falling_3"):
            macd += 25.0
        vol = _clip(100.0 * _as_float(feat.get("down_vol_share_10d")) / float(cfg["volume_bias_denom"]))
    else:
        prox = _clip(100.0 * (1.0 - pos60 / long_max))
        rsi_now = feat.get("rsi")
        rsi_min = feat.get("rsi_min_60")
        if rsi_now is None or rsi_min is None or feat.get("making_new_rsi_low") or rsi_now <= rsi_min:
            rsi_rec = 0.0
        else:
            rsi_rec = _rsi_recovery_score(float(rsi_now) - float(rsi_min), cfg)
        hist = _as_float(feat.get("macd_hist"))
        hist_prev = _as_float(feat.get("macd_hist_prev"))
        macd = 0.0
        if hist > hist_prev and hist < 0 and hist_prev < 0:
            macd += 50.0
        if hist > _as_float(feat.get("macd_prior3_min"), hist_prev):
            macd += 25.0
        if feat.get("macd_rising_3"):
            macd += 25.0
        vol = _clip(100.0 * _as_float(feat.get("up_vol_share_10d")) / float(cfg["volume_bias_denom"]))
    return {
        "dislocation_proximity": _clip(prox),
        "rsi_recovery": _clip(rsi_rec),
        "macd_hist_turn": _clip(macd),
        "volume_bias": _clip(vol),
    }


def compute_score(
    feat: Dict[str, Any],
    side: str,
    phase: str,
    cfg: Optional[Dict[str, Any]] = None,
    risk_components: Optional[Dict[str, Any]] = None,
    risk_score: Optional[float] = None,
) -> Tuple[float, Dict[str, float], List[str]]:
    cfg = cfg or DEFAULT_REVERSAL_CONFIG
    if side == "none" or phase == "none":
        return 0.0, {}, []
    weights = cfg.get("weights") or DEFAULT_REVERSAL_CONFIG["weights"]
    comps = _component_scores(feat, side, cfg)
    raw = 0.0
    for name, weight in weights.items():
        raw += _as_float(comps.get(name)) * float(weight)
    penalties: List[str] = []
    score = raw
    ret20 = _as_float(feat.get("ret_20d_atr"))
    late_ret = ret20 > float(cfg["late_ret_atr"]) if side == "long" else ret20 < -float(cfg["late_ret_atr"])
    if phase == "late" or late_ret:
        score *= float(cfg["penalty_late"])
        penalties.append("penalty_late")
    failed = bool(feat.get("failed_turn_long")) if side == "long" else bool(feat.get("failed_turn_short"))
    if failed:
        score *= float(cfg["penalty_failed_turn"])
        penalties.append("penalty_failed_turn")
    w_slope = feat.get("weekly_rsi_slope")
    if w_slope is not None:
        against = (side == "long" and w_slope < 0) or (side == "short" and w_slope > 0)
        if against:
            score *= float(cfg["penalty_weekly_against"])
            penalties.append("penalty_weekly_against")
    if side == "short" and isinstance(risk_components, dict):
        sp = risk_components.get("short_pressure")
        if sp is not None and _as_float(sp) >= float(cfg["short_pressure_threshold"]):
            score *= float(cfg["penalty_short_pressure"])
            penalties.append("penalty_short_pressure")
    if side == "short" and feat.get("vs_spy") == "market_dump":
        score *= float(cfg.get("penalty_market_dump_short", 0.85))
        penalties.append("penalty_market_dump_short")
    if phase == "watching":
        score *= float(cfg.get("penalty_watching", 0.80))
        penalties.append("penalty_watching")
    if not _recency_ok(feat, cfg, side):
        score *= float(cfg.get("penalty_stale", 0.90))
        penalties.append("penalty_stale")
    if risk_score is not None and _as_float(risk_score) >= float(cfg.get("high_risk_threshold", 70.0)):
        score *= float(cfg.get("penalty_high_risk", 0.70))
        penalties.append("penalty_high_risk")
    return round(_clip(score), 2), comps, penalties


def features_for_payload(feat: Dict[str, Any], side: str) -> Dict[str, Any]:
    rsi_days = feat.get("days_since_rsi_extreme_min") if side != "short" else feat.get("days_since_rsi_extreme_max")
    range_days = feat.get("days_since_range_low") if side != "short" else feat.get("days_since_range_high")
    failed = bool(feat.get("failed_turn_long")) if side != "short" else bool(feat.get("failed_turn_short"))
    out = {
        "rsi": None if feat.get("rsi") is None else round(float(feat["rsi"]), 2),
        "rsi_min_60": None if feat.get("rsi_min_60") is None else round(float(feat["rsi_min_60"]), 2),
        "rsi_max_60": None if feat.get("rsi_max_60") is None else round(float(feat["rsi_max_60"]), 2),
        "rsi_recover": None if feat.get("rsi_recover") is None else round(float(feat["rsi_recover"]), 2),
        "days_since_rsi_extreme": rsi_days,
        "days_since_range_extreme": range_days,
        "pos60": round(_as_float(feat.get("pos60")), 4),
        "atr_from_60d_low": None if feat.get("atr_from_60d_low") is None else round(float(feat["atr_from_60d_low"]), 3),
        "macd_hist": round(_as_float(feat.get("macd_hist")), 6),
        "macd_hist_prev": round(_as_float(feat.get("macd_hist_prev")), 6),
        "macd_rising": bool(feat.get("macd_rising")),
        "macd_rising_3": bool(feat.get("macd_rising_3")),
        "macd_falling_3": bool(feat.get("macd_falling_3")),
        "up_vol_share_10d": round(_as_float(feat.get("up_vol_share_10d")), 4),
        "down_vol_share_10d": round(_as_float(feat.get("down_vol_share_10d")), 4),
        "vol5_vs_20": None if feat.get("vol5_vs_20") is None else round(float(feat["vol5_vs_20"]), 3),
        "reclaim_sma20": bool(feat.get("reclaim_sma20")),
        "breakout_10d": bool(feat.get("breakout_10d")),
        "breakdown_10d": bool(feat.get("breakdown_10d")),
        "ret_5d_atr": round(_as_float(feat.get("ret_5d_atr")), 3),
        "ret_20d_atr": round(_as_float(feat.get("ret_20d_atr")), 3),
        "weekly_rsi_slope": None if feat.get("weekly_rsi_slope") is None else round(float(feat["weekly_rsi_slope"]), 3),
        "vs_spy": feat.get("vs_spy"),
        "failed_turn": failed,
    }
    return out


def apply_reversal_context(
    payload: Dict[str, Any],
    *,
    composite_score: Optional[float] = None,
    risk_score: Optional[float] = None,
    macro_fit: Optional[float] = None,
    direction: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Demote phase / rescale score using row overlays already on the result.

    Risk is applied in ``compute_score``; this pass covers weak-composite longs
    and short/bullish lens conflict. Mutates ``payload`` in place.
    """
    if not isinstance(payload, dict):
        return payload
    cfg = get_reversal_config(config)
    phase = str(payload.get("phase") or "none")
    side = str(payload.get("side") or "none")
    if phase == "none" or side == "none":
        return payload
    reasons = list(payload.get("reasons") or [])
    score = _as_float(payload.get("score"), 0.0)
    actionable = phase in {"early_turn", "confirmed"}

    if (
        risk_score is not None
        and _as_float(risk_score) >= float(cfg.get("high_risk_threshold", 70.0))
        and "penalty_high_risk" not in reasons
    ):
        score = round(_clip(score * float(cfg.get("penalty_high_risk", 0.70))), 2)
        reasons.append("penalty_high_risk")

    if (
        actionable
        and side == "long"
        and composite_score is not None
        and _as_float(composite_score) < float(cfg.get("weak_composite_long", 20.0))
    ):
        phase = "watching"
        if "weak_composite" not in reasons:
            reasons.append("weak_composite")
        score = round(_clip(score * float(cfg.get("penalty_weak_composite", 0.75))), 2)
        actionable = False

    dir_l = str(direction or "").lower()
    if (
        actionable
        and side == "short"
        and dir_l == "bullish"
        and macro_fit is not None
        and _as_float(macro_fit) >= float(cfg.get("lens_conflict_macro_min", 65.0))
    ):
        phase = "watching"
        if "lens_conflict" not in reasons:
            reasons.append("lens_conflict")

    payload["phase"] = phase
    payload["score"] = score
    payload["reasons"] = reasons[:12]
    return payload


def empty_payload(reason: str = "insufficient_history") -> Dict[str, Any]:
    return {
        "side": "none",
        "phase": "none",
        "score": 0.0,
        "reasons": [reason],
        "features": {},
    }


def score_ticker(
    close: Any,
    high: Any,
    low: Any,
    volume: Any,
    spy_close: Any = None,
    risk_components: Optional[Dict[str, Any]] = None,
    risk_score: Optional[float] = None,
    composite_score: Optional[float] = None,
    macro_fit: Optional[float] = None,
    direction: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute the reversal-buildup payload for one ticker."""
    cfg = get_reversal_config(config)
    close_s = _series(close)
    high_s = _series(high)
    low_s = _series(low)
    vol_s = _series(volume)
    if close_s is None or high_s is None or low_s is None or vol_s is None:
        return empty_payload("insufficient_history")
    if len(close_s) < int(cfg["min_bars"]):
        return empty_payload("insufficient_history")
    try:
        feat = compute_features(close_s, high_s, low_s, vol_s, spy_close=spy_close, cfg=cfg)
        side, phase, reasons = evaluate_phase(feat, cfg)
        score, _comps, penalties = compute_score(
            feat, side, phase, cfg=cfg, risk_components=risk_components, risk_score=risk_score
        )
        for p in penalties:
            if p not in reasons:
                reasons.append(p)
        if feat.get("vs_spy") == "idiosyncratic":
            reasons.append("idiosyncratic_vs_spy")
        if feat.get("failed_turn_long") or feat.get("failed_turn_short"):
            if "failed_turn" not in reasons:
                reasons.append("failed_turn")
        payload = {
            "side": side,
            "phase": phase,
            "score": score,
            "reasons": reasons[:12],
            "features": features_for_payload(feat, side),
        }
        if any(v is not None for v in (composite_score, macro_fit, direction)):
            apply_reversal_context(
                payload,
                composite_score=composite_score,
                macro_fit=macro_fit,
                direction=direction,
                config=config,
            )
        return payload
    except Exception:
        return empty_payload("compute_error")


def reversal_sort_key(payload: Optional[Dict[str, Any]], ticker: str) -> Tuple[int, float, int, str]:
    """Ascending sort key: phase band, then higher score, then fresher RSI extreme."""
    payload = payload or {}
    phase = str(payload.get("phase") or "none")
    band = int(PHASE_SORT_RANK.get(phase, PHASE_SORT_RANK["none"]))
    score = _as_float(payload.get("score"), 0.0)
    feats = payload.get("features") or {}
    days = feats.get("days_since_rsi_extreme")
    days_i = int(days) if days is not None else 10**9
    return (band, -score, days_i, str(ticker or "").upper())


def lift_reversal_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Copy ``signals._reversal_buildup`` onto top-level sort/filter fields."""
    signals = row.get("signals")
    payload = row.get("reversal_buildup")
    if not isinstance(payload, dict) and isinstance(signals, dict):
        raw = signals.get(EMBED_KEY)
        if isinstance(raw, dict):
            payload = raw
        elif isinstance(raw, str):
            import json
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                payload = None
    if isinstance(payload, dict):
        row["reversal_buildup"] = payload
        row["reversal_score"] = payload.get("score")
        row["reversal_phase"] = payload.get("phase")
        row["reversal_side"] = payload.get("side")
        row["reversal_reasons"] = list(payload.get("reasons") or [])
    return row


def rank_reversal_rows(
    rows: Sequence[Dict[str, Any]],
    top_n: Optional[int] = None,
    visible_phases: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Filter default-hidden phases, sort by phase band then score, then slice."""
    phases = set(visible_phases) if visible_phases is not None else set(DEFAULT_VISIBLE_PHASES)
    visible: List[Dict[str, Any]] = []
    for row in rows:
        lift_reversal_fields(row)
        phase = str(row.get("reversal_phase") or "none")
        if phase not in phases:
            continue
        visible.append(row)
    visible.sort(key=lambda r: reversal_sort_key(r.get("reversal_buildup"), str(r.get("ticker") or "")))
    if top_n is not None:
        return visible[: max(0, int(top_n))]
    return visible
