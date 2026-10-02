"""Position-action guardrails and display helpers."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

REDUCE_LANGUAGE = re.compile(
    r"\b("
    r"reduce(?:\s+substantially|\s+exposure|\s+position)?|"
    r"trim|"
    r"moderate\s+sell|"
    r"partial(?:ly)?\s+sell|"
    r"scale\s+down|"
    r"de[- ]risk"
    r")\b",
    re.IGNORECASE,
)

FULL_EXIT_LANGUAGE = re.compile(
    r"\b("
    r"full(?:y)?\s+(?:sell|exit|liquidate)|"
    r"eliminate(?:\s+position)?|"
    r"close(?:\s+out)?(?:\s+entire)?\s+position|"
    r"unconditional(?:\s+liquidation)?"
    r")\b",
    re.IGNORECASE,
)

WAIT_LANGUAGE = re.compile(
    r"\b(wait|avoid initiating|do not add|defer entry)\b",
    re.IGNORECASE,
)


def extract_decision_json_from_text(text: str) -> Dict[str, Any]:
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


def infer_position_action(state: Dict[str, Any], decision_json: Dict[str, Any]) -> Optional[str]:
    """Infer a corrected position_action from risk-debate language."""
    decision = str(decision_json.get("decision") or "").upper().strip()
    current = str(decision_json.get("position_action") or "").upper().strip()
    risk_text = _risk_debate_text(state)
    if not risk_text.strip():
        return None

    has_reduce = bool(REDUCE_LANGUAGE.search(risk_text))
    has_full_exit = bool(FULL_EXIT_LANGUAGE.search(risk_text))

    if decision == "SELL" and current in {"", "FULL_SELL"} and has_reduce and not has_full_exit:
        return "REDUCE"
    if decision == "BUY" and current in {"", "FULL_BUY"} and WAIT_LANGUAGE.search(risk_text):
        return "AVOID"
    return None


def _replace_decision_json_in_text(text: str, decision_json: Dict[str, Any]) -> str:
    marker = "DECISION_JSON:"
    idx = text.find(marker)
    if idx == -1:
        return text
    after = text[idx + len(marker):]
    brace_start = after.find("{")
    if brace_start == -1:
        return text
    depth = 0
    end = None
    for i in range(brace_start, len(after)):
        ch = after[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return text
    replacement = json.dumps(decision_json, separators=(",", ":"))
    start = idx + len(marker) + brace_start
    finish = idx + len(marker) + end + 1
    return text[:start] + replacement + text[finish:]


def enforce_position_action(state: Dict[str, Any]) -> Dict[str, Any]:
    """Align position_action with risk-debate nuance when the model overstates sizing."""
    text = state.get("final_trade_decision") or ""
    decision_json = extract_decision_json_from_text(text)
    if not decision_json:
        return {"adjusted": False}

    corrected = infer_position_action(state, decision_json)
    if not corrected:
        return {"adjusted": False}

    original = str(decision_json.get("position_action") or "").upper().strip() or None
    decision_json["position_action"] = corrected
    decision_json["position_action_corrected"] = True
    if original:
        decision_json["position_action_original"] = original

    state["final_trade_decision"] = _replace_decision_json_in_text(text, decision_json)
    state["position_action"] = corrected
    state["position_action_corrected"] = True
    return {
        "adjusted": True,
        "from": original,
        "to": corrected,
        "reason": "risk_debate_nuance",
    }


def format_decision_label(decision: str, position_action: Optional[str] = None) -> str:
    """Human-readable decision label for UI (e.g. SELL (Reduce))."""
    base = str(decision or "").upper().strip() or "-"
    action = str(position_action or "").upper().strip()

    if base == "SELL":
        if action == "REDUCE":
            return "SELL (Reduce)"
        if action == "FULL_SELL":
            return "SELL (Full Exit)"
        if action == "AVOID":
            return "SELL (Avoid)"
    if base == "BUY":
        if action == "ADD":
            return "BUY (Add)"
        if action == "FULL_BUY":
            return "BUY (Full)"
        if action == "AVOID":
            return "BUY (Avoid)"
    if base == "HOLD":
        if action == "REDUCE":
            return "HOLD (Trim)"
        if action == "AVOID":
            return "HOLD (Avoid)"
        if action == "HOLD":
            return "HOLD (Maintain)"
    return base


def parse_position_action_from_state(state: Dict[str, Any]) -> str:
    if state.get("position_action"):
        return str(state["position_action"]).upper()
    data = extract_decision_json_from_text(state.get("final_trade_decision") or "")
    return str(data.get("position_action") or "").upper()


def parse_position_action_from_json_blob(decision_json: Any) -> str:
    if isinstance(decision_json, dict):
        return str(decision_json.get("position_action") or "").upper()
    if not decision_json:
        return ""
    try:
        data = json.loads(decision_json)
        if isinstance(data, dict):
            return str(data.get("position_action") or "").upper()
    except (TypeError, json.JSONDecodeError):
        pass
    return ""
