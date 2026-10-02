"""Tests for book-action enforcer and mode-qualified report paths."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tradingagents.reporting.book_action import (
    decision_from_state,
    enforce_book_action,
)
from tradingagents.reporting.database import ResearchDatabase, create_analysis_from_state
from tradingagents.reporting.position_action import (
    extract_decision_json_from_text,
    format_decision_label,
)
from tradingagents.reporting.report_paths import (
    legacy_report_basename,
    report_basename,
    resolve_report_html,
    write_report_html_path,
)


def _judge_json(decision: str, position_action: str) -> str:
    return (
        f'DECISION_JSON: {{"decision":"{decision}","position_action":"{position_action}","conviction":"medium"}}\n'
        f"## Decision: {decision}"
    )


def _research_hold_retain() -> str:
    return (
        "FINAL TRANSACTION PROPOSAL: **HOLD**\n"
        "Maintain the existing sleeve and retain current exposure."
    )


def _research_hold_avoid_only() -> str:
    return (
        "FINAL TRANSACTION PROPOSAL: **HOLD**\n"
        "Already flat. Do not initiate a new position."
    )


def _trader_hold() -> str:
    return "FINAL TRANSACTION PROPOSAL: HOLD\nNo change to the book."


def _research_sell() -> str:
    return "FINAL TRANSACTION PROPOSAL: **SELL**\nFundamental thesis is broken."


class BookActionEnforcerTests(unittest.TestCase):
    def _enforce(self, state: dict) -> dict:
        return enforce_book_action(state, {"decision_guardrails": {"book_action": {"enabled": True}}})

    def test_airline_hold_veto_sell_reduce(self):
        for ticker in ("BA", "DAL", "UAL", "LUV"):
            with self.subTest(ticker=ticker):
                state = {
                    "investment_plan": _research_hold_retain(),
                    "trader_investment_plan": _trader_hold(),
                    "final_trade_decision": _judge_json("SELL", "REDUCE"),
                }
                result = self._enforce(state)
                self.assertTrue(result["adjusted"], ticker)
                updated = extract_decision_json_from_text(state["final_trade_decision"])
                self.assertEqual(updated["decision"], "HOLD")
                self.assertEqual(updated["position_action"], "HOLD")
                self.assertEqual(
                    format_decision_label(updated["decision"], updated["position_action"]),
                    "HOLD (Maintain)",
                )

    def test_ccl_research_sell_unchanged(self):
        state = {
            "investment_plan": _research_sell(),
            "trader_investment_plan": _trader_hold(),
            "final_trade_decision": _judge_json("SELL", "REDUCE"),
        }
        result = self._enforce(state)
        self.assertFalse(result["adjusted"])
        updated = extract_decision_json_from_text(state["final_trade_decision"])
        self.assertEqual(updated["decision"], "SELL")
        self.assertEqual(updated["position_action"], "REDUCE")

    def test_rcl_lmt_already_hold_unchanged(self):
        state = {
            "investment_plan": _research_hold_retain(),
            "trader_investment_plan": _trader_hold(),
            "final_trade_decision": _judge_json("HOLD", "HOLD"),
        }
        result = self._enforce(state)
        self.assertFalse(result["adjusted"])

    def test_rtx_dividend_income_veto_buy(self):
        state = {
            "investment_profile": {"profile_key": "dividend_income"},
            "investment_plan": _research_hold_retain(),
            "trader_investment_plan": _trader_hold(),
            "final_trade_decision": _judge_json("BUY", "ADD"),
        }
        result = self._enforce(state)
        self.assertTrue(result["adjusted"])
        self.assertEqual(result["reason"], "dividend_income_research_hold_veto_buy")
        updated = extract_decision_json_from_text(state["final_trade_decision"])
        self.assertEqual(updated["decision"], "HOLD")
        self.assertEqual(updated["position_action"], "HOLD")

    def test_do_not_initiate_only_becomes_hold_avoid(self):
        state = {
            "investment_plan": _research_hold_avoid_only(),
            "trader_investment_plan": _trader_hold(),
            "final_trade_decision": _judge_json("SELL", "REDUCE"),
        }
        result = self._enforce(state)
        self.assertTrue(result["adjusted"])
        updated = extract_decision_json_from_text(state["final_trade_decision"])
        self.assertEqual(updated["decision"], "HOLD")
        self.assertEqual(updated["position_action"], "AVOID")
        self.assertEqual(
            format_decision_label(updated["decision"], updated["position_action"]),
            "HOLD (Avoid)",
        )

    def test_trader_sell_keeps_judge_sell(self):
        state = {
            "investment_plan": _research_hold_retain(),
            "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **SELL**",
            "final_trade_decision": _judge_json("SELL", "REDUCE"),
        }
        result = self._enforce(state)
        self.assertFalse(result["adjusted"])

    def test_missing_trader_treated_as_holdish_when_research_hold(self):
        state = {
            "investment_plan": _research_hold_retain(),
            "final_trade_decision": _judge_json("SELL", "REDUCE"),
        }
        result = self._enforce(state)
        self.assertTrue(result["adjusted"])
        updated = extract_decision_json_from_text(state["final_trade_decision"])
        self.assertEqual(updated["decision"], "HOLD")

    def test_decision_from_state_after_rewrite(self):
        state = {
            "investment_plan": _research_hold_retain(),
            "trader_investment_plan": _trader_hold(),
            "final_trade_decision": _judge_json("SELL", "REDUCE"),
        }
        self._enforce(state)
        self.assertEqual(decision_from_state(state, "SELL"), "HOLD")

    def test_create_analysis_from_state_rewrites_decision_column(self):
        state = {
            "investment_plan": _research_hold_retain(),
            "trader_investment_plan": _trader_hold(),
            "final_trade_decision": _judge_json("SELL", "REDUCE"),
        }
        analysis = create_analysis_from_state(
            state=state,
            ticker="LUV",
            analysis_date="2026-09-08",
            decision="SELL",
            config={"decision_guardrails": {"book_action": {"enabled": True}}},
            duration_seconds=1.0,
        )
        self.assertEqual(analysis.decision, "HOLD")
        self.assertEqual(analysis.position_action, "HOLD")

        with tempfile.TemporaryDirectory() as tmp:
            db = ResearchDatabase(os.path.join(tmp, "qa.db"))
            loaded = db.get_analysis(db.save_analysis(analysis))
            self.assertEqual(loaded.decision, "HOLD")


class ReportPathTests(unittest.TestCase):
    def test_write_path_includes_mode(self):
        path = write_report_html_path("/tmp/out", "luv", "2026-09-08", "standard")
        self.assertEqual(path.name, "LUV_2026-09-08_standard_report.html")
        self.assertEqual(path.parent.name, "reports")

    def test_resolve_prefers_mode_file_then_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "reports" / f"{legacy_report_basename('LUV', '2026-09-08')}.html"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("legacy", encoding="utf-8")

            mode_path = Path(tmp) / "reports" / f"{report_basename('LUV', '2026-09-08', 'standard')}.html"
            mode_path.write_text("standard", encoding="utf-8")

            resolved = resolve_report_html(tmp, "LUV", "2026-09-08", "standard")
            self.assertEqual(resolved, mode_path)

            deep_resolved = resolve_report_html(tmp, "LUV", "2026-09-08", "deep")
            self.assertEqual(deep_resolved, legacy)


if __name__ == "__main__":
    unittest.main()
