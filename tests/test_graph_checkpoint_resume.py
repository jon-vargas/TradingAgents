import inspect
import tempfile
import unittest
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.graph.checkpointer import checkpoint_step, get_checkpointer, thread_id
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
import cli.main as cli_main


class TestGraphCheckpointResume(unittest.TestCase):
    def test_begin_checkpoint_compiles_with_saver(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                **DEFAULT_CONFIG,
                "data_cache_dir": tmp,
                "checkpoint_enabled": True,
            }
            graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
            graph.config = cfg
            graph.selected_analysts = ("market",)
            graph._checkpointer_ctx = None
            graph._resuming = False
            workflow = StateGraph(AgentState)
            workflow.add_node("noop", lambda state: state)
            workflow.add_edge("noop", END)
            workflow.set_entry_point("noop")
            graph.workflow = workflow
            graph.graph = workflow.compile()

            sig = graph._run_signature()
            tid = graph.begin_checkpoint("AAPL", "2026-01-02", None)
            self.assertEqual(tid, thread_id("AAPL", "2026-01-02", sig))
            self.assertIsNone(checkpoint_step(tmp, "AAPL", "2026-01-02", sig))
            graph.end_checkpoint()

    def test_same_signature_checkpoint_step_is_set(self):
        class _State(TypedDict):
            n: int

        def _step(state):
            return {"n": int(state.get("n") or 0) + 1}

        with tempfile.TemporaryDirectory() as tmp:
            signature = "analysts=market"
            tid = thread_id("AAPL", "2026-01-02", signature)
            workflow = StateGraph(_State)
            workflow.add_node("step", _step)
            workflow.add_edge(START, "step")
            workflow.add_edge("step", END)
            with get_checkpointer(tmp, "AAPL") as saver:
                compiled = workflow.compile(checkpointer=saver)
                compiled.invoke({"n": 0}, {"configurable": {"thread_id": tid}})
            self.assertIsNotNone(checkpoint_step(tmp, "AAPL", "2026-01-02", signature))
            self.assertIsNone(checkpoint_step(tmp, "AAPL", "2026-01-02", "analysts=news"))

    def test_cli_stream_is_inside_checkpoint_scope(self):
        src = inspect.getsource(cli_main.run_analysis)
        scope_at = src.find("checkpoint_scope")
        stream_at = src.find("graph.graph.stream")
        self.assertGreater(scope_at, -1)
        self.assertGreater(stream_at, scope_at)


if __name__ == "__main__":
    unittest.main()
