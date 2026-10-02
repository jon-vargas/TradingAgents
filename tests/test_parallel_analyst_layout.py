"""Parallel analyst graph layout, context copy, and run settings (#1255 / #752)."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.agents.utils.tool_context import get_current_ticker, set_current_ticker
from tradingagents.dataflows.perplexity_budget import (
    get_live_count,
    has_live_budget,
    register_live_call,
    reset_run_budget,
)
from tradingagents.dataflows.provenance import get_events, record_event, start_run
from tradingagents.graph.analyst_context import run_with_parent_context
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup, resolve_analyst_layout
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.analyst_context import bind_run_context, clear_run_context
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting.pdf_generator import render_run_settings_html


def _graph_setup(config=None) -> GraphSetup:
    tool_nodes = {k: ToolNode([]) for k in ("market", "social", "news", "fundamentals")}
    return GraphSetup(
        MagicMock(),
        MagicMock(),
        tool_nodes,
        None,
        None,
        None,
        None,
        None,
        ConditionalLogic(),
        config=config,
    )


class TestParallelAnalystLayout(unittest.TestCase):
    def test_resolve_analyst_layout_defaults_parallel(self):
        self.assertEqual(resolve_analyst_layout({}), "parallel")

    def test_parallel_graph_fan_out_and_join(self):
        cfg = {"analysis": {"analyst_layout": "parallel"}}
        workflow = _graph_setup(cfg).setup_graph(["market", "news"])
        compiled = workflow.compile()
        graph = compiled.get_graph()
        node_names = set(graph.nodes)
        self.assertIn("Market Analyst", node_names)
        self.assertIn("News Analyst", node_names)
        self.assertFalse(any(name.startswith("Msg Clear") for name in node_names))
        self.assertFalse(any(name.startswith("tools_") for name in node_names))

        start_targets = {edge[1] for edge in graph.edges if edge[0] == START}
        self.assertIn("Market Analyst", start_targets)
        self.assertIn("News Analyst", start_targets)

        bull_sources = {edge[0] for edge in graph.edges if edge[1] == "Bull Researcher"}
        self.assertIn("Market Analyst", bull_sources)
        self.assertIn("News Analyst", bull_sources)

    def test_sequential_graph_still_chains(self):
        cfg = {"analysis": {"analyst_layout": "sequential"}}
        workflow = _graph_setup(cfg).setup_graph(["market", "social"])
        graph = workflow.compile().get_graph()
        self.assertIn("Msg Clear Market", set(graph.nodes))
        self.assertIn("tools_market", set(graph.nodes))

    def test_fan_in_waits_for_both_branches(self):
        order: list[str] = []

        def branch_a(_state):
            order.append("a")
            return {"market_report": "A"}

        def branch_b(_state):
            order.append("b")
            return {"news_report": "B"}

        def join(_state):
            order.append("join")
            return {}

        wf = StateGraph(AgentState)
        wf.add_node("A", branch_a)
        wf.add_node("B", branch_b)
        wf.add_node("Join", join)
        wf.add_edge(START, "A")
        wf.add_edge(START, "B")
        wf.add_edge(["A", "B"], "Join")
        wf.add_edge("Join", END)
        wf.compile().invoke({"messages": []})
        self.assertGreater(order.index("join"), order.index("a"))
        self.assertGreater(order.index("join"), order.index("b"))

    def test_context_copy_on_worker_threads(self):
        token = set_current_ticker("PARALLEL")
        bind_run_context()
        try:
            def read_ticker():
                return get_current_ticker()

            with ThreadPoolExecutor(max_workers=2) as pool:
                copied = list(pool.map(lambda _: run_with_parent_context(read_ticker), range(2)))
            self.assertEqual(copied, ["PARALLEL", "PARALLEL"])

            start_run()
            bind_run_context()

            def record():
                record_event({"tool": "mock", "ticker": get_current_ticker()})

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: run_with_parent_context(record), range(2)))
            events = get_events()
            self.assertEqual(len(events), 2)
            self.assertTrue(all(e.get("ticker") == "PARALLEL" for e in events))
        finally:
            from tradingagents.agents.utils.tool_context import reset_current_ticker

            clear_run_context()
            reset_current_ticker(token)

    def test_parallel_subgraph_parents_do_not_share_context_instance(self):
        """Four analyst branches may invoke parent_node concurrently."""
        from langgraph.prebuilt import ToolNode

        from tradingagents.graph.analyst_subgraph import (
            compile_analyst_subgraph,
            wrap_subgraph_for_parent,
        )

        token = set_current_ticker("QUAD")
        bind_run_context()
        try:

            def fake_agent(state):
                return {"messages": state.get("messages") or []}

            parent = wrap_subgraph_for_parent(
                compile_analyst_subgraph("market", fake_agent, ToolNode([])),
                "market",
            )

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: parent({"messages": []}), range(4)))
        finally:
            from tradingagents.agents.utils.tool_context import reset_current_ticker

            clear_run_context()
            reset_current_ticker(token)

    def test_nested_subgraph_invoke_does_not_reenter_context(self):
        """Subgraph agent steps run inside parent ctx.run — no double enter."""
        from tradingagents.graph.analyst_subgraph import compile_analyst_subgraph
        from langgraph.prebuilt import ToolNode

        token = set_current_ticker("NESTED")
        bind_run_context()
        try:

            def fake_agent(state):
                return {"messages": state.get("messages") or []}

            compiled = compile_analyst_subgraph("market", fake_agent, ToolNode([]))
            from tradingagents.graph.analyst_subgraph import wrap_subgraph_for_parent

            parent = wrap_subgraph_for_parent(compiled, "market")
            parent({"messages": []})
        finally:
            from tradingagents.agents.utils.tool_context import reset_current_ticker

            clear_run_context()
            reset_current_ticker(token)

    def test_parallel_workers_share_perplexity_run_budget(self):
        reset_run_budget(max_live_calls=2)
        bind_run_context()
        try:
            def spend(_i):
                if has_live_budget("get_deep_research"):
                    register_live_call("get_deep_research")
                    return True
                return False

            with ThreadPoolExecutor(max_workers=2) as pool:
                spent = list(pool.map(lambda i: run_with_parent_context(spend, i), range(2)))
            self.assertEqual(sum(1 for ok in spent if ok), 2)
            self.assertEqual(get_live_count(), 2)
            self.assertFalse(has_live_budget("get_deep_research"))
        finally:
            clear_run_context()

    @patch("tradingagents.graph.trading_graph.TradingAgentsGraph.__init__", return_value=None)
    def test_run_settings_allowlist(self, _mock_init):
        cfg = DEFAULT_CONFIG.copy()
        cfg.update(
            {
                "llm_provider": "openai",
                "deep_think_llm": "deep-model",
                "quick_think_llm": "quick-model",
                "analysis_mode": "standard",
                "risk_profile": "growth",
                "max_debate_rounds": 2,
                "max_risk_discuss_rounds": 1,
                "backend_url": "http://secret",
                "data_vendors": {"ohlcv": "yfinance"},
                "tool_vendors": {"news": "perplexity"},
            }
        )
        cfg["analysis"] = {**(cfg.get("analysis") or {}), "analyst_layout": "parallel"}
        graph = TradingAgentsGraph(["market", "news"], config=cfg)
        graph.config = cfg
        graph.selected_analysts = ["market", "news"]
        settings = graph.run_settings()
        self.assertEqual(settings["analysts"], ["market", "news"])
        self.assertEqual(settings["data_vendors"], {"ohlcv": "yfinance"})
        self.assertNotIn("backend_url", settings)
        self.assertNotIn("results_dir", settings)

    @patch("tradingagents.graph.trading_graph.TradingAgentsGraph.__init__", return_value=None)
    def test_run_signature_includes_layout(self, _mock_init):
        cfg = DEFAULT_CONFIG.copy()
        cfg["analysis"] = {**(cfg.get("analysis") or {}), "analyst_layout": "parallel"}
        graph = TradingAgentsGraph(["market"], config=cfg)
        graph.config = cfg
        graph.selected_analysts = ["market"]
        sig = graph._run_signature()
        self.assertIn("layout=parallel", sig)

    def test_render_run_settings_header(self):
        html_line = render_run_settings_html(
            {
                "analysts": ["market", "news"],
                "analysis_mode": "deep",
                "data_vendors": {"ohlcv": "yfinance"},
            }
        )
        self.assertIn("market", html_line)
        self.assertIn("news", html_line)
        self.assertIn("yfinance", html_line)


if __name__ == "__main__":
    unittest.main()
