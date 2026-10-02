"""Deterministic book-action guardrails (Growth SELL veto, income BUY veto)."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from tradingagents.graph.signal_processing import extract_explicit_decision
from tradingagents.reporting.position_action import (
    _replace_decision_json_in_text,
    extract_decision_json_from_text,
)

RETAIN_LANGUAGE = re.compile(
    r"\b(retain|maintain|existing)\b",
    re.IGNORECASE,
)
AVOID_INITIATE_LANGUAGE = re.compile(
    r"\b(do not initiate|avoid initiating|already flat)\b",
    re.IGNORECASE,
)
FUNDAMENTAL_BREAK_LANGUAGE = re.compile(
    r"\b("
    r"broken thesis|"
    r"declining revenue|"
    r"revenue collapse|"
    r"margin collapse|"
    r"fcf collapse|"
    r"free cash flow collapse"
    r")\b",
    re.IGNORECASE,
)


def _book_action_enabled(config: Optional[Dict[str, Any]]) -> bool:
    cfg = ((config or {}).get("decision_guardrails") or {}).get("book_action") or {}
    return cfg.get("enabled", True) is not False


def _parse_agent_decisions(state: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    research = extract_explicit_decision(state.get("investment_plan") or "")
    trader_text = state.get("trader_investment_plan") or ""
    trader = extract_explicit_decision(trader_text) if str(trader_text).strip() else None
    return research, trader


def _has_fundamental_thesis_break(
    state: Dict[str, Any],
    research: Optional[str],
    trader: Optional[str],
) -> bool:
    if research == "SELL" or trader == "SELL":
        return True
    combined = "\n".join(
        str(state.get(field) or "")
        for field in ("fundamentals_report", "investment_plan", "final_trade_decision")
    )
    return bool(FUNDAMENTAL_BREAK_LANGUAGE.search(combined))


def _infer_hold_position_action(state: Dict[str, Any]) -> str:
    combined = "\n".join(
        str(state.get(field) or "")
        for field in ("investment_plan", "trader_investment_plan", "final_trade_decision")
    )
    has_retain = bool(RETAIN_LANGUAGE.search(combined))
    has_avoid = bool(AVOID_INITIATE_LANGUAGE.search(combined))
    if has_retain:
        return "HOLD"
    if has_avoid:
        return "AVOID"
    return "HOLD"


def _profile_key(state: Dict[str, Any]) -> str:
    profile = state.get("investment_profile") or {}
    if isinstance(profile, dict):
        key = profile.get("profile_key") or state.get("investment_profile_key")
        if key:
            return str(key).lower().strip()
    raw = state.get("investment_profile_key")
    return str(raw or "").lower().strip()


def enforce_book_action(
    state: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Align final DECISION_JSON with Research/Trader FTP and income profile rules."""
    if not _book_action_enabled(config):
        return {"adjusted": False}

    text = state.get("final_trade_decision") or ""
    decision_json = extract_decision_json_from_text(text)
    if not decision_json:
        return {"adjusted": False}

    original_decision = str(decision_json.get("decision") or "").upper().strip()
    original_action = str(decision_json.get("position_action") or "").upper().strip()
    if original_decision not in {"BUY", "SELL", "HOLD"}:
        return {"adjusted": False}

    research, trader = _parse_agent_decisions(state)
    profile_key = _profile_key(state)

    new_decision = original_decision
    new_action = original_action or None
    reason: Optional[str] = None

    if profile_key == "dividend_income" and research == "HOLD" and original_decision == "BUY":
        new_decision = "HOLD"
        new_action = "HOLD"
        reason = "dividend_income_research_hold_veto_buy"
    elif original_decision == "SELL":
        trader_holdish = trader in (None, "HOLD")
        if (
            research == "HOLD"
            and trader_holdish
            and not _has_fundamental_thesis_break(state, research, trader)
        ):
            new_decision = "HOLD"
            new_action = _infer_hold_position_action(state)
            reason = "research_trader_hold_veto_sell"

    if new_decision == original_decision and (new_action or "") == (original_action or ""):
        return {"adjusted": False}

    decision_json["decision_original"] = original_decision
    if original_action:
        decision_json["position_action_original"] = original_action
    decision_json["decision"] = new_decision
    decision_json["position_action"] = new_action or "HOLD"
    decision_json["decision_overridden"] = True
    decision_json["override_reason"] = reason

    state["final_trade_decision"] = _replace_decision_json_in_text(text, decision_json)
    state["decision_overridden"] = True
    state["decision_original"] = original_decision
    state["override_reason"] = reason
    state["position_action"] = decision_json["position_action"]

    return {
        "adjusted": True,
        "from_decision": original_decision,
        "to_decision": new_decision,
        "from_action": original_action or None,
        "to_action": decision_json["position_action"],
        "reason": reason,
    }


def decision_from_state(state: Dict[str, Any], fallback: str = "") -> str:
    """Return BUY/SELL/HOLD from rewritten final_trade_decision, else fallback."""
    extracted = extract_explicit_decision(state.get("final_trade_decision") or "")
    return extracted or fallback
