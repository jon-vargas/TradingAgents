"""Tests for Wave 2 hardening: symbol sanity sweep + ticker_health eviction.

Covers:
- _is_valid_symbol_format() regex gate (accepts good, rejects Wikipedia typos)
- _validate_symbols_tiered() counts format_rejected and surfaces rejects in invalid
- ResearchDatabase.record_ticker_{failure,success}() idempotent updates
- ResearchDatabase.get_stale_ticker_candidates() threshold logic
- ResearchDatabase.evict_stale_tickers() dry-run vs apply semantics
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


class TestSymbolFormatGate(unittest.TestCase):
    def test_accepts_common_shapes(self):
        from tradingagents.screening.builtin_refresh import _is_valid_symbol_format

        for good in ["A", "AA", "AAPL", "MSFT", "GOOG", "BRK.B", "BRK-B", "T", "AMZN", "NVDA"]:
            self.assertTrue(_is_valid_symbol_format(good), f"should accept: {good!r}")

    def test_rejects_obvious_junk(self):
        from tradingagents.screening.builtin_refresh import _is_valid_symbol_format

        for bad in ["", " ", "TOOLONGSYM", "123ABC", "A B", "aapl", "-AAPL", ".MSFT", "AAPL.", "AAPL-"]:
            self.assertFalse(_is_valid_symbol_format(bad), f"should reject: {bad!r}")


class TestTieredValidationFormatReject(unittest.TestCase):
    def test_format_rejects_are_counted_and_surfaced_as_invalid(self):
        from tradingagents.screening import builtin_refresh

        # Patch the external membership sets so tier1 has AAPL, tier2 has MSFT,
        # and NOTASYMBOL1234 / empty-string / lowercase variants get filtered
        # by the regex BEFORE they reach either tier.
        with patch.object(builtin_refresh, "get_active_us_symbols", return_value={"AAPL"}), \
             patch.object(builtin_refresh, "get_us_symbol_set", return_value={"MSFT"}), \
             patch.object(builtin_refresh, "_fetch_ticker_snapshot_limited",
                          return_value={"symbol": "X", "valid": False}):
            valid, invalid, _caps, stats = builtin_refresh._validate_symbols_tiered(
                ["AAPL", "MSFT", "NOTASYMBOL1234", "aapl", "123XYZ", ""],
                max_workers=1,
                chunk_size=10,
            )

        self.assertEqual(set(valid), {"AAPL", "MSFT"})
        # Format-rejected symbols appear in invalid tail
        for rej in ("NOTASYMBOL1234", "123XYZ"):
            self.assertIn(rej, invalid)
        self.assertGreaterEqual(stats["format_rejected"], 2)


class TestTickerHealthAndEviction(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="th_test_")
        self.db_path = os.path.join(self.tmpdir, "research.db")
        from tradingagents.reporting.database import ResearchDatabase
        self.db = ResearchDatabase(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_metadata(self, ticker: str) -> None:
        self.db.save_ticker_metadata(
            ticker=ticker,
            sector="Tech",
            market_cap_tier="large",
            is_profitable=True,
            has_dividend=False,
            beta=1.0,
            beta_tier="medium",
            resolved_profile="large_cap_core",
            resolved_preset="value_fisher",
        )

    def test_record_failure_increments_and_timestamps(self):
        self.db.record_ticker_failure("FAIL1", reason="delisted")
        self.db.record_ticker_failure("FAIL1", reason="delisted")
        row = self.db.get_ticker_health("FAIL1")
        self.assertIsNotNone(row)
        self.assertEqual(row["failure_count"], 2)
        self.assertEqual(row["last_failure_reason"], "delisted")
        self.assertIsNotNone(row["last_failure_at"])

    def test_record_success_increments_success_counter(self):
        self.db.record_ticker_success("OK1")
        self.db.record_ticker_success("OK1")
        row = self.db.get_ticker_health("OK1")
        self.assertEqual(row["success_count"], 2)
        self.assertEqual(row["failure_count"], 0)

    def test_candidates_respect_min_failures_and_recent_success(self):
        # FAIL1: 4 failures, no success → eligible
        for _ in range(4):
            self.db.record_ticker_failure("FAIL1")
        # FAIL2: 4 failures but just had a success → NOT eligible
        for _ in range(4):
            self.db.record_ticker_failure("FAIL2")
        self.db.record_ticker_success("FAIL2")
        # FAIL3: only 2 failures → NOT eligible (below min)
        for _ in range(2):
            self.db.record_ticker_failure("FAIL3")

        candidates = self.db.get_stale_ticker_candidates(
            min_failures=3, min_days_since_success=2,
        )
        names = {c["ticker"] for c in candidates}
        self.assertIn("FAIL1", names)
        self.assertNotIn("FAIL2", names)  # recent success
        self.assertNotIn("FAIL3", names)  # below min

    def test_evict_dry_run_does_not_delete_metadata(self):
        self._seed_metadata("BAD1")
        for _ in range(5):
            self.db.record_ticker_failure("BAD1")

        result = self.db.evict_stale_tickers(min_failures=3, min_days_since_success=2, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["evicted_count"], 0)
        # Metadata row preserved
        meta = self.db.get_ticker_metadata_bulk(["BAD1"])
        self.assertIn("BAD1", meta)

    def test_evict_apply_removes_metadata_and_marks_eviction(self):
        self._seed_metadata("BAD2")
        for _ in range(5):
            self.db.record_ticker_failure("BAD2")

        result = self.db.evict_stale_tickers(min_failures=3, min_days_since_success=2, dry_run=False)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["evicted_count"], 1)
        self.assertIn("BAD2", result["evicted_tickers"])
        # Metadata row removed
        meta = self.db.get_ticker_metadata_bulk(["BAD2"])
        self.assertNotIn("BAD2", meta)
        # Eviction audit trail retained
        row = self.db.get_ticker_health("BAD2")
        self.assertIsNotNone(row["evicted_at"])
        self.assertIn("stale_ticker", row["eviction_reason"])

    def test_already_evicted_rows_not_re_evicted(self):
        self._seed_metadata("BAD3")
        for _ in range(5):
            self.db.record_ticker_failure("BAD3")
        # First eviction
        self.db.evict_stale_tickers(min_failures=3, dry_run=False)
        # Second call should no-op (no more candidates)
        result = self.db.evict_stale_tickers(min_failures=3, dry_run=False)
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["evicted_count"], 0)

    def test_get_evicted_tickers_returns_marked_set(self):
        self._seed_metadata("BAD4")
        self._seed_metadata("BAD5")
        for sym in ("BAD4", "BAD5"):
            for _ in range(5):
                self.db.record_ticker_failure(sym)
        self.db.evict_stale_tickers(min_failures=3, dry_run=False)

        evicted = self.db.get_evicted_tickers()
        self.assertIn("BAD4", evicted)
        self.assertIn("BAD5", evicted)

    def test_get_evicted_tickers_respects_since_window(self):
        self._seed_metadata("OLDBAD")
        for _ in range(5):
            self.db.record_ticker_failure("OLDBAD")
        self.db.evict_stale_tickers(min_failures=3, dry_run=False)

        # Backdate the eviction so it falls outside a 1-day window
        old_iso = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        with self.db._connect() as conn:
            conn.execute(
                "UPDATE ticker_health SET evicted_at = ? WHERE ticker = ?",
                (old_iso, "OLDBAD"),
            )
            conn.commit()

        # Within last 1 day → empty
        recent = self.db.get_evicted_tickers(since_days=1)
        self.assertNotIn("OLDBAD", recent)
        # Within last 30 days → present
        wide = self.db.get_evicted_tickers(since_days=30)
        self.assertIn("OLDBAD", wide)
        # No window → present
        any_window = self.db.get_evicted_tickers()
        self.assertIn("OLDBAD", any_window)


if __name__ == "__main__":
    unittest.main()
