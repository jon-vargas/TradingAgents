"""Bounded enrich pass for Early Momentum lens (Form 4, dilution, catalysts).

Never called inside the full-universe OHLCV loop. Uses dedicated PPLX budget
counter — not analysis ``perplexity_budget``.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from tradingagents.screening.early_momentum import (
    DEFAULT_SPACE_BASKET,
    get_momentum_config,
    rescore_with_enrich,
)

logger = logging.getLogger(__name__)

_pplx_budget = threading.local()


def reset_pplx_budget(limit: Optional[int] = None) -> None:
    _pplx_budget.limit = int(limit) if limit is not None else None
    _pplx_budget.used = 0


def pplx_budget_used() -> int:
    return int(getattr(_pplx_budget, "used", 0) or 0)


def _consume_pplx(n: int = 1) -> bool:
    used = int(getattr(_pplx_budget, "used", 0) or 0)
    limit = getattr(_pplx_budget, "limit", None)
    if limit is not None and used + n > int(limit):
        return False
    _pplx_budget.used = used + n
    return True


def _cache_key(ticker: str, asof_date: str) -> str:
    return f"{ticker.upper()}:{asof_date}"


def _coalesce_series(primary: Any, fallback: Any) -> Any:
    """Return first non-None OHLCV/benchmark series without pandas truthiness checks."""
    if primary is not None:
        return primary
    return fallback


def _coalesce_mapping(primary: Any, fallback: Any) -> Any:
    if primary is not None:
        return primary
    return fallback


_DILUTION_KEYS = (
    "s-3", "form s-3", "atm offering", "at-the-market", "at the market",
    "dilutive", "convertible note", "equity offering", "registered direct",
    "warrant offering",
)
_ATM_KEYS = ("atm offering", "at-the-market", "at the market")
_GC_KEYS = ("going concern", "substantial doubt")
_STRONG_CATALYST_PHRASES = (
    "contract award",
    "awarded a contract",
    "awarded contract",
    "won a contract",
    "won contract",
    "government contract",
    "definitive agreement",
    "partnership agreement",
    "strategic partnership",
    "fda approval",
    "phase 3 trial",
    "mission success",
    "commercial launch agreement",
)
_CATALYST_WEAK_KEYS = ("contract", "award", "partnership", "mission")
_CATALYST_FILING_MARKERS = ("8-k", "8k", "form 8-k", "press release", "sec filing")


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _runway_months_from_info(info: Dict[str, Any]) -> Optional[float]:
    """TTM operating cash burn → months of cash. Neutral when cash-flow positive."""
    cash = _as_float(info.get("totalCash") or info.get("cash"))
    ocf = _as_float(info.get("operatingCashflow") or info.get("freeCashflow"))
    if cash is None or ocf is None or ocf >= 0 or cash < 0:
        return None
    burn = abs(ocf)
    if burn <= 0:
        return None
    return round((cash / burn) * 12.0, 1)


def _going_concern_from_info(info: Dict[str, Any], runway_months: Optional[float]) -> bool:
    if runway_months is None:
        return False
    if runway_months < 6:
        return True
    cr = _as_float(info.get("currentRatio"))
    return runway_months < 12 and cr is not None and cr < 1.0


def _verified_catalyst_from_text(blob: str) -> bool:
    """Require filing-backed or strong phrase catalysts — not generic product launches."""
    if any(phrase in blob for phrase in _STRONG_CATALYST_PHRASES):
        return True
    has_filing = any(marker in blob for marker in _CATALYST_FILING_MARKERS)
    has_weak = any(key in blob for key in _CATALYST_WEAK_KEYS)
    if not (has_filing and has_weak):
        return False
    if "launch" in blob and not any(
        token in blob for token in ("contract", "mission", "satellite", "rocket", "vehicle")
    ):
        return False
    return True


def _parse_pplx_flags(text: str) -> Dict[str, Any]:
    blob = (text or "").lower()
    return {
        "critical_dilution": any(k in blob for k in _DILUTION_KEYS),
        "atm_active": any(k in blob for k in _ATM_KEYS),
        "going_concern_text": any(k in blob for k in _GC_KEYS),
        "verified_catalyst": _verified_catalyst_from_text(blob),
    }


def _disk_enrich_get(ticker: str, asof_date: str) -> Optional[Dict[str, Any]]:
    try:
        from tradingagents.dataflows.cache import get_cache
        cached = get_cache().get("early_momentum_enrich", f"{ticker.upper()}:{asof_date}")
        return dict(cached) if isinstance(cached, dict) else None
    except Exception:
        return None


def _disk_enrich_set(ticker: str, asof_date: str, payload: Dict[str, Any]) -> None:
    try:
        from tradingagents.dataflows.cache import get_cache
        get_cache().set("early_momentum_enrich", f"{ticker.upper()}:{asof_date}", data=dict(payload))
    except Exception:
        pass


def _filter_form4_p_buys(insider_data: Dict[str, Any]) -> int:
    """Count open-market P-code purchases; exclude RSU/tax/auto-sale."""
    txs = insider_data.get("transactions") or insider_data.get("raw_transactions") or []
    count = 0
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        code = str(tx.get("transaction_code") or tx.get("code") or "").upper()
        text = str(tx.get("text") or tx.get("transaction") or "").lower()
        if code == "P" or ("purchase" in text and not any(k in text for k in ("rsu", "tax", "automatic", "award"))):
            count += 1
    buy_count = int(insider_data.get("p_buy_count") or 0)
    if buy_count:
        return buy_count
    return count


def enrich_ticker(
    ticker: str,
    *,
    asof_date: str,
    cache: Optional[Dict[str, Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
    allow_pplx: bool = True,
    asset_class: str = "equity",
) -> Dict[str, Any]:
    """Fetch sparse enrich fields for one ticker. Neutral on miss."""
    cfg = get_momentum_config(config)
    sym = ticker.upper()
    key = _cache_key(sym, asof_date)
    cache = cache if cache is not None else {}
    if key in cache:
        return dict(cache[key])
    disk = _disk_enrich_get(sym, asof_date)
    if disk:
        cache[key] = dict(disk)
        return dict(disk)

    result: Dict[str, Any] = {
        "ticker": sym,
        "asof_date": asof_date,
        "form4_p_buy_count": 0,
        "critical_dilution": False,
        "atm_active": False,
        "runway_months": None,
        "going_concern": False,
        "verified_catalyst": False,
        "binary_event_within_days": None,
        "rating_cluster_score": 0.0,
        "fetched_at": datetime.now().isoformat(),
    }

    is_etf = asset_class in {"etf", "commodity"}
    if not is_etf:
        try:
            from tradingagents.dataflows.yfinance_extended import (
                get_earnings_profile,
                get_estimate_revisions,
                get_insider_net_buy,
                get_rating_changes,
                get_ticker_info,
            )

            insider = get_insider_net_buy(sym, lookback_days=60)
            result["form4_p_buy_count"] = _filter_form4_p_buys(insider)

            ratings = get_rating_changes(sym)
            upgrades = int(ratings.get("upgrade_count") or 0)
            if upgrades >= 2:
                result["rating_cluster_score"] = min(1.0, upgrades / 3.0)

            revisions = get_estimate_revisions(sym)
            analyst_n = int(revisions.get("analyst_count") or revisions.get("number_of_analysts") or 0)
            result["analyst_count"] = analyst_n

            earnings = get_earnings_profile(sym)
            days = earnings.get("days_until_earnings")
            binary_days = int(cfg.get("binary_event_days", 5))
            if days is not None:
                try:
                    days_i = int(days)
                    if 0 <= days_i <= binary_days:
                        result["binary_event_within_days"] = days_i
                except (TypeError, ValueError):
                    pass

            info = get_ticker_info(sym) or {}
            runway = _runway_months_from_info(info)
            result["runway_months"] = runway
            result["going_concern"] = _going_concern_from_info(info, runway)
        except Exception as exc:
            logger.debug("Yahoo enrich failed for %s: %s", sym, exc)

    if allow_pplx and not is_etf and _consume_pplx(1):
        try:
            from tradingagents.dataflows.perplexity_api import get_catalyst_pipeline
            flags = _parse_pplx_flags(str(get_catalyst_pipeline(sym) or ""))
            result["critical_dilution"] = bool(flags.get("critical_dilution"))
            result["atm_active"] = bool(flags.get("atm_active"))
            if flags.get("going_concern_text"):
                result["going_concern"] = True
            if flags.get("verified_catalyst"):
                result["verified_catalyst"] = True
        except Exception as exc:
            logger.debug("PPLX dilution/catalyst enrich failed for %s: %s", sym, exc)

    cache[key] = dict(result)
    _disk_enrich_set(sym, asof_date, result)
    return result


def select_enrich_tickers(
    rows: Sequence[Dict[str, Any]],
    *,
    top_n: int,
    space_basket: Optional[Sequence[str]] = None,
) -> List[str]:
    """Top N by score_pre plus force-included space basket."""
    space = {t.upper() for t in (space_basket or DEFAULT_SPACE_BASKET)}
    scored: List[tuple] = []
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        sig = row.get("signals") or {}
        payload = sig.get("_early_momentum") if isinstance(sig, dict) else None
        if not isinstance(payload, dict):
            payload = row.get("early_momentum") or {}
        pre = float(payload.get("score_pre") or payload.get("score") or 0.0)
        if payload.get("passed_gates") is False:
            continue
        scored.append((pre, ticker))
    scored.sort(key=lambda x: (-x[0], x[1]))
    selected: List[str] = [t for _, t in scored[: max(0, int(top_n))]]
    for t in space:
        if t not in selected:
            selected.append(t)
    return list(dict.fromkeys(selected))


def enrich_and_finalize_rows(
    rows: List[Dict[str, Any]],
    *,
    scan_date: str,
    batch_data: Optional[Dict[str, Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
    cache: Optional[Dict[str, Dict[str, Any]]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """Two-pass finalize: enrich top N + space basket, recompute score_final."""
    cfg = get_momentum_config(config)
    top_n = min(int(cfg.get("enrich_top_n_cap", 150)), int(cfg.get("enrich_top_n", 80)))
    space = cfg.get("space_basket") or list(DEFAULT_SPACE_BASKET)
    pplx_limit = top_n + len(space)
    reset_pplx_budget(pplx_limit)

    tickers = select_enrich_tickers(rows, top_n=top_n, space_basket=space)
    asset_by_ticker: Dict[str, str] = {}
    for row in rows:
        t = str(row.get("ticker") or "").upper()
        if t:
            asset_by_ticker[t] = str(row.get("asset_class") or "equity")
    enrich_map: Dict[str, Dict[str, Any]] = {}
    for i, ticker in enumerate(tickers):
        if cancel_check and cancel_check():
            raise RuntimeError("early_momentum_enrich_cancelled")
        enrich_map[ticker] = enrich_ticker(
            ticker,
            asof_date=str(scan_date)[:10],
            cache=cache,
            config=config,
            allow_pplx=True,
            asset_class=asset_by_ticker.get(ticker, "equity"),
        )
        if progress_cb:
            progress_cb({"phase": "enrich", "current": i + 1, "total": len(tickers), "ticker": ticker})

    out: List[Dict[str, Any]] = []
    gated_out = 0
    for row in rows:
        row = dict(row)
        ticker = str(row.get("ticker") or "").upper()
        signals = row.get("signals") or {}
        if isinstance(signals, str):
            import json
            try:
                signals = json.loads(signals)
            except Exception:
                signals = {}
        pre = signals.get("_early_momentum") if isinstance(signals, dict) else None
        if not isinstance(pre, dict):
            pre = row.get("early_momentum") or {}

        data = (batch_data or {}).get(ticker) or {}
        benches = (batch_data or {}).get("_benchmarks") or {}
        peer_closes = _coalesce_mapping(benches.get("peer_closes"), data.get("peer_closes"))
        enrich = enrich_map.get(ticker, {})
        meta = {}
        sm = signals.get("_screening_meta") if isinstance(signals, dict) else {}
        if isinstance(sm, dict):
            meta = dict(sm)

        asset_class = str(row.get("asset_class") or meta.get("asset_class") or "equity")
        final = rescore_with_enrich(
            pre,
            close=data.get("close"),
            high=data.get("high"),
            low=data.get("low"),
            volume=data.get("volume"),
            scan_date=scan_date,
            signals=signals if isinstance(signals, dict) else {},
            meta=meta,
            asset_class=asset_class,
            enrich=enrich,
            index_trend=row.get("index_trend"),
            index_stress=bool(row.get("index_stress")),
            config=config,
            ticker=ticker,
            spy_close=_coalesce_series(data.get("spy_close"), benches.get("spy_close")),
            iwm_close=_coalesce_series(data.get("iwm_close"), benches.get("iwm_close")),
            ufo_close=_coalesce_series(data.get("ufo_close"), benches.get("ufo_close")),
            peer_closes=peer_closes,
        )
        if isinstance(signals, dict):
            signals = dict(signals)
            signals["_early_momentum"] = final
            row["signals"] = signals
        row["early_momentum"] = final
        if final.get("passed_gates") is False:
            gated_out += 1
        out.append(row)

    if progress_cb:
        progress_cb({"phase": "enrich_done", "enriched": len(tickers), "gated_out_count": gated_out, "pplx_used": pplx_budget_used()})
    return out
