"""Engine failsafes: bounded downloads, partial-scan health, thread diagnostics."""
import threading
import time
import unittest

from tradingagents.screening import engine as eng


class _Reset(unittest.TestCase):
    def setUp(self):
        with eng._ABANDONED_LOCK:
            eng._ABANDONED_DOWNLOADS.clear()

    def tearDown(self):
        with eng._ABANDONED_LOCK:
            eng._ABANDONED_DOWNLOADS.clear()
        if eng._DOWNLOAD_LOCK.locked():
            try:
                eng._DOWNLOAD_LOCK.release()
            except RuntimeError:
                pass


class TestGuardedDownload(_Reset):
    def test_returns_value_and_releases_lock(self):
        self.assertEqual(eng._guarded_download("t", lambda: 42, hard_timeout=2, lock_wait=1), 42)
        self.assertFalse(eng._DOWNLOAD_LOCK.locked())

    def test_propagates_errors(self):
        def boom():
            raise ValueError("bad")

        with self.assertRaises(ValueError):
            eng._guarded_download("t", boom, hard_timeout=2, lock_wait=1)
        self.assertFalse(eng._DOWNLOAD_LOCK.locked())

    def test_hard_timeout_abandons_thread_and_frees_lock(self):
        gate = threading.Event()
        t0 = time.monotonic()
        with self.assertRaises(eng.DownloadTimeout):
            eng._guarded_download("hang", lambda: gate.wait(30), hard_timeout=0.2, lock_wait=1)
        try:
            self.assertLess(time.monotonic() - t0, 3)
            self.assertFalse(eng._DOWNLOAD_LOCK.locked())
            self.assertEqual(len(eng._live_abandoned_downloads()), 1)
        finally:
            gate.set()

    def test_quarantine_refuses_new_download_while_zombie_alive(self):
        gate = threading.Event()
        with self.assertRaises(eng.DownloadTimeout):
            eng._guarded_download("hang", lambda: gate.wait(30), hard_timeout=0.1, lock_wait=1)
        try:
            ran = []
            with self.assertRaises(eng.DownloadTimeout) as ctx:
                eng._guarded_download("next", lambda: ran.append(1), hard_timeout=1, lock_wait=1)
            self.assertIn("abandoned", str(ctx.exception))
            self.assertEqual(ran, [])
        finally:
            gate.set()
        # Once the zombie exits, downloads are allowed again.
        deadline = time.monotonic() + 2
        while eng._live_abandoned_downloads() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(eng._guarded_download("ok", lambda: "fine", hard_timeout=1, lock_wait=1), "fine")

    def test_lock_wait_is_bounded(self):
        eng._DOWNLOAD_LOCK.acquire()
        try:
            t0 = time.monotonic()
            with self.assertRaises(eng.DownloadTimeout) as ctx:
                eng._guarded_download("blocked", lambda: 1, hard_timeout=1, lock_wait=0.2)
            self.assertIn("lock", str(ctx.exception))
            self.assertLess(time.monotonic() - t0, 3)
        finally:
            eng._DOWNLOAD_LOCK.release()


class TestScanHealth(unittest.TestCase):
    def _engine(self):
        e = eng.ScreeningEngine.__new__(eng.ScreeningEngine)
        e._lock = threading.Lock()
        e._skipped = []
        return e

    def test_clean_scan_not_partial(self):
        e = self._engine()
        e._skipped = [{"ticker": "X", "reason": "no_data"}, {"ticker": "Y", "reason": "invalid_ticker"}]
        h = e.get_scan_health()
        self.assertFalse(h["partial"])
        self.assertEqual(h["skipped_count"], 2)

    def test_timeouts_and_deadline_are_partial(self):
        e = self._engine()
        e._skipped = [
            {"ticker": "A", "reason": "download_timeout"},
            {"ticker": "B", "reason": "download_timeout"},
            {"ticker": "C", "reason": "deadline_exceeded"},
            {"ticker": "D", "reason": "no_data"},
        ]
        h = e.get_scan_health()
        self.assertTrue(h["partial"])
        self.assertEqual(h["partial_ticker_count"], 3)
        self.assertEqual(h["partial_reasons"], {"download_timeout": 2, "deadline_exceeded": 1})


class TestThreadDiagnostics(unittest.TestCase):
    def test_stack_of_blocked_thread(self):
        from tradingagents.dataflows.thread_diagnostics import all_thread_stacks, find_thread, thread_stack

        gate = threading.Event()
        t = threading.Thread(target=gate.wait, name="diag-test-thread", daemon=True)
        t.start()
        try:
            time.sleep(0.05)
            self.assertIn("wait", thread_stack(find_thread("diag-test-thread")))
            self.assertIn("diag-test-thread", [r["name"] for r in all_thread_stacks()])
        finally:
            gate.set()


if __name__ == "__main__":
    unittest.main()
