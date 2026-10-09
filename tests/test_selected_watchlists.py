"""Selected-watchlist Scan All: union of chosen lists, no index buckets."""
import copy
import json
import unittest
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException


@dataclass
class _Scored:
    ticker: str
    composite_score: float = 1.0


class _ImmediateThread:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class _FakeLimiter:
    def is_open(self):
        return False

    def cooldown_remaining(self):
        return 0.0


class _RecordingDB:
    def __init__(self):
        self.watchlists = [
            {"id": 1, "name": "Energy", "tickers": "AAPL,MSFT", "default_preset": "momentum"},
            {"id": 2, "name": "AI Metals", "tickers": "NVDA,AMD", "default_preset": None},
            {"id": 3, "name": "Tape", "tickers": "TSLA", "source": "movers"},
            {"id": 4, "name": "Empty", "tickers": ""},
        ]
        self.saved_runs = []
        self.constituent_calls = []
        self.constituents = [{"ticker": "SPY"}]

    def get_watchlists(self):
        return list(self.watchlists)

    def get_latest_index_constituents(self, index_key=None, in_scope_only=True):
        self.constituent_calls.append(index_key)
        return list(self.constituents)

    def save_screening_run(self, **kwargs):
        self.saved_runs.append(kwargs)
        return len(self.saved_runs)

    def save_screening_results(self, run_id, rows):
        return None

    def record_runtime_metric(self, metric_key, duration_seconds, context=None):
        return 1


class _CaptureEngine:
    scanned = []
    criteria = []
    budgets = []
    health = None  # set to a dict to emulate engine.get_scan_health()

    def __init__(self, config=None, db=None):
        pass

    def get_scan_health(self):
        return dict(type(self).health or {})

    def scan(self, **kwargs):
        type(self).scanned.append(list(kwargs.get("tickers") or []))
        type(self).criteria.append(dict(kwargs.get("criteria_meta") or {}))
        type(self).budgets.append(kwargs.get("time_budget_seconds"))
        tickers = kwargs.get("tickers") or ["AAPL"]
        return [_Scored(ticker=str(tickers[0]))]


def _run_selected(appmod, db, body, cfg):
    _CaptureEngine.scanned = []
    _CaptureEngine.criteria = []
    _CaptureEngine.budgets = []
    with patch.object(appmod, "DEFAULT_CONFIG", cfg), patch(
        # The real watchdog polls forever; threading.Thread is patched to run inline here.
        "webapp.app._start_job_watchdog",
    ), patch(
        "webapp.app.get_db",
        return_value=db,
    ), patch(
        "tradingagents.screening.engine.ScreeningEngine",
        _CaptureEngine,
    ), patch(
        "tradingagents.dataflows.yfinance_limiter.get_yfinance_limiter",
        return_value=_FakeLimiter(),
    ), patch(
        "threading.Thread",
        _ImmediateThread,
    ), patch(
        "webapp.app._time.sleep",
        return_value=None,
    ):
        result = appmod.run_screening_all(body, db_path="research.db")
        job = appmod._jobs[result["job_id"]]
    return result, job


