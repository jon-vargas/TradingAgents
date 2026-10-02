"""One-pass meta screening: four sleeves, lead by desk priority."""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

STRATEGY = "multi_sleeve"
EMBED_KEY = "_multi_sleeve"

REVERSAL_ELIGIBLE_PHASES = frozenset({"early_turn", "confirmed"})
MOMENTUM_HC_BUCKETS = frozenset({"confirmed", "high_conviction"})
# Confirmed band on the Momentum desk. Watch (50–65) stays on the row and cannot lead.
MOMENTUM_LEAD_MIN_SCORE = 65.0
# Actionable preset on the Opportunity desk.
OPPORTUNITY_LEAD_MIN_ENTRY = 60.0
# A percent rank needs a real set. Smaller sleeves show the native score only.
MIN_PERCENTILE_SAMPLE = 8
# First desk that clears wins the badge. Later desks stay as secondary chips.
LEAD_TIE_ORDER = ("reversal", "momentum", "base", "opportunity")
DESK_SORT_ORDER = {name: i for i, name in enumerate(LEAD_TIE_ORDER)}
# rsi_overbought = (RSI-50)/30, so 0.50 is about RSI 65.
RSI_EXTENDED_MIN = 0.50


def _percentile_rank(values: List[float]) -> Dict[int, float]:
    """Map index in values list to 0–100 percentile (higher raw value → higher pct)."""
    if not values:
        return {}
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    n = len(indexed)
    if n < 2:
        return {}
    out: Dict[int, float] = {}
    for rank, (idx, _) in enumerate(indexed):
        out[idx] = round(100.0 * rank / (n - 1), 1)
    return out


