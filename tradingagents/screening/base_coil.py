"""Base coil lens — pre-break consolidation watchlist.

OHLCV only. A high score means the range is tight and the trend is intact.
It does not call direction. Names that have already left the range belong on
Early Momentum, not here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

STRATEGY = "base_coil"
UNION_STRATEGY = "base_coil_union"
EMBED_KEY = "_base_coil"

BASE_BATCH_STRATEGIES = frozenset({STRATEGY, UNION_STRATEGY})

DEFAULT_BASE_COIL_CONFIG: Dict[str, Any] = {
    "range_lookback": 20,
    "min_box_sessions": 8,
    "box_width_max_atr": 1.5,
    "box_lookback_cap": 40,
    "sma_window": 50,
    "sma_rise_lookback": 10,
    "excess_lookback": 20,
    "vol_surge_exclude": 1.5,
    "atr_fast": 5,
    "atr_slow": 20,
    "score_weights": {
        "squeeze": 35.0,
        "contraction": 25.0,
        "days": 20.0,
        "dryup": 20.0,
    },
}


def get_base_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged = dict(DEFAULT_BASE_COIL_CONFIG)
    merged["score_weights"] = dict(DEFAULT_BASE_COIL_CONFIG["score_weights"])
    src = config or {}
    block = None
    if isinstance(src.get("screening"), dict):
        block = src["screening"].get("base_coil")
    if isinstance(src.get("base_coil"), dict) and block is None:
        block = src["base_coil"]
    if isinstance(block, dict):
        for key, value in block.items():
            if key == "score_weights" and isinstance(value, dict):
                merged["score_weights"].update(value)
            else:
                merged[key] = value
    return merged


def _as_series(value: Any) -> Optional[pd.Series]:
    if value is None:
        return None
    if isinstance(value, pd.Series):
        return value.dropna()
    try:
        series = pd.Series(value).dropna()
    except (TypeError, ValueError):
        return None
    return series if len(series) else None


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat(
        [(high - low), (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int) -> Optional[float]:
    if len(close) < window + 1:
        return None
    tr = _true_range(high, low, close)
    value = tr.rolling(window).mean().iloc[-1]
    if pd.isna(value) or float(value) <= 0:
        return None
    return float(value)


def _bandwidth_squeeze(close: pd.Series) -> float:
    """1 when bands are pinched versus their own history, 0 when they are wide."""
    if len(close) < 21:
        return 0.0
    sma = close.rolling(20).mean()
    std = close.rolling(20).std()
    bandwidth = (2 * std / sma).replace([float("inf"), float("-inf")], pd.NA).dropna()
    if len(bandwidth) < 2:
        return 0.0
    current = float(bandwidth.iloc[-1])
    avg = float(bandwidth.mean())
    if avg <= 0 or pd.isna(current):
        return 0.0
    return float(min(max(1.0 - (current / avg), 0.0), 1.0))


def _excess_n(close: pd.Series, bench: Optional[pd.Series], n: int) -> Optional[float]:
    if bench is None or len(close) <= n or len(bench) <= n:
        return None
    joined = pd.concat([close.rename("s"), bench.rename("b")], axis=1).dropna()
    if len(joined) <= n:
        return None
    s0 = float(joined["s"].iloc[-n - 1])
    b0 = float(joined["b"].iloc[-n - 1])
    if s0 == 0 or b0 == 0:
        return None
    return float(joined["s"].iloc[-1] / s0 - 1.0) - float(joined["b"].iloc[-1] / b0 - 1.0)


def _days_in_box(
    high: pd.Series,
    low: pd.Series,
    atr20: float,
    *,
    min_sessions: int,
    max_atr: float,
    lookback_cap: int,
) -> int:
    """Longest recent suffix whose high-low width is within max_atr * ATR(20)."""
    if atr20 <= 0:
        return 0
    cap = min(len(high), int(lookback_cap))
    limit = float(max_atr) * float(atr20)
    best = 0
    for n in range(int(min_sessions), cap + 1):
        width = float(high.iloc[-n:].max() - low.iloc[-n:].min())
        if width <= limit:
            best = n
        else:
            break
    return best


def score_base(
    close: Any,
    high: Any,
    low: Any,
    volume: Any,
    *,
    spy_close: Any = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Score one ticker. ``on_board`` is false when a gate fails."""
    cfg = get_base_config(config)
    weights = cfg["score_weights"]
    close_s = _as_series(close)
    high_s = _as_series(high)
    low_s = _as_series(low)
    vol_s = _as_series(volume)
    spy_s = _as_series(spy_close)

    reasons: List[str] = []
    payload: Dict[str, Any] = {
        "strategy": STRATEGY,
        "on_board": False,
        "score": None,
        "days_in_box": 0,
        "squeeze": 0.0,
        "vol_ratio": None,
        "dist_to_high_pct": None,
        "trend_pass": False,
        "inside_range": False,
        "fail_reasons": reasons,
    }
    if close_s is None or high_s is None or low_s is None or vol_s is None:
        reasons.append("missing_ohlcv")
        return payload
    n = min(len(close_s), len(high_s), len(low_s), len(vol_s))
    close_s = close_s.iloc[-n:]
    high_s = high_s.iloc[-n:]
    low_s = low_s.iloc[-n:]
    vol_s = vol_s.iloc[-n:]

    lookback = int(cfg["range_lookback"])
    if len(close_s) <= lookback:
        reasons.append("short_history")
        return payload

    prior_high = float(high_s.iloc[-(lookback + 1):-1].max())
    prior_low = float(low_s.iloc[-(lookback + 1):-1].min())
    last = float(close_s.iloc[-1])
    inside = prior_low < last < prior_high
    payload["inside_range"] = inside
    if last:
        payload["dist_to_high_pct"] = round((prior_high - last) / last * 100.0, 2)
    if not inside:
        reasons.append("outside_range")

    sma_w = int(cfg["sma_window"])
    rise_n = int(cfg["sma_rise_lookback"])
    trend_ma = False
    if len(close_s) >= sma_w + rise_n:
        sma = close_s.rolling(sma_w).mean()
        now = sma.iloc[-1]
        prev = sma.iloc[-1 - rise_n]
        if not pd.isna(now) and not pd.isna(prev):
            trend_ma = float(last) > float(now) and float(now) > float(prev)
    excess = _excess_n(close_s, spy_s, int(cfg["excess_lookback"]))
    trend_pass = bool(trend_ma or (excess is not None and excess > 0))
    payload["trend_pass"] = trend_pass
    if not trend_pass:
        reasons.append("no_trend")

    atr_slow_n = int(cfg["atr_slow"])
    atr_fast_n = int(cfg["atr_fast"])
    atr20 = _atr(high_s, low_s, close_s, atr_slow_n)
    atr5 = _atr(high_s, low_s, close_s, atr_fast_n)
    days = 0
    if atr20 is None:
        reasons.append("no_atr")
    else:
        days = _days_in_box(
            high_s,
            low_s,
            atr20,
            min_sessions=int(cfg["min_box_sessions"]),
            max_atr=float(cfg["box_width_max_atr"]),
            lookback_cap=int(cfg["box_lookback_cap"]),
        )
    payload["days_in_box"] = days
    if days < int(cfg["min_box_sessions"]):
        reasons.append("box_too_short")

    vol_ratio = None
    if len(vol_s) >= 20 and float(vol_s.iloc[-20:].mean() or 0) > 0:
        vol_ratio = float(vol_s.iloc[-5:].mean() / vol_s.iloc[-20:].mean())
        payload["vol_ratio"] = round(vol_ratio, 3)
    else:
        reasons.append("no_volume")
    if vol_ratio is not None and vol_ratio > float(cfg["vol_surge_exclude"]):
        reasons.append("volume_surge")

    squeeze = _bandwidth_squeeze(close_s)
    payload["squeeze"] = round(squeeze, 4)

    on_board = not reasons
    payload["on_board"] = on_board
    if not on_board:
        return payload

    squeeze_pts = squeeze * float(weights["squeeze"])
    contraction_pts = 0.0
    if atr5 is not None and atr20:
        ratio = atr5 / atr20
        if ratio <= 0.7:
            contraction_pts = float(weights["contraction"])
        elif ratio < 1.3:
            contraction_pts = float(weights["contraction"]) * (1.3 - ratio) / 0.6
    span = max(1, int(cfg["box_lookback_cap"]) - int(cfg["min_box_sessions"]))
    days_pts = float(weights["days"]) * min(1.0, max(0.0, (days - int(cfg["min_box_sessions"])) / span))
    dry_pts = 0.0
    if vol_ratio is not None:
        if vol_ratio <= 0.6:
            dry_pts = float(weights["dryup"])
        else:
            dry_pts = float(weights["dryup"]) * max(0.0, (float(cfg["vol_surge_exclude"]) - vol_ratio) / (float(cfg["vol_surge_exclude"]) - 0.6))
    score = max(0.0, min(100.0, squeeze_pts + contraction_pts + days_pts + dry_pts))
    payload["score"] = round(score, 1)
    return payload


