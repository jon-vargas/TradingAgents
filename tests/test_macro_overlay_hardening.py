"""Macro overlay hardening — resilient context and per-ticker scoring."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tradingagents.screening.engine import ScreeningEngine
from tradingagents.screening.macro_overlay import get_macro_overlay_context


class TestMacroOverlayContext(unittest.TestCase):
    @patch("tradingagents.screening.macro_overlay.compute_sector_momentum")
    @patch("tradingagents.screening.macro_overlay.compute_market_environment_score")
    @patch("tradingagents.dataflows.yfinance_extended.get_macro_snapshot")
    def test_never_raises_on_fetch_failure(self, mock_snap, mock_env, mock_sector):
        mock_snap.side_effect = RuntimeError("yfinance breaker open")
        mock_env.side_effect = RuntimeError("should not be called")
        mock_sector.side_effect = RuntimeError("sector fetch failed")

        macro, market_env, sector = get_macro_overlay_context()

        self.assertIsInstance(macro, dict)
        self.assertIn("score", market_env)
        self.assertIn("sector_scores", sector)


class TestComputeMacroOverlay(unittest.TestCase):
    def test_scores_all_tickers_with_baseline_fallback(self):
        engine = ScreeningEngine.__new__(ScreeningEngine)
        tier1 = {"AAA": {}, "BBB": {}}
        meta = {"AAA": {"resolved_profile": "high_growth", "sector": "Technology"}}

        fake_ctx = (
            {"market_regime": "bull", "indices": {}},
            {"score": 75.0, "breakdown": {}},
            {"sector_scores": {"Technology": 80}, "sector_ranks": {"Technology": 2},
             "etf_returns_20d": {}, "etf_returns_60d": {}, "iwm_vs_spy_20d": 0.0},
        )

        with patch(
            "tradingagents.screening.macro_overlay.get_macro_overlay_context",
            return_value=fake_ctx,
        ), patch(
            "tradingagents.screening.macro_overlay.compute_ticker_macro_overlay",
            side_effect=[{"macro_fit": 79.0, "macro_breakdown": {}}, RuntimeError("boom")],
        ):
            out = engine._compute_macro_overlay(tier1, meta)

        self.assertEqual(len(out), 2)
        self.assertEqual(out["AAA"]["macro_fit"], 79.0)
        self.assertIsNotNone(out["BBB"]["macro_fit"])


if __name__ == "__main__":
    unittest.main()
