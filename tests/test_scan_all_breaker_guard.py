import copy
import unittest
from unittest.mock import patch


class _FakeDB:
    def __init__(self):
        self.watchlists = [
            {"id": 1, "name": "WL1", "tickers": "AAPL,MSFT", "default_preset": None},
            {"id": 2, "name": "WL2", "tickers": "NVDA,AMD", "default_preset": None},
        ]

    def get_watchlists(self):
        return list(self.watchlists)

    def record_runtime_metric(self, metric_key, duration_seconds, context=None):
        return 1


class _FakeEngine:
    def __init__(self, config=None, db=None):
        self.config = config or {}
        self.db = db

    def scan(self, **kwargs):
        return []


class _ImmediateThread:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class _FakeLimiter:
    def __init__(self):
        self.open_checked = False
        self.cooldown_calls = 0

    def is_open(self):
        if not self.open_checked:
            self.open_checked = True
            return True
        return False

    def cooldown_remaining(self):
        self.cooldown_calls += 1
        if self.cooldown_calls == 1:
            return 3.0  # startup guard
        return 5.0      # between-watchlist pacing


class TestScanAllBreakerGuard(unittest.TestCase):
    def test_scan_all_defers_start_and_uses_cooldown_between_watchlists(self):
        import webapp.app as appmod

        fake_db = _FakeDB()
        fake_limiter = _FakeLimiter()
        cfg = copy.deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["mode"] = "per_watchlist"

        sleep_calls = []

        def _capture_sleep(seconds):
            sleep_calls.append(float(seconds))

        with patch.object(appmod, "DEFAULT_CONFIG", cfg), patch(
            "webapp.app.get_db",
            return_value=fake_db,
        ), patch(
            "tradingagents.screening.engine.ScreeningEngine",
            _FakeEngine,
        ), patch(
            "tradingagents.dataflows.yfinance_limiter.get_yfinance_limiter",
            return_value=fake_limiter,
        ), patch(
            "threading.Thread",
            _ImmediateThread,
        ), patch(
            "webapp.app._time.sleep",
            side_effect=_capture_sleep,
        ):
            result = appmod.run_screening_all(db_path="research.db")

        self.assertEqual(result["mode"], "per_watchlist")
        self.assertIn(3.0, sleep_calls)
        self.assertIn(5.0, sleep_calls)

    def test_union_empty_results_fail_without_persist(self):
        import webapp.app as appmod

        class _RecordingDB(_FakeDB):
            def __init__(self):
                super().__init__()
                self.saved_runs = []
                self.saved_results = []

            def get_latest_index_constituents(self, index_key=None, in_scope_only=True):
                return []

            def save_screening_run(self, **kwargs):
                self.saved_runs.append(kwargs)
                return 99

            def save_screening_results(self, run_id, rows):
                self.saved_results.append((run_id, rows))

        fake_db = _RecordingDB()
        cfg = copy.deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["mode"] = "union_buckets"
        cfg["screening"]["scan_buckets"]["enabled"] = False

        with patch.object(appmod, "DEFAULT_CONFIG", cfg), patch(
            "webapp.app.get_db",
            return_value=fake_db,
        ), patch(
            "tradingagents.screening.engine.ScreeningEngine",
            _FakeEngine,
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
            result = appmod.run_screening_all(db_path="research.db")
            job = appmod._jobs[result["job_id"]]

        self.assertEqual(job["status"], "failed")
        self.assertIn("scan_all_zero_results", str(job.get("error") or ""))
        self.assertEqual(fake_db.saved_runs, [])
        self.assertEqual(fake_db.saved_results, [])


class TestBreakerWaitAndRetry(unittest.TestCase):
    def test_wait_then_proceed(self):
        from unittest.mock import MagicMock
        from tradingagents.screening.engine import ScreeningEngine

        eng = ScreeningEngine.__new__(ScreeningEngine)
        eng._screening_config = {
            "breaker_wait_max_seconds": 900,
            "breaker_wait_max_per_trip": 360,
        }
        eng._breaker_wait_used = 0.0
        limiter = MagicMock()
        limiter.is_open.side_effect = [True, True, False]
        limiter.cooldown_remaining.return_value = 5.0

        with patch("tradingagents.screening.engine.time.sleep") as sleep:
            skip = eng._wait_for_yfinance_breaker(limiter, "test")

        self.assertFalse(skip)
        sleep.assert_called_once_with(5.0)
        self.assertEqual(eng._breaker_wait_used, 5.0)

    def test_skip_when_budget_exhausted(self):
        from unittest.mock import MagicMock
        from tradingagents.screening.engine import ScreeningEngine

        eng = ScreeningEngine.__new__(ScreeningEngine)
        eng._screening_config = {
            "breaker_wait_max_seconds": 10,
            "breaker_wait_max_per_trip": 5,
        }
        eng._breaker_wait_used = 10.0
        limiter = MagicMock()
        limiter.is_open.return_value = True
        limiter.cooldown_remaining.return_value = 30.0

        with patch("tradingagents.screening.engine.time.sleep") as sleep:
            skip = eng._wait_for_yfinance_breaker(limiter, "test")

        self.assertTrue(skip)
        sleep.assert_not_called()

    def test_scan_all_excludes_movers_and_auto_lists(self):
        import webapp.app as appmod

        eligible = appmod._is_scan_all_eligible_watchlist
        self.assertTrue(eligible({"source": "user", "name": "NASDAQ 100"}))
        self.assertTrue(eligible({"source": "built-in", "name": "S&P 100"}))
        self.assertFalse(eligible({"source": "movers", "name": "Movers: short"}))
        self.assertFalse(eligible({"source": "auto", "name": "Theme: Uranium"}))
        self.assertFalse(eligible({"source": "user", "name": "Movers: leftover"}))
        self.assertTrue(eligible({"source": "user", "name": "Movers without colon"}))


if __name__ == "__main__":
    unittest.main()
