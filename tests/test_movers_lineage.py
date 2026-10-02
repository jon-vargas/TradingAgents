import unittest


class TestScreeningRunLineage(unittest.TestCase):
    def test_create_analysis_from_state_persists_screening_run_id(self):
        from tradingagents.reporting.database import create_analysis_from_state

        analysis = create_analysis_from_state(
            state={},
            ticker="AAPL",
            analysis_date="2026-03-04",
            decision="HOLD",
            config={},
            duration_seconds=1.2,
            screening_run_id=321,
        )
        self.assertEqual(analysis.screening_run_id, 321)


if __name__ == "__main__":
    unittest.main()
