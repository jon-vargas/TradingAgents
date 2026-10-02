"""Contextual report QA checks beyond structural payload validation."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from tradingagents.reporting.position_action import (
    FULL_EXIT_LANGUAGE,
    REDUCE_LANGUAGE,
    WAIT_LANGUAGE,
)

RECOMPUTABLE_CONTEXT_WARNINGS = {
    "signal_json_missing",
    "decision_json_missing",
    "street_consensus_conflict",
    "valuation_conflict",
    "valuation_unreliable",
    "risk_nuance_conflict",
    "citations_thin",
    "decision_conflict",
    "sell_guardrail_weak",
    "deep_transcript_missing",
}

CONTEXT_WARNING_IDS = {
    "street_consensus_conflict",
    "valuation_conflict",
    "valuation_unreliable",
    "risk_nuance_conflict",
    "citations_thin",
    "decision_conflict",
    "sell_guardrail_weak",
    "deep_transcript_missing",
}

CONTEXT_WARNING_LABELS = {
    "street_consensus_conflict": "Street consensus differs from model decision",
    "valuation_conflict": "Scenario valuation differs from model decision",
    "valuation_unreliable": "Scenario valuation is unreliable (pre-profit or capped upside)",
    "risk_nuance_conflict": "Risk debate implies softer sizing than headline",
    "citations_thin": "Citations sparse across analyst sections",
    "decision_conflict": "Explicit decision conflicts with narrative",
    "sell_guardrail_weak": "SELL conflicts with consensus or valuation on weak composite evidence",
    "deep_transcript_missing": "Deep analysis completed without an earnings transcript snapshot",
}

BULLISH_STREET_RATINGS = {
    "strong_buy",
    "buy",
    "outperform",
    "overweight",
    "positive",
}
STREET_STRONG_BUY_RATINGS = {"strong_buy"}
# SELL vs a covered strong_buy is a conflict even when implied upside is in the 40s.
STREET_SELL_UPSIDE_PCT = 40.0
# Scenario books in the mid/high double digits already contradict a SELL.
VALUATION_SELL_BLENDED_UPSIDE_PCT = 75.0
VALUATION_SELL_DCF_MOS_PCT = 40.0
BEARISH_STREET_RATINGS = {
    "strong_sell",
    "sell",
    "underperform",
    "underweight",
    "negative",
}

ANALYST_SECTION_FIELDS = (
    "market_report",
    "fundamentals_report",
    "news_report",
    "sentiment_report",
)

_VENDOR_RE = re.compile(
    r"\b("
    r"yfinance|yahoo finance|yahoo|alpha vantage|finnhub|perplexity|"
    r"verified market snapshot|bloomberg|reuters|sec\.gov|edgar"
    r")\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_HUMAN_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:,)?\s+\d{4}\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"\$\d+(?:,\d{3})*(?:\.\d+)?")


def has_citation(text: str) -> bool:
    """True when a section is grounded with a source, URL, or dated print."""
    if not text:
        return False
    if re.search(r"(https?://|www\.|\[\d+\])", text, re.IGNORECASE):
        return True
    if _VENDOR_RE.search(text):
        return True
    has_price = bool(_PRICE_RE.search(text))
    has_date = bool(_ISO_DATE_RE.search(text) or _HUMAN_DATE_RE.search(text))
    return has_price and has_date


def _has_citation(text: str) -> bool:
    return has_citation(text)


def enrich_state_metadata(state: Dict[str, Any], ticker: str) -> Dict[str, Any]:
    """Attach cached analyst ratings when missing from graph state."""
    if state.get("analyst_ratings"):
        return state
    try:
        from tradingagents.dataflows.yfinance_extended import get_analyst_ratings

        state["analyst_ratings"] = get_analyst_ratings(ticker)
    except Exception:
        state["analyst_ratings"] = {}
    return state


def _normalize_rating(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _extract_decision_json(state: Dict[str, Any]) -> Dict[str, Any]:
    text = state.get("final_trade_decision") or ""
    if not text:
        return {}
    marker = "DECISION_JSON:"
    idx = text.find(marker)
    if idx == -1:
        return {}
    after = text[idx + len(marker):]
    brace_start = after.find("{")
    if brace_start == -1:
        return {}
    depth = 0
    for i in range(brace_start, len(after)):
        ch = after[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(after[brace_start:i + 1])
                    return data if isinstance(data, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}


def _risk_debate_text(state: Dict[str, Any]) -> str:
    risk_state = state.get("risk_debate_state", {})
    if not isinstance(risk_state, dict):
        return ""
    parts = [
        risk_state.get("neutral_history") or "",
        risk_state.get("safe_history") or "",
        risk_state.get("risky_history") or "",
        risk_state.get("judge_decision") or "",
    ]
    return "\n".join(str(part) for part in parts if part)


def detect_citations_thin(state: Dict[str, Any]) -> bool:
    """True when some but not all analyst sections have grounded citations."""
    cited = 0
    present = 0
    for field in ANALYST_SECTION_FIELDS:
        text = state.get(field) or ""
        if not str(text).strip():
            continue
        present += 1
        if _has_citation(text):
            cited += 1
    return present >= 2 and 0 < cited < present


def detect_citations_missing(state: Dict[str, Any]) -> bool:
    """True when analyst sections exist but none carry a grounded citation."""
    cited = 0
    present = 0
    for field in ANALYST_SECTION_FIELDS:
        text = state.get(field) or ""
        if not str(text).strip():
            continue
        present += 1
        if _has_citation(text):
            cited += 1
    return present >= 2 and cited == 0


def detect_street_consensus_conflict(state: Dict[str, Any], decision: str) -> bool:
    ratings = state.get("analyst_ratings") or {}
    if not isinstance(ratings, dict) or not ratings:
        return False

    rating = _normalize_rating(ratings.get("recommendation_key"))
    upside = ratings.get("upside_pct")
    analyst_count = ratings.get("number_of_analysts") or 0
    if not analyst_count:
        return False

    decision_upper = str(decision or "").upper().strip()
    try:
        upside_val = float(upside) if upside is not None else None
    except (TypeError, ValueError):
        upside_val = None

    if decision_upper == "SELL":
        if rating in BULLISH_STREET_RATINGS:
            if rating in STREET_STRONG_BUY_RATINGS:
                return True
            if upside_val is not None and upside_val >= STREET_SELL_UPSIDE_PCT:
                return True
        elif (not rating or rating in {"none", "unknown"}) and upside_val is not None:
            if upside_val >= STREET_SELL_UPSIDE_PCT and int(analyst_count) >= 3:
                return True
    if decision_upper == "BUY":
        if not rating:
            return False
        if rating in BEARISH_STREET_RATINGS:
            return True
        if upside_val is not None and upside_val <= -10:
            return True
    return False


def scenario_valuation_is_reliable(scenario: Dict[str, Any]) -> bool:
    if not isinstance(scenario, dict) or not scenario:
        return True
    reliability = str(scenario.get("valuation_reliability") or "").lower().strip()
    if reliability in {"unreliable", "low"}:
        return False
    if scenario.get("blended_upside_pct_capped"):
        return False
    return True


def detect_valuation_unreliable(state: Dict[str, Any]) -> bool:
    scenario = state.get("scenario_analysis") if isinstance(state.get("scenario_analysis"), dict) else {}
    if not scenario:
        return False
    return not scenario_valuation_is_reliable(scenario)


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def detect_valuation_conflict(state: Dict[str, Any], decision: str) -> bool:
    decision_upper = str(decision or "").upper().strip()
    scenario = state.get("scenario_analysis") if isinstance(state.get("scenario_analysis"), dict) else {}
    if not scenario_valuation_is_reliable(scenario):
        return False
    blended_upside = _float_or_none(scenario.get("blended_upside_pct") if scenario else None)

    intrinsic = state.get("intrinsic_value") if isinstance(state.get("intrinsic_value"), dict) else {}
    dcf_mos = _float_or_none(intrinsic.get("margin_of_safety_pct") if intrinsic else None)

    if decision_upper == "SELL":
        if blended_upside is not None and blended_upside >= VALUATION_SELL_BLENDED_UPSIDE_PCT:
            return True
        if dcf_mos is not None and dcf_mos >= VALUATION_SELL_DCF_MOS_PCT:
            return True
    if decision_upper == "BUY" and blended_upside is not None and blended_upside <= 0:
        return True
    return False


def detect_risk_nuance_conflict(state: Dict[str, Any], decision: str) -> bool:
    """Flag hard SELL/BUY when risk debate language implies a softer action."""
    decision_upper = str(decision or "").upper().strip()
    if decision_upper not in {"BUY", "SELL"}:
        return False

    decision_json = _extract_decision_json(state)
    position_action = str(decision_json.get("position_action") or "").upper().strip()
    if position_action in {"REDUCE", "AVOID", "ADD"}:
        return False

    risk_text = _risk_debate_text(state)
    if not risk_text.strip():
        return False

    has_reduce_language = bool(REDUCE_LANGUAGE.search(risk_text))
    has_full_exit_language = bool(FULL_EXIT_LANGUAGE.search(risk_text))

    if decision_upper == "SELL":
        if has_reduce_language and not has_full_exit_language:
            return True
        if position_action in {"FULL_SELL"} and has_reduce_language and not has_full_exit_language:
            return True
    if decision_upper == "BUY" and WAIT_LANGUAGE.search(risk_text):
        return True
    return False


def detect_sell_guardrail_weak(
    state: Dict[str, Any],
    decision: str,
) -> bool:
    """Return whether the canonical warn-only SELL guardrail applies."""
    try:
        from tradingagents.dataflows.config import get_config
        from tradingagents.graph.signal_aggregator import compute_signal_summary
        from tradingagents.graph.signal_processing import evaluate_sell_guardrail

        ticker = str(state.get("company_of_interest") or "")
        if not ticker:
            return False
        config = get_config()
        composite = compute_signal_summary(
            state,
            ticker,
            data_completeness=config.get("data_completeness", 1.0),
            config=config,
        ).get("composite")
        return bool(
            evaluate_sell_guardrail(
                decision,
                composite,
                str(state.get("risk_profile") or config.get("risk_profile") or "growth"),
                state=state,
                config=config,
            )
        )
    except Exception:
        return False


def classify_warnings(warnings: List[str]) -> Dict[str, List[str]]:
    """Split QA warnings into integrity issues vs contextual tensions."""
    integrity: List[str] = []
    context: List[str] = []
    for warning in warnings or []:
        key = str(warning)
        if key in CONTEXT_WARNING_IDS:
            context.append(key)
        else:
            integrity.append(key)
    return {"integrity": integrity, "context": context, "all": list(warnings or [])}


def format_warning_groups_html(warnings: List[str]) -> str:
    """Render grouped warnings for report appendix."""
    groups = classify_warnings(warnings)
    lines: List[str] = []
    if groups["integrity"]:
        lines.append("**Integrity**")
        for item in groups["integrity"]:
            lines.append(f"- {item}")
    if groups["context"]:
        lines.append("**Context**")
        for item in groups["context"]:
            label = CONTEXT_WARNING_LABELS.get(item, item)
            lines.append(f"- {item}: {label}")
    return "\n".join(lines)


def enrich_analysis_api_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Add decision label and grouped QA warnings for API/UI consumers."""
    from tradingagents.reporting.position_action import (
        format_decision_label,
        parse_position_action_from_json_blob,
    )

    warnings = record.get("qa_warnings") or record.get("report_warnings") or []
    if isinstance(warnings, str):
        try:
            warnings = json.loads(warnings)
        except json.JSONDecodeError:
            warnings = [warnings]

    position_action = (
        record.get("position_action")
        or parse_position_action_from_json_blob(record.get("decision_json"))
    )
    record["qa_warnings"] = warnings
    record["qa_warning_groups"] = classify_warnings(warnings)
    record["position_action"] = position_action
    record["decision_label"] = format_decision_label(record.get("decision"), position_action)
    profile = record.get("investment_profile")
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except json.JSONDecodeError:
            profile = {}
    profile = profile if isinstance(profile, dict) else {}
    record["investment_profile_info"] = {
        "key": profile.get("profile_key") or None,
        "display_name": profile.get("display_name") or "Generic / legacy",
        "resolved_from": profile.get("resolved_from") or "legacy",
    }
    return record


