import unittest

from tradingagents.agents.utils.technical_indicators_tools import normalize_indicator_args
from tradingagents.agents.utils.tool_context import reset_current_ticker, set_current_ticker
from tradingagents.reporting.decision_scorecard import (
    compute_decision_scorecard,
    evaluate_profile_gates,
    extract_lockup_adv,
    extract_satellite_ladder,
    render_decision_scorecard_html,
    resolve_effective_risk_limits,
    resolve_scorecard_limits,
)
from tradingagents.reporting.pdf_generator import (
    compute_data_quality_score,
    display_decision_from_state,
    extract_confidence_score,
)
from tradingagents.screening.context_packet import build_context_packet, format_screening_context


class IndicatorArgTests(unittest.TestCase):
    def test_recovers_ticker_when_both_args_are_atr(self):
        token = set_current_ticker("SPCX")
        try:
            symbol, indicator = normalize_indicator_args("atr", "atr")
            self.assertEqual(symbol, "SPCX")
            self.assertEqual(indicator, "atr")
        finally:
            reset_current_ticker(token)

    def test_swaps_when_symbol_is_indicator(self):
        symbol, indicator = normalize_indicator_args("rsi", "AAPL")
        self.assertEqual(symbol, "AAPL")
        self.assertEqual(indicator, "rsi")


