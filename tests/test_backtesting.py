"""Unit tests for backtesting engine."""
import unittest
from datetime import datetime
from types import SimpleNamespace


class TestInferCorrectness(unittest.TestCase):
    def test_buy_positive_return(self):
        from tradingagents.backtesting.engine import _infer_correctness
        self.assertTrue(_infer_correctness("BUY", 0.05))

    def test_buy_negative_return(self):
        from tradingagents.backtesting.engine import _infer_correctness
        self.assertFalse(_infer_correctness("BUY", -0.03))

    def test_sell_negative_return(self):
        from tradingagents.backtesting.engine import _infer_correctness
        self.assertTrue(_infer_correctness("SELL", -0.05))

    def test_sell_positive_return(self):
        from tradingagents.backtesting.engine import _infer_correctness
        self.assertFalse(_infer_correctness("SELL", 0.03))

    def test_hold_small_move_correct(self):
        from tradingagents.backtesting.engine import _infer_correctness
        self.assertTrue(_infer_correctness("HOLD", 0.01, lookahead_days=30))

    def test_hold_large_move_incorrect(self):
        from tradingagents.backtesting.engine import _infer_correctness
        self.assertFalse(_infer_correctness("HOLD", 0.10, lookahead_days=30))

    def test_hold_none_return(self):
        from tradingagents.backtesting.engine import _infer_correctness
        self.assertIsNone(_infer_correctness("HOLD", None))

    def test_hold_avoid_is_not_buy(self):
        from tradingagents.backtesting.engine import _infer_correctness
        self.assertTrue(_infer_correctness("HOLD (Avoid)", 0.01, lookahead_days=7))
        self.assertFalse(_infer_correctness("HOLD (Avoid)", 0.05, lookahead_days=7))


class TestFindIndexByCalendarDate(unittest.TestCase):
    def test_exact_match(self):
        from tradingagents.backtesting.engine import _find_index_by_calendar_date
        series = [
            {"Date": "2026-01-05", "Close": "100"},
            {"Date": "2026-01-06", "Close": "101"},
            {"Date": "2026-01-07", "Close": "102"},
        ]
        idx = _find_index_by_calendar_date(series, datetime(2026, 1, 6))
        self.assertEqual(idx, 1)

    def test_weekend_rolls_forward(self):
        from tradingagents.backtesting.engine import _find_index_by_calendar_date
        series = [
            {"Date": "2026-01-05", "Close": "100"},
            {"Date": "2026-01-06", "Close": "101"},
            {"Date": "2026-01-09", "Close": "102"},
        ]
        idx = _find_index_by_calendar_date(series, datetime(2026, 1, 7))
        self.assertEqual(idx, 2, "Saturday should roll forward to Monday")

    def test_no_match_returns_none(self):
        from tradingagents.backtesting.engine import _find_index_by_calendar_date
        series = [{"Date": "2026-01-05", "Close": "100"}]
        idx = _find_index_by_calendar_date(series, datetime(2026, 2, 1))
        self.assertIsNone(idx)