def compute_context_warnings(
    state: Dict[str, Any],
    decision: str,
    *,
    ticker: Optional[str] = None,
) -> List[str]:
    """Return contextual QA warnings derived from analysis state."""
    warnings: List[str] = []
    if not state:
        return warnings

    symbol = ticker or state.get("company_of_interest") or ""
    if symbol:
        enrich_state_metadata(state, str(symbol))

    if detect_citations_thin(state):
        warnings.append("citations_thin")
    if detect_citations_missing(state):
        warnings.append("citations_missing")
    if detect_street_consensus_conflict(state, decision):
        warnings.append("street_consensus_conflict")
    if detect_valuation_conflict(state, decision):
        warnings.append("valuation_conflict")
    if detect_valuation_unreliable(state):
        warnings.append("valuation_unreliable")
    if detect_risk_nuance_conflict(state, decision):
        warnings.append("risk_nuance_conflict")
    if detect_sell_guardrail_weak(state, decision):
        warnings.append("sell_guardrail_weak")
    if _detect_deep_transcript_missing(state):
        warnings.append("deep_transcript_missing")
    return warnings


def _detect_deep_transcript_missing(state: Dict[str, Any]) -> bool:
    mode = str(state.get("analysis_mode") or "").lower().strip()
    if mode != "deep":
        return False
    ticker = str(state.get("company_of_interest") or "")
    from tradingagents.dataflows.instrument_identity import is_commodity_etf

    identity = state.get("instrument_identity") if isinstance(state.get("instrument_identity"), dict) else None
    if is_commodity_etf(ticker, identity):
        return False
    transcript = state.get("earnings_transcript_snapshot") or ""
    if str(transcript).strip():
        return False
    if str(transcript).startswith("[Earnings transcript snapshot unavailable"):
        return True
    return True
