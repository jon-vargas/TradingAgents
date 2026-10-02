# TradingAgents/graph/signal_processing.py

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

from langchain_openai import ChatOpenAI

logger = logging.getLogger("tradingagents.graph.signal_processing")

VALID_DECISIONS = {"BUY", "SELL", "HOLD"}

# ── Programmatic decision threshold guardrails ───────────────────────────
# Maps risk_profile → {decision → minimum composite threshold}.
# A decision is valid only if the composite meets or exceeds its threshold.
DECISION_THRESHOLDS = {
    "aggressive": {
        "BUY": -0.10,
        "SELL": None,
        "HOLD": None,
    },
    "growth": {
        "BUY": 0.15,
        "SELL": None,
        "HOLD": None,
    },
    "conservative": {
        "BUY": 0.35,
        "SELL": None,
        "HOLD": None,
    },
}


def _has_deterministic_fundamental_break(state: Optional[Dict[str, Any]]) -> bool:
    """Only a dual bearish fundamentals/research signal suppresses SELL warnings."""
    if not state:
        return False
    try:
        from tradingagents.reporting.attribution import _extract_signal_json
        from tradingagents.graph.signal_aggregator import _stance_to_score

        scores = []
        for field in ("fundamentals_report", "investment_plan"):
            signal = _extract_signal_json(state.get(field) or "")
            if not signal:
                return False
            scores.append(_stance_to_score(signal.get("stance", "")))
        return len(scores) == 2 and all(score < 0 for score in scores)
    except Exception:
        return False


def evaluate_sell_guardrail(
    decision: str,
    composite: Optional[float],
    risk_profile: str,
    state: Optional[Dict[str, Any]] = None,
    config: Optional[dict] = None,
) -> Optional[str]:
    """Return a warning reason for a weak conflicted SELL, else ``None``."""
    if str(decision or "").upper() != "SELL" or composite is None:
        return None
    sell_cfg = ((config or {}).get("decision_guardrails", {}).get("sell") or {})
    if not sell_cfg.get("enabled", False) or _has_deterministic_fundamental_break(state):
        return None
    profile = str(risk_profile or "growth").lower().strip()
    threshold = (sell_cfg.get("conflicted_min_composite") or {}).get(profile)
    if threshold is None or float(composite) <= float(threshold):
        return None
    try:
        from tradingagents.reporting.context_qc import (
            detect_street_consensus_conflict,
            detect_valuation_conflict,
        )

        street_conflict = detect_street_consensus_conflict(state or {}, "SELL")
        valuation_conflict = detect_valuation_conflict(state or {}, "SELL")
    except Exception:
        return None
    if not (street_conflict or valuation_conflict):
        return None
    conflicts = []
    if street_conflict:
        conflicts.append("street consensus")
    if valuation_conflict:
        conflicts.append("scenario valuation")
    return (
        f"composite {float(composite):+.2f} above conflicted SELL floor "
        f"{float(threshold):+.2f} with {' and '.join(conflicts)} conflict"
    )


