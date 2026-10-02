import copy
import threading
import time
import unittest
from unittest.mock import patch


class _FakeDB:
    pass


class _FakeMoversService:
    call_count = 0

    def __init__(self, db=None, config=None):
        self.db = db
        self.config = config or {}

    def run_scan(self, include_losers=None, top_n=None, source_lists=None, allow_preclose_skip=False):
        _FakeMoversService.call_count += 1
        time.sleep(0.25)
        return {"snapshot_id": "snap-1", "skipped": False}


class _FakeScreeningEngine:
    def __init__(self, db=None):
        self.db = db
        self.last_run_id = None

    def scan(self, tickers=None, watchlist_id=None, enable_enhanced=True, **kwargs):
        self.last_run_id = int(watchlist_id or 0) * 10
        return [{"ticker": "AAPL", "composite_score": 50.0}]


class TestMoversSchedulerLock(unittest.TestCase):
    @patch("tradingagents.screening.movers.MoversIntelligenceService", _FakeMoversService)
    @patch("webapp.screening_scheduler.get_db", return_value=_FakeDB())
    def test_postclose_movers_overlap_lock_prevents_double_run(self, _mock_db):
        from tradingagents.default_config import DEFAULT_CONFIG
        from webapp.screening_scheduler import ScreeningScheduler

        _FakeMoversService.call_count = 0
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["screening"]["movers"]["enabled"] = True
        cfg["screening"]["movers"]["manual_only"] = False
        cfg["screening"]["movers"]["auto_scan_postclose_enabled"] = True

        scheduler = ScreeningScheduler(db_path=":memory:", config=cfg)

        t1 = threading.Thread(target=scheduler._run_postclose_movers, args=("2026-03-24",))
        t2 = threading.Thread(target=scheduler._run_postclose_movers, args=("2026-03-24",))
        t1.start()
        time.sleep(0.02)
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(_FakeMoversService.call_count, 1)
        self.assertFalse(scheduler.get_status().get("movers_running"))

    @patch("webapp.screening_scheduler.time.sleep", lambda *_args, **_kwargs: None)
    @patch("tradingagents.screening.engine.ScreeningEngine", _FakeScreeningEngine)
    @patch("webapp.screening_scheduler.get_db", return_value=_FakeDB())
    def test_postclose_snapshot_uses_engine_run_id_for_each_watchlist(self, _mock_db):
        from tradingagents.default_config import DEFAULT_CONFIG
        from webapp.screening_scheduler import ScreeningScheduler

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        scheduler = ScreeningScheduler(db_path=":memory:", config=cfg)
        scheduler._get_scannable_watchlists = lambda **_kw: [
            {"id": 1, "name": "WL One", "tickers": "AAPL,MSFT"},
            {"id": 2, "name": "WL Two", "tickers": "NVDA,AMD"},
        ]

        captured = {}

        def _capture(_db, scan_results):
            captured.update(scan_results)

        scheduler._run_auto_alerts = _capture
        scheduler._run_auto_analyze = lambda *_a, **_k: None

        scheduler._run_postclose_snapshot()

        self.assertIn(1, captured)
        self.assertIn(2, captured)
        self.assertEqual(captured[1][0], 10)
        self.assertEqual(captured[2][0], 20)


if __name__ == "__main__":
    unittest.main()
