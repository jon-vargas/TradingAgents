"""
Quantitative Signal Aggregator

Parses SIGNAL_JSON from upstream agents and deterministic data signals,
computes a weighted composite, and produces a text summary for injection
into the Risk Manager prompt.
"""

import logging
import statistics as _statistics
from typing import Any, Dict, List, Optional, Tuple

from tradingagents.reporting.attribution import _extract_signal_json

logger = logging.getLogger("tradingagents.graph.signal_aggregator")

# ── Weight configuration ────────────────────────────────────────────────
# LLM agent signals (60% total)
LLM_WEIGHTS = {
    "Market": 0.12,
    "Fundamentals": 0.15,
    "News": 0.09,
    "Sentiment": 0.06,
    "Research": 0.09,
    "Trading Plan": 0.09,
}

# Deterministic data signals (40% total)
# transcript_kpi is additive when available; weights renormalize automatically
# because aggregate_signals computes total_weight from present sources.
DATA_WEIGHTS = {
    "estimate_revisions": 0.18,
    "rating_changes": 0.12,
    "weekly_trend": 0.10,
    "transcript_kpi": 0.08,  # structured earnings transcript KPI signal (optional)
}


def _normalized_group_weights(
    raw_weights: Dict[str, Any],
    active_keys: List[str],
    target_total: float,
) -> Dict[str, float]:
    """Normalize active overlay keys to a stable group-level total."""
    selected = {
        key: max(0.0, float(raw_weights.get(key, 0.0)))
        for key in active_keys
    }
    total = sum(selected.values())
    if total <= 0:
        return {}
    return {key: value * target_total / total for key, value in selected.items()}


def _resolve_weights(
    state: Dict[str, Any],
    *,
    transcript_available: bool,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, Any]]:
    """Return default weights or a profile overlay with explicit 60/40 sleeves."""
    profile = state.get("investment_profile") if isinstance(state.get("investment_profile"), dict) else {}
    profile_key = str(state.get("investment_profile_key") or profile.get("profile_key") or "")
    cfg = config
    if cfg is None:
        from tradingagents.default_config import DEFAULT_CONFIG

        cfg = DEFAULT_CONFIG
    overlay = (cfg or {}).get("signal_weight_overlays", {}).get(profile_key)
    if not isinstance(overlay, dict):
        return dict(LLM_WEIGHTS), dict(DATA_WEIGHTS), {
            "weight_overlay_key": None,
            "effective_weights": {"llm": dict(LLM_WEIGHTS), "data": dict(DATA_WEIGHTS)},
        }

    llm_active = list(LLM_WEIGHTS)
    data_active = [key for key in DATA_WEIGHTS if transcript_available or key != "transcript_kpi"]
    llm_weights = _normalized_group_weights(overlay.get("llm") or {}, llm_active, 0.60)
    data_weights = _normalized_group_weights(overlay.get("data") or {}, data_active, 0.40)
    if not llm_weights or not data_weights:
        logger.warning("Invalid signal-weight overlay for %s; using defaults", profile_key)
        return dict(LLM_WEIGHTS), dict(DATA_WEIGHTS), {
            "weight_overlay_key": None,
            "effective_weights": {"llm": dict(LLM_WEIGHTS), "data": dict(DATA_WEIGHTS)},
        }
    return llm_weights, data_weights, {
        "weight_overlay_key": profile_key,
        "effective_weights": {"llm": llm_weights, "data": data_weights},
    }


STATE_FIELD_MAP = {
    "Market": "market_report",
    "Fundamentals": "fundamentals_report",
    "News": "news_report",
    "Sentiment": "sentiment_report",
    "Research": "investment_plan",
    "Trading Plan": "trader_investment_plan",
}


_BULLISH_TOKENS = (
    "bull", "buy", "positive", "outperform",
    "optimistic", "favorable", "strong", "upside", "accumulate", "overweight",
)
_BEARISH_TOKENS = (
    "bear", "sell", "negative", "underperform",
    "pessimistic", "unfavorable", "weak", "downside", "reduce", "underweight",
)
_NEUTRAL_TOKENS = ("hold", "neutral", "mixed", "flat")