class TestSelectedWatchlistScan(unittest.TestCase):
    def _cfg(self, appmod):
        cfg = copy.deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["mode"] = "per_watchlist"
        cfg["screening"]["scan_all"]["union_feature_flag"] = False
        cfg["screening"]["scan_buckets"]["enabled"] = True
        return cfg

    def test_selected_ids_union_skips_buckets(self):
        import webapp.app as appmod

        db = _RecordingDB()
        body = appmod.ScreenAllRequest(watchlist_ids=[2, 1, 1])
        result, job = _run_selected(appmod, db, body, self._cfg(appmod))

        self.assertEqual(result["selection"], "selected")
        self.assertEqual(result["mode"], "union_buckets")
        self.assertEqual(result["watchlist_names"], ["AI Metals", "Energy"])
        self.assertEqual(result["ticker_count"], 4)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(db.constituent_calls, [])
        self.assertEqual(_CaptureEngine.scanned, [["NVDA", "AMD", "AAPL", "MSFT"]])
        self.assertEqual(len(db.saved_runs), 2)
        for saved in db.saved_runs:
            criteria = json.loads(saved["criteria"])
            self.assertEqual(criteria["source_mode"], "selected_watchlists")
            self.assertEqual(criteria["strategy"], "screen_all_union")
            self.assertEqual(criteria["selected_watchlist_names"], ["AI Metals", "Energy"])
            self.assertEqual(criteria["selected_watchlist_ids"], [2, 1])
            self.assertTrue(criteria.get("screen_all_job_id"))

    def test_rejects_one_ineligible_and_multi_sleeve(self):
        import webapp.app as appmod

        db = _RecordingDB()
        cfg = self._cfg(appmod)
        cases = [
            appmod.ScreenAllRequest(watchlist_ids=[1]),
            appmod.ScreenAllRequest(watchlist_ids=[1, 3]),
            appmod.ScreenAllRequest(watchlist_ids=[1, 99]),
            appmod.ScreenAllRequest(watchlist_ids=[1, 4]),
        ]
        for body in cases:
            with self.subTest(body=body):
                with patch.object(appmod, "DEFAULT_CONFIG", cfg), patch(
                    "webapp.app.get_db",
                    return_value=db,
                ):
                    with self.assertRaises(HTTPException) as ctx:
                        appmod.run_screening_all(body, db_path="research.db")
                self.assertEqual(ctx.exception.status_code, 400)

    def test_full_scan_all_still_prepends_buckets(self):
        import webapp.app as appmod

        db = _RecordingDB()
        cfg = self._cfg(appmod)
        cfg["screening"]["scan_all"]["mode"] = "union_buckets"
        cfg["screening"]["scan_all"]["union_feature_flag"] = True
        result, job = _run_selected(appmod, db, appmod.ScreenAllRequest(), cfg)

        self.assertEqual(result["selection"], "all")
        self.assertEqual(result["watchlist_names"], [])
        self.assertEqual(job["status"], "completed")
        self.assertTrue(db.constituent_calls)
        scanned = _CaptureEngine.scanned[-1]
        self.assertIn("SPY", scanned)
        self.assertIn("AAPL", scanned)
        self.assertNotIn("TSLA", scanned)

    def test_selected_all_modes_union(self):
        import webapp.app as appmod

        db = _RecordingDB()
        body = appmod.ScreenAllRequest(mode="multi_sleeve", watchlist_ids=[2, 1])
        result, job = _run_selected(appmod, db, body, self._cfg(appmod))

        self.assertEqual(result["selection"], "selected")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(db.constituent_calls, [])
        self.assertEqual(_CaptureEngine.scanned, [["NVDA", "AMD", "AAPL", "MSFT"]])
        self.assertEqual(_CaptureEngine.criteria[-1]["strategy"], "multi_sleeve")
        self.assertEqual(_CaptureEngine.criteria[-1]["source_mode"], "selected_watchlists")
        self.assertFalse(_CaptureEngine.criteria[-1]["run_enrich"])
        self.assertTrue(_CaptureEngine.criteria[-1].get("multi_sleeve_all_job_id"))
        self.assertNotIn("screen_all_job_id", _CaptureEngine.criteria[-1])
        self.assertEqual(len(db.saved_runs), 2)
        for saved in db.saved_runs:
            criteria = json.loads(saved["criteria"])
            self.assertEqual(criteria["strategy"], "multi_sleeve_union")
            self.assertEqual(criteria["source_mode"], "selected_watchlists")
            self.assertTrue(criteria.get("multi_sleeve_all_job_id"))
            self.assertNotIn("screen_all_job_id", criteria)

    def test_union_scan_gets_time_budget(self):
        import webapp.app as appmod

        db = _RecordingDB()
        body = appmod.ScreenAllRequest(mode="multi_sleeve", watchlist_ids=[2, 1])
        _run_selected(appmod, db, body, self._cfg(appmod))
        budget = _CaptureEngine.budgets[-1]
        self.assertIsNotNone(budget)
        # base 1200 + 2.0 * 4 tickers
        self.assertAlmostEqual(budget, 1208.0)

    def test_partial_scan_is_flagged_on_job_and_runs(self):
        import webapp.app as appmod

        db = _RecordingDB()
        _CaptureEngine.health = {
            "partial": True,
            "partial_ticker_count": 2,
            "partial_reasons": {"download_timeout": 2},
        }
        try:
            body = appmod.ScreenAllRequest(mode="multi_sleeve", watchlist_ids=[2, 1])
            _, job = _run_selected(appmod, db, body, self._cfg(appmod))
        finally:
            _CaptureEngine.health = None
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job["result"]["partial"])
        self.assertEqual(job["result"]["partial_ticker_count"], 2)
        for saved in db.saved_runs:
            health = json.loads(saved["criteria"])["scan_health"]
            self.assertTrue(health["partial"])
            self.assertEqual(health["partial_reasons"], {"download_timeout": 2})

    def test_complete_scan_is_not_partial(self):
        import webapp.app as appmod

        db = _RecordingDB()
        body = appmod.ScreenAllRequest(mode="multi_sleeve", watchlist_ids=[2, 1])
        _, job = _run_selected(appmod, db, body, self._cfg(appmod))
        self.assertFalse(job["result"]["partial"])

    def test_scan_all_rejects_multi_sleeve(self):
        import webapp.app as appmod

        db = _RecordingDB()
        with patch.object(appmod, "DEFAULT_CONFIG", self._cfg(appmod)), patch(
            "webapp.app.get_db",
            return_value=db,
        ):
            with self.assertRaises(HTTPException) as ctx:
                appmod.run_screening_all(
                    appmod.ScreenAllRequest(mode="multi_sleeve"),
                    db_path="research.db",
                )
        self.assertEqual(ctx.exception.status_code, 400)


