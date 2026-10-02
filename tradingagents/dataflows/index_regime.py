"""Index-level regime classification: trend × stress overlay."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

_TREND_LABELS = {
    "bull": "Bull",
    "bear": "Bear",
    "neutral": "Neutral",
    "unknown": "Unknown",
}

_STRESS_LABELS = {
    "quiet": "Quiet",
    "stressed": "Stressed",
    "unknown": "Unknown",
}


def classify_index_trend(sp500: Dict[str, Any]) -> str:
    """Classify SPX trend using hysteresis band + 50-SMA confirmation."""
    spy_cur = sp500.get("current")
    spy_sma200 = sp500.get("sma_200")
    spy_sma50 = sp500.get("sma_50")
    if not spy_cur or not spy_sma200:
        return "unknown"
    pct_from_200 = (spy_cur / spy_sma200 - 1) * 100
    if pct_from_200 <= -5.0:
        return "bear"
    if pct_from_200 >= 2.0 and spy_sma50 is not None and spy_cur > spy_sma50:
        return "bull"
    return "neutral"


def _vix_is_stressed(vix_level: str, vix_value: Optional[float]) -> bool:
    if vix_level in ("elevated", "panic"):
        return True
    if vix_value is not None and vix_value >= 25:
        return True
    return False


def _credit_is_stressed(credit_stress: str) -> bool:
    return credit_stress in ("elevated", "high")


def _breadth_is_weak(sector_breadth: str) -> bool:
    return sector_breadth in ("weak", "very_weak")


def _signal_unavailable(value: str) -> bool:
    return not value or value == "unknown"


def classify_index_stress(
    *,
    vix_level: str = "unknown",
    vix_value: Optional[float] = None,
    credit_stress: str = "unknown",
    sector_breadth: str = "unknown",
) -> Tuple[str, Dict[str, Any]]:
    """Classify index stress with deterministic precedence rules."""
    evidence: Dict[str, Any] = {
        "vix_level": vix_level,
        "vix_value": vix_value,
        "credit_stress": credit_stress,
        "sector_breadth": sector_breadth,
        "rule": None,
    }

    vix_usable = not _signal_unavailable(vix_level) or vix_value is not None
    credit_usable = not _signal_unavailable(credit_stress)

    if _vix_is_stressed(vix_level, vix_value):
        evidence["rule"] = "vix_elevated"
        return "stressed", evidence
    if _credit_is_stressed(credit_stress):
        evidence["rule"] = "credit_elevated"
        return "stressed", evidence

    if _breadth_is_weak(sector_breadth) and (not vix_usable or not credit_usable):
        evidence["rule"] = "breadth_weak_with_missing_vix_or_credit"
        return "stressed", evidence

    if vix_usable and credit_usable:
        if _breadth_is_weak(sector_breadth):
            evidence["rule"] = "weak_breadth_with_usable_vix_credit"
            return "unknown", evidence
        evidence["rule"] = "vix_credit_quiet"
        return "quiet", evidence

    evidence["rule"] = "partial_data"
    return "unknown", evidence


def build_index_regime_fields(
    index_trend: str,
    index_stress: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build compound slug/label fields; market_regime mirrors index_trend."""
    trend_slug = index_trend if index_trend in _TREND_LABELS else "unknown"
    stress_slug = index_stress if index_stress in _STRESS_LABELS else "unknown"

    if trend_slug == "unknown" and stress_slug == "unknown":
        index_regime = "unknown"
        index_regime_label = "Unknown"
    elif stress_slug == "unknown":
        index_regime = f"{trend_slug}_unknown"
        index_regime_label = f"{_TREND_LABELS[trend_slug]} / Unknown"
    elif trend_slug == "unknown":
        index_regime = f"unknown_{stress_slug}"
        index_regime_label = f"Unknown / {_STRESS_LABELS[stress_slug]}"
    else:
        index_regime = f"{trend_slug}_{stress_slug}"
        index_regime_label = f"{_TREND_LABELS[trend_slug]} / {_STRESS_LABELS[stress_slug]}"

    return {
        "index_trend": trend_slug,
        "index_stress": stress_slug,
        "index_regime": index_regime,
        "index_regime_label": index_regime_label,
        "index_regime_evidence": evidence or {},
        "market_regime": trend_slug,
    }


def unknown_macro_snapshot(
    reason: str = "unknown",
    as_of_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Explicit unknown snapshot for invalid dates or fetch failures."""
    fields = build_index_regime_fields("unknown", "unknown", {"rule": reason})
    return {
        "indices": {},
        "vix_level": "unknown",
        "vix_trend": "unknown",
        "yield_curve": "unknown",
        "yield_curve_trend": "unknown",
        "dollar_trend": "unknown",
        "dollar_pct_from_sma50": 0.0,
        "credit_stress": "unknown",
        "sector_breadth": "unknown",
        "sectors_above_sma50": 0,
        "sectors_total": 0,
        "fetched_at": None,
        "as_of_date": as_of_date,
        "error": reason,
        **fields,
    }


def normalize_index_regime(macro: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize index regime fields from any macro snapshot (new or legacy)."""
    macro = macro or {}
    trend = macro.get("index_trend") or macro.get("market_regime") or "unknown"
    stress = macro.get("index_stress") or "unknown"
    label = macro.get("index_regime_label")
    slug = macro.get("index_regime") or "unknown"

    if not label or label == "Unknown":
        if trend != "unknown" and stress != "unknown":
            built = build_index_regime_fields(trend, stress, macro.get("index_regime_evidence"))
            label = built["index_regime_label"]
            slug = built["index_regime"]
        elif trend != "unknown":
            label = _TREND_LABELS.get(trend, str(trend).title())
        else:
            label = "Unknown"

    if slug == "unknown" and trend != "unknown" and stress != "unknown":
        slug = f"{trend}_{stress}"

    return {
        "index_trend": trend,
        "index_stress": stress,
        "index_regime": slug,
        "index_regime_label": label,
        "market_regime": trend,
        "index_regime_evidence": macro.get("index_regime_evidence") or {},
        "as_of_date": macro.get("as_of_date"),
        "fetched_at": macro.get("fetched_at"),
    }


def index_regime_display_label(macro: Optional[Dict[str, Any]]) -> str:
    return normalize_index_regime(macro).get("index_regime_label", "Unknown")


def index_regime_storage_fields(macro: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Fields for criteria JSON, drift snapshots, and audit metadata."""
    normalized = normalize_index_regime(macro)
    return {
        "regime": normalized["index_trend"],
        "regime_label": normalized["index_regime_label"],
        "index_regime": normalized["index_regime"],
        "index_trend": normalized["index_trend"],
        "index_stress": normalized["index_stress"],
        "index_regime_label": normalized["index_regime_label"],
    }
