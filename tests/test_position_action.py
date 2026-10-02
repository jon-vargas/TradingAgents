import unittest

from tradingagents.reporting.position_action import (
    enforce_position_action,
    extract_decision_json_from_text,
    format_decision_label,
    infer_position_action,
)
from tradingagents.reporting.context_qc import classify_warnings, enrich_analysis_api_record


class PositionActionTests(unittest.TestCase):
    def test_infer_reduce_from_risk_debate(self):
        state = {
            "risk_debate_state": {
                "neutral_history": "Neutral Analyst: Reduce substantially and retain only a modest residual stake.",
            }
        }
        decision_json = {"decision": "SELL", "position_action": "FULL_SELL"}
        self.assertEqual(infer_position_action(state, decision_json), "REDUCE")

    def test_enforce_position_action_rewrites_decision_json(self):
        state = {
            "final_trade_decision": (
                'DECISION_JSON: {"decision":"SELL","position_action":"FULL_SELL","conviction":"high"}\n'
                "## Decision: SELL"
            ),
            "risk_debate_state": {
                "neutral_history": "Neutral Analyst: moderate sell or reduce recommendation.",
            },
        }
        result = enforce_position_action(state)
        self.assertTrue(result["adjusted"])
        self.assertEqual(result["to"], "REDUCE")
        updated = extract_decision_json_from_text(state["final_trade_decision"])
        self.assertEqual(updated.get("position_action"), "REDUCE")
        self.assertTrue(updated.get("position_action_corrected"))

    def test_format_decision_label(self):
        self.assertEqual(format_decision_label("SELL", "REDUCE"), "SELL (Reduce)")
        self.assertEqual(format_decision_label("SELL", "FULL_SELL"), "SELL (Full Exit)")
        self.assertEqual(format_decision_label("HOLD", "AVOID"), "HOLD (Avoid)")
        self.assertEqual(format_decision_label("HOLD", "REDUCE"), "HOLD (Trim)")
        self.assertEqual(format_decision_label("HOLD", "HOLD"), "HOLD (Maintain)")

    def test_classify_warnings(self):
        groups = classify_warnings(
            ["fundamentals_report_truncated", "street_consensus_conflict", "signal_json_missing"]
        )
        self.assertEqual(groups["integrity"], ["fundamentals_report_truncated", "signal_json_missing"])
        self.assertEqual(groups["context"], ["street_consensus_conflict"])

    def test_enrich_analysis_api_record(self):
        record = enrich_analysis_api_record(
            {
                "decision": "SELL",
                "decision_json": '{"decision":"SELL","position_action":"REDUCE"}',
                "qa_warnings": ["street_consensus_conflict", "bull_summary_truncated"],
            }
        )
        self.assertEqual(record["decision_label"], "SELL (Reduce)")
        self.assertEqual(record["position_action"], "REDUCE")
        self.assertEqual(len(record["qa_warning_groups"]["context"]), 1)
        self.assertEqual(len(record["qa_warning_groups"]["integrity"]), 1)

    def test_create_analysis_from_state_persists_position_action(self):
        import os
        import tempfile

        from tradingagents.reporting.database import ResearchDatabase, create_analysis_from_state

        analysis = create_analysis_from_state(
            state={
                "final_trade_decision": (
                    'DECISION_JSON: {"decision":"SELL","position_action":"REDUCE","conviction":"medium"}\n'
                    "## Decision: SELL"
                )
            },
            ticker="AVGO",
            analysis_date="2026-08-23",
            decision="SELL",
            config={},
            duration_seconds=1.0,
        )
        self.assertEqual(analysis.position_action, "REDUCE")
        with tempfile.TemporaryDirectory() as tmp:
            db = ResearchDatabase(os.path.join(tmp, "qa.db"))
            analysis_id = db.save_analysis(analysis)
            loaded = db.get_analysis(analysis_id)
            self.assertEqual(loaded.position_action, "REDUCE")

    def test_row_to_analysis_loads_scenario_and_intrinsic_fields(self):
        import os
        import tempfile

        from tradingagents.reporting.database import ResearchDatabase, create_analysis_from_state

        analysis = create_analysis_from_state(
            state={
                "final_trade_decision": (
                    'DECISION_JSON: {"decision":"SELL","position_action":"REDUCE"}'
                ),
                "scenario_analysis": {"blended_upside_pct": 84.3},
                "intrinsic_value": {"fair_value": 384.1, "margin_of_safety_pct": 4.2},
            },
            ticker="AVGO",
            analysis_date="2026-08-23",
            decision="SELL",
            config={},
            duration_seconds=1.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = ResearchDatabase(os.path.join(tmp, "qa.db"))
            analysis_id = db.save_analysis(analysis)
            loaded = db.get_analysis(analysis_id)
            self.assertIn("84.3", loaded.scenario_analysis or "")
            self.assertEqual(loaded.intrinsic_value, 384.1)
            self.assertIn("4.2", loaded.intrinsic_value_data or "")


if __name__ == "__main__":
    unittest.main()