def validate_decision(
    decision: str,
    composite: Optional[float],
    risk_profile: str,
    config: Optional[dict] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Check whether the LLM's decision is consistent with the risk profile thresholds.

    Returns a dict with:
      - final_decision: the decision to use (may differ from original if enforced)
      - original_decision: what the LLM said
      - override_reason: explanation if overridden, else None
      - guardrail_triggered: bool
    """
    result = {
        "final_decision": decision,
        "original_decision": decision,
        "override_reason": None,
        "guardrail_triggered": False,
        "buy_guardrail": None,
        "sell_guardrail": None,
        "severity": None,
    }

    guardrail_cfg = (config or {}).get("decision_guardrails", {})
    if not guardrail_cfg.get("enabled", False):
        return result

    if composite is None:
        return result

    thresholds = DECISION_THRESHOLDS.get(risk_profile, {})
    min_composite = thresholds.get(decision)
    if min_composite is not None and composite < min_composite:
        reason = (
            f"composite {composite:+.2f} below {risk_profile} "
            f"{decision} threshold {min_composite:+.2f}"
        )
        result["guardrail_triggered"] = True
        result["override_reason"] = reason
        result["buy_guardrail"] = reason
        result["severity"] = "warning"

        mode = guardrail_cfg.get("mode", "warn")
        if mode == "enforce":
            result["final_decision"] = "HOLD"
            logger.warning(
                "Decision guardrail ENFORCED: %s → HOLD (%s)", decision, reason,
            )
        else:
            logger.warning(
                "Decision guardrail WARNING: %s may violate thresholds (%s)",
                decision, reason,
            )

    sell_reason = evaluate_sell_guardrail(
        decision, composite, risk_profile, state=state, config=config
    )
    if sell_reason:
        result["guardrail_triggered"] = True
        result["sell_guardrail"] = sell_reason
        result["severity"] = "warning"
        logger.warning("SELL guardrail WARNING: %s", sell_reason)

    return result


def _extract_decision_json(text: str) -> Optional[dict]:
    """Try to extract and parse DECISION_JSON from the Risk Manager output."""
    if not text:
        return None
    marker = "DECISION_JSON:"
    idx = text.find(marker)
    if idx == -1:
        return None
    after = text[idx + len(marker):]
    brace_start = after.find("{")
    if brace_start == -1:
        return None
    depth = 0
    for i in range(brace_start, len(after)):
        ch = after[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = after[brace_start:i + 1]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
    return None


def _extract_decision_from_text(text: str) -> Optional[str]:
    """Regex fallback: look for explicit BUY/SELL/HOLD in common patterns."""
    if not text:
        return None
    patterns = [
        r"\*{0,2}Decision:\s*\*{0,2}\s*(BUY|SELL|HOLD)",
        r"(?:FINAL\s+(?:TRANSACTION\s+)?(?:PROPOSAL|RECOMMENDATION|DECISION))\s*:?\s*\*{0,2}\s*(BUY|SELL|HOLD)",
        r'"decision"\s*:\s*"(BUY|SELL|HOLD)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def extract_explicit_decision(text: str) -> Optional[str]:
    """Return BUY/SELL/HOLD from DECISION_JSON or an explicit decision header."""
    data = _extract_decision_json(text)
    if data:
        decision = str(data.get("decision") or "").upper()
        if decision in VALID_DECISIONS:
            return decision
    regex_decision = _extract_decision_from_text(text)
    if regex_decision in VALID_DECISIONS:
        return regex_decision
    return None


class SignalProcessor:
    """Processes trading signals to extract actionable decisions."""

    def __init__(self, quick_thinking_llm: ChatOpenAI):
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, full_signal: str) -> str:
        """
        Extract the core decision from the Risk Manager's output.

        Priority:
        1. Parse DECISION_JSON deterministically
        2. Regex fallback for common patterns
        3. LLM fallback (original behavior)
        """
        # 1. Try deterministic DECISION_JSON extraction
        decision_data = _extract_decision_json(full_signal)
        if decision_data:
            decision = str(decision_data.get("decision", "")).upper()
            if decision in VALID_DECISIONS:
                conviction = decision_data.get("conviction", "medium")
                logger.info(
                    "Signal extracted via DECISION_JSON: %s (conviction: %s)",
                    decision, conviction,
                )
                return decision

        # 2. Regex fallback
        regex_decision = _extract_decision_from_text(full_signal)
        if regex_decision and regex_decision in VALID_DECISIONS:
            logger.info("Signal extracted via regex: %s", regex_decision)
            return regex_decision

        # 3. LLM fallback
        logger.info("Falling back to LLM for signal extraction")
        messages = [
            (
                "system",
                "You are an efficient assistant designed to analyze paragraphs or financial reports provided by a group of analysts. Your task is to extract the investment decision: SELL, BUY, or HOLD. Provide only the extracted decision (SELL, BUY, or HOLD) as your output, without adding any additional text or information.",
            ),
            ("human", full_signal),
        ]
        raw = self.quick_thinking_llm.invoke(messages).content.strip().upper()
        if raw in VALID_DECISIONS:
            logger.info("LLM fallback returned valid decision: %s", raw)
            return raw
        for token in VALID_DECISIONS:
            if token in raw:
                logger.info("LLM fallback contained '%s' in response: %s", token, raw[:80])
                return token
        logger.warning(
            "LLM fallback returned unrecognized output '%s' — defaulting to HOLD",
            raw[:120],
        )
        return "HOLD"