def _reversal_eligible(payload: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(payload, dict):
        return None
    phase = str(payload.get("phase") or "")
    if phase not in REVERSAL_ELIGIBLE_PHASES:
        return None
    score = payload.get("score")
    if score is None:
        return None
    try:
        return float(score)
    except (TypeError, ValueError):
        return None


def _momentum_score(payload: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(payload, dict) or not payload.get("passed_gates", True):
        return None
    raw = payload.get("score_final")
    if raw is None:
        raw = payload.get("score_pre") or payload.get("score")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _momentum_eligible(payload: Optional[Dict[str, Any]]) -> Optional[float]:
    score = _momentum_score(payload)
    if score is None or score < MOMENTUM_LEAD_MIN_SCORE:
        return None
    return score


def _base_eligible(payload: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(payload, dict) or not payload.get("on_board"):
        return None
    score = payload.get("score")
    if score is None:
        return None
    try:
        return float(score)
    except (TypeError, ValueError):
        return None


def _opportunity_eligible(composite_score: Optional[float], entry_quality: Optional[float]) -> Optional[float]:
    """Opportunity can lead only when Entry clears the Actionable preset."""
    if entry_quality is None:
        return None
    try:
        entry = float(entry_quality)
        composite = float(composite_score) if composite_score is not None else None
    except (TypeError, ValueError):
        return None
    if composite is None or entry < OPPORTUNITY_LEAD_MIN_ENTRY:
        return None
    return composite


def _direction_conflict(
    opp_direction: Optional[str],
    reversal_payload: Optional[Dict[str, Any]],
) -> bool:
    if not isinstance(reversal_payload, dict):
        return False
    if str(reversal_payload.get("phase") or "") not in REVERSAL_ELIGIBLE_PHASES:
        return False
    side = str(reversal_payload.get("side") or "").lower()
    opp = str(opp_direction or "").lower()
    if side == "short" and opp == "bullish":
        return True
    if side == "long" and opp == "bearish":
        return True
    return False


def _lead_native(
    lead_sleeve: str,
    *,
    composite_score: Optional[float],
    direction: Optional[str],
    reversal: Optional[Dict[str, Any]],
    momentum: Optional[Dict[str, Any]],
    base: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Native score and labels for the winning sleeve."""
    empty = {"lead_score": None, "lead_phase": None, "lead_side": None, "lead_bucket": None}
    if lead_sleeve == "opportunity":
        return {
            "lead_score": composite_score,
            "lead_phase": None,
            "lead_side": direction or None,
            "lead_bucket": None,
        }
    if lead_sleeve == "reversal" and isinstance(reversal, dict):
        return {
            "lead_score": reversal.get("score"),
            "lead_phase": reversal.get("phase"),
            "lead_side": reversal.get("side"),
            "lead_bucket": None,
        }
    if lead_sleeve == "momentum" and isinstance(momentum, dict):
        score = momentum.get("score_final")
        if score is None:
            score = momentum.get("score_pre") or momentum.get("score")
        return {
            "lead_score": score,
            "lead_phase": None,
            "lead_side": None,
            "lead_bucket": momentum.get("bucket"),
        }
    if lead_sleeve == "base" and isinstance(base, dict):
        return {
            "lead_score": base.get("score"),
            "lead_phase": None,
            "lead_side": None,
            "lead_bucket": "on_board" if base.get("on_board") else None,
        }
    return empty


def _pick_lead(
    percentiles: Dict[str, Optional[float]],
    scores: Dict[str, Optional[float]],
) -> Tuple[str, Optional[float]]:
    """First desk that cleared its bar wins. Percentile does not steal the badge."""
    for name in LEAD_TIE_ORDER:
        if scores.get(name) is None:
            continue
        return name, percentiles.get(name)
    return "", None


def _cleared_sleeves(
    scores: Dict[str, Optional[float]],
    reversal: Optional[Dict[str, Any]],
    momentum: Optional[Dict[str, Any]],
    base: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Every desk that cleared, in lead order, including the winner."""
    cleared: List[Dict[str, Any]] = []
    if scores.get("reversal") is not None and isinstance(reversal, dict):
        cleared.append({
            "sleeve": "reversal",
            "side": reversal.get("side") or "",
            "phase": reversal.get("phase") or "",
        })
    if scores.get("momentum") is not None:
        cleared.append({
            "sleeve": "momentum",
            "bucket": (momentum or {}).get("bucket") or "",
        })
    if scores.get("base") is not None:
        cleared.append({"sleeve": "base"})
    if scores.get("opportunity") is not None:
        cleared.append({"sleeve": "opportunity"})
    return cleared


def _location_label(
    signals: Optional[Dict[str, Any]],
    reversal: Optional[Dict[str, Any]],
    entry_quality: Optional[float],
) -> str:
    """Where the price sits. Entry is the timing shape; this is the location.

    extended: a late reversal, or RSI stretched to about 65 or higher.
    at_target: no upside versus the published target, and other analyst signals exist
    so a missing target is not labeled as at-target.
    pullback: a fresh reversal, or Entry cleared without those extension flags.
    """
    sig = signals if isinstance(signals, dict) else {}
    phase = str((reversal or {}).get("phase") or "") if isinstance(reversal, dict) else ""

    def _num(key: str) -> Optional[float]:
        raw = sig.get(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    rsi_ob = _num("rsi_overbought")
    if phase == "late" or (rsi_ob is not None and rsi_ob >= RSI_EXTENDED_MIN):
        return "extended"
    pvt = _num("price_vs_target")
    analyst_present = any(
        (val := _num(key)) is not None and val > 0
        for key in ("valuation_gap", "estimate_momentum", "rating_momentum")
    )
    if pvt is not None and pvt <= 0.0 and analyst_present:
        return "at_target"
    try:
        entry = float(entry_quality) if entry_quality is not None else None
    except (TypeError, ValueError):
        entry = None
    if phase in REVERSAL_ELIGIBLE_PHASES or (entry is not None and entry >= OPPORTUNITY_LEAD_MIN_ENTRY):
        return "pullback"
    return ""


def _lifecycle_note(
    base_payload: Optional[Dict[str, Any]],
    mom_payload: Optional[Dict[str, Any]],
) -> str:
    if not isinstance(base_payload, dict) or not base_payload.get("on_board"):
        return ""
    if not isinstance(mom_payload, dict):
        return ""
    bucket = str(mom_payload.get("bucket") or "")
    if bucket in MOMENTUM_HC_BUCKETS:
        return "base_on_board_with_momentum_breakout"
    return ""


def attach_multi_sleeve_meta(results: Sequence[Any]) -> None:
    """Compute sleeve percentiles and lead sleeve on each ScreeningResult (in place)."""
    rows = list(results)
    opp_raw: List[Tuple[int, float]] = []
    rev_raw: List[Tuple[int, float]] = []
    mom_raw: List[Tuple[int, float]] = []
    base_raw: List[Tuple[int, float]] = []

    for i, r in enumerate(rows):
        sig = getattr(r, "signals", None) or {}
        opp_v = _opportunity_eligible(getattr(r, "composite_score", None), getattr(r, "entry_quality", None))
        rev_v = _reversal_eligible(sig.get("_reversal_buildup"))
        mom_v = _momentum_eligible(sig.get("_early_momentum"))
        base_v = _base_eligible(sig.get("_base_coil"))
        if opp_v is not None:
            opp_raw.append((i, opp_v))
        if rev_v is not None:
            rev_raw.append((i, rev_v))
        if mom_v is not None:
            mom_raw.append((i, mom_v))
        if base_v is not None:
            base_raw.append((i, base_v))

    def _pct_map(pairs: List[Tuple[int, float]]) -> Tuple[Dict[int, float], set]:
        if not pairs:
            return {}, set()
        if len(pairs) < MIN_PERCENTILE_SAMPLE:
            return {}, {pairs[0][0]} if len(pairs) == 1 else set()
        vals = [p[1] for p in pairs]
        idx_map = {j: pairs[j][0] for j in range(len(pairs))}
        pr = _percentile_rank(vals)
        return {idx_map[j]: pr[j] for j in pr}, set()

    opp_pct, opp_sole = _pct_map(opp_raw)
    rev_pct, rev_sole = _pct_map(rev_raw)
    mom_pct, mom_sole = _pct_map(mom_raw)
    base_pct, base_sole = _pct_map(base_raw)

    for i, r in enumerate(rows):
        sig = getattr(r, "signals", None) or {}
        if not isinstance(sig, dict):
            sig = {}
            r.signals = sig
        rev_p = sig.get("_reversal_buildup")
        mom_p = sig.get("_early_momentum")
        base_p = sig.get("_base_coil")

        sleeves: Dict[str, Optional[float]] = {
            "opportunity": opp_pct.get(i),
            "reversal": rev_pct.get(i),
            "momentum": mom_pct.get(i),
            "base": base_pct.get(i),
        }
        native_scores = {
            "opportunity": next((v for idx, v in opp_raw if idx == i), None),
            "reversal": next((v for idx, v in rev_raw if idx == i), None),
            "momentum": next((v for idx, v in mom_raw if idx == i), None),
            "base": next((v for idx, v in base_raw if idx == i), None) if len(base_raw) >= 2 else None,
        }
        lead_sleeve, lead_percentile = _pick_lead(sleeves, native_scores)
        cleared = _cleared_sleeves(
            native_scores,
            rev_p if isinstance(rev_p, dict) else None,
            mom_p if isinstance(mom_p, dict) else None,
            base_p if isinstance(base_p, dict) else None,
        )

        late_chase = False
        if isinstance(mom_p, dict):
            late_chase = bool(mom_p.get("late_chase"))
            if not late_chase:
                domains = mom_p.get("domain_detail") or {}
                detail_a = domains.get("A") if isinstance(domains, dict) else {}
                if isinstance(detail_a, dict) and detail_a.get("late_chase"):
                    late_chase = True

        native = _lead_native(
            lead_sleeve,
            composite_score=getattr(r, "composite_score", None),
            direction=getattr(r, "direction", None),
            reversal=rev_p if isinstance(rev_p, dict) else None,
            momentum=mom_p if isinstance(mom_p, dict) else None,
            base=base_p if isinstance(base_p, dict) else None,
        )
        meta = {
            "lead_sleeve": lead_sleeve,
            "lead_percentile": lead_percentile,
            "lead_score": native["lead_score"],
            "lead_phase": native["lead_phase"],
            "lead_side": native["lead_side"],
            "lead_bucket": native["lead_bucket"],
            "percentile_opportunity": sleeves["opportunity"],
            "percentile_reversal": sleeves["reversal"],
            "percentile_momentum": sleeves["momentum"],
            "percentile_base": sleeves["base"],
            "opportunity_only": i in opp_sole,
            "reversal_only": i in rev_sole,
            "momentum_only": i in mom_sole,
            "base_only": i in base_sole,
            "direction_conflict": _direction_conflict(getattr(r, "direction", None), rev_p),
            "lifecycle_note": _lifecycle_note(base_p, mom_p),
            "late_chase": late_chase,
            "cleared": cleared,
            "location": _location_label(sig, rev_p if isinstance(rev_p, dict) else None, getattr(r, "entry_quality", None)),
        }
        sig[EMBED_KEY] = meta


def _lead_sort_tuple(meta: Dict[str, Any], ticker: str) -> Tuple:
    """Group by desk, then best native score inside that desk. No lead last."""
    sleeve = str(meta.get("lead_sleeve") or "")
    if not sleeve:
        return (1, len(DESK_SORT_ORDER), 0.0, ticker)
    try:
        score = float(meta.get("lead_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return (0, DESK_SORT_ORDER.get(sleeve, len(DESK_SORT_ORDER)), -score, ticker)


def multi_sleeve_sort_key(row: Any) -> Tuple:
    sig = getattr(row, "signals", None) or {}
    meta = sig.get(EMBED_KEY) if isinstance(sig, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    return _lead_sort_tuple(meta, str(getattr(row, "ticker", "") or ""))


def reversal_ohlcv_for_score(data: Dict[str, Any], scan_date: str, config: Any = None) -> Dict[str, Any]:
    """Deep-copy OHLCV and apply reversal session truncate without touching the shared batch."""
    from tradingagents.screening.reversal_buildup import truncate_batch_data

    if not data:
        return data
    copied = copy.deepcopy(data)
    out = truncate_batch_data({"_": copied}, scan_date, cfg=config)
    return out.get("_", copied)


def lift_multi_sleeve_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    signals = row.get("signals")
    payload = row.get("multi_sleeve")
    if not isinstance(payload, dict) and isinstance(signals, dict):
        raw = signals.get(EMBED_KEY)
        if isinstance(raw, dict):
            payload = raw
        elif isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                payload = None
    if isinstance(payload, dict):
        row["multi_sleeve"] = payload
        row["lead_sleeve"] = payload.get("lead_sleeve")
        row["lead_percentile"] = payload.get("lead_percentile")
        row["lead_score"] = payload.get("lead_score")
        row["lead_phase"] = payload.get("lead_phase")
        row["lead_side"] = payload.get("lead_side")
        row["lead_bucket"] = payload.get("lead_bucket")
        row["late_chase"] = bool(payload.get("late_chase"))
        row["percentile_opportunity"] = payload.get("percentile_opportunity")
        row["percentile_reversal"] = payload.get("percentile_reversal")
        row["percentile_momentum"] = payload.get("percentile_momentum")
        row["percentile_base"] = payload.get("percentile_base")
        row["opportunity_only"] = bool(payload.get("opportunity_only"))
        row["reversal_only"] = bool(payload.get("reversal_only"))
        row["momentum_only"] = bool(payload.get("momentum_only"))
        row["base_only"] = bool(payload.get("base_only"))
        row["direction_conflict"] = payload.get("direction_conflict")
        row["lifecycle_note"] = payload.get("lifecycle_note") or ""
        row["location"] = payload.get("location") or ""
        cleared = payload.get("cleared")
        row["cleared"] = cleared if isinstance(cleared, list) else []
    if isinstance(signals, dict):
        if signals.get("_reversal_buildup") and "reversal_buildup" not in row:
            row["reversal_buildup"] = signals.get("_reversal_buildup")
        if signals.get("_early_momentum") and "early_momentum" not in row:
            row["early_momentum"] = signals.get("_early_momentum")
        if signals.get("_base_coil") and "base_coil" not in row:
            row["base_coil"] = signals.get("_base_coil")
    return row


def rank_multi_sleeve_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Recompute lead fields from sleeve payloads, then sort.

    Saved runs keep the original ``_multi_sleeve`` blob. Reading them through
    this helper applies the current eligibility rules without a rescan.
    """
    lifted = [lift_multi_sleeve_fields(dict(r)) for r in rows]
    _recompute_dict_rows(lifted)
    lifted.sort(key=lambda r: _lead_sort_tuple(r.get("multi_sleeve") or {}, str(r.get("ticker") or "")))
    return lifted


def _recompute_dict_rows(rows: List[Dict[str, Any]]) -> None:
    """Refresh ``_multi_sleeve`` on API rows from the four sleeve payloads."""
    class _Row:
        def __init__(self, raw: Dict[str, Any]) -> None:
            sig = raw.get("signals")
            if not isinstance(sig, dict):
                sig = {}
                raw["signals"] = sig
            rev = raw.get("reversal_buildup") or sig.get("_reversal_buildup")
            mom = raw.get("early_momentum") or sig.get("_early_momentum")
            base = raw.get("base_coil") or sig.get("_base_coil")
            if isinstance(rev, dict):
                sig["_reversal_buildup"] = rev
            if isinstance(mom, dict):
                sig["_early_momentum"] = mom
            if isinstance(base, dict):
                sig["_base_coil"] = base
            self.ticker = raw.get("ticker")
            self.composite_score = raw.get("composite_score")
            self.entry_quality = raw.get("entry_quality")
            self.direction = raw.get("direction")
            self.signals = sig
            self._raw = raw

    wrapped = [_Row(r) for r in rows]
    attach_multi_sleeve_meta(wrapped)
    for item in wrapped:
        item._raw.pop("multi_sleeve", None)
        lift_multi_sleeve_fields(item._raw)
