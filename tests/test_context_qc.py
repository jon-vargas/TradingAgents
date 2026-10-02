import unittest

from tradingagents.reporting.context_qc import (
    compute_context_warnings,
    detect_citations_thin,
    detect_risk_nuance_conflict,
    detect_street_consensus_conflict,
    detect_valuation_conflict,
    detect_valuation_unreliable,
)


class ContextQcTests(unittest.TestCase):
    def test_detect_citations_thin(self):
        state = {
            "market_report": "Price $28.23 on 2026-08-19",
            "fundamentals_report": "Revenue grew per yfinance data",
            "news_report": "Headline summary only",
            "sentiment_report": "Social tone improved",
        }
        self.assertTrue(detect_citations_thin(state))

    def test_yahoo_finance_and_human_dates_count_as_citations(self):
        from tradingagents.reporting.context_qc import has_citation

        self.assertTrue(has_citation("Data basis: Yahoo Finance through August 24, 2026."))
        self.assertTrue(has_citation("Close $51.95 on August 24, 2026."))
        self.assertFalse(has_citation("Narrative without a source or dated print."))

    def test_street_consensus_conflict_on_sell_vs_strong_buy(self):
        state = {
            "analyst_ratings": {
                "recommendation_key": "strong_buy",
                "upside_pct": 162.0,
                "number_of_analysts": 11,
            }
        }
        self.assertTrue(detect_street_consensus_conflict(state, "SELL"))
        self.assertFalse(detect_street_consensus_conflict(state, "HOLD"))

    def test_street_consensus_conflict_on_sell_vs_strong_buy_mid_forties_upside(self):
        state = {
            "analyst_ratings": {
                "recommendation_key": "strong_buy",
                "upside_pct": 42.84,
                "number_of_analysts": 46,
            }
        }
        self.assertTrue(detect_street_consensus_conflict(state, "SELL"))

    def test_street_consensus_conflict_on_sell_vs_buy_with_forty_upside(self):
        state = {
            "analyst_ratings": {
                "recommendation_key": "buy",
                "upside_pct": 40.0,
                "number_of_analysts": 12,
            }
        }
        self.assertTrue(detect_street_consensus_conflict(state, "SELL"))

    def test_street_consensus_no_conflict_on_sell_vs_buy_with_modest_upside(self):
        state = {
            "analyst_ratings": {
                "recommendation_key": "buy",
                "upside_pct": 18.0,
                "number_of_analysts": 12,
            }
        }
        self.assertFalse(detect_street_consensus_conflict(state, "SELL"))

    def test_street_consensus_conflict_on_sell_with_missing_rating_but_high_upside(self):
        state = {
            "analyst_ratings": {
                "recommendation_key": "none",
                "upside_pct": 59.63,
                "number_of_analysts": 6,
            }
        }
        self.assertTrue(detect_street_consensus_conflict(state, "SELL"))

    def test_valuation_conflict_on_sell_with_high_blended_upside(self):
        state = {
            "scenario_analysis": {
                "blended_upside_pct": 383.8,
            }
        }
        self.assertTrue(detect_valuation_conflict(state, "SELL"))
        self.assertFalse(detect_valuation_conflict(state, "HOLD"))

    def test_valuation_conflict_skipped_when_scenario_unreliable(self):
        state = {
            "scenario_analysis": {
                "blended_upside_pct": None,
                "blended_upside_pct_raw": 1171.1,
                "blended_upside_pct_capped": True,
                "valuation_reliability": "unreliable",
            }
        }
        self.assertTrue(detect_valuation_unreliable(state))
        self.assertFalse(detect_valuation_conflict(state, "SELL"))
        warnings = compute_context_warnings(state, "SELL")
        self.assertIn("valuation_unreliable", warnings)
        self.assertNotIn("valuation_conflict", warnings)

    def test_deep_transcript_missing_warning(self):
        state = {
            "analysis_mode": "deep",
            "company_of_interest": "SOFI",
            "earnings_transcript_snapshot": "",
        }
        warnings = compute_context_warnings(state, "HOLD")
        self.assertIn("deep_transcript_missing", warnings)

    def test_deep_transcript_not_required_for_commodity_etf(self):
        state = {
            "analysis_mode": "deep",
            "company_of_interest": "GLD",
            "instrument_identity": {"quote_type": "ETF", "company_name": "SPDR Gold Shares"},
            "earnings_transcript_snapshot": "",
        }
        warnings = compute_context_warnings(state, "HOLD")
        self.assertNotIn("deep_transcript_missing", warnings)

    def test_scenario_upside_cap_marks_unreliable(self):
        from tradingagents.dataflows.yfinance_extended import _finalize_scenario_valuation

        result = _finalize_scenario_valuation(
            {
                "valuation_method": "P/S (pre-profit fallback)",
                "blended_upside_pct": 1171.1,
            }
        )
        self.assertIsNone(result.get("blended_upside_pct"))
        self.assertEqual(result.get("valuation_reliability"), "unreliable")
        self.assertTrue(result.get("blended_upside_pct_capped"))

    def test_valuation_conflict_on_sell_with_mid_double_digit_blended_upside(self):
        state = {"scenario_analysis": {"blended_upside_pct": 84.3}}
        self.assertTrue(detect_valuation_conflict(state, "SELL"))
        self.assertFalse(detect_valuation_conflict({"scenario_analysis": {"blended_upside_pct": 60.0}}, "SELL"))

    def test_valuation_conflict_on_sell_with_high_dcf_margin_of_safety(self):
        state = {
            "scenario_analysis": {"blended_upside_pct": 12.0},
            "intrinsic_value": {"margin_of_safety_pct": 41.0},
        }
        self.assertTrue(detect_valuation_conflict(state, "SELL"))
        self.assertFalse(
            detect_valuation_conflict(
                {
                    "scenario_analysis": {"blended_upside_pct": 12.0},
                    "intrinsic_value": {"margin_of_safety_pct": 4.2},
                },
                "SELL",
            )
        )

    def test_risk_nuance_conflict_when_debate_says_reduce(self):
        state = {
            "final_trade_decision": (
                'DECISION_JSON: {"decision":"SELL","conviction":"high"}\nDecision: SELL'
            ),
            "risk_debate_state": {
                "neutral_history": (
                    "Neutral Analyst: Reduce substantially, retain only a small speculative stake."
                ),
            },
        }
        self.assertTrue(detect_risk_nuance_conflict(state, "SELL"))

    def test_risk_nuance_not_flagged_when_position_action_is_reduce(self):
        state = {
            "final_trade_decision": (
                'DECISION_JSON: {"decision":"SELL","position_action":"REDUCE"}\nDecision: SELL'
            ),
            "risk_debate_state": {
                "neutral_history": "Neutral Analyst: Reduce substantially.",
            },
        }
        self.assertFalse(detect_risk_nuance_conflict(state, "SELL"))

    def test_compute_context_warnings_apld_like(self):
        state = {
            "company_of_interest": "APLD",
            "analyst_ratings": {
                "recommendation_key": "strong_buy",
                "upside_pct": 162.94,
                "number_of_analysts": 11,
            },
            "scenario_analysis": {"blended_upside_pct": 383.8},
            "final_trade_decision": (
                'DECISION_JSON: {"decision":"SELL","conviction":"high"}\nDecision: SELL'
            ),
            "risk_debate_state": {
                "neutral_history": "Neutral Analyst: moderate sell or reduce recommendation.",
            },
            "market_report": "No citations here",
            "fundamentals_report": "Cited yfinance revenue on 2026-08-19 at $611M",
            "news_report": "Narrative only",
            "sentiment_report": "Narrative only",
        }
        warnings = compute_context_warnings(state, "SELL")
        self.assertIn("street_consensus_conflict", warnings)
        self.assertIn("valuation_conflict", warnings)
        self.assertIn("risk_nuance_conflict", warnings)
        self.assertIn("citations_thin", warnings)

    def test_analysis_api_record_projects_profile_and_handles_legacy_rows(self):
        from tradingagents.reporting.context_qc import enrich_analysis_api_record

        resolved = enrich_analysis_api_record(
            {
                "decision": "BUY",
                "investment_profile": (
                    '{"profile_key":"high_growth","display_name":"High Growth",'
                    '"resolved_from":"metadata_cache"}'
                ),
            }
        )
        legacy = enrich_analysis_api_record({"decision": "HOLD", "investment_profile": ""})
        self.assertEqual(resolved["investment_profile_info"]["key"], "high_growth")
        self.assertEqual(resolved["investment_profile_info"]["resolved_from"], "metadata_cache")
        self.assertEqual(legacy["investment_profile_info"]["display_name"], "Generic / legacy")


if __name__ == "__main__":
    unittest.main()