class TestJobWatchdog(unittest.TestCase):
    def _job(self, appmod, job_id, **extra):
        import time

        data = {"status": "running", "job_type": "screen_all", "started_ts": time.time(), **extra}
        appmod._set_job(job_id, data)

    def test_stalled_job_is_failed_and_cancel_flagged(self):
        import webapp.app as appmod

        job_id = "wd-stall-test-0001"
        self._job(appmod, job_id)
        t = appmod._start_job_watchdog(
            job_id, worker_name="nope", stall_seconds=0.05, hard_limit_seconds=60, poll_seconds=0.02
        )
        t.join(5)
        job = appmod._jobs[job_id]
        self.assertEqual(job["status"], "failed")
        self.assertIn("scan_all_timeout", job["error"])
        self.assertTrue(job["cancel_requested"])
        self.assertIn("reason", job["watchdog"])

    def test_hard_limit_fails_job_even_with_fresh_progress(self):
        import time
        import webapp.app as appmod

        job_id = "wd-hard-test-0002"
        self._job(appmod, job_id)
        t = appmod._start_job_watchdog(
            job_id, worker_name="nope", stall_seconds=999, hard_limit_seconds=0.05, poll_seconds=0.02
        )
        appmod._update_job(job_id, progress={"current": 1, "total": 4})
        t.join(5)
        self.assertEqual(appmod._jobs[job_id]["status"], "failed")
        self.assertIn("hard limit", appmod._jobs[job_id]["error"])

    def test_watchdog_exits_when_job_finishes(self):
        import webapp.app as appmod

        job_id = "wd-done-test-0003"
        self._job(appmod, job_id)
        t = appmod._start_job_watchdog(
            job_id, worker_name="nope", stall_seconds=999, hard_limit_seconds=999, poll_seconds=0.02
        )
        appmod._update_job(job_id, status="completed")
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(appmod._jobs[job_id]["status"], "completed")