class TestMetricsNormalizeAndSigned(unittest.TestCase):
    def test_hold_before_buy(self):
        from tradingagents.backtesting.metrics import normalize_decision
        self.assertEqual(normalize_decision("HOLD (Avoid)"), "HOLD")
        self.assertEqual(normalize_decision("SELL (Reduce)"), "SELL")
        self.assertEqual(normalize_decision("BUY"), "BUY")
        self.assertEqual(normalize_decision("**HOLD**"), "HOLD")

    def test_signed_pnl_and_hold_excluded_mean(self):
        from tradingagents.backtesting.metrics import signed_return, summarize_analyses
        self.assertAlmostEqual(signed_return("BUY", 0.05), 0.05)
        self.assertAlmostEqual(signed_return("SELL", 0.05), -0.05)
        self.assertIsNone(signed_return("HOLD", 0.05))

        rows = [
            SimpleNamespace(decision="BUY", actual_return_7d=0.05, alpha_7d=0.02, was_correct=True),
            SimpleNamespace(decision="SELL", actual_return_7d=0.05, alpha_7d=0.01, was_correct=False),
            SimpleNamespace(decision="HOLD", actual_return_7d=0.05, alpha_7d=0.03, was_correct=True),
        ]
        book = summarize_analyses(rows)
        self.assertAlmostEqual(book["avg_signed_return_7d"], 0.0)
        self.assertAlmostEqual(book["avg_tape_return_7d"], 0.05)
        self.assertNotAlmostEqual(book["avg_signed_return_7d"], book["avg_tape_return_7d"] * 2 / 3)

    def test_alpha_identity_signs_stored_alpha(self):
        from tradingagents.backtesting.metrics import signed_return
        r_stock, r_spy = 0.04, 0.01
        stored_alpha = r_stock - r_spy
        self.assertAlmostEqual(signed_return("BUY", stored_alpha), stored_alpha)
        self.assertAlmostEqual(signed_return("SELL", stored_alpha), -stored_alpha)

    def test_zero_return_is_not_a_directional_hit(self):
        from tradingagents.backtesting.metrics import horizon_correct
        self.assertFalse(horizon_correct("BUY", 0.0, 7))
        self.assertFalse(horizon_correct("SELL", 0.0, 7))

    def test_seven_day_accuracy_differs_from_mixed_was_correct(self):
        from tradingagents.backtesting.metrics import summarize_analyses
        rows = [
            SimpleNamespace(
                decision="BUY",
                actual_return_7d=-0.01,
                actual_return_30d=0.08,
                was_correct=True,
            ),
            SimpleNamespace(
                decision="SELL",
                actual_return_7d=-0.02,
                actual_return_30d=-0.02,
                was_correct=True,
            ),
        ]
        book = summarize_analyses(rows)
        self.assertEqual(book["directional_hits_7d"], 1)
        self.assertEqual(book["directional_n_7d"], 2)
        self.assertAlmostEqual(book["directional_accuracy_7d"], 0.5)
        self.assertAlmostEqual(book["mixed_horizon_accuracy"], 1.0)

    def test_n7_not_equal_n30_when_30d_missing(self):
        from tradingagents.backtesting.metrics import summarize_analyses
        rows = [
            SimpleNamespace(decision="BUY", actual_return_7d=0.01, actual_return_30d=0.02),
            SimpleNamespace(decision="BUY", actual_return_7d=0.01, actual_return_30d=None),
        ]
        book = summarize_analyses(rows)
        self.assertEqual(book["n_7d"], 2)
        self.assertEqual(book["n_30d"], 1)

    def test_return_vol_requires_n_10_and_signed_7d_only(self):
        from tradingagents.backtesting.metrics import return_vol, summarize_analyses
        self.assertIsNone(return_vol([0.01] * 9))
        self.assertIsNotNone(return_vol([0.01 + i * 0.001 for i in range(10)]))
        mixed = [
            SimpleNamespace(
                decision="BUY",
                actual_return_7d=0.01 * (i + 1),
                actual_return_30d=0.50,
            )
            for i in range(10)
        ]
        book = summarize_analyses(mixed)
        expected = return_vol([0.01 * (i + 1) for i in range(10)])
        thirty = return_vol([0.50 + i * 0.01 for i in range(10)])
        self.assertAlmostEqual(book["return_vol_7d"], expected)
        self.assertNotAlmostEqual(book["return_vol_7d"], thirty)

    def test_attribution_explicit_bull_independent_of_final_sell(self):
        from tradingagents.backtesting.metrics import compute_researcher_attribution
        rows = [
            SimpleNamespace(
                decision="SELL",
                actual_return_7d=0.05,
                bull_summary="Decision: BUY",
                bear_summary="",
                investment_decision="",
            )
        ]
        attr = compute_researcher_attribution(rows)
        self.assertEqual(attr["bull"]["wins"], 1)
        self.assertEqual(attr["bull"]["n"], 1)
        self.assertEqual(attr["bear"]["n"], 0)
        self.assertEqual(attr["bear"]["skipped"], 1)

    def test_attribution_skips_unparsed(self):
        from tradingagents.backtesting.metrics import compute_researcher_attribution
        rows = [
            SimpleNamespace(
                decision="BUY",
                actual_return_7d=0.05,
                bull_summary="lengthy debate with no explicit header",
                bear_summary="same",
                investment_decision="",
            )
        ]
        attr = compute_researcher_attribution(rows)
        self.assertEqual(attr["bull"]["n"], 0)
        self.assertEqual(attr["bull"]["skipped"], 1)

    def test_stats_payload_uses_summarize_analyses(self):
        from tradingagents.backtesting.metrics import stats_api_payload, summarize_analyses
        rows = [
            SimpleNamespace(decision="BUY", actual_return_7d=0.05, alpha_7d=0.01, was_correct=True),
            SimpleNamespace(decision="SELL", actual_return_7d=0.04, alpha_7d=0.01, was_correct=True),
        ]
        book = summarize_analyses(rows)
        payload = stats_api_payload(book)
        self.assertAlmostEqual(payload["avg_signed_return_7d"], round(book["avg_signed_return_7d"] * 100, 2))
        self.assertAlmostEqual(payload["avg_tape_return_7d"], round(book["avg_tape_return_7d"] * 100, 2))
        self.assertEqual(payload["directional_n_7d"], 2)
        self.assertEqual(payload["accuracy"], 50.0)
        self.assertEqual(payload["mixed_horizon_accuracy"], 100.0)
        self.assertNotEqual(payload["accuracy"], payload["mixed_horizon_accuracy"])


if __name__ == "__main__":
    unittest.main()
