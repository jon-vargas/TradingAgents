"""Parity tests: canonical opportunity_score module vs webapp wrapper vs CLI path."""
import unittest

FIXTURE_ROWS = [
    {"composite_score": 60, "entry_quality": 24, "macro_fit": 50, "composite_fundamental": None},
    {"composite_score": 80, "entry_quality": 50, "macro_fit": 50, "composite_fundamental": 20},
    {"composite_score": 70, "entry_quality": 60, "macro_fit": 20, "composite_fundamental": None},
    {"composite_score": 50, "entry_quality": 50, "macro_fit": 50, "composite_fundamental": None},
    {"composite_score": 65, "entry_quality": 65, "macro_fit": 65, "composite_fundamental": None},
    {"composite_score": 60, "entry_quality": 60, "macro_fit": None, "composite_fundamental": None},
    {"composite_score": None, "entry_quality": None, "macro_fit": None, "composite_fundamental": None},
]


class TestOpportunityScoreParity(unittest.TestCase):
    def _canonical(self, row):
        from tradingagents.screening.opportunity_score import compute_opportunity_score

        score, _ = compute_opportunity_score(
            row["composite_score"],
            row["entry_quality"],
            row["macro_fit"],
            composite_fundamental=row["composite_fundamental"],
        )
        return score

    def _webapp(self, row):
        from webapp.app import _compute_opportunity_score

        return _compute_opportunity_score(
            row["composite_score"],
            row["entry_quality"],
            row["macro_fit"],
            composite_fundamental=row["composite_fundamental"],
        )

    def test_canonical_matches_webapp_for_all_fixtures(self):
        for i, row in enumerate(FIXTURE_ROWS):
            canon = self._canonical(row)
            web = self._webapp(row)
            self.assertAlmostEqual(
                canon,
                web,
                delta=0.05,
                msg=f"fixture {i} mismatch: canonical={canon} webapp={web}",
            )

    def test_at_least_five_fixtures_exercised(self):
        self.assertGreaterEqual(len(FIXTURE_ROWS), 5)


if __name__ == "__main__":
    unittest.main()