def preceded_by_base(
    close: Any,
    high: Any,
    low: Any,
    volume: Any,
    *,
    spy_close: Any = None,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when the bars before the last session were a qualified base."""
    close_s = _as_series(close)
    high_s = _as_series(high)
    low_s = _as_series(low)
    vol_s = _as_series(volume)
    if close_s is None or high_s is None or low_s is None or vol_s is None:
        return False
    if len(close_s) < 3:
        return False
    spy_s = _as_series(spy_close)
    spy_prefix = spy_s.iloc[:-1] if spy_s is not None and len(spy_s) > 1 else spy_s
    prior = score_base(
        close_s.iloc[:-1],
        high_s.iloc[:-1],
        low_s.iloc[:-1],
        vol_s.iloc[:-1],
        spy_close=spy_prefix,
        config=config,
    )
    return bool(prior.get("on_board"))


def base_sort_key(payload: Optional[Dict[str, Any]], ticker: str = "") -> Tuple:
    p = payload or {}
    if not p.get("on_board"):
        return (1, 0.0, ticker)
    score = float(p.get("score") or 0.0)
    return (0, -score, ticker)


def lift_base_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    signals = row.get("signals")
    payload = row.get("base_coil")
    if not isinstance(payload, dict) and isinstance(signals, dict):
        raw = signals.get(EMBED_KEY)
        if isinstance(raw, dict):
            payload = raw
    if isinstance(payload, dict):
        row["base_coil"] = payload
        row["base_score"] = payload.get("score")
        row["base_days"] = payload.get("days_in_box")
        row["base_squeeze"] = payload.get("squeeze")
        row["base_vol_ratio"] = payload.get("vol_ratio")
        row["base_dist_to_high_pct"] = payload.get("dist_to_high_pct")
        row["base_trend_pass"] = payload.get("trend_pass")
        row["base_on_board"] = payload.get("on_board")
    return row


def rank_base_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    visible: List[Dict[str, Any]] = []
    for row in rows:
        lift_base_fields(row)
        payload = row.get("base_coil") or {}
        if payload.get("on_board") is False:
            continue
        visible.append(row)
    visible.sort(key=lambda r: base_sort_key(r.get("base_coil"), str(r.get("ticker") or "")))
    return visible
