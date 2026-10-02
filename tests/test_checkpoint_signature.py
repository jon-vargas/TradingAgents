import unittest

from tradingagents.graph.checkpointer import checkpoint_step, thread_id
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG


class TestCheckpointSignature(unittest.TestCase):
    def test_thread_id_changes_with_signature(self):
        a = thread_id("AAPL", "2026-01-01", "sig-a")
        b = thread_id("AAPL", "2026-01-01", "sig-b")
        self.assertNotEqual(a, b)

    def test_signature_changes_with_analysts(self):
        cfg = {**DEFAULT_CONFIG, "checkpoint_enabled": False}

        def _sig(analysts):
            g = TradingAgentsGraph.__new__(TradingAgentsGraph)
            g.selected_analysts = tuple(analysts)
            g.config = cfg
            return g._run_signature()

        self.assertNotEqual(_sig(["market"]), _sig(["market", "news"]))

    def test_checkpoint_step_missing_db(self):
        self.assertIsNone(
            checkpoint_step("/tmp/tradingagents_test_cp_none", "ZZZZ", "2099-01-01", "x")
        )


if __name__ == "__main__":
    unittest.main()
