"""Discovery run history persistence."""
import tempfile
import unittest
from pathlib import Path

from tradingagents.reporting.database import ResearchDatabase


class TestDiscoveryRunHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = ResearchDatabase(self.tmp.name)

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_save_and_list_manual_run(self):
        run_id = self.db.save_discovery_run(
            source="manual",
            template_id="small_cap_momentum",
            theme="Small-cap AI infrastructure",
            criteria="recent contracts",
            market_cap_filter="over $500M under $2B",
            max_results=20,
            preset="momentum_hunter",
            profile="high_growth",
            ticker_count=3,
            validated_count=2,
            new_count=1,
            tickers=[
                {
                    "ticker": "PLTR",
                    "validated": True,
                    "novelty": "new",
                    "sector": "Technology",
                    "market_cap_actual": 120000000000,
                    "rationale": "AI platform",
                },
                {"ticker": "FAKE", "validated": False},
            ],
        )
        self.assertGreater(run_id, 0)

        detail = self.db.get_discovery_run(run_id)
        assert detail is not None
        pltr = detail["tickers"][0]
        self.assertEqual(pltr.get("sector"), "Technology")
        self.assertEqual(pltr.get("rationale"), "AI platform")

        runs = self.db.get_discovery_runs(limit=5)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["id"], run_id)
        self.assertEqual(runs[0]["source"], "manual")
        self.assertEqual(runs[0]["validated_count"], 2)
        self.assertNotIn("tickers", runs[0])

        detail = self.db.get_discovery_run(run_id)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(len(detail["tickers"]), 2)
        self.assertEqual(detail["tickers"][0]["ticker"], "PLTR")

    def test_save_auto_run_with_summary(self):
        run_id = self.db.save_discovery_run(
            source="auto",
            external_run_id="ad-123",
            theme="Auto-discovery · 3 themes",
            validated_count=2,
            summary={"created_count": 2, "themes_found": 3, "status": "completed"},
        )
        detail = self.db.get_discovery_run(run_id)
        assert detail is not None
        self.assertEqual(detail["source"], "auto")
        self.assertEqual(detail["summary"]["created_count"], 2)

    def test_filter_by_source(self):
        self.db.save_discovery_run(source="manual", theme="Manual hopper")
        self.db.save_discovery_run(source="auto", theme="Auto batch")
        manual = self.db.get_discovery_runs(source="manual")
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["theme"], "Manual hopper")


if __name__ == "__main__":
    unittest.main()
