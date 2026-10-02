"""Early Momentum lens — gates, 0–100 domain score, buckets, event flags.

Pure scoring functions. No Yahoo/Perplexity calls. Enrichment lives in
``early_momentum_enrich.py``. Config under ``screening.early_momentum``.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

STRATEGY = "early_momentum"
UNION_STRATEGY = "early_momentum_union"
EMBED_KEY = "_early_momentum"

EXCLUDED_SCAN_ALL_STRATEGIES = frozenset({
    STRATEGY,
    UNION_STRATEGY,
    "reversal_buildup",
    "reversal_all_union",
    "base_coil",
    "base_coil_union",
    "movers",
    "long_horizon",
})

MOMENTUM_BATCH_STRATEGIES = frozenset({STRATEGY, UNION_STRATEGY})

BUCKET_WATCH = "watch"
BUCKET_CONFIRMED = "confirmed"
BUCKET_HIGH_CONVICTION = "high_conviction"
BUCKET_EVENT = "event_driven"

NY_TZ = ZoneInfo("America/New_York")

DEFAULT_SPACE_BASKET = (
    "ASTS", "LUNR", "PL", "GSAT", "FLY", "RKLB", "RDW", "BKSY",
)

DEFAULT_EARLY_MOMENTUM_CONFIG: Dict[str, Any] = {
    "min_price": 5.0,
    "min_market_cap_usd": 300_000_000,
    "min_share_adv_50d": 500_000,
    "min_dollar_adv_50d_usd": 2_000_000,
    "float_warn_max": 50_000_000,
    "allowed_exchanges": ["NMS", "NGM", "NYQ", "NCM", "BTS", "PCX", "ASE"],
    "enrich_top_n": 80,
    "enrich_top_n_cap": 150,
    "hc_min_c": 6,
    "space_basket": list(DEFAULT_SPACE_BASKET),
    "peer_basket_version": "v1",
    "iwm_ticker": "IWM",
    "ufo_ticker": "UFO",
    "late_chase_ret_20d": 0.40,
    "late_chase_penalty_min": 3,
    "late_chase_penalty_max": 8,
    "binary_event_days": 5,
    "runway_months_veto": 12,
    "going_concern_cap": 49,
    "domain_shrink_factor": 0.85,
    "session_timezone": "America/New_York",
    "session_close_hour": 16,
    "session_close_minute": 0,
    "bucket_watch_min": 50,
    "bucket_confirmed_min": 60,
    "bucket_hc_min": 80,
    "domain_caps": {"A": 25, "B": 20, "C": 20, "D": 15, "E": 10, "F": 20},
    "pplx_budget_default": 100,
}


def get_momentum_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge screening.early_momentum over module defaults."""
    merged = dict(DEFAULT_EARLY_MOMENTUM_CONFIG)
    merged["domain_caps"] = dict(DEFAULT_EARLY_MOMENTUM_CONFIG["domain_caps"])
    merged["space_basket"] = list(DEFAULT_EARLY_MOMENTUM_CONFIG["space_basket"])
    src = config or {}
    block = None
    if isinstance(src.get("screening"), dict):
        block = src["screening"].get("early_momentum")
    if isinstance(src.get("early_momentum"), dict) and block is None:
        block = src["early_momentum"]
    if isinstance(block, dict):
        for key, value in block.items():
            if key == "domain_caps" and isinstance(value, dict):
                merged["domain_caps"].update(value)
            elif key == "space_basket" and isinstance(value, (list, tuple)):
                merged["space_basket"] = list(value)
            else:
                merged[key] = value
    return merged


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(min(hi, max(lo, value)))


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def is_regular_session_open(
    scan_date: str,
    now: Optional[datetime] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    cfg = cfg or DEFAULT_EARLY_MOMENTUM_CONFIG
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

    def _bar_date(ts: Any):
        try:
            stamp = pd.Timestamp(ts)
        except Exception:
            return None
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert(NY_TZ)
        return stamp.date()

    mask = [_bar_date(idx) is not None and _bar_date(idx) < cutoff for idx in series.index]
    truncated = series.loc[mask]
    return truncated if not truncated.empty else series.iloc[0:0]


def truncate_batch_data(batch_data: Dict[str, Dict[str, Any]], scan_date: str, cfg: Optional[Dict[str, Any]] = None) -> None:
    """In-place EOD truncation for domain A inputs."""
    for ticker, data in batch_data.items():
        for key in ("close", "high", "low", "volume"):
            if key in data and data[key] is not None:
                data[key] = truncate_price_series(data[key], scan_date, cfg=cfg)


def _exchange_ok(meta: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    allowed = {str(x).upper() for x in (cfg.get("allowed_exchanges") or [])}
    if not allowed:
        return True
    exch = str(meta.get("exchange") or meta.get("fullExchangeName") or "").upper()
    if not exch:
        return True
    return any(a in exch or exch in a for a in allowed)


def check_gates(
    *,
    price: Optional[float],
    meta: Optional[Dict[str, Any]],
    share_adv_50d: Optional[float],
    dollar_adv_50d: Optional[float],
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Hard gates — fail excludes from Momentum board."""
    cfg = cfg or DEFAULT_EARLY_MOMENTUM_CONFIG
    meta = meta or {}
    flags: Dict[str, Any] = {}
    passed = True
    reasons: List[str] = []

    p = _as_float(price)
    if p is None or p < float(cfg.get("min_price", 5.0)):
        passed = False
        reasons.append("price_gate")

    mcap = _as_float(meta.get("market_cap") or meta.get("marketCap"))
    if mcap is not None and mcap < float(cfg.get("min_market_cap_usd", 300_000_000)):
        passed = False
        reasons.append("mcap_gate")

    min_share = float(cfg.get("min_share_adv_50d", 500_000))
    if share_adv_50d is not None and share_adv_50d < min_share:
        passed = False
        reasons.append("share_adv_gate")

    min_dollar = float(cfg.get("min_dollar_adv_50d_usd", 2_000_000))
    if dollar_adv_50d is not None and dollar_adv_50d < min_dollar:
        passed = False
        reasons.append("dollar_adv_gate")

    if not _exchange_ok(meta, cfg):
        passed = False
        reasons.append("exchange_gate")

    float_shares = _as_float(meta.get("float_shares") or meta.get("floatShares"))
    if float_shares is not None and float_shares < float(cfg.get("float_warn_max", 50_000_000)):
        flags["low_float"] = True

    flags["gate_reasons"] = reasons
    return passed, flags


def _compute_share_adv(volume: pd.Series, window: int = 50) -> Optional[float]:
    if volume is None or len(volume) < window:
        return None
    return float(volume.iloc[-window:].mean())


def _ret_n(close: pd.Series, n: int) -> Optional[float]:
    if close is None or len(close) <= n:
        return None
    base = float(close.iloc[-n - 1])
    if base == 0:
        return None
    return float(close.iloc[-1] / base - 1.0)


def _excess_n(close: Optional[pd.Series], bench: Optional[pd.Series], n: int) -> Optional[float]:
    """Ticker return minus benchmark return over n sessions (date-aligned)."""
    if close is None or bench is None:
        return None
    joined = pd.concat([close.rename("s"), bench.rename("b")], axis=1).dropna()
    if len(joined) <= n:
        return None
    s0 = float(joined["s"].iloc[-n - 1])
    b0 = float(joined["b"].iloc[-n - 1])
    if s0 == 0 or b0 == 0:
        return None
    return float(joined["s"].iloc[-1] / s0 - 1.0) - float(joined["b"].iloc[-1] / b0 - 1.0)


def _pos_excess_pts(excess: Optional[float], cap_portion: float, scale: float = 0.15) -> float:
    if excess is None or excess <= 0 or cap_portion <= 0:
        return 0.0
    return min(cap_portion, (excess / scale) * cap_portion)


def _peer_mean_close(
    peer_closes: Optional[Mapping[str, pd.Series]],
    exclude: str,
) -> Optional[pd.Series]:
    series: List[pd.Series] = []
    for name, close in (peer_closes or {}).items():
        if str(name).upper() == str(exclude or "").upper():
            continue
        if close is None or len(close) < 22:
            continue
        series.append(close)
    if len(series) < 2:
        return None
    return pd.concat(series, axis=1).mean(axis=1)


def _breakout_confirmed(close: pd.Series, high: pd.Series) -> Tuple[bool, bool, bool]:
    """Returns (above_20d_high, reclaim_50ma, first_breakout_today)."""
    if close is None or len(close) < 51:
        return False, False, False
    last_close = float(close.iloc[-1])
    hi20 = float(high.iloc[-21:-1].max()) if len(high) >= 21 else float(high.iloc[:-1].max())
    above_20 = last_close > hi20
    sma50 = close.rolling(50).mean()
    if pd.isna(sma50.iloc[-1]):
        reclaim = False
    else:
        below_count = int((close.iloc[-11:-1] < sma50.iloc[-11:-1]).sum())
        reclaim = below_count >= 10 and last_close > float(sma50.iloc[-1])
    first_break = above_20 or reclaim
    return above_20, reclaim, first_break


def _domain_a(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    volume_surge_signal: Optional[float],
    cfg: Dict[str, Any],
    session_incomplete: bool,
) -> Tuple[float, Dict[str, Any]]:
    cap = float(cfg["domain_caps"]["A"])
    detail: Dict[str, Any] = {}
    if close is None or len(close) < 21:
        return 0.0, detail

    pts = 0.0
    rvol_pts = min(cap * 0.35, (_as_float(volume_surge_signal, 0.0) or 0.0) * cap * 0.35)
    pts += rvol_pts
    detail["rvol_pts"] = round(rvol_pts, 2)

    if len(close) >= 2 and len(high) >= 1 and len(low) >= 1:
        rng = float(high.iloc[-1] - low.iloc[-1])
        if rng > 0:
            close_strength = (float(close.iloc[-1]) - float(low.iloc[-1])) / rng
            cs_pts = min(cap * 0.20, close_strength * cap * 0.20)
            pts += cs_pts
            detail["close_strength_pts"] = round(cs_pts, 2)

    if len(close) >= 50:
        sma20 = close.rolling(20).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        if not pd.isna(sma20) and not pd.isna(sma50) and float(sma50) > 0:
            if float(close.iloc[-1]) > float(sma20) > float(sma50):
                ma_pts = cap * 0.20
                pts += ma_pts
                detail["ma_stack_pts"] = round(ma_pts, 2)

    above_20, reclaim, breakout = _breakout_confirmed(close, high)
    breakout_pts = 0.0
    if not session_incomplete and breakout:
        breakout_pts = cap * 0.25
        pts += breakout_pts
    detail["breakout_pts"] = round(breakout_pts, 2)
    detail["above_20d_high"] = above_20
    detail["reclaim_50ma"] = reclaim

    shrink = float(cfg.get("domain_shrink_factor", 0.85))
    if rvol_pts >= cap * 0.30 and breakout_pts >= cap * 0.20:
        pts = min(pts, cap * shrink)

    return _clip(pts, 0, cap), detail


def _domain_b(
    close: pd.Series,
    relative_strength_signal: Optional[float],
    cfg: Dict[str, Any],
    *,
    ticker: str = "",
    spy_close: Optional[pd.Series] = None,
    iwm_close: Optional[pd.Series] = None,
    ufo_close: Optional[pd.Series] = None,
    peer_closes: Optional[Mapping[str, pd.Series]] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Relative strength vs versioned SPY/IWM/UFO/space peers — not watchlist percentile."""
    cap = float(cfg["domain_caps"]["B"])
    rs_sig = _as_float(relative_strength_signal, 0.0) or 0.0
    space = {str(t).upper() for t in (cfg.get("space_basket") or DEFAULT_SPACE_BASKET)}
    sym = str(ticker or "").upper()
    is_space = bool(sym) and sym in space
    horizons = ((21, 0.40), (63, 0.35), (126, 0.25))
    rs_w, spy_w, iwm_w, space_w = 0.30, 0.30, 0.20, 0.20
    if not is_space:
        spy_w += space_w
        space_w = 0.0

    spy_ex_20 = _excess_n(close, spy_close, 20)
    if spy_ex_20 is not None:
        rs_pts = _pos_excess_pts(spy_ex_20, cap * rs_w, scale=0.08)
    else:
        rs_pts = rs_sig * cap * rs_w

    persist_spy = 0.0
    spy_excess: Dict[str, Optional[float]] = {}
    for n, w in horizons:
        ex = _excess_n(close, spy_close, n)
        spy_excess[f"{n}d"] = None if ex is None else round(ex, 4)
        persist_spy += _pos_excess_pts(ex, cap * spy_w * w)

    persist_iwm = 0.0
    iwm_excess: Dict[str, Optional[float]] = {}
    for n, w in horizons:
        ex = _excess_n(close, iwm_close, n)
        if ex is not None:
            iwm_excess[f"{n}d"] = round(ex, 4)
        persist_iwm += _pos_excess_pts(ex, cap * iwm_w * w)

    peer_pts = 0.0
    if is_space and space_w > 0:
        peer_mean = _peer_mean_close(peer_closes, sym)
        for n, w in horizons:
            ufo_ex = _excess_n(close, ufo_close, n)
            peer_ex = _excess_n(close, peer_mean, n)
            if ufo_ex is not None and peer_ex is not None:
                blended = 0.5 * ufo_ex + 0.5 * peer_ex
            else:
                blended = ufo_ex if ufo_ex is not None else peer_ex
            peer_pts += _pos_excess_pts(blended, cap * space_w * w)

    pts = rs_pts + persist_spy + persist_iwm + peer_pts
    shrink = float(cfg.get("domain_shrink_factor", 0.85))
    if rs_pts >= cap * 0.22 and persist_spy >= cap * 0.22:
        pts = min(pts, cap * shrink)

    return _clip(pts, 0, cap), {
        "rs_signal": rs_sig,
        "peer_basket_version": str(cfg.get("peer_basket_version") or "v1"),
        "is_space_peer": is_space,
        "spy_excess_20d": None if spy_ex_20 is None else round(spy_ex_20, 4),
        "spy_excess": spy_excess,
        "iwm_excess": iwm_excess,
        "rs_pts": round(rs_pts, 2),
        "persist_spy_pts": round(persist_spy, 2),
        "persist_iwm_pts": round(persist_iwm, 2),
        "ufo_peer_pts": round(peer_pts, 2),
    }


def _domain_c(
    estimate_momentum: Optional[float],
    analyst_count: Optional[int],
    asset_class: str,
    cfg: Dict[str, Any],
) -> Tuple[float, Dict[str, Any]]:
    cap = float(cfg["domain_caps"]["C"])
    if asset_class in {"etf", "commodity"}:
        return 0.0, {"skipped": "etf_commodity"}
    if analyst_count is not None and analyst_count < 2:
        return 0.0, {"skipped": "insufficient_analysts"}
    sig = _as_float(estimate_momentum)
    if sig is None:
        return 0.0, {"missing": True}
    return _clip(sig * cap, 0, cap), {"estimate_momentum": sig}


def _domain_d(
    enrich: Optional[Dict[str, Any]],
    cfg: Dict[str, Any],
    asset_class: str = "equity",
) -> Tuple[float, Dict[str, Any]]:
    cap = float(cfg["domain_caps"]["D"])
    if asset_class in {"etf", "commodity"}:
        return 0.0, {"skipped": "etf_commodity"}
    enrich = enrich or {}
    pts = 0.0
    detail: Dict[str, Any] = {}
    p_buys = int(enrich.get("form4_p_buy_count") or 0)
    if p_buys >= 2:
        pts += cap * 0.40
        detail["insider_cluster"] = True
    elif p_buys == 1:
        pts += cap * 0.20
    if enrich.get("verified_catalyst"):
        pts += cap * 0.45
        detail["verified_catalyst"] = True
    rating_cluster = enrich.get("rating_cluster_score")
    if _as_float(rating_cluster):
        pts += min(cap * 0.15, float(rating_cluster) * cap * 0.15)
    return _clip(pts, 0, cap), detail


def _domain_e(
    short_pressure: Optional[float],
    domain_a_pts: float,
    cfg: Dict[str, Any],
) -> Tuple[float, Dict[str, Any]]:
    cap = float(cfg["domain_caps"]["E"])
    si = _as_float(short_pressure)
    if si is None:
        return 0.0, {"missing": True}
    raw = si * cap
    a_confirms = domain_a_pts >= float(cfg["domain_caps"]["A"]) * 0.35
    if not a_confirms:
        raw = min(raw, 5.0)
    return _clip(raw, 0, cap), {"short_pressure": si, "capped_without_a": not a_confirms}


def _domain_f_deductions(
    enrich: Optional[Dict[str, Any]],
    index_spy_below_50: bool,
    index_stress: bool,
    cfg: Dict[str, Any],
) -> Tuple[float, Dict[str, Any]]:
    cap = float(cfg["domain_caps"]["F"])
    enrich = enrich or {}
    pts = 0.0
    detail: Dict[str, Any] = {}
    if enrich.get("critical_dilution"):
        pts += cap * 0.35
        detail["critical_dilution"] = True
    if enrich.get("atm_active"):
        pts += cap * 0.15
    runway = _as_float(enrich.get("runway_months"))
    if runway is not None and runway < float(cfg.get("runway_months_veto", 12)):
        pts += cap * 0.25
        detail["short_runway"] = True
    if enrich.get("binary_event_within_days") is not None:
        days = int(enrich["binary_event_within_days"])
        if days <= int(cfg.get("binary_event_days", 5)):
            pts += cap * 0.20
            detail["binary_event_days"] = days
    if index_spy_below_50 and index_stress:
        pts += cap * 0.15
        detail["index_stress"] = True
    return _clip(pts, 0, cap), detail


def _late_chase_penalty(close: pd.Series, breakout: bool, cfg: Dict[str, Any]) -> float:
    if not breakout:
        return 0.0
    ret20 = _ret_n(close, 20)
    if ret20 is None or ret20 <= float(cfg.get("late_chase_ret_20d", 0.40)):
        return 0.0
    lo = float(cfg.get("late_chase_penalty_min", 3))
    hi = float(cfg.get("late_chase_penalty_max", 8))
    excess = min(1.0, (ret20 - 0.40) / 0.30)
    return lo + excess * (hi - lo)


def _weekly_demote_bucket(bucket: str, weekly_alignment: Optional[float]) -> str:
    if weekly_alignment is None:
        return bucket
    if weekly_alignment < 0.35 and bucket == BUCKET_HIGH_CONVICTION:
        return BUCKET_CONFIRMED
    if weekly_alignment < 0.25 and bucket == BUCKET_CONFIRMED:
        return BUCKET_WATCH
    return bucket


def _assign_bucket(score: float, cfg: Dict[str, Any]) -> str:
    if score >= float(cfg.get("bucket_hc_min", 80)):
        return BUCKET_HIGH_CONVICTION
    if score >= float(cfg.get("bucket_confirmed_min", 65)):
        return BUCKET_CONFIRMED
    if score >= float(cfg.get("bucket_watch_min", 50)):
        return BUCKET_WATCH
    return "below_watch"


def _hc_eligible(domains: Dict[str, float], enrich: Optional[Dict[str, Any]], cfg: Dict[str, Any]) -> bool:
    c = domains.get("C", 0.0)
    hc_min_c = float(cfg.get("hc_min_c", 6))
    if c >= hc_min_c:
        return True
    if enrich and enrich.get("verified_catalyst"):
        return True
    return False


def score_ticker(
    close: Any,
    high: Any,
    low: Any,
    volume: Any,
    *,
    scan_date: str,
    signals: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
    asset_class: str = "equity",
    enrich: Optional[Dict[str, Any]] = None,
    index_trend: Optional[str] = None,
    index_stress: Optional[bool] = None,
    now: Optional[datetime] = None,
    config: Optional[Dict[str, Any]] = None,
    phase: str = "final",
    ticker: str = "",
    spy_close: Any = None,
    iwm_close: Any = None,
    ufo_close: Any = None,
    peer_closes: Optional[Mapping[str, pd.Series]] = None,
) -> Dict[str, Any]:
    """Score one ticker. ``phase='pre'`` skips enrich-dependent D/F fields."""
    cfg = get_momentum_config(config)
    signals = signals or {}
    meta = meta or {}
    enrich = enrich or {}

    close_s = close if isinstance(close, pd.Series) else pd.Series(close) if close is not None else None
    high_s = high if isinstance(high, pd.Series) else pd.Series(high) if high is not None else None
    low_s = low if isinstance(low, pd.Series) else pd.Series(low) if low is not None else None
    vol_s = volume if isinstance(volume, pd.Series) else pd.Series(volume) if volume is not None else None

    if close_s is not None:
        close_s = truncate_price_series(close_s, scan_date, now=now, cfg=cfg)
    if high_s is not None:
        high_s = truncate_price_series(high_s, scan_date, now=now, cfg=cfg)
    if low_s is not None:
        low_s = truncate_price_series(low_s, scan_date, now=now, cfg=cfg)
    if vol_s is not None:
        vol_s = truncate_price_series(vol_s, scan_date, now=now, cfg=cfg)

    session_incomplete = is_regular_session_open(scan_date, now=now, cfg=cfg)
    price = float(close_s.iloc[-1]) if close_s is not None and len(close_s) else None
    share_adv = _compute_share_adv(vol_s) if vol_s is not None else None
    dollar_adv = (share_adv * price) if share_adv is not None and price is not None else _as_float(meta.get("avg_dollar_volume_usd"))

    passed, gate_flags = check_gates(
        price=price,
        meta=meta,
        share_adv_50d=share_adv,
        dollar_adv_50d=dollar_adv,
        cfg=cfg,
    )

    a_pts, a_detail = _domain_a(
        close_s, high_s, low_s, vol_s,
        _as_float(signals.get("volume_surge")),
        cfg,
        session_incomplete,
    )
    spy_s = spy_close if isinstance(spy_close, pd.Series) else None
    iwm_s = iwm_close if isinstance(iwm_close, pd.Series) else None
    ufo_s = ufo_close if isinstance(ufo_close, pd.Series) else None
    if spy_s is not None:
        spy_s = truncate_price_series(spy_s, scan_date, now=now, cfg=cfg)
    if iwm_s is not None:
        iwm_s = truncate_price_series(iwm_s, scan_date, now=now, cfg=cfg)
    if ufo_s is not None:
        ufo_s = truncate_price_series(ufo_s, scan_date, now=now, cfg=cfg)
    b_pts, b_detail = _domain_b(
        close_s,
        _as_float(signals.get("relative_strength")),
        cfg,
        ticker=ticker,
        spy_close=spy_s,
        iwm_close=iwm_s,
        ufo_close=ufo_s,
        peer_closes=peer_closes,
    )
    analyst_n = _as_float(meta.get("analyst_count") or meta.get("numberOfAnalystOpinions"))
    c_pts, c_detail = _domain_c(
        _as_float(signals.get("estimate_momentum")),
        int(analyst_n) if analyst_n is not None else None,
        asset_class,
        cfg,
    )
    d_pts, d_detail = (0.0, {}) if phase == "pre" else _domain_d(enrich, cfg, asset_class)
    e_pts, e_detail = _domain_e(_as_float(signals.get("short_pressure")), a_pts, cfg)

    spy_below = index_trend == "below_50d" if index_trend else bool(enrich.get("index_spy_below_50"))
    stress = bool(index_stress) if index_stress is not None else bool(enrich.get("index_stress"))
    f_pts, f_detail = (0.0, {}) if phase == "pre" else _domain_f_deductions(enrich, spy_below, stress, cfg)

    domains = {"A": a_pts, "B": b_pts, "C": c_pts, "D": d_pts, "E": e_pts}
    raw = sum(domains.values()) - f_pts

    _, _, breakout = _breakout_confirmed(close_s, high_s) if close_s is not None and high_s is not None else (False, False, False)
    late_pen = _late_chase_penalty(close_s, breakout, cfg) if close_s is not None else 0.0
    raw -= late_pen
    coil_preceded = False
    if breakout and close_s is not None and high_s is not None and low_s is not None and vol_s is not None:
        from tradingagents.screening.base_coil import preceded_by_base
        coil_preceded = preceded_by_base(
            close_s, high_s, low_s, vol_s, spy_close=spy_s, config=config,
        )

    runway = _as_float(enrich.get("runway_months"))
    if enrich.get("going_concern") and runway is not None:
        if runway < float(cfg.get("runway_months_veto", 12)):
            raw = min(raw, float(cfg.get("going_concern_cap", 49)))

    score = _clip(raw, 0, 100)
    event_flag = None
    binary_days = enrich.get("binary_event_within_days")
    if phase != "pre" and binary_days is not None and int(binary_days) <= int(cfg.get("binary_event_days", 5)):
        event_flag = BUCKET_EVENT

    bucket = _assign_bucket(score, cfg)
    weekly = _as_float(signals.get("weekly_trend_alignment"))
    bucket = _weekly_demote_bucket(bucket, weekly)

    if event_flag == BUCKET_EVENT:
        bucket = BUCKET_EVENT
    elif bucket == BUCKET_HIGH_CONVICTION and not _hc_eligible(domains, enrich if phase != "pre" else None, cfg):
        bucket = BUCKET_CONFIRMED
    if asset_class in {"etf", "commodity"} and bucket == BUCKET_HIGH_CONVICTION:
        if not (enrich.get("verified_catalyst") if phase != "pre" else False):
            bucket = BUCKET_CONFIRMED

    asof = str(scan_date)[:10]
    if close_s is not None and len(close_s):
        try:
            asof = str(pd.Timestamp(close_s.index[-1]).date())
        except Exception:
            pass

    payload: Dict[str, Any] = {
        "strategy": STRATEGY,
        "score": round(score, 1),
        "score_pre": round(score, 1) if phase == "pre" else round(enrich.get("_score_pre", score), 1),
        "score_final": round(score, 1) if phase != "pre" else None,
        "bucket": bucket,
        "event_flag": event_flag,
        "passed_gates": passed,
        "domains": {k: round(v, 2) for k, v in domains.items()},
        "f_deduction": round(f_pts, 2),
        "domain_detail": {"A": a_detail, "B": b_detail, "C": c_detail, "D": d_detail, "E": e_detail, "F": f_detail},
        "gate_flags": gate_flags,
        "asof_date": asof,
        "data_quality": "enrich_contemporaneous_not_pit" if phase != "pre" else "pre_enrich",
        "session_incomplete": session_incomplete,
        "critical_dilution": bool(enrich.get("critical_dilution")),
        "is_space_peer": bool(b_detail.get("is_space_peer")),
        "peer_basket_version": b_detail.get("peer_basket_version"),
        "coil_preceded": bool(coil_preceded),
        "late_chase": late_pen > 0,
    }
    if phase == "pre":
        payload["score_pre"] = round(score, 1)
        payload["score_final"] = None
    else:
        payload["score_pre"] = round(enrich.get("_score_pre", score), 1)
        payload["score_final"] = round(score, 1)
        payload["score"] = round(score, 1)
    return payload


def rescore_with_enrich(
    pre_payload: Dict[str, Any],
    *,
    close: Any,
    high: Any,
    low: Any,
    volume: Any,
    scan_date: str,
    signals: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
    asset_class: str = "equity",
    enrich: Optional[Dict[str, Any]] = None,
    index_trend: Optional[str] = None,
    index_stress: Optional[bool] = None,
    config: Optional[Dict[str, Any]] = None,
    ticker: str = "",
    spy_close: Any = None,
    iwm_close: Any = None,
    ufo_close: Any = None,
    peer_closes: Optional[Mapping[str, pd.Series]] = None,
) -> Dict[str, Any]:
    enrich = dict(enrich or {})
    enrich["_score_pre"] = pre_payload.get("score_pre") or pre_payload.get("score")
    final = score_ticker(
        close, high, low, volume,
        scan_date=scan_date,
        signals=signals,
        meta=meta,
        asset_class=asset_class,
        enrich=enrich,
        index_trend=index_trend,
        index_stress=index_stress,
        config=config,
        ticker=ticker,
        spy_close=spy_close,
        iwm_close=iwm_close,
        ufo_close=ufo_close,
        peer_closes=peer_closes,
        phase="final",
    )
    return final


def momentum_sort_key(payload: Optional[Dict[str, Any]], ticker: str = "") -> Tuple:
    p = payload or {}
    if not p.get("passed_gates", True):
        return (1, 0.0, ticker)
    score = _as_float(p.get("score_final") or p.get("score"), 0.0) or 0.0
    return (0, -score, ticker)


def lift_momentum_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    signals = row.get("signals")
    payload = row.get("early_momentum")
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
        row["early_momentum"] = payload
        row["momentum_score"] = payload.get("score_final") or payload.get("score")
        row["momentum_bucket"] = payload.get("bucket")
        row["momentum_event_flag"] = payload.get("event_flag")
        row["momentum_domains"] = payload.get("domains")
        row["momentum_critical_dilution"] = bool(payload.get("critical_dilution"))
        row["momentum_is_space"] = bool(payload.get("is_space_peer"))
        row["passed_gates"] = payload.get("passed_gates", True)
    return row


def rank_momentum_rows(
    rows: Sequence[Dict[str, Any]],
    top_n: Optional[int] = None,
    *,
    require_gates: bool = True,
) -> List[Dict[str, Any]]:
    visible: List[Dict[str, Any]] = []
    for row in rows:
        lift_momentum_fields(row)
        if require_gates and row.get("passed_gates") is False:
            continue
        if isinstance(row.get("early_momentum"), dict) and row["early_momentum"].get("passed_gates") is False:
            continue
        visible.append(row)
    visible.sort(key=lambda r: momentum_sort_key(r.get("early_momentum"), str(r.get("ticker") or "")))
    if top_n is not None:
        return visible[: max(0, int(top_n))]
    return visible
