"""Synthetic OHLCV tests for the Base coil lens and the momentum coil flag."""
from __future__ import annotations

import math
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd

from tradingagents.screening.base_coil import preceded_by_base, score_base
from tradingagents.screening.early_momentum import score_ticker


def _series(values) -> pd.Series:
    idx = pd.bdate_range("2025-01-02", periods=len(values))
    return pd.Series([float(v) for v in values], index=idx)


def _tight_base(n: int = 80, box: int = 14):
    """Rising trend, then a tight box still inside the prior range."""
    closes = []
    px = 50.0
    for i in range(n):
        if i < n - box:
            px += 0.45
        else:
            anchor = closes[n - box - 1]
            px = anchor + (0.12 if i % 2 == 0 else -0.08)
        closes.append(px)
    close = _series(closes)
    high = close.copy()
    low = close.copy()
    for i in range(n):
        pad = 0.25 if i >= n - box else 0.55
        high.iloc[i] = close.iloc[i] + pad
        low.iloc[i] = close.iloc[i] - pad
    volume = _series([2_000_000.0] * (n - 5) + [700_000.0] * 5)
    return close, high, low, volume


class TestBaseCoilGates(unittest.TestCase):
    def test_tight_box_under_rising_average_stays_on_board(self):
        close, high, low, volume = _tight_base()
        payload = score_base(close, high, low, volume)
        self.assertTrue(payload["on_board"], payload.get("fail_reasons"))
        self.assertTrue(payload["inside_range"])
        self.assertTrue(payload["trend_pass"])
        self.assertGreaterEqual(payload["days_in_box"], 8)
        self.assertIsNotNone(payload["score"])
        self.assertGreaterEqual(payload["score"], 0)
        self.assertLessEqual(payload["score"], 100)
        self.assertLessEqual(payload["vol_ratio"], 1.5)

    def test_close_through_range_high_is_excluded(self):
        close, high, low, volume = _tight_base()
        prior_high = float(high.iloc[-21:-1].max())
        close = close.copy()
        high = high.copy()
        close.iloc[-1] = prior_high + 1.5
        high.iloc[-1] = close.iloc[-1] + 0.2
        payload = score_base(close, high, low, volume)
        self.assertFalse(payload["on_board"])
        self.assertIn("outside_range", payload["fail_reasons"])

    def test_one_quiet_day_inside_a_wide_range_is_excluded(self):
        n = 80
        closes = [100.0 + 8.0 * math.sin(i / 3.0) for i in range(n)]
        closes[-1] = closes[-2]
        close = _series(closes)
        high = close + 2.5
        low = close - 2.5
        high.iloc[-1] = close.iloc[-1] + 0.05
        low.iloc[-1] = close.iloc[-1] - 0.05
        volume = _series([2_000_000.0] * n)
        payload = score_base(close, high, low, volume)
        self.assertFalse(payload["on_board"])
        self.assertIn("box_too_short", payload["fail_reasons"])

    def test_flat_average_without_spy_excess_is_excluded(self):
        n = 80
        close = _series([100.0] * n)
        high = close + 0.3
        low = close - 0.3
        volume = _series([2_000_000.0] * (n - 5) + [700_000.0] * 5)
        spy = _series([100.0] * n)
        payload = score_base(close, high, low, volume, spy_close=spy)
        self.assertFalse(payload["trend_pass"])
        self.assertFalse(payload["on_board"])
        self.assertIn("no_trend", payload["fail_reasons"])


