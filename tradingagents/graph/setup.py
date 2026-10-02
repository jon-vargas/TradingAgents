# TradingAgents/graph/setup.py

from typing import Dict, Any, List
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph, START
from langgraph.prebuilt import ToolNode

from tradingagents.agents import *
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.dataflows.config import get_config

from .analyst_subgraph import compile_analyst_subgraph, wrap_subgraph_for_parent
from .conditional_logic import ConditionalLogic

# Every target a shared conditional router can return. Each edge driven by the
# router maps all of them, so a fall-through return (e.g. speaker-label drift)
# can never hit a missing path_map entry and crash LangGraph mid-run.
DEBATE_PATH_MAP = {
    "Bull Researcher": "Bull Researcher",
    "Bear Researcher": "Bear Researcher",
    "Research Manager": "Research Manager",
}
RISK_ANALYSIS_PATH_MAP = {
    "Risky Analyst": "Risky Analyst",
    "Safe Analyst": "Safe Analyst",
    "Neutral Analyst": "Neutral Analyst",
    "Risk Judge": "Risk Judge",
}


def resolve_analyst_layout(config: Dict[str, Any] | None = None) -> str:
    cfg = config or get_config()
    layout = str((cfg.get("analysis") or {}).get("analyst_layout") or "parallel").lower()
    return layout if layout in ("parallel", "sequential") else "parallel"


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: ChatOpenAI,
        deep_thinking_llm: ChatOpenAI,
        tool_nodes: Dict[str, ToolNode],
        bull_memory,
        bear_memory,
        trader_memory,
        invest_judge_memory,
        risk_manager_memory,
        conditional_logic: ConditionalLogic,
        risk_profile: str = "conservative",
        config: Dict[str, Any] | None = None,
    ):
        """Initialize with required components."""
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.tool_nodes = tool_nodes
        self.bull_memory = bull_memory
        self.bear_memory = bear_memory
        self.trader_memory = trader_memory
        self.invest_judge_memory = invest_judge_memory
        self.risk_manager_memory = risk_manager_memory
        self.conditional_logic = conditional_logic
        self.risk_profile = risk_profile
        self.config = config

    def _build_analyst_nodes(
        self, selected_analysts: List[str]
    ) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, ToolNode]]:
        analyst_nodes = {}
        delete_nodes = {}
        tool_nodes = {}

        if "market" in selected_analysts:
            analyst_nodes["market"] = create_market_analyst(self.quick_thinking_llm)
            delete_nodes["market"] = create_msg_delete()
            tool_nodes["market"] = self.tool_nodes["market"]

        if "social" in selected_analysts:
            analyst_nodes["social"] = create_social_media_analyst(self.quick_thinking_llm)
            delete_nodes["social"] = create_msg_delete()
            tool_nodes["social"] = self.tool_nodes["social"]

        if "news" in selected_analysts:
            analyst_nodes["news"] = create_news_analyst(self.quick_thinking_llm)
            delete_nodes["news"] = create_msg_delete()
            tool_nodes["news"] = self.tool_nodes["news"]

        if "fundamentals" in selected_analysts:
            analyst_nodes["fundamentals"] = create_fundamentals_analyst(
                self.quick_thinking_llm
            )
            delete_nodes["fundamentals"] = create_msg_delete()
            tool_nodes["fundamentals"] = self.tool_nodes["fundamentals"]

        return analyst_nodes, delete_nodes, tool_nodes

    def _wire_sequential_analysts(
        self,
        workflow: StateGraph,
        selected_analysts: List[str],
        analyst_nodes: Dict[str, Any],
        delete_nodes: Dict[str, Any],
        tool_nodes: Dict[str, ToolNode],
    ) -> None:
        for analyst_type, node in analyst_nodes.items():
            workflow.add_node(f"{analyst_type.capitalize()} Analyst", node)
            workflow.add_node(
                f"Msg Clear {analyst_type.capitalize()}", delete_nodes[analyst_type]
            )
            workflow.add_node(f"tools_{analyst_type}", tool_nodes[analyst_type])

        first_analyst = selected_analysts[0]
        workflow.add_edge(START, f"{first_analyst.capitalize()} Analyst")

        for i, analyst_type in enumerate(selected_analysts):
            current_analyst = f"{analyst_type.capitalize()} Analyst"
            current_tools = f"tools_{analyst_type}"
            current_clear = f"Msg Clear {analyst_type.capitalize()}"

            workflow.add_conditional_edges(
                current_analyst,
                getattr(self.conditional_logic, f"should_continue_{analyst_type}"),
                [current_tools, current_clear],
            )
            workflow.add_edge(current_tools, current_analyst)

            if i < len(selected_analysts) - 1:
                next_analyst = f"{selected_analysts[i+1].capitalize()} Analyst"
                workflow.add_edge(current_clear, next_analyst)
            else:
                workflow.add_edge(current_clear, "Bull Researcher")

    def _wire_parallel_analysts(
        self,
        workflow: StateGraph,
        selected_analysts: List[str],
        analyst_nodes: Dict[str, Any],
        tool_nodes: Dict[str, ToolNode],
    ) -> List[str]:
        parent_nodes: List[str] = []
        for analyst_type in selected_analysts:
            if analyst_type not in analyst_nodes:
                continue
            label = f"{analyst_type.capitalize()} Analyst"
            compiled = compile_analyst_subgraph(
                analyst_type, analyst_nodes[analyst_type], tool_nodes[analyst_type]
            )
            workflow.add_node(label, wrap_subgraph_for_parent(compiled, analyst_type))
            parent_nodes.append(label)

        for label in parent_nodes:
            workflow.add_edge(START, label)
        return parent_nodes

    def setup_graph(
        self, selected_analysts=["market", "social", "news", "fundamentals"]
    ):
        """Set up the agent workflow graph (uncompiled StateGraph)."""
        if len(selected_analysts) == 0:
            raise ValueError("Trading Agents Graph Setup Error: no analysts selected!")

        analyst_nodes, delete_nodes, tool_nodes = self._build_analyst_nodes(selected_analysts)
        layout = resolve_analyst_layout(self.config)

        bull_researcher_node = create_bull_researcher(
            self.quick_thinking_llm, self.bull_memory
        )
        bear_researcher_node = create_bear_researcher(
            self.quick_thinking_llm, self.bear_memory
        )
        research_manager_node = create_research_manager(
            self.deep_thinking_llm, self.invest_judge_memory
        )
        trader_node = create_trader(self.quick_thinking_llm, self.trader_memory)

        risky_analyst = create_risky_debator(self.quick_thinking_llm)
        neutral_analyst = create_neutral_debator(self.quick_thinking_llm)
        safe_analyst = create_safe_debator(self.quick_thinking_llm)
        risk_manager_node = create_risk_manager(
            self.deep_thinking_llm, self.risk_manager_memory, self.risk_profile
        )

        workflow = StateGraph(AgentState)

        parallel_analyst_nodes: List[str] = []
        if layout == "parallel":
            parallel_analyst_nodes = self._wire_parallel_analysts(
                workflow, selected_analysts, analyst_nodes, tool_nodes
            )
        else:
            self._wire_sequential_analysts(
                workflow, selected_analysts, analyst_nodes, delete_nodes, tool_nodes
            )

        workflow.add_node("Bull Researcher", bull_researcher_node)
        if layout == "parallel" and parallel_analyst_nodes:
            if len(parallel_analyst_nodes) == 1:
                workflow.add_edge(parallel_analyst_nodes[0], "Bull Researcher")
            else:
                workflow.add_edge(parallel_analyst_nodes, "Bull Researcher")
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Risky Analyst", risky_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Safe Analyst", safe_analyst)
        workflow.add_node("Risk Judge", risk_manager_node)

        workflow.add_conditional_edges(
            "Bull Researcher",
            self.conditional_logic.should_continue_debate,
            DEBATE_PATH_MAP,
        )
        workflow.add_conditional_edges(
            "Bear Researcher",
            self.conditional_logic.should_continue_debate,
            DEBATE_PATH_MAP,
        )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Risky Analyst")
        workflow.add_conditional_edges(
            "Risky Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            RISK_ANALYSIS_PATH_MAP,
        )
        workflow.add_conditional_edges(
            "Safe Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            RISK_ANALYSIS_PATH_MAP,
        )
        workflow.add_conditional_edges(
            "Neutral Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            RISK_ANALYSIS_PATH_MAP,
        )

        workflow.add_edge("Risk Judge", END)

        return workflow
