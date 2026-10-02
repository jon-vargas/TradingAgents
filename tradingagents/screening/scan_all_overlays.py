"""Post-processing overlays for Scan All consolidated ranking."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from tradingagents.screening.discovery import (
    HUNTER_PRESETS,
    canonical_preset_name,
    is_implausible_mega_identity,
)
from tradingagents.screening.opportunity_score import compute_opportunity_score

IMPLAUSIBLE_MEGA_OPP_MULTIPLIER = 0.15


def _is_etf_row(row: Dict[str, Any]) -> bool:
    asset = str(row.get("asset_class") or "").strip().lower()
    if asset == "etf":
        return True
    preset = str(row.get("resolved_preset") or row.get("preset") or "").strip().lower()
    return preset == "etf_technical"


def _passes_headline_floor(
    row: Dict[str, Any],
    *,
    min_coverage_pct: float,
    min_dollar_adv_usd: float,
) -> bool:
    if row.get("liquidity_pass") is False:
        return False
    adv = row.get("avg_dollar_volume_usd")
    try:
        adv_val = float(adv) if adv is not None else 0.0
    except (TypeError, ValueError):
        adv_val = 0.0
    if min_dollar_adv_usd > 0 and adv_val < min_dollar_adv_usd:
        return False
    if _is_etf_row(row):
        return True
    cov = row.get("signal_coverage_pct")
    if cov is None:
        return True
    try:
        return float(cov) >= min_coverage_pct
    except (TypeError, ValueError):
        return True


def headline_top_opportunities(
    rows: List[Dict[str, Any]],
    top: int,
    headline_cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Rotate cap bands (and optionally ETFs) so one universe cannot own the table.

    Prefers names that clear a coverage/ADV floor, then backfills from the same
    band so a thin quality set cannot leave a sleeve empty.
    """
    limit = max(0, int(top or 0))
    if limit <= 0 or not rows:
        return []
    cfg = headline_cfg or {}
    min_cov = float(cfg.get("min_coverage_pct", 50) or 0)
    min_adv = float(cfg.get("min_dollar_adv_usd", 5_000_000) or 0)
    include_etf = bool(cfg.get("include_etf_slot", True))
    etf_max = max(0, int(cfg.get("etf_max", 2) or 0))

    bands: Dict[str, List[Dict[str, Any]]] = {
        "mega_large": [],
        "mid": [],
        "small_micro": [],
        "etf": [],
    }
    unknown: List[Dict[str, Any]] = []
    for row in rows:
        if include_etf and _is_etf_row(row):
            bands["etf"].append(row)
            continue
        tier = str(row.get("market_cap_tier") or "unknown").lower()
        if tier in {"mega", "large"}:
            bands["mega_large"].append(row)
        elif tier == "mid":
            bands["mid"].append(row)
        elif tier in {"small", "micro"}:
            bands["small_micro"].append(row)
        else:
            unknown.append(row)

    order = ["mega_large", "mid", "small_micro"]
    if include_etf and bands["etf"]:
        order.append("etf")

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    cursors = {band: 0 for band in order}

    def _take(band: str, *, require_floor: bool, restart: bool = False) -> Optional[Dict[str, Any]]:
        bucket = bands[band]
        i = 0 if restart else cursors[band]
        while i < len(bucket):
            row = bucket[i]
            ticker = str(row.get("ticker") or "").upper()
            cursors[band] = i + 1
            i += 1
            if not ticker or ticker in seen:
                continue
            if require_floor and not _passes_headline_floor(
                row, min_coverage_pct=min_cov, min_dollar_adv_usd=min_adv
            ):
                continue
            return row
        return None

    etf_used = 0
    while len(out) < limit:
        progressed = False
        for band in order:
            if band == "etf" and etf_used >= etf_max:
                continue
            if band == "etf":
                row = _take(band, require_floor=True)
            else:
                row = _take(band, require_floor=True) or _take(
                    band, require_floor=False, restart=True
                )
            if row is None:
                continue
            out.append(row)
            seen.add(str(row.get("ticker") or "").upper())
            if band == "etf":
                etf_used += 1
            progressed = True
            if len(out) >= limit:
                break
        if not progressed:
            break
    for row in unknown:
        if len(out) >= limit:
            break
        ticker = str(row.get("ticker") or "").upper()
        if ticker and ticker not in seen:
            out.append(row)
            seen.add(ticker)
    return out


