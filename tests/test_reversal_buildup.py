"""Reversal-buildup v1 tests.

Synthetic OHLCV only — no live Yahoo. Historical windows we already measured
for later manual QA (not asserted here): ACHR 2026-07-10→07-24, DDD / GSAT /
ZIM / SLV washout-to-turn tapes.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.screening.context_packet import build_context_packet, format_screening_context
from tradingagents.screening.engine import ScreeningEngine
from tradingagents.screening.reversal_buildup import (
    DEFAULT_VISIBLE_PHASES,
    apply_reversal_context,
    compute_score,
    empty_payload,
    evaluate_phase,
    lift_reversal_fields,
    rank_reversal_rows,
    reversal_sort_key,
    score_ticker,
    session_complete_tag,
    truncate_batch_data,
    truncate_price_series,
)


def _latest_runs_per_watchlist(runs, results, min_results=1):
    out = []
    for run in runs:
        if not run.get("watchlist_id"):
            continue
        count = run.get("results_count")
        if count is None:
            count = len(results.get(run.get("id"), []) or [])
            if count == 0:
                count = 1
        if int(count or 0) < int(min_results):
            continue
        out.append(run)
    return sorted(
        out,
        key=lambda row: (str(row.get("run_at") or ""), int(row.get("id") or 0)),
        reverse=True,
    )


def _base_feat(**overrides):
    feat = {
        "n_bars": 80,
        "rsi": 38.0,
        "rsi_min_60": 24.0,
        "rsi_max_60": 62.0,
        "rsi_recover": 14.0,
        "days_since_rsi_extreme_min": 6,
        "days_since_rsi_extreme_max": 40,
        "days_since_range_low": 5,
        "days_since_range_high": 50,
        "pos60": 0.18,
        "pos60_min": 0.04,
        "pos60_max": 0.92,
        "macd_hist": -0.04,
        "macd_hist_prev": -0.07,
        "macd_rising": True,
        "macd_rising_3": True,
        "macd_falling_3": False,
        "macd_prior3_min": -0.09,
        "macd_prior3_max": -0.03,
        "rsi_rising_3": True,
        "rsi_falling_3": False,
        "up_vol_share_10d": 0.48,
        "down_vol_share_10d": 0.32,
        "reclaim_sma20": False,
        "breakout_10d": False,
        "breakdown_10d": False,
        "ret_20d_atr": 0.4,
        "weekly_rsi_slope": 0.5,
        "failed_turn_long": False,
        "failed_turn_short": False,
        "making_new_rsi_low": False,
        "making_new_rsi_high": False,
        "vs_spy": "idiosyncratic",
        "close": 42.0,
    }
    feat.update(overrides)
    return feat


def _ohlcv(close_vals, volume=None, start="2025-10-01"):
    idx = pd.bdate_range(start, periods=len(close_vals))
    close = pd.Series(close_vals, index=idx, dtype=float)
    high = close + 0.6
    low = close - 0.6
    if volume is None:
        volume = pd.Series(1_000_000.0, index=idx)
    spy = pd.Series(np.linspace(400, 420, len(close_vals)), index=idx)
    return close, high, low, volume, spy


def _washout_then_turn(n=90, bounce=12):
    """Prolonged decline then a short bounce with up-day volume."""
    decline = np.linspace(95, 36, n - bounce)
    bounce_path = np.linspace(36, 50, bounce)
    close = np.concatenate([decline, bounce_path])
    vol = np.full(n, 700_000.0)
    for i in range(n - bounce - 5, n - bounce):
        vol[i] = 1_800_000.0  # capitulation
    for i in range(n - bounce, n):
        vol[i] = 1_400_000.0 if bounce_path[i - (n - bounce)] > (bounce_path[i - (n - bounce) - 1] if i > n - bounce else 36) else 600_000.0
    return _ohlcv(close, pd.Series(vol, index=pd.bdate_range("2025-10-01", periods=n)))


def _still_dumping(n=80):
    close = np.linspace(90, 32, n)
    return _ohlcv(close)


class TestPhaseStateMachine(unittest.TestCase):
    def test_none_insufficient_history(self):
        side, phase, reasons = evaluate_phase(_base_feat(n_bars=20))
        self.assertEqual(side, "none")
        self.assertEqual(phase, "none")
        self.assertIn("insufficient_history", reasons)

    def test_none_no_dislocation(self):
        side, phase, reasons = evaluate_phase(_base_feat(rsi_min_60=45, pos60_min=0.40))
        self.assertEqual(phase, "none")
        self.assertIn("no_dislocation", reasons)

    def test_late_assigned_first(self):
        side, phase, _ = evaluate_phase(_base_feat(rsi=62, pos60=0.55, reclaim_sma20=True, breakout_10d=True))
        self.assertEqual(phase, "late")
        self.assertEqual(side, "long")

    def test_confirmed_near_zone_allows_small_extension(self):
        side, phase, reasons = evaluate_phase(_base_feat(
            pos60=0.32,
            reclaim_sma20=True,
            rsi=42,
        ))
        self.assertEqual(phase, "confirmed")
        self.assertEqual(side, "long")
        self.assertIn("reclaim_sma20", reasons)

    def test_confirmed_left_setup_zone_is_watching(self):
        side, phase, reasons = evaluate_phase(_base_feat(
            pos60=0.40,
            reclaim_sma20=True,
            rsi=42,
        ))
        self.assertEqual(phase, "watching")
        self.assertEqual(side, "long")
        self.assertIn("left_setup_zone", reasons)
        self.assertIn("reclaim_sma20", reasons)

    def test_thin_rsi_travel_caps_at_watching(self):
        _, phase, reasons = evaluate_phase(_base_feat(
            rsi=72.0,
            rsi_min_60=40.0,
            rsi_max_60=73.0,
            pos60=0.88,
            pos60_min=0.20,
            pos60_max=0.95,
            days_since_rsi_extreme_min=40,
            days_since_rsi_extreme_max=4,
            days_since_range_high=3,
            macd_rising=False,
            macd_hist=0.05,
            macd_hist_prev=0.09,
            macd_falling_3=True,
            rsi_falling_3=True,
            reclaim_sma20=True,
            down_vol_share_10d=0.70,
            weekly_rsi_slope=-1.0,
        ))
        self.assertEqual(phase, "watching")
        self.assertIn("thin_rsi_travel", reasons)

    def test_short_weekly_against_demotes_to_watching(self):
        _, phase, reasons = evaluate_phase(_base_feat(
            rsi=68.0,
            rsi_min_60=40.0,
            rsi_max_60=82.0,
            pos60=0.88,
            pos60_min=0.20,
            pos60_max=0.95,
            days_since_rsi_extreme_min=40,
            days_since_rsi_extreme_max=4,
            days_since_range_high=3,
            macd_rising=False,
            macd_hist=0.05,
            macd_hist_prev=0.09,
            macd_falling_3=True,
            rsi_falling_3=True,
            reclaim_sma20=True,
            down_vol_share_10d=0.70,
            weekly_rsi_slope=2.5,
        ))
        self.assertEqual(phase, "watching")
        self.assertIn("weekly_against_hard", reasons)

    def test_long_weekly_against_stays_confirmed(self):
        _, phase, reasons = evaluate_phase(_base_feat(
            pos60=0.18,
            reclaim_sma20=True,
            rsi=42,
            weekly_rsi_slope=-4.0,
        ))
        self.assertEqual(phase, "confirmed")
        self.assertNotIn("weekly_against_hard", reasons)

    def test_early_turn_requires_quartile_and_hist_turn(self):
        side, phase, _ = evaluate_phase(_base_feat(
            pos60=0.18,
            reclaim_sma20=False,
            breakout_10d=False,
            up_vol_share_10d=0.40,
            macd_rising_3=True,
            rsi_rising_3=True,
            macd_rising=True,
            macd_hist=-0.03,
        ))
        self.assertEqual(phase, "early_turn")
        self.assertEqual(side, "long")

    def test_early_turn_without_persist(self):
        side, phase, reasons = evaluate_phase(_base_feat(
            pos60=0.18,
            reclaim_sma20=False,
            breakout_10d=False,
            up_vol_share_10d=0.40,
            macd_rising_3=False,
            rsi_rising_3=False,
            macd_rising=True,
            macd_hist=-0.03,
        ))
        self.assertEqual(phase, "early_turn")
        self.assertEqual(side, "long")
        self.assertIn("persist_pending", reasons)

    def test_volume_without_hist_turn_is_watching(self):
        _, phase, reasons = evaluate_phase(_base_feat(
            pos60=0.18,
            reclaim_sma20=True,
            macd_rising=False,
            macd_hist=0.02,
            macd_hist_prev=-0.01,
        ))
        self.assertEqual(phase, "watching")
        self.assertIn("volume_without_turn", reasons)

    def test_stale_extreme_does_not_floor_phase(self):
        side, phase, reasons = evaluate_phase(_base_feat(
            days_since_rsi_extreme_min=22,
            days_since_range_low=18,
            reclaim_sma20=True,
        ))
        self.assertEqual(phase, "confirmed")
        self.assertEqual(side, "long")
        self.assertIn("stale_extreme", reasons)

    def test_long_weekly_hard_demote_slope(self):
        _, phase, reasons = evaluate_phase(_base_feat(
            pos60=0.18,
            reclaim_sma20=True,
            rsi=42,
            weekly_rsi_slope=-6.0,
        ))
        self.assertEqual(phase, "watching")
        self.assertIn("weekly_against_hard", reasons)

    def test_late_rsi_long_at_55(self):
        _, phase, reasons = evaluate_phase(_base_feat(rsi=55, pos60=0.18, reclaim_sma20=True))
        self.assertEqual(phase, "late")
        self.assertIn("late_rsi", reasons)

    def test_late_pos60_just_over_040(self):
        _, phase, reasons = evaluate_phase(_base_feat(
            pos60=0.41,
            rsi=42,
            reclaim_sma20=True,
            ret_20d_atr=0.2,
        ))
        self.assertEqual(phase, "late")
        self.assertIn("late_mid_band", reasons)

    def test_watching_dislocation_only(self):
        _, phase, _ = evaluate_phase(_base_feat(
            macd_rising=False,
            macd_rising_3=False,
            rsi_rising_3=False,
            reclaim_sma20=False,
            breakout_10d=False,
            up_vol_share_10d=0.20,
            macd_hist=-0.2,
            macd_hist_prev=-0.1,
        ))
        self.assertEqual(phase, "watching")

    def test_short_early_turn_mirror(self):
        _, phase, _ = evaluate_phase(_base_feat(
            rsi=72,
            rsi_min_60=40,
            rsi_max_60=82,
            pos60=0.88,
            pos60_min=0.20,
            pos60_max=0.95,
            days_since_rsi_extreme_max=4,
            days_since_range_high=3,
            macd_rising=False,
            macd_hist=0.05,
            macd_hist_prev=0.09,
            macd_falling_3=True,
            rsi_falling_3=True,
            reclaim_sma20=True,
            breakdown_10d=False,
            down_vol_share_10d=0.40,
            up_vol_share_10d=0.30,
            weekly_rsi_slope=-1.0,
        ))
        self.assertEqual(phase, "early_turn")


class TestScoreLock(unittest.TestCase):
    def test_recovering_outranks_deeper_oversold_flat(self):
        recovering = _base_feat(rsi=38, rsi_min_60=23, making_new_rsi_low=False, pos60=0.20,
                                macd_hist=-0.02, macd_hist_prev=-0.06, macd_rising=True,
                                macd_rising_3=True, up_vol_share_10d=0.50)
        dumping = _base_feat(rsi=18, rsi_min_60=18, making_new_rsi_low=True, pos60=0.08,
                             macd_hist=-0.12, macd_hist_prev=-0.10, macd_rising=False,
                             macd_rising_3=False, up_vol_share_10d=0.25)
        s_rec, _, _ = compute_score(recovering, "long", "early_turn")
        s_dump, _, _ = compute_score(dumping, "long", "watching")
        self.assertGreater(s_rec, s_dump)

    def test_late_penalty_multiplier(self):
        feat = _base_feat()
        raw, _, _ = compute_score(feat, "long", "early_turn")
        penalized, _, penalties = compute_score(feat, "long", "late")
        self.assertIn("penalty_late", penalties)
        self.assertLess(penalized, raw)
        self.assertAlmostEqual(penalized, round(raw * 0.70, 2), places=1)

    def test_failed_turn_penalty(self):
        base = _base_feat()
        failed = _base_feat(failed_turn_long=True)
        s0, _, _ = compute_score(base, "long", "early_turn")
        s1, _, penalties = compute_score(failed, "long", "early_turn")
        self.assertIn("penalty_failed_turn", penalties)
        self.assertAlmostEqual(s1, round(s0 * 0.75, 2), places=1)

    def test_weekly_against_penalty(self):
        base = _base_feat(weekly_rsi_slope=1.0)
        against = _base_feat(weekly_rsi_slope=-1.2)
        s0, _, _ = compute_score(base, "long", "early_turn")
        s1, _, penalties = compute_score(against, "long", "early_turn")
        self.assertIn("penalty_weekly_against", penalties)
        self.assertAlmostEqual(s1, round(s0 * 0.85, 2), places=1)

    def test_short_pressure_penalty(self):
        feat = _base_feat(rsi=75, pos60=0.85, vs_spy="idiosyncratic")
        s0, _, _ = compute_score(feat, "short", "early_turn", risk_components={"short_pressure": 0.2})
        s1, _, penalties = compute_score(feat, "short", "early_turn", risk_components={"short_pressure": 0.7})
        self.assertIn("penalty_short_pressure", penalties)
        self.assertAlmostEqual(s1, round(s0 * 0.90, 2), places=1)

    def test_watching_penalty(self):
        feat = _base_feat()
        raw, _, _ = compute_score(feat, "long", "early_turn")
        watching, _, penalties = compute_score(feat, "long", "watching")
        self.assertIn("penalty_watching", penalties)
        self.assertAlmostEqual(watching, round(raw * 0.80, 2), places=1)

    def test_stale_extreme_penalty(self):
        fresh = _base_feat()
        stale = _base_feat(days_since_rsi_extreme_min=22, days_since_range_low=18)
        s0, _, _ = compute_score(fresh, "long", "confirmed")
        s1, _, penalties = compute_score(stale, "long", "confirmed")
        self.assertIn("penalty_stale", penalties)
        self.assertAlmostEqual(s1, round(s0 * 0.90, 2), places=1)

    def test_high_risk_penalty(self):
        feat = _base_feat()
        s0, _, _ = compute_score(feat, "long", "confirmed", risk_score=40)
        s1, _, penalties = compute_score(feat, "long", "confirmed", risk_score=77)
        self.assertIn("penalty_high_risk", penalties)
        self.assertAlmostEqual(s1, round(s0 * 0.70, 2), places=1)

    def test_rsi_recovery_decays_after_peak(self):
        peak = _base_feat(rsi=34.0, rsi_min_60=24.0)
        stretched = _base_feat(rsi=51.0, rsi_min_60=24.0)
        _, comps_peak, _ = compute_score(peak, "long", "confirmed")
        _, comps_stretch, _ = compute_score(stretched, "long", "confirmed")
        self.assertGreater(comps_peak["rsi_recovery"], comps_stretch["rsi_recovery"])
        self.assertAlmostEqual(comps_peak["rsi_recovery"], 100.0, places=0)
        self.assertLess(comps_stretch["rsi_recovery"], 20.0)

    def test_weak_composite_demotes_long(self):
        payload = {
            "side": "long",
            "phase": "confirmed",
            "score": 42.5,
            "reasons": ["reclaim_sma20"],
            "features": {},
        }
        apply_reversal_context(payload, composite_score=8.1, direction="bullish")
        self.assertEqual(payload["phase"], "watching")
        self.assertIn("weak_composite", payload["reasons"])
        self.assertAlmostEqual(payload["score"], round(42.5 * 0.75, 2), places=1)

    def test_lens_conflict_demotes_short(self):
        payload = {
            "side": "short",
            "phase": "confirmed",
            "score": 56.3,
            "reasons": ["breakdown_10d"],
            "features": {},
        }
        apply_reversal_context(
            payload, composite_score=43.5, macro_fit=70.0, direction="bullish"
        )
        self.assertEqual(payload["phase"], "watching")
        self.assertIn("lens_conflict", payload["reasons"])
        self.assertEqual(payload["score"], 56.3)

    def test_phase_band_outranks_score(self):
        early = {"phase": "early_turn", "score": 40, "features": {"days_since_rsi_extreme": 3}}
        confirmed = {"phase": "confirmed", "score": 70, "features": {"days_since_rsi_extreme": 2}}
        watching = {"phase": "watching", "score": 90, "features": {"days_since_rsi_extreme": 1}}
        self.assertLess(reversal_sort_key(early, "B"), reversal_sort_key(confirmed, "A"))
        self.assertLess(reversal_sort_key(confirmed, "A"), reversal_sort_key(watching, "W"))

    def test_market_dump_short_penalty(self):
        base = _base_feat(vs_spy="idiosyncratic")
        dump = _base_feat(vs_spy="market_dump")
        s0, _, _ = compute_score(base, "short", "early_turn")
        s1, _, penalties = compute_score(dump, "short", "early_turn")
        self.assertIn("penalty_market_dump_short", penalties)
        self.assertAlmostEqual(s1, round(s0 * 0.85, 2), places=1)

    def test_tie_break_fresher_extreme_first(self):
        a = {"score": 50.0, "features": {"days_since_rsi_extreme": 2}}
        b = {"score": 50.0, "features": {"days_since_rsi_extreme": 9}}
        self.assertLess(reversal_sort_key(a, "ZZZ"), reversal_sort_key(b, "AAA"))

    def test_config_weights_sum_to_one(self):
        weights = DEFAULT_CONFIG["screening"]["reversal_buildup"]["weights"]
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)


class TestEodTruncate(unittest.TestCase):
    def test_drops_today_bar_before_close(self):
        today = datetime(2026, 8, 14, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        idx = pd.bdate_range("2026-07-01", periods=30)
        # Force last index date to scan date
        idx = idx[:-1].append(pd.DatetimeIndex([pd.Timestamp("2026-08-14")]))
        series = pd.Series(np.linspace(10, 40, len(idx)), index=idx)
        truncated = truncate_price_series(series, "2026-08-14", now=today)
        self.assertTrue(all(pd.Timestamp(i).date() < today.date() for i in truncated.index))
        self.assertLess(len(truncated), len(series))

    def test_historical_date_is_noop(self):
        idx = pd.bdate_range("2026-01-02", periods=20)
        series = pd.Series(range(20), index=idx, dtype=float)
        now = datetime(2026, 8, 14, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        out = truncate_price_series(series, "2026-01-30", now=now)
        self.assertEqual(len(out), len(series))

    def test_truncate_batch_includes_spy(self):
        today = datetime(2026, 8, 14, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        idx = pd.bdate_range("2026-07-01", periods=20).append(pd.DatetimeIndex([pd.Timestamp("2026-08-14")]))
        close = pd.Series(np.linspace(10, 30, len(idx)), index=idx)
        spy = pd.Series(np.linspace(400, 410, len(idx)), index=idx)
        batch = {
            "AAA": {"close": close, "high": close + 1, "low": close - 1, "volume": pd.Series(1e6, index=idx), "spy_close": spy},
        }
        truncate_batch_data(batch, "2026-08-14", now=today)
        self.assertNotIn(pd.Timestamp("2026-08-14"), batch["AAA"]["close"].index)
        self.assertNotIn(pd.Timestamp("2026-08-14"), batch["AAA"]["spy_close"].index)

    def test_session_tag_open_vs_closed(self):
        morning = datetime(2026, 8, 14, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        evening = datetime(2026, 8, 14, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(session_complete_tag("2026-08-14", now=morning), "open")
        self.assertEqual(session_complete_tag("2026-08-14", now=evening), "closed")


class TestSyntheticTapes(unittest.TestCase):
    def test_flat_range_is_none(self):
        close, high, low, volume, spy = _ohlcv(np.full(80, 50.0))
        payload = score_ticker(close, high, low, volume, spy_close=spy)
        self.assertEqual(payload["phase"], "none")
        self.assertEqual(payload["side"], "none")

    def test_insufficient_history(self):
        close, high, low, volume, spy = _ohlcv(np.linspace(50, 40, 30))
        payload = score_ticker(close, high, low, volume, spy_close=spy)
        self.assertEqual(payload, empty_payload("insufficient_history"))

    def test_washout_turn_beats_still_dumping(self):
        c1, h1, l1, v1, s1 = _washout_then_turn()
        c2, h2, l2, v2, s2 = _still_dumping()
        turn = score_ticker(c1, h1, l1, v1, spy_close=s1)
        dump = score_ticker(c2, h2, l2, v2, spy_close=s2)
        self.assertIn(turn["phase"], {"early_turn", "confirmed", "watching", "late"})
        self.assertNotEqual(dump["phase"], "confirmed")
        turn_feat = turn.get("features") or {}
        dump_feat = dump.get("features") or {}
        self.assertGreater(float(turn_feat.get("rsi_recover") or 0), float(dump_feat.get("rsi_recover") or 0))
        self.assertTrue(turn_feat.get("macd_rising"))

    def test_failed_turn_ranks_below_early_turn_features(self):
        early = _base_feat(failed_turn_long=False)
        failed = _base_feat(failed_turn_long=True)
        s_early, _, _ = compute_score(early, "long", "early_turn")
        s_fail, _, _ = compute_score(failed, "long", "watching")
        self.assertGreater(s_early, s_fail)

    def test_vs_spy_idiosyncratic_when_spy_not_washed(self):
        close, high, low, volume, _ = _washout_then_turn()
        spy = pd.Series(np.linspace(400, 430, len(close)), index=close.index)
        payload = score_ticker(close, high, low, volume, spy_close=spy)
        vs = (payload.get("features") or {}).get("vs_spy")
        self.assertIn(vs, {None, "idiosyncratic", "market_dump", "mixed"})


class _FakeDB:
    def __init__(self):
        self.saved_criteria = None
        self.saved_results = None
        self.last_run_id = 7

    def get_evicted_tickers(self, since_days=None):
        return set()

    def get_screening_runs(self, limit=5):
        return []

    def get_screening_results(self, run_id):
        return []

    def get_watchlist(self, watchlist_id):
        return None

    def save_ticker_metadata(self, ticker, **kwargs):
        return None

    def save_screening_run(self, watchlist_id, criteria, ticker_count, results_count):
        self.saved_criteria = json.loads(criteria)
        return self.last_run_id

    def save_screening_results(self, run_id, results):
        self.saved_results = results


def _engine_scan(tickers, batch, **scan_kwargs):
    db = _FakeDB()
    engine = ScreeningEngine(config=DEFAULT_CONFIG, db=db)

    def _fake_fetch(chunk, date):
        return {t: batch[t] for t in chunk if t in batch}

    with patch.object(engine, "_fetch_batch_ohlcv", side_effect=_fake_fetch), \
         patch.object(engine, "_fetch_info_cached", return_value={}), \
         patch.object(engine, "_get_regime", return_value="neutral"), \
         patch.object(engine, "_compute_macro_overlay", return_value={}), \
         patch.object(engine, "_load_prior_signals", return_value={}), \
         patch.object(engine, "_fetch_enhanced_data") as mock_enh:
        results = engine.scan(
            tickers=tickers,
            date="2026-07-20",
            preset="momentum_hunter",
            watchlist_id=1,
            enable_enhanced=True,
            **scan_kwargs,
        )
    return engine, db, results, mock_enh


class TestEngineWiring(unittest.TestCase):
    def test_forces_enhanced_off_and_ranks_by_reversal(self):
        turn = _washout_then_turn()
        dump = _still_dumping()
        batch = {
            "TURN": {"close": turn[0], "high": turn[1], "low": turn[2], "volume": turn[3], "info": {}, "spy_close": turn[4]},
            "DUMP": {"close": dump[0], "high": dump[1], "low": dump[2], "volume": dump[3], "info": {}, "spy_close": dump[4]},
        }
        # Inflate DUMP composite by making it look like a momentum name in the
        # engine's other signals: we only care that reversal rank is used.
        _, db, results, mock_enh = _engine_scan(
            ["TURN", "DUMP"],
            batch,
            criteria_meta={"strategy": "reversal_buildup"},
        )
        mock_enh.assert_not_called()
        self.assertEqual(db.saved_criteria.get("strategy"), "reversal_buildup")
        payloads = {
            r.ticker: (r.signals or {}).get("_reversal_buildup") for r in results
        }
        self.assertTrue(all(payloads[t] for t in ("TURN", "DUMP")))
        ranked = sorted(results, key=lambda r: r.rank)
        expected = sorted(
            results,
            key=lambda r: reversal_sort_key(
                (r.signals or {}).get("_reversal_buildup"), r.ticker
            ),
        )
        self.assertEqual([r.ticker for r in ranked], [r.ticker for r in expected])

    def test_standard_mode_does_not_require_reversal_payload(self):
        dump = _still_dumping()
        batch = {
            "DUMP": {"close": dump[0], "high": dump[1], "low": dump[2], "volume": dump[3], "info": {}, "spy_close": dump[4]},
        }
        _, _, results, mock_enh = _engine_scan(["DUMP"], batch)
        self.assertTrue(results)
        self.assertIsNone((results[0].signals or {}).get("_reversal_buildup"))
        mock_enh.assert_called()


class TestPersistLiftAndReadPaths(unittest.TestCase):
    def test_lift_round_trip(self):
        row = {
            "ticker": "AAA",
            "signals": {
                "_reversal_buildup": {
                    "side": "long",
                    "phase": "early_turn",
                    "score": 61.2,
                    "reasons": ["macd_hist_rising_neg"],
                    "features": {"days_since_rsi_extreme": 4},
                }
            },
        }
        lift_reversal_fields(row)
        self.assertEqual(row["reversal_phase"], "early_turn")
        self.assertEqual(row["reversal_score"], 61.2)
        self.assertEqual(row["signals"]["_reversal_buildup"]["phase"], "early_turn")

    def test_rank_filter_then_sort_then_slice(self):
        rows = [
            {"ticker": "LATE", "signals": {"_reversal_buildup": {"phase": "late", "score": 90, "features": {"days_since_rsi_extreme": 1}}}},
            {"ticker": "B", "signals": {"_reversal_buildup": {"phase": "early_turn", "score": 40, "features": {"days_since_rsi_extreme": 3}}}},
            {"ticker": "A", "signals": {"_reversal_buildup": {"phase": "confirmed", "score": 70, "features": {"days_since_rsi_extreme": 2}}}},
            {"ticker": "NONE", "signals": {"_reversal_buildup": {"phase": "none", "score": 99, "features": {}}}},
            {"ticker": "WATCH", "signals": {"_reversal_buildup": {"phase": "watching", "score": 80, "features": {}}}},
        ]
        top = rank_reversal_rows(rows, top_n=2)
        self.assertEqual([r["ticker"] for r in top], ["B", "A"])
        self.assertEqual(set(r["reversal_phase"] for r in top), DEFAULT_VISIBLE_PHASES)

    def test_enrich_does_not_resort(self):
        import webapp.app as appmod

        class _DB:
            def get_ticker_metadata_bulk(self, tickers):
                return {t: {"sector": "Tech", "avg_dollar_volume_usd": 5_000_000} for t in tickers}

            def get_upcoming_events(self, **_kwargs):
                return []

        rows = [
            {"ticker": "LOW", "rank": 1, "composite_score": 20, "signals": {"_reversal_buildup": {"phase": "early_turn", "score": 80}}, "direction": "bullish"},
            {"ticker": "HIGH", "rank": 2, "composite_score": 90, "signals": {"_reversal_buildup": {"phase": "early_turn", "score": 40}}, "direction": "bullish"},
        ]
        cfg = deepcopy(DEFAULT_CONFIG["screening"])
        cfg["scan_all"] = dict(cfg.get("scan_all") or {})
        cfg["scan_all"]["weekly_mtf"] = {"enabled": False}
        out = appmod._enrich_screening_rows(rows, db=_DB(), screening_config=cfg)
        self.assertEqual([r["ticker"] for r in out], ["LOW", "HIGH"])
        self.assertEqual(out[0]["reversal_score"], 80)

    def test_db_persist_strategy_and_embed(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="reversal_persist_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            wl = int(db.create_watchlist(name="Rev", tickers="AAA", description="t"))
            run_id = db.save_screening_run(
                watchlist_id=wl,
                criteria=json.dumps({"strategy": "reversal_buildup", "preset": "momentum_hunter"}),
                ticker_count=1,
                results_count=1,
            )
            db.save_screening_results(run_id, [{
                "ticker": "AAA",
                "composite_score": 12.0,
                "direction": "bullish",
                "signals": {"_reversal_buildup": {"phase": "early_turn", "score": 55.0, "side": "long", "reasons": []}},
                "signal_deltas": {},
                "rank": 1,
            }])
            run = db.get_screening_run(run_id)
            self.assertEqual(run.get("strategy"), "reversal_buildup")
            rows = db.get_screening_results(run_id)
            sigs = rows[0]["signals"]
            if isinstance(sigs, str):
                sigs = json.loads(sigs)
            self.assertEqual(sigs["_reversal_buildup"]["score"], 55.0)
        finally:
            os.remove(db_path)

    def test_context_packet_includes_reversal(self):
        packet = build_context_packet({
            "ticker": "AAA",
            "opportunity_score": 40,
            "composite_score": 30,
            "direction": "bullish",
            "rank": 1,
            "signals": {"_reversal_buildup": {
                "phase": "early_turn", "side": "long", "score": 62.5,
                "reasons": ["macd_hist_rising_neg"],
            }},
        }, run_id=9, preset="momentum_hunter")
        summary = format_screening_context(packet)
        self.assertIn("Reversal: long / early_turn", summary)
        self.assertIn("macd_hist_rising_neg", summary)

    def test_scan_all_excludes_reversal_before_cluster(self):
        import webapp.app as appmod

        class _DB:
            def __init__(self):
                self.runs = [
                    {
                        "id": 3, "watchlist_id": 1, "watchlist_name": "Core",
                        "run_at": "2026-07-20T12:10:00", "strategy": "reversal_buildup",
                        "criteria": '{"strategy":"reversal_buildup"}',
                    },
                    {
                        "id": 2, "watchlist_id": 2, "watchlist_name": "Growth",
                        "run_at": "2026-07-20T12:05:00",
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist"}',
                    },
                    {
                        "id": 1, "watchlist_id": 1, "watchlist_name": "Core",
                        "run_at": "2026-07-20T12:00:00",
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist"}',
                    },
                ]
                row = {"ticker": "AAA", "composite_score": 70, "entry_quality": 60, "macro_fit": 60,
                       "direction": "bullish", "rank": 1, "signals": {}}
                self.results = {1: [dict(row)], 2: [dict(row, ticker="BBB")], 3: [dict(row, ticker="REV")]}

            def get_screening_runs(self, limit=50):
                return self.runs[:limit]

            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return _latest_runs_per_watchlist(self.runs, self.results, min_results)

            def get_screening_results(self, run_id):
                return deepcopy(self.results[run_id])

            def get_ticker_metadata_bulk(self, tickers):
                return {t: {"sector": "Technology", "market_cap_tier": "large", "avg_dollar_volume_usd": 10_000_000} for t in tickers}

            def get_upcoming_events(self, **_kwargs):
                return []

            def record_runtime_metric(self, *_args, **_kwargs):
                return 1

        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(appmod, "DEFAULT_CONFIG", cfg):
            data = appmod.get_scan_all_consolidated(top=5, bottom=2, db_path="test.db")
        tickers = {r["ticker"] for r in data["top_opportunities"]}
        self.assertNotIn("REV", tickers)
        self.assertNotEqual(data.get("lens"), "reversal")

    def test_screen_all_engine_criteria_reversal_stamps_job_id(self):
        import webapp.app as appmod

        meta = appmod._screen_all_engine_criteria("reversal_buildup", "union_buckets", "job-123")
        self.assertEqual(meta["strategy"], "reversal_buildup")
        self.assertEqual(meta["reversal_all_job_id"], "job-123")
        self.assertNotIn("screen_all_job_id", meta)
        self.assertNotEqual(meta["strategy"], "screen_all")
        self.assertEqual(
            appmod._screen_all_persist_strategy("reversal_buildup", "union_buckets"),
            "reversal_all_union",
        )
        self.assertEqual(
            appmod._screen_all_persist_strategy("reversal_buildup", "per_watchlist"),
            "reversal_buildup",
        )
        std = appmod._screen_all_engine_criteria("standard", "per_watchlist", "job-123")
        self.assertEqual(std["strategy"], "screen_all")
        self.assertNotIn("reversal_all_job_id", std)

    def test_reversal_latest_clusters_and_dedups_by_buildup(self):
        import webapp.app as appmod

        job = "rev-job-1"

        def _row(ticker, score, phase, side, composite, direction="bullish"):
            return {
                "ticker": ticker,
                "composite_score": composite,
                "entry_quality": 50,
                "macro_fit": 50,
                "direction": direction,
                "rank": 1,
                "signals": {
                    "_reversal_buildup": {
                        "phase": phase,
                        "side": side,
                        "score": score,
                        "reasons": [],
                        "features": {"days_since_rsi_extreme": 2},
                    }
                },
            }

        class _DB:
            def __init__(self):
                crit = json.dumps({
                    "strategy": "reversal_all_union",
                    "source_mode": "union_buckets",
                    "reversal_all_job_id": job,
                })
                self.runs = [
                    {
                        "id": 11, "watchlist_id": 1, "watchlist_name": "Core",
                        "run_at": "2026-08-14T15:10:00", "strategy": "reversal_all_union",
                        "criteria": crit,
                    },
                    {
                        "id": 10, "watchlist_id": 2, "watchlist_name": "Growth",
                        "run_at": "2026-08-14T15:10:00", "strategy": "reversal_all_union",
                        "criteria": crit,
                    },
                    {
                        "id": 9, "watchlist_id": 3, "watchlist_name": "Opp",
                        "run_at": "2026-08-14T15:00:00", "strategy": "screen_all",
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist"}',
                    },
                    {
                        "id": 8, "watchlist_id": 4, "watchlist_name": "Opp2",
                        "run_at": "2026-08-14T14:55:00", "strategy": "screen_all",
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist"}',
                    },
                ]
                self.results = {
                    11: [
                        _row("OPEN", 69.8, "confirmed", "long", 12.8),
                        _row("TMO", 40.0, "watching", "short", 44.1),
                        _row("LATE", 90.0, "late", "long", 10.0),
                    ],
                    10: [
                        _row("OPEN", 50.0, "confirmed", "long", 80.0),
                        _row("NUE", 69.8, "early_turn", "short", 8.0, direction="bullish"),
                    ],
                    9: [_row("TMO", 99.0, "confirmed", "short", 90.0)],
                    8: [_row("AAA", 10.0, "none", "none", 20.0)],
                }

            def get_screening_runs(self, limit=50):
                return self.runs[:limit]

            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return _latest_runs_per_watchlist(self.runs, self.results, min_results)

            def get_screening_results(self, run_id):
                return deepcopy(self.results[run_id])

            def get_ticker_metadata_bulk(self, tickers):
                return {
                    t: {"sector": "Technology", "market_cap_tier": "large", "avg_dollar_volume_usd": 10_000_000}
                    for t in tickers
                }

            def get_upcoming_events(self, **_kwargs):
                return []

            def record_runtime_metric(self, *_args, **_kwargs):
                return 1

        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(appmod, "DEFAULT_CONFIG", cfg):
            opp = appmod.get_scan_all_consolidated(top=10, bottom=2, strategy="standard", db_path="test.db")
            rev = appmod.get_scan_all_consolidated(top=10, bottom=2, strategy="reversal_buildup", db_path="test.db")

        self.assertNotIn("OPEN", {r["ticker"] for r in opp["top_opportunities"]})
        self.assertEqual(rev["lens"], "reversal")
        longs = [r["ticker"] for r in rev["long_setups"]]
        shorts = [r["ticker"] for r in rev["short_setups"]]
        self.assertIn("OPEN", longs)
        self.assertNotIn("LATE", longs)
        self.assertNotIn("TMO", shorts)
        self.assertIn("NUE", shorts)
        open_row = next(r for r in rev["long_setups"] if r["ticker"] == "OPEN")
        self.assertEqual(open_row["reversal_score"], 69.8)
        self.assertEqual(open_row["watchlist_name"], "Core")
        nue = next(r for r in rev["short_setups"] if r["ticker"] == "NUE")
        self.assertEqual(nue["reversal_side"], "short")
        self.assertEqual(nue["direction"], "bullish")

    def _rev_row(self, ticker, score, phase, side, composite=20.0, direction="bullish"):
        return {
            "ticker": ticker,
            "composite_score": composite,
            "entry_quality": 50,
            "macro_fit": 50,
            "direction": direction,
            "rank": 1,
            "signals": {
                "_reversal_buildup": {
                    "phase": phase,
                    "side": side,
                    "score": score,
                    "reasons": [],
                    "features": {"days_since_rsi_extreme": 2},
                }
            },
        }

    def test_one_reversal_single_only_is_404(self):
        import webapp.app as appmod
        from fastapi import HTTPException

        class _DB:
            def __init__(self):
                self.runs = [{
                    "id": 1329, "watchlist_id": 1, "watchlist_name": "My Watchlist",
                    "run_at": "2026-08-14T16:00:00", "strategy": "reversal_buildup",
                    "results_count": 1,
                    "criteria": '{"strategy":"reversal_buildup","source_mode":"single_watchlist"}',
                }]
                outer = TestPersistLiftAndReadPaths
                self.results = {1329: [outer._rev_row(outer, "OPEN", 69.8, "confirmed", "long")]}

            def get_screening_runs(self, limit=50):
                return self.runs[:limit]

            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return _latest_runs_per_watchlist(self.runs, self.results, min_results)

            def get_screening_results(self, run_id):
                return deepcopy(self.results[run_id])

            def get_ticker_metadata_bulk(self, tickers):
                return {t: {"sector": "Technology", "market_cap_tier": "large", "avg_dollar_volume_usd": 10_000_000} for t in tickers}

            def get_upcoming_events(self, **_kwargs):
                return []

            def record_runtime_metric(self, *_args, **_kwargs):
                return 1

        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(appmod, "DEFAULT_CONFIG", cfg):
            with self.assertRaises(HTTPException) as ctx:
                appmod.get_scan_all_consolidated(top=10, bottom=2, strategy="reversal_buildup", db_path="test.db")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_reversal_excludes_untagged_opportunity_run(self):
        import webapp.app as appmod
        from fastapi import HTTPException

        class _DB:
            def __init__(self):
                self.runs = [{
                    "id": 1430, "watchlist_id": 1, "watchlist_name": "My Watchlist",
                    "run_at": "2026-08-18T17:06:00", "results_count": 1,
                    "criteria": '{"source_mode":"single_watchlist"}',
                }]
                self.results = {1430: [{
                    "ticker": "PGY", "composite_score": 90, "entry_quality": 80,
                    "macro_fit": 70, "direction": "bullish", "rank": 1, "signals": {},
                }]}

            def get_screening_runs(self, limit=50):
                return self.runs[:limit]

            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return _latest_runs_per_watchlist(self.runs, self.results, min_results)

            def get_screening_results(self, run_id):
                return deepcopy(self.results[run_id])

            def get_ticker_metadata_bulk(self, tickers):
                return {t: {"sector": "Technology", "market_cap_tier": "large"} for t in tickers}

            def get_upcoming_events(self, **_kwargs):
                return []

            def record_runtime_metric(self, *_args, **_kwargs):
                return 1

        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(appmod, "DEFAULT_CONFIG", cfg):
            with self.assertRaises(HTTPException) as ctx:
                appmod.get_scan_all_consolidated(top=10, bottom=2, strategy="reversal_buildup", db_path="test.db")
            self.assertEqual(ctx.exception.status_code, 404)
            opp = appmod.get_scan_all_consolidated(top=10, bottom=2, strategy="standard", db_path="test.db")
        self.assertEqual(len(opp["watchlist_summaries"]), 1)
        self.assertEqual(opp["watchlist_summaries"][0]["source"], "single")

    def test_same_list_older_opportunity_and_newer_reversal_both_boards(self):
        import webapp.app as appmod

        class _DB:
            def __init__(self):
                self.runs = [
                    {
                        "id": 50, "watchlist_id": 1, "watchlist_name": "My Watchlist",
                        "run_at": "2026-08-18T18:00:00", "strategy": "reversal_buildup",
                        "results_count": 1,
                        "criteria": '{"strategy":"reversal_buildup","source_mode":"single_watchlist"}',
                    },
                    {
                        "id": 40, "watchlist_id": 1, "watchlist_name": "My Watchlist",
                        "run_at": "2026-08-14T12:00:00", "strategy": "screen_all",
                        "results_count": 1,
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","screen_all_job_id":"job-opp"}',
                    },
                ]
                self.results = {
                    50: [self_outer._rev_row("OPEN", 69.8, "confirmed", "long")],
                    40: [{
                        "ticker": "PGY", "composite_score": 88, "entry_quality": 80,
                        "macro_fit": 70, "direction": "bullish", "rank": 1, "signals": {},
                    }],
                }

            def get_screening_runs(self, limit=50):
                return self.runs[:limit]

            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return _latest_runs_per_watchlist(self.runs, self.results, min_results)

            def get_screening_results(self, run_id):
                return deepcopy(self.results[run_id])

            def get_ticker_metadata_bulk(self, tickers):
                return {t: {"sector": "Technology", "market_cap_tier": "large", "avg_dollar_volume_usd": 10_000_000} for t in tickers}

            def get_upcoming_events(self, **_kwargs):
                return []

            def record_runtime_metric(self, *_args, **_kwargs):
                return 1

        self_outer = self
        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(appmod, "DEFAULT_CONFIG", cfg):
            opp = appmod.get_scan_all_consolidated(top=10, bottom=2, strategy="standard", db_path="test.db")
            rev = appmod.get_scan_all_consolidated(top=10, bottom=2, strategy="reversal_buildup", db_path="test.db")
        self.assertEqual(opp["watchlist_summaries"][0]["run_id"], 40)
        self.assertEqual({r["ticker"] for r in opp["top_opportunities"]}, {"PGY"})
        self.assertEqual(rev["watchlist_summaries"][0]["run_id"], 50)
        self.assertEqual([r["ticker"] for r in rev["long_setups"]], ["OPEN"])

    def test_reversal_scan_all_plus_later_single_is_overlay(self):
        import webapp.app as appmod

        job = "rev-job-2"

        class _DB:
            def __init__(self):
                crit = json.dumps({
                    "strategy": "reversal_all_union",
                    "source_mode": "union_buckets",
                    "reversal_all_job_id": job,
                    "screen_all_job_id": job,
                })
                self.runs = [
                    {
                        "id": 21, "watchlist_id": 1, "watchlist_name": "Core",
                        "run_at": "2026-08-16T12:00:00", "strategy": "reversal_buildup",
                        "results_count": 1,
                        "criteria": '{"strategy":"reversal_buildup","source_mode":"single_watchlist"}',
                    },
                    {
                        "id": 11, "watchlist_id": 1, "watchlist_name": "Core",
                        "run_at": "2026-08-14T15:10:00", "strategy": "reversal_all_union",
                        "results_count": 1, "criteria": crit,
                    },
                    {
                        "id": 10, "watchlist_id": 2, "watchlist_name": "Growth",
                        "run_at": "2026-08-14T15:10:00", "strategy": "reversal_all_union",
                        "results_count": 1, "criteria": crit,
                    },
                ]
                self.results = {
                    21: [self_outer._rev_row("OPEN", 80.0, "confirmed", "long")],
                    11: [self_outer._rev_row("TMO", 50.0, "early_turn", "short")],
                    10: [self_outer._rev_row("NUE", 69.8, "early_turn", "short")],
                }

            def get_screening_runs(self, limit=50):
                return self.runs[:limit]

            def get_latest_screening_runs_per_watchlist(self, min_results=1):
                return _latest_runs_per_watchlist(self.runs, self.results, min_results)

            def get_screening_results(self, run_id):
                return deepcopy(self.results[run_id])

            def get_ticker_metadata_bulk(self, tickers):
                return {t: {"sector": "Technology", "market_cap_tier": "large", "avg_dollar_volume_usd": 10_000_000} for t in tickers}

            def get_upcoming_events(self, **_kwargs):
                return []

            def record_runtime_metric(self, *_args, **_kwargs):
                return 1

        self_outer = self
        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(appmod, "DEFAULT_CONFIG", cfg):
            rev = appmod.get_scan_all_consolidated(top=10, bottom=2, strategy="reversal_buildup", db_path="test.db")
        by_wl = {s["watchlist_id"]: s for s in rev["watchlist_summaries"]}
        self.assertEqual(by_wl[1]["source"], "overlay")
        self.assertEqual(by_wl[1]["run_id"], 21)
        self.assertEqual(by_wl[2]["source"], "scan_all")
        self.assertEqual(len(rev["single_run_overlays"]), 1)
        self.assertEqual(rev["source_mode"], "mixed")
        self.assertIsNone(rev.get("universe_scanned"))


if __name__ == "__main__":
    unittest.main()
