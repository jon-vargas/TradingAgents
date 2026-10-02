"""Per-analyst compiled subgraphs for parallel layout (#1255)."""

from __future__ import annotations

from typing import Any, Callable, Dict, FrozenSet, Set

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.analysts.analyst_finalize import (
    _FINALIZED_FLAG_KEYS,
    _REPORT_KEYS,
)
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.graph.analyst_context import run_with_parent_context

_ANALYST_EXTRA_OUTPUT: Dict[str, FrozenSet[str]] = {
    "market": frozenset(),
    "social": frozenset(),
    "news": frozenset({"catalyst_pipeline"}),
    "fundamentals": frozenset(
        {"earnings_quality", "intrinsic_value", "scenario_analysis", "peer_comps"}
    ),
}


def output_keys_for_analyst(analyst_type: str) -> Set[str]:
    keys = {
        _REPORT_KEYS[analyst_type],
        _FINALIZED_FLAG_KEYS[analyst_type],
    }
    keys.update(_ANALYST_EXTRA_OUTPUT.get(analyst_type, frozenset()))
    return keys


def strip_parent_update(update: Dict[str, Any], analyst_type: str) -> Dict[str, Any]:
    """Drop messages and unrelated keys before merging into parent state."""
    allowed = output_keys_for_analyst(analyst_type)
    return {k: v for k, v in update.items() if k in allowed}


def _tools_or_end(state: AgentState, *, analyst_type: str, tools_node: str) -> str:
    from tradingagents.graph.conditional_logic import ConditionalLogic

    report_key = _REPORT_KEYS[analyst_type]
    finalized_key = _FINALIZED_FLAG_KEYS[analyst_type]
    messages = state.get("messages") or []
    if messages:
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return tools_node
    if ConditionalLogic._analyst_done(state, report_key, finalized_key):
        return END
    if not messages:
        return END
    return END


def compile_analyst_subgraph(
    analyst_type: str,
    agent_node: Callable,
    tool_node: ToolNode,
) -> Any:
    """One analyst as a private message loop; parent receives report fields only."""

    def agent_step(state: AgentState) -> Dict[str, Any]:
        # Parent ``wrap_subgraph_for_parent`` already runs ``invoke`` inside
        # ``run_with_parent_context``. Re-entering the same Context here raises
        # "cannot enter context: ... is already entered".
        return agent_node(state)

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", agent_step)
    workflow.add_node("tools", tool_node)
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        lambda s: _tools_or_end(s, analyst_type=analyst_type, tools_node="tools"),
        ["tools", END],
    )
    workflow.add_edge("tools", "agent")
    return workflow.compile()


def wrap_subgraph_for_parent(compiled_subgraph: Any, analyst_type: str) -> Callable:
    """Parent node: run subgraph on full state; merge reports only."""

    def parent_node(state: AgentState) -> Dict[str, Any]:
        out = run_with_parent_context(compiled_subgraph.invoke, state)
        if not isinstance(out, dict):
            return {}
        return strip_parent_update(out, analyst_type)

    return parent_node