def apply_scan_all_overlays(
    rows: List[Dict[str, Any]],
    *,
    screening_config: dict,
    db=None,
) -> List[Dict[str, Any]]:
    """Apply liquidity, event blackout, and enhanced opportunity scoring to deduped rows."""
    liq_cfg = screening_config.get("liquidity_policy", {})
    min_adv = float(liq_cfg.get("scan_all_min_dollar_adv_usd", 2_000_000))
    liq_mode = screening_config.get("liquidity_gate_mode", "penalize")

    eb_cfg = screening_config.get("event_blackout", {})
    blackout_days = int(eb_cfg.get("days", 5))
    blackout_penalty = float(eb_cfg.get("opportunity_penalty", 0.12))
    exempt_presets = set(eb_cfg.get("exempt_presets", ["earnings_play"]))

    cov_cfg = dict(screening_config.get("coverage_penalty") or {})
    scan_cov = (screening_config.get("scan_all") or {}).get("coverage_penalty") or {}
    if scan_cov:
        cov_cfg.update(scan_cov)
    cov_mode = cov_cfg.get("mode", "warn")
    cov_threshold = float(cov_cfg.get("threshold", 60))
    cov_multiplier = float(cov_cfg.get("multiplier", 0.95))

    risk_cfg = screening_config.get("risk_penalty", {})
    risk_enabled = bool(risk_cfg.get("enabled", True))
    risk_threshold = float(risk_cfg.get("threshold", 70))
    risk_multiplier = float(risk_cfg.get("multiplier", 0.9))

    liq_multiplier = float(liq_cfg.get("opportunity_penalty_multiplier", 0.88))

    event_map: Dict[str, List[dict]] = {}
    if db and rows:
        tickers = [r["ticker"] for r in rows]
        try:
            events = db.get_upcoming_events(
                ticker=None,
                event_types=["earnings"],
                max_days=blackout_days,
                limit=5000,
            )
            for ev in events:
                t = (ev.get("ticker") or "").upper()
                if t:
                    event_map.setdefault(t, []).append(ev)
        except Exception:
            event_map = {}

    out: List[Dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        meta = row.get("ticker_metadata") or {}
        adv = meta.get("avg_dollar_volume_usd")
        if adv is None:
            adv = row.get("avg_dollar_volume_usd")
        row["avg_dollar_volume_usd"] = adv
        row["liquidity_pass"] = adv is not None and float(adv) >= min_adv

        weekly = None
        ma_crossover = None
        sigs = row.get("signals") or {}
        if isinstance(sigs, dict):
            weekly = sigs.get("weekly_trend_alignment")
            ma_crossover = sigs.get("ma_crossover")
        meta_block = sigs.get("_screening_meta") if isinstance(sigs, dict) else None
        if isinstance(meta_block, dict):
            row.setdefault("signal_coverage_pct", meta_block.get("signal_coverage_pct"))
            row.setdefault("tier_reached", meta_block.get("tier_reached"))
            if not row.get("resolved_preset"):
                row["resolved_preset"] = meta_block.get("resolved_preset") or row.get("preset")
        row["resolved_preset"] = canonical_preset_name(row.get("resolved_preset"))
        resolved = canonical_preset_name(
            str(row.get("resolved_preset") or row.get("preset") or "").strip().lower()
        )
        is_hunter = resolved in HUNTER_PRESETS
        opp, opp_meta = compute_opportunity_score(
            row.get("composite_score"),
            row.get("entry_quality"),
            row.get("macro_fit"),
            composite_fundamental=row.get("composite_fundamental"),
            weekly_alignment=weekly,
            use_fundamental_composite=not is_hunter,
            ma_crossover=ma_crossover if is_hunter else None,
        )
        row["opportunity_score"] = opp
        row["opp_pre_penalty_score"] = opp
        row["opp_macro_penalty_applied"] = opp_meta.get("opp_macro_penalty_applied", False)
        row["opp_liquidity_penalty_applied"] = False
        row["opp_coverage_penalty_applied"] = False
        row["opp_earnings_penalty_applied"] = False
        row["opp_risk_penalty_applied"] = False
        row["opp_implausible_identity_applied"] = False
        row["ma_crossover_dampener_applied"] = bool(opp_meta.get("ma_crossover_dampener_applied"))
        row["used_fundamental_composite"] = bool(opp_meta.get("used_fundamental_composite"))

        if liq_mode == "penalize" and not row["liquidity_pass"] and adv is not None:
            row["opportunity_score"] = round(max(0.0, opp * liq_multiplier), 1)
            row["opp_liquidity_penalty_applied"] = True
        elif liq_mode == "exclude" and not row["liquidity_pass"]:
            row["liquidity_excluded"] = True

        flags: List[str] = []
        preset = (row.get("preset") or row.get("watchlist_preset") or "").lower()
        resolved = str(row.get("resolved_preset") or "").lower()
        ticker = (row.get("ticker") or "").upper()
        evts = event_map.get(ticker, [])
        if evts and preset not in exempt_presets and resolved not in exempt_presets:
            flags.append("earnings_blackout")
            if eb_cfg.get("enabled", True):
                row["opportunity_score"] = round(
                    max(0.0, row["opportunity_score"] * (1.0 - blackout_penalty)), 1
                )
                row["opp_earnings_penalty_applied"] = True
        row["event_risk_flags"] = flags

        cov = row.get("signal_coverage_pct")
        if cov_mode == "penalize" and cov is not None and float(cov) < cov_threshold:
            row["opportunity_score"] = round(max(0.0, row["opportunity_score"] * cov_multiplier), 1)
            row["opp_coverage_penalty_applied"] = True

        risk = row.get("risk_score")
        if (
            risk_enabled
            and risk is not None
            and float(risk) >= risk_threshold
        ):
            row["opportunity_score"] = round(
                max(0.0, row["opportunity_score"] * risk_multiplier), 1
            )
            row["opp_risk_penalty_applied"] = True

        cap = meta.get("market_cap")
        if cap is None:
            cap = row.get("market_cap")
        if is_implausible_mega_identity(
            ticker or row.get("ticker"),
            cap,
            row.get("market_cap_tier") or meta.get("market_cap_tier") or "",
        ):
            row["opportunity_score"] = round(
                max(0.0, row["opportunity_score"] * IMPLAUSIBLE_MEGA_OPP_MULTIPLIER), 1
            )
            row["opp_implausible_identity_applied"] = True

        if liq_mode != "exclude" or row.get("liquidity_pass", True):
            out.append(row)
    return out