def _stance_to_score(stance: str) -> float:
    """Convert a textual stance to a numeric score: +1 (bullish), 0, -1 (bearish)."""
    if not stance:
        return 0.0
    low = stance.lower()
    if any(tok in low for tok in _BULLISH_TOKENS):
        return 1.0
    if any(tok in low for tok in _BEARISH_TOKENS):
        return -1.0
    if any(tok in low for tok in _NEUTRAL_TOKENS):
        return 0.0
    logger.warning("Unrecognized stance '%s' — defaulting to neutral (0.0)", stance)
    return 0.0


def _stance_label(score: float) -> str:
    if score > 0.3:
        return "BULLISH"
    if score < -0.3:
        return "BEARISH"
    return "NEUTRAL"


def _normalize_confidence(raw) -> float:
    """Normalize confidence to 0.0–1.0 range, handling both 0–1 and 0–100 scales."""
    if not isinstance(raw, (int, float)):
        return 0.5
    val = float(raw)
    if val > 1.0:
        val = val / 100.0
    return max(0.0, min(1.0, val))


def _infer_signal_from_report(text: str, section: str) -> Dict[str, Any]:
    """Recover a stance when SIGNAL_JSON was truncated off the report tail."""
    if not text:
        return {}
    try:
        from tradingagents.graph.signal_processing import extract_explicit_decision
    except Exception:
        return {}
    decision = extract_explicit_decision(text)
    if not decision:
        return {}
    stance = {"BUY": "bullish", "SELL": "bearish", "HOLD": "neutral"}[decision]
    return {
        "section": section,
        "stance": stance,
        "confidence": 0.45,
        "key_factors": ["inferred from transaction proposal"],
        "inferred": True,
    }


