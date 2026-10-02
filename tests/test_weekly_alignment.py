"""Tests for shared weekly alignment transforms."""
import unittest

from tradingagents.screening.weekly_alignment import (
    score_weekly_alignment,
    weekly_trend_label,
)


FIXTURE_WEEKLY = {
    "weekly_close": 100.0,
    "weekly_rsi": 55.0,
    "confluence_score": 0.7,
}

FIXTURE_NEUTRAL = {
    "weekly_close": 100.0,
    "confluence_score": 0.5,
}

FIXTURE_BEARISH = {
    "weekly_close": 100.0,
    "confluence_score": 0.2,
}


class TestWeeklyAlignment(unittest.TestCase):
    def test_screening_score_in_zero_one(self):
        self.assertAlmostEqual(score_weekly_alignment(FIXTURE_WEEKLY), 0.7)
        self.assertEqual(score_weekly_alignment(None), 0.0)
        self.assertEqual(score_weekly_alignment({}), 0.0)

    def test_aggregator_signed_from_same_fixture(self):
        label = weekly_trend_label(FIXTURE_WEEKLY)
        self.assertGreater(label["score"], 0.0)
        self.assertEqual(label["label"], "BULLISH")

    def test_neutral_confluence(self):
        label = weekly_trend_label(FIXTURE_NEUTRAL)
        self.assertEqual(label["label"], "NEUTRAL")
        self.assertAlmostEqual(label["score"], 0.0, places=2)

    def test_bearish_confluence(self):
        label = weekly_trend_label(FIXTURE_BEARISH)
        self.assertLess(label["score"], 0.0)
        self.assertEqual(label["label"], "BEARISH")

    def test_dual_transform_same_input(self):
        screening = score_weekly_alignment(FIXTURE_WEEKLY)
        agg = weekly_trend_label(FIXTURE_WEEKLY)
        self.assertGreaterEqual(screening, 0.0)
        self.assertLessEqual(screening, 1.0)
        self.assertGreaterEqual(agg["score"], -1.0)
        self.assertLessEqual(agg["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
