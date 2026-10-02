import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from fastapi import HTTPException


class TestMoversGuards(unittest.TestCase):
    def test_movers_kill_switch_defaults_off(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        movers_cfg = DEFAULT_CONFIG["screening"]["movers"]
        self.assertTrue(movers_cfg.get("manual_only", True))
        self.assertFalse(movers_cfg.get("auto_scan_postclose_enabled", False))

    def test_get_movers_runs_filters_before_pagination(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="movers_runs_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            watchlist_id = int(
                db.create_watchlist(
                    name="Movers Pagination Test",
                    tickers="AAA,BBB",
                    description="test",
                )
            )
            now = datetime(2026, 3, 24, 20, 0, 0)
            with db._connect() as conn:
                for i in range(300):
                    run_at = (now - timedelta(minutes=i)).isoformat()
                    criteria = {"strategy": "movers"} if i % 2 == 0 else {"strategy": "other"}
                    conn.execute(
                        """INSERT INTO screening_runs (watchlist_id, run_at, criteria, ticker_count, results_count)
                           VALUES (?, ?, ?, ?, ?)""",
                        (watchlist_id, run_at, json.dumps(criteria), 2, 0),
                    )
                conn.commit()

            runs = db.get_movers_runs(limit=20, offset=120)
            self.assertEqual(len(runs), 20)
            for row in runs:
                self.assertEqual(row.get("criteria", {}).get("strategy"), "movers")
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_save_screening_run_persists_strategy_column(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="movers_strategy_col_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            watchlist_id = int(
                db.create_watchlist(
                    name="Movers Strategy Column",
                    tickers="AAA,BBB",
                    description="test",
                )
            )
            run_id = db.save_screening_run(
                watchlist_id=watchlist_id,
                criteria=json.dumps({"strategy": "movers", "horizon": "short"}),
                ticker_count=2,
                results_count=0,
            )
            with db._connect() as conn:
                row = conn.execute("SELECT strategy FROM screening_runs WHERE id = ?", (run_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(str(row["strategy"]), "movers")
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_require_movers_strategy_run_rejects_non_movers(self):
        from tradingagents.reporting.database import ResearchDatabase
        from webapp.app import _require_movers_strategy_run

        fd, db_path = tempfile.mkstemp(prefix="movers_guard_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            watchlist_id = int(
                db.create_watchlist(
                    name="Movers Guard Test",
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
                        json.dumps({"strategy": "fundamental"}),
                        2,
                        0,
                    ),
                )
                run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.commit()

            with self.assertRaises(HTTPException) as err:
                _require_movers_strategy_run(db, run_id)
            self.assertEqual(err.exception.status_code, 404)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_decorate_movers_expand_fields_lifts_scorecard(self):
        from webapp.app import _decorate_movers_expand_fields

        rows = [
            {
                "ticker": "BMY",
                "composite_score": 44.0,
                "signals": {
                    "_factor_scorecard": {"Value": 62.0, "Momentum": 40.0},
                    "_screening_meta": {
                        "signal_coverage_pct": 71.0,
                        "tier_reached": "enhanced",
                        "resolved_preset": "movers_swing_1to5d",
                    },
                },
            }
        ]
        out = _decorate_movers_expand_fields(
            rows,
            {"watchlist_id": 65, "criteria": {"preset": "movers_swing_1to5d"}},
        )
        self.assertEqual(out[0]["factor_scorecard"]["Value"], 62.0)
        self.assertEqual(out[0]["signal_coverage_pct"], 71.0)
        self.assertEqual(out[0]["watchlist_id"], 65)
        self.assertEqual(out[0]["resolved_preset"], "movers_swing_1to5d")


if __name__ == "__main__":
    unittest.main()
