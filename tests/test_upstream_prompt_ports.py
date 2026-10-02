"""Tests for upstream prompt-quality ports (debate openers, trader grounding, research Hold)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.trader.trader import create_trader
from tradingagents.agents.utils.debate_text import opponent_argument_or_opening

_REPORTS = {
    "company_of_interest": "LUV",
    "market_report": "Close $39.18. ATR: $1.25. RSI 39.",
    "sentiment_report": "Neutral social tone.",
    "news_report": "Quiet tape.",
    "fundamentals_report": "Revenue stable.",
}


def _mock_memory():
    memory = MagicMock()
    memory.get_memories.return_value = []
    return memory


def _capturing_llm(captured: dict, *, key: str = "prompt"):
    llm = MagicMock()

    def _invoke(arg):
        if isinstance(arg, list):
            captured[key] = arg
        else:
            captured[key] = arg
        return MagicMock(content="FINAL TRANSACTION PROPOSAL: **HOLD**\nSIGNAL_JSON: {}")

    llm.invoke.side_effect = _invoke
    return llm


def _investment_state(current_response: str = "") -> dict:
    return {
        **_REPORTS,
        "investment_debate_state": {
            "history": "",
            "bull_history": "",
            "bear_history": "",
            "current_response": current_response,
            "count": 0,
        },
    }


def _trader_state(market_report: str = _REPORTS["market_report"]) -> dict:
    return {
        **_REPORTS,
        "market_report": market_report,
        "investment_plan": "FINAL TRANSACTION PROPOSAL: **HOLD**\nWait for repair.",
        "trader_investment_plan": "",
        "risk_profile": "growth",
    }


class OpponentHelperTests(unittest.TestCase):
    def test_opponent_helper_empty_vs_real(self):
        self.assertIn("has not spoken yet", opponent_argument_or_opening("", "bear analyst"))
        self.assertEqual(
            opponent_argument_or_opening("  real point ", "bear"),
            "real point",
        )


class BullBearOpenerTests(unittest.TestCase):
    def test_bull_opening_prompt_has_marker(self):
        captured = {}
        create_bull_researcher(_capturing_llm(captured), _mock_memory())(
            _investment_state("")
        )
        self.assertIn("has not spoken yet", captured["prompt"])

    def test_bear_opening_prompt_has_marker(self):
        captured = {}
        create_bear_researcher(_capturing_llm(captured), _mock_memory())(
            _investment_state("")
        )
        self.assertIn("has not spoken yet", captured["prompt"])

    def test_bull_passes_real_opponent(self):
        captured = {}
        opponent = "Bear Analyst: Margins are compressing."
        create_bull_researcher(_capturing_llm(captured), _mock_memory())(
            _investment_state(opponent)
        )
        self.assertIn(opponent, captured["prompt"])
        self.assertNotIn("has not spoken yet", captured["prompt"])

    def test_bear_passes_real_opponent(self):
        captured = {}
        opponent = "Bull Analyst: Demand is recovering."
        create_bear_researcher(_capturing_llm(captured), _mock_memory())(
            _investment_state(opponent)
        )
        self.assertIn(opponent, captured["prompt"])
        self.assertNotIn("has not spoken yet", captured["prompt"])


class TraderGroundingTests(unittest.TestCase):
    def test_trader_includes_market_report_when_present(self):
        captured = {}
        create_trader(_capturing_llm(captured), _mock_memory())(_trader_state())
        messages = captured["prompt"]
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("Technical Market Report:", user)
        self.assertIn("Close $39.18", user)
        self.assertIn("Ground concrete price levels", system)

    def test_trader_omits_market_section_when_empty(self):
        captured = {}
        create_trader(_capturing_llm(captured), _mock_memory())(
            _trader_state(market_report="")
        )
        messages = captured["prompt"]
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertNotIn("Technical Market Report:", user)
        self.assertNotIn("Ground concrete price levels", system)

    def test_trader_requires_absolute_price_language(self):
        captured = {}
        create_trader(_capturing_llm(captured), _mock_memory())(_trader_state())
        system = captured["prompt"][0]["content"]
        self.assertIn("absolute price levels", system.lower())
        self.assertIn("never a percentage", system.lower())

    def test_trader_keeps_signal_json_contract(self):
        captured = {}
        create_trader(_capturing_llm(captured), _mock_memory())(_trader_state())
        system = captured["prompt"][0]["content"]
        self.assertIn("FINAL TRANSACTION PROPOSAL", system)
        self.assertIn("SIGNAL_JSON", system)


class ResearchManagerHoldTests(unittest.TestCase):
    def test_research_manager_allows_hold_under_ambiguity(self):
        captured = {}
        state = {
            **_REPORTS,
            "investment_debate_state": {
                "history": "Bull Analyst: case\nBear Analyst: case",
                "bull_history": "",
                "bear_history": "",
                "current_response": "",
                "count": 2,
            },
        }
        create_research_manager(_capturing_llm(captured), _mock_memory())(state)
        prompt = captured["prompt"]
        self.assertIn("Choose **HOLD**", prompt)
        self.assertIn("conflicting", prompt.lower())
        self.assertNotIn("Avoid defaulting to Hold", prompt)
        self.assertIn("FINAL TRANSACTION PROPOSAL", prompt)
        self.assertIn("SIGNAL_JSON", prompt)


if __name__ == "__main__":
    unittest.main()