class DecisionScorecardTests(unittest.TestCase):
    def _state(self):
        return {
            "company_of_interest": "SPCX",
            "risk_profile": "growth",
            "fundamentals_report": 'SIGNAL_JSON: {"section":"Fundamentals","stance":"bullish","confidence":0.8}',
            "investment_plan": "FINAL TRANSACTION PROPOSAL: **BUY**",
            "market_report": 'SIGNAL_JSON: {"section":"Market","stance":"neutral","confidence":0.5}\nVolume of 119.5 million shares.',
            "news_report": "A 319 million share lockup expired; 319 million shares became eligible.",
            "trader_investment_plan": (
                "FINAL TRANSACTION PROPOSAL: **BUY**\n"
                "Staged one-third entry now, add the second third on a close above $145."
            ),
            "final_trade_decision": (
                'DECISION_JSON: {"decision":"HOLD","position_action":"AVOID","conviction":"high","composite_signal":0.42}\n'
                "Beta: 3.50, Max Drawdown: -48.8%, VaR (95%): -9.04%."
            ),
            "risk_metrics": {"beta": 3.5, "max_drawdown_pct": -48.8, "var_95_pct": -9.04},
            "screening_context": {
                "run_id": 1441,
                "opp_score": 79.4,
                "composite": 39.8,
                "entry_quality": 70,
                "preset": "momentum_hunter",
            },
        }

    def test_pdf_display_prefers_final_rating(self):
        self.assertEqual(
            display_decision_from_state({"final_rating": "REVIEW"}, "BUY"),
            "REVIEW",
        )
        self.assertEqual(display_decision_from_state({}, "HOLD"), "HOLD")

    def test_final_rating_review_overrides_label(self):
        state = self._state()
        state["final_rating"] = "REVIEW"
        card = compute_decision_scorecard(state, "BUY", "SPCX", risk_profile="growth")
        self.assertEqual(card["decision_label"], "REVIEW")

    def test_quoted_rating_not_used_from_debate(self):
        from tradingagents.agents.rating import extract_rating_from_label

        text = (
            'DECISION_JSON: {"decision":"BUY"}\n'
            '> Bear: "Rating: SELL" is wrong.\n'
            "Rating: BUY\n"
        )
        self.assertEqual(extract_rating_from_label(text), "BUY")

    def test_profile_gates_fail_growth_limits(self):
        gates = evaluate_profile_gates(
            {"beta": 3.5, "max_drawdown_pct": 48.8, "var_95_pct": 9.04},
            "growth",
            gate_tolerance_pct=0.0,
        )
        self.assertTrue(all(g["pass"] is False for g in gates))

    def test_profile_gates_hairline_var_passes_with_tolerance(self):
        gates = evaluate_profile_gates(
            {"beta": None, "max_drawdown_pct": None, "var_95_pct": 3.04},
            "growth",
            gate_tolerance_pct=0.05,
        )
        var_gate = next(g for g in gates if g["label"] == "VaR 95%")
        self.assertTrue(var_gate["pass"])

    def test_commodity_etf_uses_commodity_limits_and_skips_beta(self):
        state = {
            "instrument_identity": {"quote_type": "ETF", "company_name": "SPDR Gold Shares"},
        }
        limits, skip_beta = resolve_scorecard_limits(state, "GLD", "growth")
        self.assertEqual(limits["var_95_pct"], 3.5)
        self.assertTrue(skip_beta)
        gates = evaluate_profile_gates(
            {"beta": 1.2, "max_drawdown_pct": 26.4, "var_95_pct": 3.04},
            "growth",
            limits=limits,
            skip_beta=True,
            gate_tolerance_pct=0.05,
        )
        dd = next(g for g in gates if g["label"] == "Max drawdown")
        var = next(g for g in gates if g["label"] == "VaR 95%")
        beta = next(g for g in gates if g["label"] == "Beta")
        self.assertIsNone(beta["pass"])
        self.assertTrue(dd["pass"])
        self.assertTrue(var["pass"])

    def test_only_explicit_or_watchlist_growth_profile_can_relax_limits(self):
        explicit = resolve_effective_risk_limits(
            "growth", "high_growth", "explicit"
        )
        auto = resolve_effective_risk_limits(
            "growth", "high_growth", "metadata_cache"
        )
        conservative = resolve_effective_risk_limits(
            "conservative", "high_growth", "explicit"
        )
        self.assertEqual(explicit["beta"], 2.2)
        self.assertEqual(auto["beta"], 1.8)
        self.assertEqual(conservative["beta"], 1.2)
        watchlist = resolve_effective_risk_limits(
            "growth", "high_growth", "watchlist"
        )
        self.assertEqual(watchlist["beta"], 2.2)

    def test_scorecard_hold_avoid_and_lockup(self):
        card = compute_decision_scorecard(self._state(), "HOLD", "SPCX", "growth")
        self.assertEqual(card["decision_label"], "HOLD (Avoid)")
        self.assertEqual(card["risk_budget"], "fail")
        lockup = extract_lockup_adv(self._state())
        self.assertAlmostEqual(lockup["ratio"], 319.0 / 119.5, places=1)
        html = render_decision_scorecard_html(card)
        self.assertIn("HOLD (Avoid)", html)
        self.assertIn("Profile limits", html)
        self.assertIn("Satellite spec", html)
        self.assertIn("run #1441", html)

    def test_satellite_ladder_only_when_core_is_hold(self):
        state = self._state()
        self.assertTrue(extract_satellite_ladder(state, "HOLD", "AVOID"))
        self.assertFalse(extract_satellite_ladder(state, "BUY", "FULL_BUY"))

    def test_confidence_haircut_for_research_buy_vs_hold(self):
        state = self._state()
        hold = extract_confidence_score(state, "HOLD")
        buy_state = dict(state)
        buy_state["final_trade_decision"] = (
            'DECISION_JSON: {"decision":"BUY","position_action":"FULL_BUY","conviction":"high","composite_signal":0.42}'
        )
        buy = extract_confidence_score(buy_state, "BUY")
        self.assertLess(hold, buy)
        self.assertLess(hold, 72)

    def test_confidence_haircut_for_research_sell_vs_hold(self):
        state = self._state()
        state["investment_plan"] = "FINAL TRANSACTION PROPOSAL: **SELL**"
        state["trader_investment_plan"] = "FINAL TRANSACTION PROPOSAL: **SELL**\nTrim the book."
        hold = extract_confidence_score(state, "HOLD")
        sell_state = dict(state)
        sell_state["final_trade_decision"] = (
            'DECISION_JSON: {"decision":"SELL","position_action":"REDUCE","conviction":"high"}'
        )
        sell = extract_confidence_score(sell_state, "SELL")
        self.assertLess(hold, sell)

    def test_avoid_exec_summary_uses_profile_limits_not_lockup(self):
        from tradingagents.reporting.pdf_generator import create_executive_summary

        state = self._state()
        state["news_report"] = "No lockup or secondary in the tape."
        state["market_report"] = 'SIGNAL_JSON: {"section":"Market","stance":"neutral","confidence":0.5}'
        text = create_executive_summary(state, "HOLD", "COHR", "2026-08-21")
        self.assertIn("HOLD (Avoid)", text)
        self.assertIn("profile limits", text.lower())
        self.assertNotIn("supply event", text.lower())

    def test_avoid_exec_summary_mentions_supply_when_lockup_ratio_ge_one(self):
        from tradingagents.reporting.pdf_generator import create_executive_summary

        state = self._state()
        state["risk_metrics"] = {"beta": 1.1, "max_drawdown_pct": 10.0, "var_95_pct": 1.5}
        text = create_executive_summary(state, "HOLD", "SPCX", "2026-08-21")
        self.assertIn("HOLD (Avoid)", text)
        self.assertIn("supply event", text.lower())

    def test_sell_reduce_exec_summary_uses_label_and_trim_language(self):
        from tradingagents.reporting.pdf_generator import create_executive_summary

        state = self._state()
        state["final_trade_decision"] = (
            'DECISION_JSON: {"decision":"SELL","position_action":"REDUCE","conviction":"medium"}'
        )
        state["investment_plan"] = "FINAL TRANSACTION PROPOSAL: **SELL**"
        state["market_report"] = "Current price $368.45"
        text = create_executive_summary(state, "SELL", "AVGO", "2026-08-23")
        self.assertIn("SELL (Reduce)", text)
        self.assertIn("Trim existing exposure", text)
        self.assertIn("do not initiate a new long", text.lower())
        self.assertNotIn("elevated risk profile with uncertain near-term catalysts", text)

    def test_data_quality_haircut_for_atr_args(self):
        clean = compute_data_quality_score(
            [
                {"method": "get_stock_data", "vendor": "yfinance", "status": "success"},
                {"method": "get_indicators", "vendor": "yfinance", "status": "success", "args": ["SPCX", "atr"]},
                {"method": "get_fundamentals", "vendor": "yfinance", "status": "success"},
                {"method": "get_news", "vendor": "finnhub", "status": "success"},
            ]
        )
        bad = compute_data_quality_score(
            [
                {"method": "get_stock_data", "vendor": "yfinance", "status": "success"},
                {"method": "get_indicators", "vendor": "yfinance", "status": "success", "args": ["atr", "atr"]},
                {"method": "get_fundamentals", "vendor": "yfinance", "status": "success"},
                {"method": "get_news", "vendor": "finnhub", "status": "success"},
            ]
        )
        self.assertLess(bad, clean)
        self.assertLess(bad, 100)

    def test_screening_packet_includes_ma_crossover_note(self):
        packet = build_context_packet(
            {
                "ticker": "SPCX",
                "opportunity_score": 79.4,
                "composite_score": 39.8,
                "entry_quality": 70,
                "signals": {"ma_crossover": 0.0, "_screening_meta": {"resolved_preset": "momentum_hunter"}},
            },
            run_id=1441,
            preset="default",
        )
        text = format_screening_context(packet)
        self.assertIn("overlay rank is not a buy ticket", text)
        self.assertIn("momentum_hunter", text)


if __name__ == "__main__":
    unittest.main()