def _extract_llm_signals(
    state: Dict[str, Any],
    exclude_sections: Optional[set] = None,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Extract SIGNAL_JSON from each agent report in state.

    Args:
        exclude_sections: Optional set of section names to skip (e.g. {"Research", "Trading Plan"}).

    Returns:
        Tuple of (signals dict, list of section names that had report text but no valid SIGNAL_JSON).
    """
    signals = {}
    active_sections = {
        k: v for k, v in STATE_FIELD_MAP.items()
        if not (exclude_sections and k in exclude_sections)
    }
    for section, field in active_sections.items():
        text = state.get(field, "")
        if not text:
            continue
        parsed = _extract_signal_json(text)
        if not parsed:
            parsed = _infer_signal_from_report(text, section)
            if parsed:
                parsed["inferred"] = True
        if parsed:
            stance_raw = parsed.get("stance", "")
            confidence = _normalize_confidence(parsed.get("confidence"))
            key_factors = parsed.get("key_factors", [])
            signals[section] = {
                "stance": stance_raw,
                "score": _stance_to_score(stance_raw),
                "confidence": confidence,
                "key_factors": key_factors[:3] if key_factors else [],
                "inferred": bool(parsed.get("inferred")),
            }

    missing_signals: List[str] = []
    for section, field in active_sections.items():
        text = state.get(field, "")
        if text and section not in signals:
            missing_signals.append(section)
            logger.warning(
                "Analyst '%s' produced a report but no valid SIGNAL_JSON — "
                "signal will be treated as neutral (0.0) in composite",
                section,
            )

    return signals, missing_signals


def _parse_transcript_kpi(transcript_text: str) -> Dict[str, Any]:
    """Parse a Perplexity earnings transcript snapshot string into a structured KPI signal.

    Handles the two known string contracts:
    - ``EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:\\n{...}`` — strip prefix then json.loads
    - ``EARNINGS_TRANSCRIPT_SNAPSHOT_RAW:\\n{...}``  — emit QA warning, return neutral

    Returns a signal dict compatible with ``compute_signal_summary``:
    {score, label, detail, kpis, qa_warning}.
    """
    import json

    null_signal = {"score": 0.0, "label": "N/A", "detail": "No transcript data", "kpis": {}, "qa_warning": None}

    if not transcript_text or not isinstance(transcript_text, str):
        return null_signal

    text = transcript_text.strip()

    # Handle _RAW fallback — parse was unsuccessful at collection time
    if text.startswith("EARNINGS_TRANSCRIPT_SNAPSHOT_RAW:"):
        warn = "Transcript snapshot unavailable (RAW fallback) — KPI signal skipped"
        logger.warning("transcript_kpi: %s", warn)
        return {**null_signal, "qa_warning": warn}

    # Handle successful JSON snapshot
    if text.startswith("EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:"):
        payload_str = text[len("EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:"):].strip()
    else:
        payload_str = text  # Try to parse bare JSON as fallback

    try:
        payload = json.loads(payload_str)
    except (json.JSONDecodeError, ValueError) as e:
        warn = f"Transcript KPI JSON parse failed: {e}"
        logger.warning("transcript_kpi: %s", warn)
        return {**null_signal, "qa_warning": warn}

    if not isinstance(payload, dict):
        return {**null_signal, "qa_warning": "Transcript payload is not a dict"}

    # Extract KPI directions with defaults
    guidance = str(payload.get("guidance_direction") or payload.get("guidance", "") or "unknown").lower()
    revenue = str(payload.get("revenue_direction") or payload.get("revenue_growth", "") or "unknown").lower()
    margin = str(payload.get("margin_direction") or payload.get("margin_trend", "") or "unknown").lower()
    tone = str(payload.get("tone", "") or "unknown").lower()

    # Compute signal score: each dimension contributes -1 / 0 / +1
    def _dir_score(val: str, pos_tokens: tuple, neg_tokens: tuple) -> float:
        if any(t in val for t in pos_tokens):
            return 1.0
        if any(t in val for t in neg_tokens):
            return -1.0
        return 0.0

    guidance_score = _dir_score(guidance, ("raise", "above", "upward", "increase"), ("lower", "below", "cut", "reduce", "miss"))
    revenue_score = _dir_score(revenue, ("accelerat", "strong", "beat"), ("decelerat", "slow", "miss", "weak"))
    margin_score = _dir_score(margin, ("expand", "improv", "above"), ("compress", "below", "decline"))
    tone_score = _dir_score(tone, ("confident", "optimistic", "strong"), ("cautious", "uncertain", "concern"))

    agg_score = (guidance_score * 0.4 + revenue_score * 0.3 + margin_score * 0.2 + tone_score * 0.1)

    if agg_score > 0.2:
        label = "BULLISH"
    elif agg_score < -0.2:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    kpis = {
        "guidance": guidance,
        "revenue": revenue,
        "margin": margin,
        "tone": tone,
    }
    detail = f"guidance={guidance}, revenue={revenue}, margin={margin}, tone={tone}"

    return {
        "score": round(agg_score, 3),
        "label": label,
        "detail": detail,
        "kpis": kpis,
        "qa_warning": None,
    }


def _compute_estimate_revision_signal(ticker: str) -> Dict[str, Any]:
    """Determine estimate revision direction from yfinance data."""
    try:
        from tradingagents.dataflows.yfinance_extended import get_estimate_revisions
        data = get_estimate_revisions(ticker)
        if not data:
            return {"score": 0.0, "label": "N/A", "detail": "No data"}

        eps_trend = data.get("eps_trend", {})
        revisions = []
        for period_key, trend in eps_trend.items():
            current = trend.get("current")
            ago_30 = trend.get("30d_ago")
            if current is not None and ago_30 is not None and ago_30 != 0:
                pct = ((current - ago_30) / abs(ago_30)) * 100
                revisions.append(pct)

        if not revisions:
            return {"score": 0.0, "label": "N/A", "detail": "No trend data"}

        avg_rev = sum(revisions) / len(revisions)
        score = max(-1.0, min(1.0, avg_rev / 10.0))
        if abs(score) < 0.05:
            label = "NEUTRAL"
        elif score > 0:
            label = "BULLISH"
        else:
            label = "BEARISH"
        return {"score": round(score, 4), "label": label, "detail": f"EPS revised {avg_rev:+.1f}% over 30d"}
    except Exception as e:
        logger.debug("Estimate revision signal failed for %s: %s", ticker, e)
        return {"score": 0.0, "label": "N/A", "detail": str(e)}


def _compute_rating_change_signal(ticker: str) -> Dict[str, Any]:
    """Determine analyst rating change momentum."""
    try:
        from tradingagents.dataflows.yfinance_extended import get_rating_changes
        data = get_rating_changes(ticker)
        if not data or not data.get("actions"):
            return {"score": 0.0, "label": "N/A", "detail": "No data"}

        net = data.get("net_upgrades_90d", 0)
        total = data.get("total_actions_90d", 0)

        recent = data["actions"][:1]
        latest_str = ""
        if recent:
            a = recent[0]
            latest_str = f"(latest: {a.get('firm', '?')} {a.get('action', '?')} to {a.get('to_grade', '?')})"

        denom = max(total, 1)
        score = max(-1.0, min(1.0, net / denom))
        if abs(score) < 0.05:
            label = "NEUTRAL"
        elif score > 0:
            label = "BULLISH"
        else:
            label = "BEARISH"
        detail = f"Net {net:+d}/{total} actions in 90d {latest_str}"
        return {"score": round(score, 4), "label": label, "detail": detail}
    except Exception as e:
        logger.debug("Rating change signal failed for %s: %s", ticker, e)
        return {"score": 0.0, "label": "N/A", "detail": str(e)}


def _compute_weekly_trend_signal(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """Determine weekly technical trend direction."""
    try:
        from tradingagents.dataflows.yfinance_extended import get_weekly_technicals
        from tradingagents.screening.weekly_alignment import weekly_trend_label

        data = get_weekly_technicals(ticker, as_of_date=as_of_date)
        return weekly_trend_label(data)
    except Exception as e:
        logger.debug("Weekly trend signal failed for %s: %s", ticker, e)
        return {"score": 0.0, "label": "N/A", "detail": str(e)}


def _composite_label(composite: float) -> str:
    """Map composite score to a human-readable label with symmetric thresholds."""
    if -0.05 <= composite <= 0.05:
        return "NEUTRAL"
    if composite > 0.35:
        return "STRONGLY BULLISH"
    if composite > 0.15:
        return "MODERATELY BULLISH"
    if composite > 0.05:
        return "SLIGHTLY BULLISH"
    if composite < -0.35:
        return "STRONGLY BEARISH"
    if composite < -0.15:
        return "MODERATELY BEARISH"
    return "SLIGHTLY BEARISH"


def compute_signal_summary(
    state: Dict[str, Any],
    ticker: str,
    exclude_sections: Optional[set] = None,
    data_completeness: float = 1.0,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Compute the full quantitative signal summary.

    Args:
        state: Agent state dict containing reports.
        ticker: Stock ticker symbol.
        exclude_sections: Optional set of LLM section names to skip
            (e.g. {"Research", "Trading Plan"} when called before those exist).
        data_completeness: Mode-aware multiplier (quick=0.75, standard=0.90, deep=1.0).

    Returns a dict with:
      - llm_signals: per-agent signal details
      - data_signals: per-source deterministic signal details
      - composite: weighted composite score (-1 to +1)
      - bullish_count, bearish_count, neutral_count: dimension tallies
      - avg_confidence: mean confidence across LLM signals
      - data_completeness: the multiplier applied
      - text_block: formatted text for prompt injection
    """
    llm_signals, missing_signals = _extract_llm_signals(state, exclude_sections=exclude_sections)

    as_of_date = state.get("trade_date")
    data_signals = {
        "estimate_revisions": _compute_estimate_revision_signal(ticker),
        "rating_changes": _compute_rating_change_signal(ticker),
        "weekly_trend": _compute_weekly_trend_signal(ticker, as_of_date=as_of_date),
    }

    # Transcript KPI signal (deterministic, from Perplexity snapshot if present)
    transcript_text = state.get("earnings_transcript_snapshot", "")
    if transcript_text and str(transcript_text).strip():
        kpi_signal = _parse_transcript_kpi(str(transcript_text))
        data_signals["transcript_kpi"] = kpi_signal
        if kpi_signal.get("qa_warning"):
            logger.warning("transcript_kpi QA: %s", kpi_signal["qa_warning"])

    llm_weights, data_weights, weight_metadata = _resolve_weights(
        state,
        transcript_available="transcript_kpi" in data_signals,
        config=config,
    )

    # Issue 32: Index regime (trend × stress) from graph-owned snapshot
    index_trend: Optional[str] = None
    index_stress: Optional[str] = None
    index_regime: Optional[str] = None
    index_regime_label = "UNKNOWN"
    market_regime: Optional[str] = None
    try:
        macro = state.get("macro_snapshot")
        if not isinstance(macro, dict) or not macro:
            from tradingagents.dataflows.yfinance_extended import get_macro_snapshot
            macro = get_macro_snapshot(as_of_date=as_of_date)
        from tradingagents.dataflows.index_regime import normalize_index_regime

        normalized = normalize_index_regime(macro if isinstance(macro, dict) else {})
        index_trend = normalized.get("index_trend")
        index_stress = normalized.get("index_stress")
        index_regime = normalized.get("index_regime")
        index_regime_label = normalized.get("index_regime_label") or "UNKNOWN"
        trend = normalized.get("market_regime")
        if trend and trend != "unknown":
            market_regime = str(trend).upper()
        else:
            market_regime = "UNKNOWN"
    except Exception:
        pass

    # Confidence-weighted composite (Option A: normalize by confidence-weighted
    # denominators so the composite stays in the -1..+1 range even when
    # confidence < 1.0).
    weighted_sum = 0.0
    total_weight = 0.0

    active_llm_weights = {k: v for k, v in llm_weights.items()
                          if not (exclude_sections and k in exclude_sections)}

    for section, weight in active_llm_weights.items():
        sig = llm_signals.get(section)
        if sig:
            conf = sig["confidence"]
            weighted_sum += sig["score"] * conf * weight
            total_weight += conf * weight

    for source, weight in data_weights.items():
        sig = data_signals.get(source)
        if sig:
            weighted_sum += sig["score"] * weight
            total_weight += weight

    composite = (weighted_sum / total_weight if total_weight > 0 else 0.0) * data_completeness

    # Dimension tallies (use effective score = score * confidence for LLM signals)
    all_effective = []
    for s in llm_signals.values():
        all_effective.append(s["score"] * s["confidence"])
    for s in data_signals.values():
        all_effective.append(s["score"])

    bullish_count = sum(1 for s in all_effective if s > 0.05)
    bearish_count = sum(1 for s in all_effective if s < -0.05)
    neutral_count = len(all_effective) - bullish_count - bearish_count
    total_dims = len(all_effective)

    # Issue 30: Ensemble disagreement
    if len(all_effective) >= 2:
        disagreement = _statistics.stdev(all_effective)
        if disagreement > 0.6:
            disagreement_label = "HIGH"
        elif disagreement > 0.3:
            disagreement_label = "MODERATE"
        else:
            disagreement_label = "LOW"
    else:
        disagreement = 0.0
        disagreement_label = "N/A"

    confidences = [s["confidence"] for s in llm_signals.values() if s.get("confidence") is not None]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    # Build text block
    lines = ["--- QUANTITATIVE SIGNAL SUMMARY ---", "", "LLM Analyst Signals:"]

    section_labels = {
        "Market": "Technical",
        "Fundamentals": "Fundamentals",
        "News": "News/Macro",
        "Sentiment": "Sentiment",
        "Research": "Research Consensus",
        "Trading Plan": "Trader Assessment",
    }

    active_sections = [s for s in STATE_FIELD_MAP if not (exclude_sections and s in exclude_sections)]
    for section in active_sections:
        label = section_labels.get(section, section)
        sig = llm_signals.get(section)
        if sig:
            stance = _stance_label(sig["score"])
            conf = sig["confidence"]
            factors = ", ".join(sig["key_factors"]) if sig["key_factors"] else "N/A"
            lines.append(f"  {label}: {stance} (confidence: {conf:.2f}) - Key: {factors}")
        else:
            lines.append(f"  {label}: NOT AVAILABLE")

    lines.append("")
    lines.append("Deterministic Data Signals:")

    data_labels = {
        "estimate_revisions": "Estimate Revisions",
        "rating_changes": "Rating Changes",
        "weekly_trend": "Weekly Trend",
        "transcript_kpi": "Transcript KPIs",
    }
    for source in data_weights:
        sig = data_signals.get(source)
        if sig is None:
            continue
        label = data_labels.get(source, source)
        lines.append(f"  {label}: {sig['label']} ({sig['score']:+.2f}) - {sig['detail']}")

    label = _composite_label(composite)

    lines.append("")
    if weight_metadata.get("weight_overlay_key"):
        lines.append(f"Weight Overlay: {weight_metadata['weight_overlay_key']}")
    lines.append(f"Index Regime: {index_regime_label}")
    if index_stress == "stressed":
        lines.append("  -> Index stress elevated — require stronger name-level setup for new longs")
    if index_trend == "bear":
        lines.append("  -> Bear trend: higher conviction required for BUY signals")
    elif index_trend == "bull":
        lines.append("  -> Bull trend: supportive environment for long positions")

    lines.append("")
    lines.append(f"Composite Signal: {composite:+.2f} ({label})")
    lines.append(f"Bullish Dimensions: {bullish_count}/{total_dims} | Neutral: {neutral_count}/{total_dims} | Bearish: {bearish_count}/{total_dims}")
    lines.append(f"Average Confidence: {avg_confidence:.2f}")
    lines.append(f"Signal Disagreement: {disagreement_label} (std={disagreement:.2f})")
    if disagreement_label == "HIGH":
        lines.append("  -> Elevated uncertainty — potential for large moves in either direction")
    if data_completeness < 1.0:
        lines.append(f"Data Completeness: {data_completeness:.0%} (mode-adjusted)")
    if missing_signals:
        lines.append(f"Missing Signals: {', '.join(missing_signals)} (treated as neutral)")
    inferred_signals = [
        section for section, sig in llm_signals.items() if sig.get("inferred")
    ]
    if inferred_signals:
        lines.append(
            "Inferred Signals (no SIGNAL_JSON, lower confidence): "
            + ", ".join(inferred_signals)
        )

    # Factor Scorecard (injected when available from prior screening run)
    factor_scorecard = state.get("factor_scorecard")
    if isinstance(factor_scorecard, dict) and factor_scorecard:
        lines.append("")
        lines.append("Factor Scorecard (screening-derived, 0-100 per family):")
        for family, score in sorted(factor_scorecard.items()):
            bar_len = int(score / 10)
            bar = ("█" * bar_len).ljust(10)
            lines.append(f"  {family:12s}: {bar} {score:.0f}/100")

    text_block = "\n".join(lines)

    return {
        "llm_signals": llm_signals,
        "data_signals": data_signals,
        "composite": round(composite, 4),
        "composite_label": label,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "neutral_count": neutral_count,
        "total_dimensions": total_dims,
        "avg_confidence": round(avg_confidence, 4),
        "data_completeness": data_completeness,
        "disagreement": round(disagreement, 4),
        "disagreement_label": disagreement_label,
        "missing_signals": missing_signals,
        "inferred_signals": inferred_signals,
        "market_regime": market_regime,
        "index_trend": index_trend,
        "index_stress": index_stress,
        "index_regime": index_regime,
        "index_regime_label": index_regime_label,
        **weight_metadata,
        "text_block": text_block,
    }
