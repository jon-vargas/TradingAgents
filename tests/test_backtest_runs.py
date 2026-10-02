import csv
import tempfile
import unittest
from pathlib import Path

from tradingagents.reporting import ResearchDatabase, Analysis, BacktestRun


class BacktestRunTests(unittest.TestCase):
    def test_get_analyses_by_date_range(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test_research.db"
            db = ResearchDatabase(str(db_path))

            db.save_analysis(Analysis(ticker="AAA", analysis_date="2026-01-01", decision="BUY"))
            db.save_analysis(Analysis(ticker="AAA", analysis_date="2026-01-15", decision="SELL"))
            db.save_analysis(Analysis(ticker="AAA", analysis_date="2026-02-01", decision="HOLD"))

            results = db.get_analyses_by_date_range(start_date="2026-01-10", end_date="2026-01-31")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].analysis_date, "2026-01-15")

    def test_save_backtest_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test_research.db"
            db = ResearchDatabase(str(db_path))

            run = BacktestRun(
                ticker="AAA",
                start_date="2026-01-01",
                end_date="2026-01-31",
                limit_count=25,
                lookahead_days="[7, 14, 30]",
                slippage_bps=5.0,
                transaction_cost_bps=2.0,
                updated_count=3,
                skipped_count=1,
                avg_return=0.02,
                win_rate=0.5,
                accuracy=0.67,
            )
            run_id = db.save_backtest_run(run)
            self.assertTrue(run_id)

            runs = db.get_backtest_runs(limit=1)
            self.assertEqual(runs[0].ticker, "AAA")
            self.assertEqual(runs[0].slippage_bps, 5.0)
            self.assertIsNone(runs[0].avg_signed_return_7d)

            signed_id = db.save_backtest_run(
                BacktestRun(
                    ticker="BBB",
                    avg_signed_return_7d=0.0071,
                    return_vol_7d=0.42,
                )
            )
            self.assertTrue(signed_id)
            signed_run = next(r for r in db.get_backtest_runs(limit=5) if r.ticker == "BBB")
            self.assertAlmostEqual(signed_run.avg_signed_return_7d, 0.0071)
            self.assertAlmostEqual(signed_run.return_vol_7d, 0.42)

    def test_export_backtest_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test_research.db"
            db = ResearchDatabase(str(db_path))
            db.save_backtest_run(
                BacktestRun(
                    ticker="AAA",
                    start_date="2026-01-01",
                    end_date="2026-01-31",
                    limit_count=25,
                    lookahead_days="[7, 14, 30]",
                    slippage_bps=1.0,
                    transaction_cost_bps=1.0,
                    updated_count=1,
                    skipped_count=0,
                    avg_return=0.01,
                    win_rate=1.0,
                    accuracy=1.0,
                    run_at="2026-01-15T12:00:00",
                )
            )
            db.save_backtest_run(
                BacktestRun(
                    ticker="BBB",
                    start_date="2026-02-01",
                    end_date="2026-02-28",
                    limit_count=10,
                    lookahead_days="[7, 14, 30]",
                    slippage_bps=2.0,
                    transaction_cost_bps=2.0,
                    updated_count=1,
                    skipped_count=0,
                    avg_return=0.02,
                    win_rate=1.0,
                    accuracy=1.0,
                    run_at="2026-02-01T12:00:00",
                )
            )
            csv_path = Path(temp_dir) / "backtest_runs.csv"
            db.export_backtest_runs(str(csv_path), limit=5, ticker="AAA")

            with csv_path.open("r", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader)
                row = next(reader)
                aggregate = next(reader)

            self.assertIn("slippage_bps", header)
            self.assertIn("transaction_cost_bps", header)
            self.assertIn("avg_return", header)
            self.assertIn("bull_win_rate", header)
            self.assertIn("bear_win_rate", header)
            self.assertTrue(row)
            self.assertIn("AAA", row)
            self.assertTrue(any("AGGREGATE" in cell for cell in aggregate))

    def test_backtest_run_filters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test_research.db"
            db = ResearchDatabase(str(db_path))

            db.save_backtest_run(
                BacktestRun(
                    ticker="AAA",
                    run_at="2026-01-10T10:00:00",
                    lookahead_days="[7, 14, 30]",
                )
            )
            db.save_backtest_run(
                BacktestRun(
                    ticker="BBB",
                    run_at="2026-02-10T10:00:00",
                    lookahead_days="[7, 14, 30]",
                )
            )

            runs = db.get_backtest_runs(limit=10, ticker="BBB")
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].ticker, "BBB")

            runs = db.get_backtest_runs(limit=10, run_start_date="2026-02-01", run_end_date="2026-02-28")
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].ticker, "BBB")


if __name__ == "__main__":
    unittest.main()