class TestCoilPrecededFlag(unittest.TestCase):
    def test_breakout_after_base_sets_flag_without_changing_score(self):
        close, high, low, volume = _tight_base()
        prior_high = float(high.iloc[-20:].max())
        extra_idx = close.index[-1] + pd.tseries.offsets.BDay(1)
        close = pd.concat([close, pd.Series([prior_high + 2.0], index=[extra_idx])])
        high = pd.concat([high, pd.Series([prior_high + 2.4], index=[extra_idx])])
        low = pd.concat([low, pd.Series([prior_high + 0.4], index=[extra_idx])])
        volume = pd.concat([volume, pd.Series([4_000_000.0], index=[extra_idx])])

        self.assertTrue(preceded_by_base(close, high, low, volume))
        scan_date = str(close.index[-1].date())
        now = datetime(2026, 6, 1, 18, 0)
        flagged = score_ticker(
            close, high, low, volume,
            scan_date=scan_date,
            now=now,
            phase="final",
            ticker="BOX",
        )
        self.assertTrue(flagged["coil_preceded"])
        with patch("tradingagents.screening.base_coil.preceded_by_base", return_value=False):
            ignored = score_ticker(
                close, high, low, volume,
                scan_date=scan_date,
                now=now,
                phase="final",
                ticker="BOX",
            )
        self.assertFalse(ignored["coil_preceded"])
        self.assertEqual(flagged["score_final"], ignored["score_final"])


class TestBaseCoilApiIsolation(unittest.TestCase):
    def test_base_run_excluded_from_opportunity_lens(self):
        import webapp.app as appmod

        single = {"strategy": "base_coil", "criteria": '{"strategy":"base_coil"}'}
        batch = {
            "strategy": "base_coil_union",
            "criteria": '{"strategy":"base_coil_union","base_coil_all_job_id":"b1","source_mode":"union_buckets"}',
        }
        self.assertFalse(appmod._run_matches_lens(single, "opportunity"))
        self.assertFalse(appmod._run_matches_lens(batch, "opportunity"))
        self.assertTrue(appmod._run_matches_lens(single, "base"))
        self.assertTrue(appmod._is_lens_scan_all_run(batch, "base"))
        self.assertFalse(appmod._is_scan_all_provenance_run(batch))
        self.assertEqual(appmod._scan_all_job_id_for_lens(batch, "base"), "b1")

    def test_base_scan_all_404_when_only_orphan_single_exists(self):
        import webapp.app as appmod
        from fastapi import HTTPException

        class _DB:
            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return [{
                    "id": 40,
                    "watchlist_id": 3,
                    "watchlist_name": "Base candidates",
                    "run_at": "2026-09-01T12:00:00",
                    "strategy": "base_coil",
                    "results_count": 1,
                    "criteria": '{"source_mode":"single_watchlist","strategy":"base_coil"}',
                }]

            def get_screening_results(self, run_id):
                return []

        with patch.object(appmod, "get_db", return_value=_DB()):
            with self.assertRaises(HTTPException) as ctx:
                appmod.get_scan_all_consolidated(
                    top=5, bottom=2, strategy="base_coil", db_path="test.db",
                )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_run_results_lift_base_fields(self):
        import webapp.app as appmod

        class _DB:
            def get_screening_run(self, run_id):
                return {
                    "id": run_id,
                    "strategy": "base_coil",
                    "criteria": '{"strategy":"base_coil"}',
                    "watchlist_id": 3,
                }

            def get_screening_results(self, run_id):
                return [{
                    "ticker": "BOX",
                    "composite_score": 12.0,
                    "direction": "neutral",
                    "rank": 1,
                    "signals": {
                        "_base_coil": {
                            "on_board": True,
                            "score": 72.0,
                            "days_in_box": 12,
                            "squeeze": 0.41,
                            "vol_ratio": 0.62,
                            "dist_to_high_pct": 1.8,
                            "trend_pass": True,
                        },
                    },
                }]

        with patch.object(appmod, "get_db", return_value=_DB()):
            payload = appmod.get_screening_run_results(77, db_path="test.db")
        self.assertEqual(payload["strategy"], "base_coil")
        row = payload["results"][0]
        self.assertEqual(row["base_score"], 72.0)
        self.assertEqual(row["base_days"], 12)
        self.assertEqual(row["base_squeeze"], 0.41)
        self.assertEqual(row["base_vol_ratio"], 0.62)
        self.assertEqual(row["base_dist_to_high_pct"], 1.8)
        self.assertTrue(row["base_trend_pass"])


if __name__ == "__main__":
    unittest.main()
