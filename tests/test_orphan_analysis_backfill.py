"""Tests for orphan HTML report backfill parsing."""
from __future__ import annotations

import unittest
from pathlib import Path

from scripts.backfill_orphan_analyses import (
    build_analysis_record,
    find_orphan_reports,
    parse_orphan_report_html,
    parse_report_filename,
)


class OrphanAnalysisBackfillTests(unittest.TestCase):
    def test_parse_report_filename(self):
        parsed = parse_report_filename(Path("LUV_2026-09-08_report.html"))
        self.assertEqual(parsed, ("LUV", "2026-09-08"))

        parsed_mode = parse_report_filename(Path("LUV_2026-09-08_standard_report.html"))
        self.assertEqual(parsed_mode, ("LUV", "2026-09-08"))

    def test_parse_luv_html_summary_fields(self):
        html_path = Path("research_output/reports/LUV_2026-09-08_report.html")
        if not html_path.exists():
            self.skipTest("LUV orphan report not present locally")
        parsed = parse_orphan_report_html(html_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["decision"], "SELL")
        self.assertEqual(parsed["position_action"], "REDUCE")
        self.assertIn(parsed["analysis_mode"], {"deep", "standard"})
        self.assertEqual(parsed["risk_profile"], "growth")
        self.assertGreater(parsed["confidence"], 0)
        self.assertGreater(parsed["data_quality_score"], 0)
        self.assertIn("decision", parsed["decision_json"])

    def test_build_analysis_record_sets_backfill_tag(self):
        record = build_analysis_record(
            "LUV",
            "2026-09-08",
            {
                "created_at": "2026-09-08T11:57:00",
                "decision": "SELL",
                "position_action": "REDUCE",
                "confidence": 74.0,
                "data_quality_score": 100,
                "debate_rounds": 2,
                "duration_seconds": 443.0,
                "llm_provider": "openai",
                "deep_think_model": "gpt-test",
                "risk_profile": "growth",
                "analysis_mode": "deep",
                "investment_profile": "",
                "decision_json": '{"decision":"SELL"}',
                "final_decision": "DECISION_JSON: {}",
                "notes": "test",
                "tags": "orphan-backfill",
            },
        )
        self.assertEqual(record.ticker, "LUV")
        self.assertEqual(record.tags, "orphan-backfill")

    def test_find_orphan_reports_excludes_existing_db_rows(self):
        import os
        import tempfile

        from tradingagents.reporting.database import Analysis, ResearchDatabase

        html_path = Path("research_output/reports/LUV_2026-09-08_report.html")
        if not html_path.exists():
            self.skipTest("LUV orphan report not present locally")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "qa.db")
            db = ResearchDatabase(db_path)
            db.save_analysis(
                Analysis(
                    ticker="LUV",
                    analysis_date="2026-09-08",
                    decision="HOLD",
                    created_at="2026-09-08T12:00:00",
                )
            )
            orphans = find_orphan_reports(db, html_path.parent, ticker="LUV", date="2026-09-08")
            self.assertEqual(orphans, [])


if __name__ == "__main__":
    unittest.main()
