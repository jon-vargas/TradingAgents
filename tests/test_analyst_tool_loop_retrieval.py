"""Ensure analyst tool loops run before reports (parallel subgraph retrieval)."""
from __future__ import annotations

import unittest
from typing import Any, List

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode

from tradingagents.agents.analysts.analyst_finalize import invoke_analyst_chain
from tradingagents.graph.analyst_subgraph import compile_analyst_subgraph, wrap_subgraph_for_parent
from tradingagents.graph.signal_aggregator import _extract_llm_signals
from tradingagents.reporting.attribution import _extract_signal_json

_CONFIG = {
    "analysis": {"max_tool_rounds": 20},
    "token_budget": {"analyst": {"market_max_tool_messages": 6}},
}


class _FakePrompt:
    def __or__(self, other: Any) -> Any:
        return other


class _MultiStepLLM:
    """First turn: tool call only; after ToolMessage: full report with SIGNAL_JSON."""

    def bind_tools(self, tools: Any, parallel_tool_calls: bool = False) -> "_MultiStepLLM":
        return self

    def invoke(self, messages: List[Any]) -> AIMessage:
        tool_payloads = [
            str(getattr(m, "content", ""))
            for m in messages
            if getattr(m, "type", "") == "tool"
        ]
        if not tool_payloads:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_stock_data",
                        "args": {"ticker": "SNEX"},
                        "id": "call-1",
                    }
                ],
            )
        if not any("CLOSE" in p for p in tool_payloads):
            raise AssertionError(f"Expected tool OHLCV payload, got: {tool_payloads!r}")
        return AIMessage(
            content=(
                "Price trend constructive from retrieved OHLCV.\n"
                'SIGNAL_JSON: {"section":"Market","stance":"bullish","confidence":0.75,'
                '"key_factors":["trend"]}'
            )
        )


@tool
def get_stock_data(ticker: str) -> str:
    """Fetch OHLCV for ticker."""
    return f"DATE,CLOSE\n2026-10-01,50.0 ticker={ticker}"


class TestAnalystToolLoopRetrieval(unittest.TestCase):
    def test_invoke_analyst_chain_does_not_stub_on_first_tool_turn(self):
        llm = _MultiStepLLM()
        step1 = invoke_analyst_chain(
            analyst_type="market",
            config=_CONFIG,
            default_cap=6,
            invoke_messages=[HumanMessage(content="Analyze SNEX")],
            prompt=_FakePrompt(),
            llm=llm,
            tools=[get_stock_data],
        )
        self.assertNotIn("market_report", step1)
        self.assertTrue(getattr(step1["messages"][0], "tool_calls", None))

    def test_parallel_subgraph_retrieves_tool_data_and_signal_json(self):
        def market_agent(state):
            messages = state.get("messages") or [HumanMessage(content="Analyze SNEX")]
            return invoke_analyst_chain(
                analyst_type="market",
                config=_CONFIG,
                default_cap=6,
                invoke_messages=messages,
                prompt=_FakePrompt(),
                llm=_MultiStepLLM(),
                tools=[get_stock_data],
            )

        compiled = compile_analyst_subgraph("market", market_agent, ToolNode([get_stock_data]))
        parent = wrap_subgraph_for_parent(compiled, "market")
        out = parent({"messages": [HumanMessage(content="Analyze SNEX")]})

        report = out.get("market_report") or ""
        self.assertNotEqual(report.strip(), "Report synthesis complete (tool cap reached).")
        self.assertIn("SIGNAL_JSON", report)
        self.assertIn("OHLCV", report)
        self.assertIsNotNone(_extract_signal_json(report))

        signals, missing = _extract_llm_signals(
            {"market_report": report, "fundamentals_report": "", "news_report": "", "sentiment_report": ""}
        )
        self.assertIn("Market", signals)
        self.assertNotIn("Market", missing)


if __name__ == "__main__":
    unittest.main()
