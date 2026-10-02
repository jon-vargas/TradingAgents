import json
import tempfile
import unittest
from pathlib import Path

from tradingagents.reporting.database import ResearchDatabase, Analysis


class TestSnapshotIndexing(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test.db")
        self.db = ResearchDatabase(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_analysis(
        self,
        ticker: str = "TEST",
        sec_snapshot: str = "",
        transcript_snapshot: str = "",
    ) -> Analysis:
        return Analysis(
            ticker=ticker,
            analysis_date="2026-01-27",
            decision="BUY",
            sec_filings_snapshot=sec_snapshot,
            earnings_transcript_snapshot=transcript_snapshot,
            has_sec_snapshot=1 if sec_snapshot else 0,
            has_transcript_snapshot=1 if transcript_snapshot else 0,
        )

    def test_index_kpis_from_transcript(self):
        transcript = json.dumps(
            {
                "kpis": [
                    {"name": "Revenue", "value": "$1B", "period": "Q4", "context": "Beat estimates"},
                    {"name": "EPS", "value": "$2.00", "period": "Q4", "context": "Record high"},
                ]
            }
        )
        analysis = self._make_analysis(
            transcript_snapshot=f"EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:\n{transcript}"
        )
        analysis_id = self.db.save_analysis(analysis)

        kpis = self.db.search_kpis(ticker="TEST")
        self.assertEqual(len(kpis), 2)
        kpi_names = {k["kpi_name"] for k in kpis}
        self.assertIn("Revenue", kpi_names)
        self.assertIn("EPS", kpi_names)

    def test_index_guidance_from_transcript(self):
        transcript = json.dumps(
            {
                "guidance": [
                    {"metric": "revenue", "range": "$1.1B-$1.2B", "timeframe": "Q1", "context": ""},
                ]
            }
        )
        analysis = self._make_analysis(
            transcript_snapshot=f"EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:\n{transcript}"
        )
        self.db.save_analysis(analysis)

        guidance = self.db.search_guidance(ticker="TEST")
        self.assertEqual(len(guidance), 1)
        self.assertEqual(guidance[0]["metric"], "revenue")
        self.assertEqual(guidance[0]["guidance_range"], "$1.1B-$1.2B")

    def test_index_risks_from_sec(self):
        sec = json.dumps(
            {
                "filings": [
                    {
                        "form": "10-K",
                        "filing_date": "2026-01-15",
                        "risk_factors": ["Competition risk", "Regulatory risk"],
                    }
                ]
            }
        )
        analysis = self._make_analysis(sec_snapshot=f"SEC_FILINGS_SNAPSHOT_JSON:\n{sec}")
        self.db.save_analysis(analysis)

        risks = self.db.search_risks(ticker="TEST")
        self.assertEqual(len(risks), 2)
        risk_texts = [r["risk_text"] for r in risks]
        self.assertTrue(any("Competition" in r for r in risk_texts))

    def test_kpi_deltas(self):
        transcript1 = json.dumps({"kpis": [{"name": "Revenue", "value": "$1B", "period": "Q3"}]})
        analysis1 = self._make_analysis(
            transcript_snapshot=f"EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:\n{transcript1}"
        )
        id1 = self.db.save_analysis(analysis1)

        transcript2 = json.dumps({"kpis": [{"name": "Revenue", "value": "$1.2B", "period": "Q4"}]})
        analysis2 = self._make_analysis(
            transcript_snapshot=f"EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:\n{transcript2}"
        )
        id2 = self.db.save_analysis(analysis2)

        deltas = self.db.compute_kpi_deltas("TEST", id2)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["current_value"], "$1.2B")
        self.assertEqual(deltas[0]["previous_value"], "$1B")

    def test_guidance_shifts(self):
        transcript1 = json.dumps(
            {"guidance": [{"metric": "eps", "range": "$2.00-$2.10", "timeframe": "FY"}]}
        )
        analysis1 = self._make_analysis(
            transcript_snapshot=f"EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:\n{transcript1}"
        )
        self.db.save_analysis(analysis1)

        transcript2 = json.dumps(
            {"guidance": [{"metric": "eps", "range": "$2.20-$2.30", "timeframe": "FY"}]}
        )
        analysis2 = self._make_analysis(
            transcript_snapshot=f"EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:\n{transcript2}"
        )
        id2 = self.db.save_analysis(analysis2)

        shifts = self.db.compute_guidance_shifts("TEST", id2)
        self.assertEqual(len(shifts), 1)
        self.assertEqual(shifts[0]["current_range"], "$2.20-$2.30")
        self.assertEqual(shifts[0]["previous_range"], "$2.00-$2.10")


if __name__ == "__main__":
    unittest.main()
