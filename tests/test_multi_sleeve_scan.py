"""One-pass multi_sleeve screening (synthetic OHLCV)."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.screening.engine import ScreeningEngine
from tradingagents.screening.multi_sleeve import attach_multi_sleeve_meta


class _FakeDB:
    last_run_id = 42
    saved_criteria = None
    saved_results = None

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
         patch.object(engine, "_fetch_enhanced_data") as mock_enh, \
         patch.object(engine, "finalize_early_momentum", side_effect=lambda results, **kw: results):
        results = engine.scan(
            tickers=tickers,
            date="2026-07-20",
            preset="momentum_hunter",
            watchlist_id=1,
            enable_enhanced=True,
            **scan_kwargs,
        )
    return engine, db, results, mock_enh


def _synthetic_batch(n: int = 80):
    import pandas as pd

    def _series(values):
        idx = pd.bdate_range("2025-01-02", periods=len(values))
        return pd.Series([float(v) for v in values], index=idx)

    closes = []
    px = 50.0
    box = 14
    for i in range(n):
        if i < n - box:
            px += 0.45
        else:
            anchor = closes[n - box - 1] if closes else px
            px = anchor + (0.12 if i % 2 == 0 else -0.08)
        closes.append(px)
    close = _series(closes)
    high = close + 0.4
    low = close - 0.4
    volume = _series([2_000_000.0] * n)
    spy = _series([100.0 + 0.05 * i for i in range(n)])
    return {"close": close, "high": high, "low": low, "volume": volume, "spy_close": spy, "info": {}}


class TestMultiSleeveScan(unittest.TestCase):
    def test_scan_embeds_four_sleeves_and_meta(self):
        batch = {"AAA": _synthetic_batch()}
        _, db, results, mock_enh = _engine_scan(
            ["AAA"],
            batch,
            criteria_meta={"strategy": "multi_sleeve", "run_enrich": False},
        )
        mock_enh.assert_called()
        self.assertEqual(db.saved_criteria.get("strategy"), "multi_sleeve")
        self.assertEqual(len(results), 1)
        for r in results:
            sig = r.signals or {}
            self.assertIn("_reversal_buildup", sig)
            self.assertIn("_early_momentum", sig)
            self.assertIn("_base_coil", sig)
            self.assertIn("_multi_sleeve", sig)
            meta = sig["_multi_sleeve"]
            self.assertIn("lead_sleeve", meta)
            self.assertIn("lead_percentile", meta)

    def test_direction_conflict_flag(self):
        from tradingagents.screening.engine import ScreeningResult

        r = ScreeningResult(
            ticker="X",
            composite_score=70.0,
            direction="bullish",
            signals={
                "_reversal_buildup": {"phase": "confirmed", "score": 80.0, "side": "short"},
                "_early_momentum": {"passed_gates": True, "score_final": 60.0},
                "_base_coil": {"on_board": False, "score": 10.0},
            },
            signal_deltas={},
        )
        attach_multi_sleeve_meta([r])
        meta = (r.signals or {})["_multi_sleeve"]
        self.assertTrue(meta.get("direction_conflict"))
        self.assertEqual(meta.get("lead_sleeve"), "reversal")
        self.assertEqual(meta.get("lead_score"), 80.0)
        self.assertEqual(meta.get("lead_phase"), "confirmed")
        self.assertEqual(meta.get("lead_side"), "short")

    def test_watching_reversal_does_not_conflict(self):
        from tradingagents.screening.engine import ScreeningResult

        bull = ScreeningResult(
            ticker="NVT",
            composite_score=42.0,
            direction="bullish",
            signals={
                "_reversal_buildup": {"phase": "watching", "score": 18.0, "side": "short"},
                "_early_momentum": {"passed_gates": True, "score_final": 26.0, "bucket": "below_watch"},
                "_base_coil": {"on_board": True, "score": 36.6},
            },
            signal_deltas={},
        )
        peer = ScreeningResult(
            ticker="HYLN",
            composite_score=62.0,
            direction="bullish",
            signals={
                "_reversal_buildup": {"phase": "none", "score": 0, "side": "long"},
                "_early_momentum": {"passed_gates": True, "score_final": 10.0, "bucket": "below_watch"},
                "_base_coil": {"on_board": False},
            },
            signal_deltas={},
        )
        attach_multi_sleeve_meta([bull, peer])
        meta = (bull.signals or {})["_multi_sleeve"]
        self.assertFalse(meta.get("direction_conflict"))
        self.assertTrue(meta.get("base_only"))
        self.assertIsNone(meta.get("percentile_base"))
        self.assertNotEqual(meta.get("lead_sleeve"), "base")
        self.assertIsNone(meta.get("percentile_momentum"))

    def test_rank_recomputes_stale_meta(self):
        from tradingagents.screening.multi_sleeve import rank_multi_sleeve_rows

        rows = [
            {
                "ticker": "NVT",
                "composite_score": 42.0,
                "entry_quality": 50.0,
                "direction": "bullish",
                "signals": {
                    "_multi_sleeve": {"lead_sleeve": "base", "lead_percentile": 100.0},
                    "_base_coil": {"on_board": True, "score": 36.6},
                    "_early_momentum": {"passed_gates": True, "score_final": 20.0, "bucket": "below_watch"},
                    "_reversal_buildup": {"phase": "watching", "score": 18.0, "side": "short"},
                },
            },
            {
                "ticker": "HYLN",
                "composite_score": 62.0,
                "entry_quality": 65.0,
                "direction": "bullish",
                "signals": {
                    "_multi_sleeve": {"lead_sleeve": "opportunity", "lead_percentile": 80.0},
                    "_base_coil": {"on_board": False},
                    "_early_momentum": {"passed_gates": True, "score_final": 10.0, "bucket": "below_watch"},
                    "_reversal_buildup": {"phase": "none", "score": 0, "side": "long"},
                },
            },
        ]
        ranked = rank_multi_sleeve_rows(rows)
        self.assertEqual(ranked[0]["ticker"], "HYLN")
        self.assertEqual(ranked[0]["lead_sleeve"], "opportunity")
        self.assertEqual(ranked[1]["lead_sleeve"], "")
        self.assertFalse(ranked[1]["direction_conflict"])

    def test_late_chase_copied_from_momentum(self):
        from tradingagents.screening.engine import ScreeningResult

        r = ScreeningResult(
            ticker="Y",
            composite_score=40.0,
            direction="bullish",
            signals={
                "_reversal_buildup": {"phase": "none", "score": 10.0, "side": "long"},
                "_early_momentum": {"passed_gates": True, "score_final": 72.0, "bucket": "confirmed", "late_chase": True},
                "_base_coil": {"on_board": False},
            },
            signal_deltas={},
        )
        attach_multi_sleeve_meta([r])
        meta = (r.signals or {})["_multi_sleeve"]
        self.assertTrue(meta.get("late_chase"))
        self.assertEqual(meta.get("lead_sleeve"), "momentum")
        self.assertEqual(meta.get("lead_score"), 72.0)
        self.assertEqual(meta.get("lead_bucket"), "confirmed")

    def test_desk_bars_gate_the_lead(self):
        from tradingagents.screening.engine import ScreeningResult

        watch = ScreeningResult(
            ticker="PANW",
            composite_score=37.0,
            entry_quality=50.0,
            direction="bullish",
            signals={
                "_early_momentum": {"passed_gates": True, "score_final": 52.5, "bucket": "watch"},
                "_reversal_buildup": {"phase": "late", "score": 18.0, "side": "long"},
                "_base_coil": {"on_board": False},
            },
            signal_deltas={},
        )
        fade = ScreeningResult(
            ticker="FTNT",
            composite_score=45.0,
            entry_quality=50.0,
            direction="neutral",
            signals={
                "_early_momentum": {"passed_gates": True, "score_final": 36.0, "bucket": "below_watch"},
                "_reversal_buildup": {"phase": "confirmed", "score": 81.7, "side": "short"},
                "_base_coil": {"on_board": False},
            },
            signal_deltas={},
        )
        actionable = ScreeningResult(
            ticker="HYLN",
            composite_score=62.0,
            entry_quality=65.0,
            direction="bullish",
            signals={
                "_early_momentum": {"passed_gates": False, "score_final": 53.0, "bucket": "watch"},
                "_reversal_buildup": {"phase": "none", "score": 0, "side": "long"},
                "_base_coil": {"on_board": False},
            },
            signal_deltas={},
        )
        attach_multi_sleeve_meta([watch, fade, actionable])
        by = {r.ticker: (r.signals or {})["_multi_sleeve"] for r in (watch, fade, actionable)}
        self.assertEqual(by["PANW"]["lead_sleeve"], "")
        self.assertEqual(by["FTNT"]["lead_sleeve"], "reversal")
        self.assertEqual(by["FTNT"]["lead_side"], "short")
        self.assertIsNone(by["FTNT"]["percentile_reversal"])
        self.assertEqual(by["HYLN"]["lead_sleeve"], "opportunity")
        self.assertIsNone(by["HYLN"]["percentile_opportunity"])

    def test_specialized_desk_keeps_the_badge(self):
        from tradingagents.screening.engine import ScreeningResult
        from tradingagents.screening.multi_sleeve import rank_multi_sleeve_rows

        both = ScreeningResult(
            ticker="ENPH",
            composite_score=80.0,
            entry_quality=70.0,
            direction="bullish",
            signals={
                "rsi_overbought": 0.0,
                "price_vs_target": 1.0,
                "_early_momentum": {"passed_gates": True, "score_final": 40.0, "bucket": "below_watch"},
                "_reversal_buildup": {"phase": "early_turn", "score": 52.0, "side": "long"},
                "_base_coil": {"on_board": False},
            },
            signal_deltas={},
        )
        opp_only = ScreeningResult(
            ticker="ANF",
            composite_score=90.0,
            entry_quality=78.0,
            direction="bullish",
            signals={
                "rsi_overbought": 0.0,
                "price_vs_target": 0.5,
                "valuation_gap": 0.4,
                "_early_momentum": {"passed_gates": True, "score_final": 30.0, "bucket": "below_watch"},
                "_reversal_buildup": {"phase": "late", "score": 20.0, "side": "long"},
                "_base_coil": {"on_board": False},
            },
            signal_deltas={},
        )
        attach_multi_sleeve_meta([both, opp_only])
        self.assertEqual(both.signals["_multi_sleeve"]["lead_sleeve"], "reversal")
        self.assertEqual(both.signals["_multi_sleeve"]["location"], "pullback")
        also = [c["sleeve"] for c in both.signals["_multi_sleeve"]["cleared"]]
        self.assertEqual(also, ["reversal", "opportunity"])
        self.assertEqual(opp_only.signals["_multi_sleeve"]["lead_sleeve"], "opportunity")
        self.assertEqual(opp_only.signals["_multi_sleeve"]["location"], "extended")

        ranked = rank_multi_sleeve_rows([
            {"ticker": "ANF", "composite_score": 90.0, "entry_quality": 78.0, "direction": "bullish", "signals": opp_only.signals},
            {"ticker": "ENPH", "composite_score": 80.0, "entry_quality": 70.0, "direction": "bullish", "signals": both.signals},
        ])
        self.assertEqual([r["ticker"] for r in ranked], ["ENPH", "ANF"])


class TestMultiSleeveApiLift(unittest.TestCase):
    def test_lift_and_rank(self):
        from tradingagents.screening.multi_sleeve import lift_multi_sleeve_fields, rank_multi_sleeve_rows

        rows = [
            {
                "ticker": "B",
                "signals": {
                    "_multi_sleeve": {
                        "lead_sleeve": "momentum",
                        "lead_percentile": 50.0,
                    }
                },
            },
            {
                "ticker": "A",
                "signals": {
                    "_multi_sleeve": {
                        "lead_sleeve": "reversal",
                        "lead_percentile": 90.0,
                    }
                },
            },
        ]
        ranked = rank_multi_sleeve_rows(rows)
        self.assertEqual(ranked[0]["ticker"], "A")
        self.assertIsNone(ranked[0].get("lead_percentile"))


if __name__ == "__main__":
    unittest.main()
