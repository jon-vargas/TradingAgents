"""Shared analyst tool-round cap and finalize behavior (#1420 port)."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage

logger = logging.getLogger("tradingagents.agents.analyst_finalize")

_ANALYST_BUDGET_KEYS = {
    "market": "market_max_tool_messages",
    "social": "social_max_tool_messages",
    "news": "news_max_tool_messages",
    "fundamentals": "fundamentals_max_tool_messages",
}

_REPORT_KEYS = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}

_FINALIZED_FLAG_KEYS = {
    "market": "analyst_finalized_market",
    "social": "analyst_finalized_social",
    "news": "analyst_finalized_news",
    "fundamentals": "analyst_finalized_fundamentals",
}


def effective_tool_message_cap(config: Dict[str, Any], analyst_type: str, default_cap: int) -> int:
    """min(analysis.max_tool_rounds, token_budget per-analyst cap)."""
    analysis_cfg = config.get("analysis") or {}
    max_rounds = int(analysis_cfg.get("max_tool_rounds") or 20)
    token_budget = config.get("token_budget") or {}
    analyst_budget = token_budget.get("analyst") or {}
    budget_key = _ANALYST_BUDGET_KEYS.get(analyst_type, "")
    per_analyst = int(analyst_budget.get(budget_key, default_cap) or default_cap)
    return max(1, min(max_rounds, per_analyst))


def count_tool_messages(messages: List[Any]) -> int:
    return sum(1 for message in messages if getattr(message, "type", "") == "tool")


def should_force_finalize(
    tool_messages: int,
    max_tool_messages: int,
    *,
    extra_trigger: bool = False,
) -> bool:
    return tool_messages >= max_tool_messages or extra_trigger


def strip_tool_calls_from_message(message: AIMessage) -> AIMessage:
    """Ensure conditional routers do not re-enter tools after finalize."""
    if not getattr(message, "tool_calls", None):
        return message
    content = message.content
    if isinstance(content, list):
        text = " ".join(
            str(block.get("text", block)) if isinstance(block, dict) else str(block)
            for block in content
        )
    else:
        text = str(content or "")
    return AIMessage(content=text or "Report synthesis complete (tool cap reached).")


def build_finalize_invoke_messages(invoke_messages: List[Any]) -> List[Any]:
    return list(invoke_messages) + [
        HumanMessage(
            content=(
                "You have reached the tool call limit. Write your full analyst report now "
                "using the data already retrieved. Do not request any more tools."
            )
        )
    ]


def finalize_analyst_result(
    analyst_type: str,
    result: AIMessage,
    *,
    force_finalize: bool,
) -> Tuple[AIMessage, str, Dict[str, Any]]:
    """Normalize LLM output after invoke; persist report only when finalizing."""
    report_key = _REPORT_KEYS[analyst_type]
    flag_key = _FINALIZED_FLAG_KEYS[analyst_type]

    pending_tools = getattr(result, "tool_calls", None)
    if not force_finalize and pending_tools:
        # Mid-loop turn: router must see tool_calls; do not stub a report yet.
        return result, "", {"messages": [result]}

    normalized = result
    if force_finalize and pending_tools:
        normalized = strip_tool_calls_from_message(result)

    report = ""
    if not getattr(normalized, "tool_calls", None):
        content = normalized.content
        if isinstance(content, str):
            report = content
        elif content is not None:
            report = str(content)
    elif force_finalize:
        report = str(normalized.content or "")

    state_updates: Dict[str, Any] = {
        "messages": [normalized],
        report_key: report,
    }
    if force_finalize or report:
        state_updates[flag_key] = True
    return normalized, report, state_updates


def invoke_analyst_chain(
    *,
    analyst_type: str,
    config: Dict[str, Any],
    default_cap: int,
    invoke_messages: List[Any],
    prompt: Any,
    llm: Any,
    tools: List[Any],
    extra_finalize_trigger: bool = False,
    extra_finalize_predicate: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    max_tool_messages = effective_tool_message_cap(config, analyst_type, default_cap)
    tool_messages = count_tool_messages(invoke_messages)
    extra = extra_finalize_trigger or (extra_finalize_predicate() if extra_finalize_predicate else False)
    force_finalize = should_force_finalize(tool_messages, max_tool_messages, extra_trigger=extra)

    if force_finalize:
        logger.warning(
            "%s analyst forcing finalize (tool_messages=%d/%d)",
            analyst_type,
            tool_messages,
            max_tool_messages,
        )
        chain = prompt | llm
        messages_in = build_finalize_invoke_messages(invoke_messages)
    else:
        try:
            chain = prompt | llm.bind_tools(tools, parallel_tool_calls=False)
        except TypeError:
            chain = prompt | llm.bind_tools(tools)
        messages_in = invoke_messages

    result = chain.invoke(messages_in)
    if not isinstance(result, AIMessage):
        result = AIMessage(content=str(result))

    _, _, state_updates = finalize_analyst_result(analyst_type, result, force_finalize=force_finalize)
    if not force_finalize and len(getattr(result, "tool_calls", None) or []) == 0:
        content = result.content
        state_updates[_REPORT_KEYS[analyst_type]] = content if isinstance(content, str) else str(content or "")

    return state_updates
