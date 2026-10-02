"""Early Momentum lens tests — synthetic OHLCV only."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tradingagents.screening.early_momentum import (
    BUCKET_CONFIRMED,
    BUCKET_EVENT,
    BUCKET_HIGH_CONVICTION,
    check_gates,
    is_regular_session_open,
    lift_momentum_fields,
    rank_momentum_rows,
    rescore_with_enrich,
    score_ticker,
    truncate_price_series,
)
from tradingagents.screening.early_momentum_enrich import (
    _filter_form4_p_buys,
    _parse_pplx_flags,
    _runway_months_from_info,
    enrich_and_finalize_rows,
    select_enrich_tickers,
)


def _ohlcv(close_vals, volume=None, start="2025-06-01"):
    idx = pd.bdate_range(start, periods=len(close_vals))
    close = pd.Series(close_vals, index=idx, dtype=float)
    high = close + 0.8
    low = close - 0.8
    vol = volume if volume is not None else pd.Series(2_000_000.0, index=idx)
    return close, high, low, vol


class TestEarlyMomentumGates(unittest.TestCase):
    def test_gates_drop_cheap_illiquid(self):
        passed, flags = check_gates(price=2.0, meta={"market_cap": 100_000_000}, share_adv_50d=100_000, dollar_adv_50d=500_000)
        self.assertFalse(passed)
        self.assertIn("price_gate", flags["gate_reasons"])

    def test_gates_pass_liquid_name(self):
        passed, _ = check_gates(price=25.0, meta={"market_cap": 5_000_000_000}, share_adv_50d=2_000_000, dollar_adv_50d=50_000_000)
        self.assertTrue(passed)


class TestEarlyMomentumScoring(unittest.TestCase):
    def test_domain_shrink_rvol_breakout_same_day(self):
        n = 80
        close = np.linspace(40, 55, n)
        close[-1] = 58
        c, h, l, v = _ohlcv(close)
        v.iloc[-1] = v.iloc[-21:-1].mean() * 6
        pre = score_ticker(
            c, h, l, v,
            scan_date="2026-01-15",
            signals={"volume_surge": 1.0, "relative_strength": 0.5},
            meta={"market_cap": 5e9},
            phase="pre",
        )
        self.assertLess(pre["domains"]["A"], 25)

    def test_late_chase_penalty_on_rip(self):
        n = 80
        close = np.linspace(30, 50, n - 1)
        close = np.append(close, 58)
        c, h, l, v = _ohlcv(close)
        pre = score_ticker(c, h, l, v, scan_date="2026-01-15", signals={"volume_surge": 0.9}, phase="pre")
        self.assertLess(pre["score"], 100)

    def test_event_override_at_high_score(self):
        c, h, l, v = _ohlcv(np.linspace(40, 60, 80))
        final = score_ticker(
            c, h, l, v,
            scan_date="2026-01-15",
            signals={"volume_surge": 0.9, "relative_strength": 0.9, "estimate_momentum": 0.8, "weekly_trend_alignment": 0.7},
            enrich={"binary_event_within_days": 3, "_score_pre": 88, "form4_p_buy_count": 2, "verified_catalyst": True},
            meta={"market_cap": 5e9, "analyst_count": 5},
            phase="final",
        )
        self.assertEqual(final["event_flag"], BUCKET_EVENT)
        self.assertEqual(final["bucket"], BUCKET_EVENT)

    def test_going_concern_caps_score(self):
        c, h, l, v = _ohlcv(np.linspace(40, 70, 80))
        final = score_ticker(
            c, h, l, v,
            scan_date="2026-01-15",
            signals={"volume_surge": 1.0, "relative_strength": 1.0},
            enrich={"going_concern": True, "runway_months": 6, "_score_pre": 90},
            phase="final",
        )
        self.assertLessEqual(final["score"], 49)

    def test_missing_revisions_neutral_not_penalty(self):
        c, h, l, v = _ohlcv(np.linspace(40, 55, 80))
        pre = score_ticker(
            c, h, l, v,
            scan_date="2026-01-15",
            signals={"estimate_momentum": None},
            meta={"analyst_count": 1},
            phase="pre",
        )
        self.assertEqual(pre["domains"]["C"], 0.0)

    def test_ab_max_empty_cd_not_high_conviction(self):
        c, h, l, v = _ohlcv(np.linspace(40, 70, 80))
        final = score_ticker(
            c, h, l, v,
            scan_date="2026-01-15",
            signals={"volume_surge": 1.0, "relative_strength": 1.0, "weekly_trend_alignment": 0.8},
            enrich={"_score_pre": 85},
            meta={"analyst_count": 0},
            phase="final",
        )
        self.assertNotEqual(final["bucket"], BUCKET_HIGH_CONVICTION)

    def test_incomplete_session_skips_breakout(self):
        n = 80
        close = np.linspace(40, 55, n)
        close[-1] = 60
        c, h, l, v = _ohlcv(close)
        tz_now = datetime(2026, 1, 15, 14, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("tradingagents.screening.early_momentum.is_regular_session_open", return_value=True):
            pre = score_ticker(
                c, h, l, v,
                scan_date="2026-01-15",
                signals={"volume_surge": 0.8},
                now=tz_now,
                phase="pre",
            )
        self.assertTrue(pre.get("session_incomplete"))
        self.assertEqual(pre["domain_detail"]["A"].get("breakout_pts", 0), 0)

    def test_etf_skips_eps_hc_without_catalyst(self):
        c, h, l, v = _ohlcv(np.linspace(100, 120, 80))
        final = score_ticker(
            c, h, l, v,
            scan_date="2026-01-15",
            signals={"volume_surge": 1.0, "relative_strength": 1.0, "weekly_trend_alignment": 0.9},
            asset_class="etf",
            enrich={"_score_pre": 90},
            phase="final",
        )
        self.assertEqual(final["domains"]["C"], 0.0)
        if final["score"] >= 80:
            self.assertNotEqual(final["bucket"], BUCKET_HIGH_CONVICTION)


class TestEnrichHelpers(unittest.TestCase):
    def test_form4_p_code_filter(self):
        data = {
            "transactions": [
                {"transaction_code": "P", "text": "Purchase"},
                {"transaction_code": "S", "text": "Sale"},
                {"text": "RSU award tax withholding"},
            ]
        }
        self.assertEqual(_filter_form4_p_buys(data), 1)

    def test_select_enrich_includes_space_basket(self):
        rows = [
            {"ticker": "ZZZZ", "signals": {"_early_momentum": {"score_pre": 99, "passed_gates": True}}},
            {"ticker": "ASTS", "signals": {"_early_momentum": {"score_pre": 10, "passed_gates": True}}},
        ]
        picked = select_enrich_tickers(rows, top_n=1, space_basket=["ASTS"])
        self.assertIn("ASTS", picked)
        self.assertIn("ZZZZ", picked)


class TestLensHelpers(unittest.TestCase):
    def test_lift_and_rank_skips_gated_out(self):
        rows = [
            {"ticker": "AAA", "signals": {"_early_momentum": {"score_final": 80, "passed_gates": True}}},
            {"ticker": "BBB", "signals": {"_early_momentum": {"score_final": 90, "passed_gates": False}}},
        ]
        ranked = rank_momentum_rows(rows)
        tickers = [r["ticker"] for r in ranked]
        self.assertEqual(tickers, ["AAA"])
        lift = lift_momentum_fields(rows[0])
        self.assertEqual(lift["momentum_score"], 80)


class TestLensIsolation(unittest.TestCase):
    def test_momentum_excluded_from_opportunity_lens(self):
        import webapp.app as appmod

        opp = appmod._run_matches_lens({"strategy": "early_momentum_union"}, "opportunity")
        rev = appmod._run_matches_lens({"strategy": "early_momentum_union"}, "reversal")
        mom = appmod._run_matches_lens({"strategy": "early_momentum_union"}, "momentum")
        self.assertFalse(opp)
        self.assertFalse(rev)
        self.assertTrue(mom)

    def test_momentum_not_opportunity_scan_all_provenance(self):
        import webapp.app as appmod

        run = {
            "strategy": "early_momentum_union",
            "criteria": '{"source_mode":"union_buckets","early_momentum_all_job_id":"job-1"}',
        }
        self.assertFalse(appmod._is_scan_all_provenance_run(run))
        self.assertTrue(appmod._is_lens_scan_all_run(run, "momentum"))
        self.assertFalse(appmod._is_lens_scan_all_run(run, "opportunity"))

    def test_engine_criteria_omits_screen_all_job_id(self):
        import webapp.app as appmod

        meta = appmod._screen_all_engine_criteria("early_momentum", "union_buckets", "job-m")
        self.assertEqual(meta["early_momentum_all_job_id"], "job-m")
        self.assertEqual(meta["strategy"], "early_momentum_union")
        self.assertNotIn("screen_all_job_id", meta)
        per = appmod._screen_all_engine_criteria("early_momentum", "per_watchlist", "job-m")
        self.assertNotIn("screen_all_job_id", per)
        self.assertEqual(per["early_momentum_all_job_id"], "job-m")


class TestDomainBPeerRS(unittest.TestCase):
    def test_excess_vs_spy_not_absolute_return(self):
        n = 140
        close_vals = np.linspace(40, 56, n)
        c, h, l, v = _ohlcv(close_vals)
        spy_flat = pd.Series(np.full(n, 100.0), index=c.index)
        spy_match = pd.Series(np.linspace(100, 140, n), index=c.index)
        rip = score_ticker(c, h, l, v, scan_date="2026-01-15", spy_close=spy_flat, phase="pre")
        matched = score_ticker(c, h, l, v, scan_date="2026-01-15", spy_close=spy_match, phase="pre")
        self.assertGreater(rip["domains"]["B"], matched["domains"]["B"])

    def test_space_peer_beats_ufo_not_watchlist_rank(self):
        n = 140
        idx = pd.bdate_range("2025-01-01", periods=n)
        asts = pd.Series(np.linspace(20, 40, n), index=idx)
        ufo = pd.Series(np.linspace(30, 31, n), index=idx)
        peer = pd.Series(np.linspace(10, 11, n), index=idx)
        h = asts + 0.8
        l = asts - 0.8
        vol = pd.Series(2_000_000.0, index=idx)
        beat = score_ticker(
            asts, h, l, vol,
            scan_date="2026-01-15",
            ticker="ASTS",
            spy_close=ufo,
            iwm_close=ufo,
            ufo_close=ufo,
            peer_closes={"LUNR": peer, "RKLB": peer + 1},
            phase="pre",
        )
        lag_close = pd.Series(np.linspace(40, 18, n), index=idx)
        lag = score_ticker(
            lag_close, lag_close + 0.8, lag_close - 0.8, vol,
            scan_date="2026-01-15",
            ticker="ASTS",
            spy_close=ufo,
            iwm_close=ufo,
            ufo_close=ufo,
            peer_closes={"LUNR": peer, "RKLB": peer + 1},
            phase="pre",
        )
        self.assertTrue(beat["is_space_peer"])
        self.assertGreater(beat["domains"]["B"], lag["domains"]["B"])
        self.assertEqual(beat["peer_basket_version"], "v1")


class TestEnrichFinalizeRows(unittest.TestCase):
    def test_finalize_does_not_use_pandas_truthiness_on_benchmarks(self):
        c, h, l, v = _ohlcv(np.linspace(40, 55, 80))
        spy = pd.Series(np.linspace(400, 420, 80), index=c.index)
        pre = score_ticker(
            c, h, l, v,
            scan_date="2026-01-15",
            signals={"volume_surge": 0.8, "relative_strength": 0.5},
            meta={"market_cap": 5e9},
            spy_close=spy,
            phase="pre",
        )
        rows = [{
            "ticker": "TEST",
            "asset_class": "equity",
            "signals": {"_early_momentum": pre},
        }]
        batch_data = {
            "TEST": {"close": c, "high": h, "low": l, "volume": v, "spy_close": spy},
            "_benchmarks": {"spy_close": spy, "iwm_close": spy, "ufo_close": spy, "peer_closes": {}},
        }
        with patch("tradingagents.screening.early_momentum_enrich.enrich_ticker", return_value={"form4_p_buy_count": 0}):
            out = enrich_and_finalize_rows(
                rows,
                scan_date="2026-01-15",
                batch_data=batch_data,
                config={"early_momentum": {"enrich_top_n": 5, "enrich_top_n_cap": 5}},
            )
        self.assertEqual(len(out), 1)
        final = out[0]["early_momentum"]
        self.assertIsNotNone(final.get("score_final"))


class TestEnrichDF(unittest.TestCase):
    def test_two_p_code_cluster_scores_more_than_one(self):
        c, h, l, v = _ohlcv(np.linspace(40, 50, 80))
        one = score_ticker(c, h, l, v, scan_date="2026-01-15", enrich={"form4_p_buy_count": 1}, phase="final")
        two = score_ticker(c, h, l, v, scan_date="2026-01-15", enrich={"form4_p_buy_count": 2}, phase="final")
        self.assertGreater(two["domains"]["D"], one["domains"]["D"])
        self.assertTrue(two["domain_detail"]["D"].get("insider_cluster"))

    def test_etf_skips_domain_d(self):
        c, h, l, v = _ohlcv(np.linspace(40, 50, 80))
        final = score_ticker(
            c, h, l, v,
            scan_date="2026-01-15",
            asset_class="etf",
            enrich={"form4_p_buy_count": 4, "verified_catalyst": True},
            phase="final",
        )
        self.assertEqual(final["domains"]["D"], 0.0)
        self.assertEqual(final["domain_detail"]["D"].get("skipped"), "etf_commodity")

    def test_runway_and_s3_parsers(self):
        months = _runway_months_from_info({"totalCash": 60_000_000, "operatingCashflow": -120_000_000})
        self.assertAlmostEqual(months, 6.0, places=1)
        flags = _parse_pplx_flags("Filed an S-3 ATM offering; 8-K notes a new launch contract")
        self.assertTrue(flags["critical_dilution"])
        self.assertTrue(flags["atm_active"])
        self.assertTrue(flags["verified_catalyst"])

    def test_pplx_catalyst_rejects_generic_product_launch(self):
        flags = _parse_pplx_flags("Company announced a product launch at CES with strong marketing buzz")
        self.assertFalse(flags["verified_catalyst"])

    def test_bucket_confirmed_threshold_at_60(self):
        from tradingagents.screening.early_momentum import _assign_bucket, get_momentum_config
        cfg = get_momentum_config({"screening": {"early_momentum": {"bucket_confirmed_min": 60}}})
        self.assertEqual(_assign_bucket(61.0, cfg), "confirmed")
        self.assertEqual(_assign_bucket(59.9, cfg), "watch")


if __name__ == "__main__":
    unittest.main()
