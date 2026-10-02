"""Analyst tool-round cap and conditional router (#1420)."""
import unittest

from langchain_core.messages import AIMessage
from langgraph.graph import END

from tradingagents.agents.analysts.analyst_finalize import (
    effective_tool_message_cap,
    finalize_analyst_result,
    strip_tool_calls_from_message,
)
from tradingagents.graph.analyst_subgraph import _tools_or_end
from tradingagents.graph.conditional_logic import ConditionalLogic


class TestAnalystToolRoundCap(unittest.TestCase):
    def test_effective_cap_is_min_of_global_and_per_analyst(self):
        config = {
            "analysis": {"max_tool_rounds": 20},
            "token_budget": {"analyst": {"market_max_tool_messages": 6}},
        }
        self.assertEqual(effective_tool_message_cap(config, "market", 6), 6)
        config["analysis"]["max_tool_rounds"] = 3
        self.assertEqual(effective_tool_message_cap(config, "market", 6), 3)

    def test_finalize_strips_tool_calls_and_sets_report(self):
        msg = AIMessage(content="Final report body", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "1"}])
        _, report, updates = finalize_analyst_result("market", msg, force_finalize=True)
        self.assertEqual(report, "Final report body")
        self.assertTrue(updates.get("analyst_finalized_market"))
        self.assertFalse(getattr(updates["messages"][0], "tool_calls", None))

    def test_mid_loop_tool_turn_does_not_stub_report(self):
        msg = AIMessage(content="", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "1"}])
        _, report, updates = finalize_analyst_result("market", msg, force_finalize=False)
        self.assertEqual(report, "")
        self.assertNotIn("market_report", updates)
        self.assertNotIn("analyst_finalized_market", updates)
        self.assertTrue(getattr(updates["messages"][0], "tool_calls", None))

    def test_router_exits_when_report_present(self):
        logic = ConditionalLogic()
        state = {
            "messages": [AIMessage(content="Final report")],
            "market_report": "done",
        }
        self.assertEqual(logic.should_continue_market(state), "Msg Clear Market")

    def test_router_exits_when_finalized_flag(self):
        logic = ConditionalLogic()
        state = {
            "messages": [AIMessage(content="done")],
            "market_report": "",
            "analyst_finalized_market": True,
        }
        self.assertEqual(logic.should_continue_market(state), "Msg Clear Market")

    def test_parallel_inner_router_exits_to_end(self):
        pending = [AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}])]
        done = {"messages": [AIMessage(content="done")], "market_report": "done"}
        finalized = {
            "messages": [AIMessage(content="done")],
            "market_report": "",
            "analyst_finalized_market": True,
        }
        self.assertEqual(_tools_or_end(done, analyst_type="market", tools_node="tools"), END)
        self.assertEqual(
            _tools_or_end(finalized, analyst_type="market", tools_node="tools"),
            END,
        )
        self.assertEqual(
            _tools_or_end({"messages": pending, "market_report": ""}, analyst_type="market", tools_node="tools"),
            "tools",
        )
        stubbed = {
            "messages": pending,
            "market_report": "Report synthesis complete (tool cap reached).",
            "analyst_finalized_market": True,
        }
        self.assertEqual(
            _tools_or_end(stubbed, analyst_type="market", tools_node="tools"),
            "tools",
        )

    def test_strip_tool_calls_clears_pending_tools(self):
        msg = AIMessage(content="text", tool_calls=[{"name": "t", "args": {}, "id": "1"}])
        cleaned = strip_tool_calls_from_message(msg)
        self.assertEqual(cleaned.content, "text")
        self.assertFalse(getattr(cleaned, "tool_calls", None))


if __name__ == "__main__":
    unittest.main()
