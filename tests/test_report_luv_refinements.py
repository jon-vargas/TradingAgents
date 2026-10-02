"""Guards for LUV-style report accuracy bugs."""
from __future__ import annotations

import unittest

from tradingagents.reporting.pdf_generator import (
    _eq_positive_flag,
    _plan_uses_short_geometry,
    extract_confidence_score,
    extract_trading_metrics,
)
from tradingagents.screening.discovery import normalize_dividend_yield
from tradingagents.screening.ticker_resolver import resolve_profile_and_preset


class ReportLuvRefinementTests(unittest.TestCase):
    def test_sell_reduce_uses_long_protective_geometry(self):
        self.assertFalse(_plan_uses_short_geometry("SELL", "REDUCE"))
        metrics = extract_trading_metrics(
            {
                "market_report": "LUV closed at $39.18. ATR: $1.25",
                "fundamentals_report": "",
            },
            "LUV",
            "2026-09-08",
            decision="SELL",
            position_action="REDUCE",
        )
        entry = float(metrics["entry"])
        stop = float(metrics["stop_loss"])
        target = float(metrics["target_1"])
        self.assertLess(stop, entry)
        self.assertGreater(target, entry)
        self.assertEqual(metrics["plan_kind"], "reduce")

    def test_explicit_short_keeps_inverted_geometry(self):
        self.assertTrue(_plan_uses_short_geometry("SHORT", "SHORT"))
        metrics = extract_trading_metrics(
            {
                "market_report": "Closed at $39.18. ATR: $1.25",
                "fundamentals_report": "",
            },
            "LUV",
            "2026-09-08",
            decision="SHORT",
            position_action="SHORT",
        )
        entry = float(metrics["entry"])
        self.assertGreater(float(metrics["stop_loss"]), entry)
        self.assertLess(float(metrics["target_1"]), entry)
        self.assertEqual(metrics["plan_kind"], "short")

    def test_eq_flags_accept_compute_keys(self):
        payload = {"has_positive_ocf": True, "has_positive_net_income": True}
        self.assertTrue(_eq_positive_flag(payload, "ocf"))
        self.assertTrue(_eq_positive_flag(payload, "ni"))
        self.assertFalse(_eq_positive_flag({}, "ocf"))

    def test_scenario_upside_key_is_upside_pct(self):
        from tradingagents.reporting.pdf_generator import generate_html_report
        from tradingagents.default_config import DEFAULT_CONFIG

        html = generate_html_report(
            state={
                "final_trade_decision": "HOLD",
                "market_report": "Close $39.18. ATR: 1.25",
                "fundamentals_report": "Revenue stable.",
                "news_report": "Quiet tape.",
                "sentiment_report": "Neutral.",
                "scenario_analysis": {
                    "blended_fair_value": 46.97,
                    "blended_upside_pct": 20.2,
                    "scenarios": {
                        "bull": {"price": 66.39, "eps": 6.31, "pe": 10.5, "upside_pct": 69.4},
                        "base": {"price": 47.07, "eps": 5.81, "pe": 8.1, "upside_pct": 20.1},
                        "bear": {"price": 27.34, "eps": 4.82, "pe": 5.7, "upside_pct": -30.2},
                    },
                },
                "data_provenance": [
                    {"method": "get_stock_data", "vendor": "yfinance", "status": "success"},
                    {"method": "get_indicators", "vendor": "yfinance", "status": "success"},
                ],
            },
            ticker="LUV",
            analysis_date="2026-09-08",
            decision="HOLD",
            config=DEFAULT_CONFIG,
            duration_seconds=10,
            output_path=None,
        )
        self.assertIn("69.4%", html)
        self.assertNotIn(">N/A%</td>", html.split("Scenario Analysis", 1)[-1].split("</table>", 1)[0])

    def test_normalize_dividend_yield_rejects_182(self):
        self.assertAlmostEqual(normalize_dividend_yield(0.018), 0.018)
        self.assertAlmostEqual(normalize_dividend_yield(1.8), 0.018)
        self.assertIsNone(normalize_dividend_yield(182))

    def test_airline_profile_not_income(self):
        profile, _ = resolve_profile_and_preset(
            "Industrials",
            "large",
            True,
            True,
            "high",
            industry="Airlines",
        )
        self.assertEqual(profile, "large_cap_core")

    def test_hold_override_to_sell_haircuts_confidence(self):
        hold_state = {
            "investment_plan": "FINAL TRANSACTION PROPOSAL: **HOLD**. Wait for the repair.",
            "trader_investment_plan": "Hold the book. No new risk.",
            "market_report": "Close $39.18.",
            "fundamentals_report": "Cash flow mixed.",
            "news_report": "Quiet tape.",
            "sentiment_report": "Neutral.",
        }
        aligned = dict(hold_state)
        aligned["investment_plan"] = "FINAL TRANSACTION PROPOSAL: **SELL**"
        aligned["trader_investment_plan"] = "FINAL TRANSACTION PROPOSAL: **SELL**. Reduce."
        overridden = extract_confidence_score(hold_state, "SELL")
        consensus = extract_confidence_score(aligned, "SELL")
        self.assertLess(overridden, consensus)
        self.assertLessEqual(overridden, consensus - 10)


if __name__ == "__main__":
    unittest.main()
