"""
Targeted tests for movers per-horizon lineage:
  - get_movers_runs only returns movers strategy rows (not other strategies)
  - /api/movers/runs/{id}/queue-top rejects non-movers run IDs with 404
"""

import json
import os
import tempfile
import unittest
from datetime import datetime

from fastapi import HTTPException


class TestMoversLineagePerHorizon(unittest.TestCase):
    def test_movers_runs_returns_only_movers_strategy(self):
        """get_movers_runs must exclude non-movers screening_runs regardless of limit."""
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="movers_lineage_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            watchlist_id = int(
                db.create_watchlist(
                    name="Lineage Test WL",
                    tickers="AAA,BBB",
                    description="test",
                )
            )
            now = datetime(2026, 3, 24, 20, 0, 0)
            with db._connect() as conn:
                # Insert one movers run and one non-movers run
                conn.execute(
                    """INSERT INTO screening_runs (watchlist_id, run_at, criteria, ticker_count, results_count)
                       VALUES (?, ?, ?, ?, ?)""",
                    (watchlist_id, now.isoformat(), json.dumps({"strategy": "movers"}), 5, 5),
                )
                conn.execute(
                    """INSERT INTO screening_runs (watchlist_id, run_at, criteria, ticker_count, results_count)
                       VALUES (?, ?, ?, ?, ?)""",
                    (watchlist_id, now.isoformat(), json.dumps({"strategy": "fundamental"}), 5, 5),
                )
                conn.commit()

            runs = db.get_movers_runs(limit=10, offset=0)
            self.assertEqual(len(runs), 1, "Should return only the 1 movers run")
            self.assertEqual(runs[0]["criteria"]["strategy"], "movers")
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_queue_movers_top_requires_movers_run_id(self):
        """POST /api/movers/runs/{id}/queue-top returns 404 for a non-movers run_id."""
        from tradingagents.reporting.database import ResearchDatabase
        from webapp.app import _require_movers_strategy_run

        fd, db_path = tempfile.mkstemp(prefix="movers_queue_guard_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            watchlist_id = int(
                db.create_watchlist(
                    name="Queue Guard Test WL",
                    tickers="AAA,BBB",
                    description="test",
                )
            )
            with db._connect() as conn:
                conn.execute(
                    """INSERT INTO screening_runs (watchlist_id, run_at, criteria, ticker_count, results_count)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        watchlist_id,
                        datetime(2026, 3, 24, 20, 0, 0).isoformat(),
                        json.dumps({"strategy": "momentum"}),
                        5,
                        5,
                    ),
                )
                run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.commit()

            with self.assertRaises(HTTPException) as ctx:
                _require_movers_strategy_run(db, run_id)
            self.assertEqual(ctx.exception.status_code, 404)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