class TestSelectedWatchlistBoard(unittest.TestCase):
    def test_provenance_for_each_lens(self):
        import webapp.app as appmod

        cases = [
            ("opportunity", "screen_all_union", "screen_all_job_id"),
            ("reversal", "reversal_all_union", "reversal_all_job_id"),
            ("momentum", "early_momentum_union", "early_momentum_all_job_id"),
            ("base", "base_coil_union", "base_coil_all_job_id"),
        ]
        for lens, strategy, job_key in cases:
            run = {
                "strategy": strategy,
                "criteria": {
                    "strategy": strategy,
                    "source_mode": "selected_watchlists",
                    job_key: "job-1",
                },
            }
            self.assertTrue(appmod._is_lens_scan_all_run(run, lens), lens)

        meta = appmod._screen_all_engine_criteria("early_momentum", "selected_watchlists", "job-1")
        self.assertEqual(meta["strategy"], "early_momentum_union")
        self.assertFalse(meta["run_enrich"])
        multi = appmod._screen_all_engine_criteria("multi_sleeve", "selected_watchlists", "job-1")
        self.assertEqual(multi["strategy"], "multi_sleeve")
        self.assertFalse(multi["run_enrich"])
        self.assertEqual(
            appmod._screen_all_persist_strategy("base_coil", "selected_watchlists"),
            "base_coil_union",
        )

    def test_selected_job_hides_older_scan_all(self):
        import webapp.app as appmod

        def _run(run_id, wl_id, name, source, job, when):
            criteria = {
                "strategy": "screen_all_union",
                "source_mode": source,
                "screen_all_job_id": job,
            }
            if source == "selected_watchlists":
                criteria["selected_watchlist_names"] = ["Energy", "AI Metals"]
                criteria["selected_watchlist_ids"] = [1, 2]
            return {
                "id": run_id,
                "watchlist_id": wl_id,
                "watchlist_name": name,
                "strategy": "screen_all_union",
                "results_count": 2,
                "run_at": when,
                "criteria": criteria,
            }

        rows = [
            _run(11, 1, "Energy", "selected_watchlists", "job-sel", "2026-10-07T12:00:00"),
            _run(12, 2, "AI Metals", "selected_watchlists", "job-sel", "2026-10-07T12:00:00"),
            _run(13, 3, "Healthcare", "union_buckets", "job-old", "2026-09-20T12:00:00"),
        ]

        class _DB:
            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return rows

        with patch.object(appmod, "_board_now", return_value=datetime(2026, 10, 7, 15, 0, 0)):
            board = appmod._latest_runs_for_lens(_DB(), "opportunity")

        self.assertEqual(
            sorted(r["watchlist_id"] for r in board["runs"]),
            [1, 2],
        )
        self.assertEqual(board["selected_watchlist_names"], ["Energy", "AI Metals"])
        self.assertEqual(board["source_mode"], "selected_watchlists")

    def test_full_scan_all_still_merges_in_age_lists(self):
        import webapp.app as appmod

        rows = []
        for idx, name in enumerate(("Energy", "AI Metals", "Healthcare"), start=1):
            rows.append({
                "id": idx,
                "watchlist_id": idx,
                "watchlist_name": name,
                "strategy": "screen_all_union",
                "results_count": 2,
                "run_at": "2026-10-01T12:00:00",
                "criteria": {
                    "strategy": "screen_all_union",
                    "source_mode": "union_buckets",
                    "screen_all_job_id": f"job-{idx}",
                },
            })

        class _DB:
            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return rows

        with patch.object(appmod, "_board_now", return_value=datetime(2026, 10, 7, 15, 0, 0)):
            board = appmod._latest_runs_for_lens(_DB(), "opportunity")

        self.assertEqual(sorted(r["watchlist_id"] for r in board["runs"]), [1, 2, 3])
        self.assertIsNone(board["selected_watchlist_names"])

    def test_all_modes_lens_is_job_scoped_and_hidden_from_opportunity(self):
        import webapp.app as appmod

        def _multi(run_id, wl_id, name, job, when):
            return {
                "id": run_id,
                "watchlist_id": wl_id,
                "watchlist_name": name,
                "strategy": "multi_sleeve_union",
                "results_count": 1,
                "run_at": when,
                "criteria": {
                    "strategy": "multi_sleeve_union",
                    "source_mode": "selected_watchlists",
                    "multi_sleeve_all_job_id": job,
                    "selected_watchlist_names": ["Energy", "AI Metals"],
                    "selected_watchlist_ids": [1, 2],
                },
            }

        rows = [
            _multi(21, 1, "Energy", "job-multi", "2026-10-07T12:00:00"),
            _multi(22, 2, "AI Metals", "job-multi", "2026-10-07T12:00:00"),
            _multi(23, 3, "Healthcare", "job-old", "2026-09-20T12:00:00"),
            {
                "id": 30,
                "watchlist_id": 1,
                "watchlist_name": "Energy",
                "strategy": "screen_all_union",
                "results_count": 1,
                "run_at": "2026-10-06T12:00:00",
                "criteria": {
                    "strategy": "screen_all_union",
                    "source_mode": "union_buckets",
                    "screen_all_job_id": "job-opp",
                },
            },
        ]

        class _DB:
            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return rows

            def get_screening_results(self, run_id):
                return [{
                    "ticker": "NVDA" if run_id == 21 else "AMD",
                    "composite_score": 70,
                    "direction": "bullish",
                    "signals": {
                        "_multi_sleeve": {
                            "lead_sleeve": "momentum",
                            "lead_percentile": 90,
                            "lead_score": 80,
                            "location": "pullback",
                            "cleared": [{"sleeve": "momentum"}, {"sleeve": "base"}],
                        },
                    },
                }]

        with patch.object(appmod, "_board_now", return_value=datetime(2026, 10, 7, 15, 0, 0)), patch(
            "webapp.app.get_db",
            return_value=_DB(),
        ):
            multi = appmod._latest_runs_for_lens(_DB(), "multi")
            opportunity = appmod._latest_runs_for_lens(_DB(), "opportunity")
            payload = appmod._get_multi_sleeve_all_consolidated(db_path="research.db")

        self.assertEqual(sorted(r["watchlist_id"] for r in multi["runs"]), [1, 2])
        self.assertEqual(multi["selected_watchlist_names"], ["Energy", "AI Metals"])
        self.assertTrue(all(r["strategy"] == "screen_all_union" for r in opportunity["runs"]))
        self.assertEqual(payload["lens"], "multi")
        self.assertEqual(payload["selected_watchlist_names"], ["Energy", "AI Metals"])
        self.assertEqual(
            [row["ticker"] for row in payload["top_opportunities"]],
            ["AMD", "NVDA"],
        )
        self.assertTrue(all(row["lead_sleeve"] == "momentum" for row in payload["top_opportunities"]))
        self.assertTrue(all(row["watchlist_breadth"] == 1 for row in payload["top_opportunities"]))


if __name__ == "__main__":
    unittest.main()
