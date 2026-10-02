import unittest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting.pdf_generator import (
    ReportPayload,
    _build_risk_assessment_text,
    apply_report_qc,
    build_report_payload,
    extract_confidence_score,
)


def _sample_state() -> dict:
    signal = (
        'FINAL TRANSACTION PROPOSAL: **HOLD**\n'
        'SIGNAL_JSON: {"section":"Market","stance":"bearish","confidence":0.78,'
        '"key_factors":["Price below EMA","Negative MACD"]}\n\n'
        "Detailed market narrative with $28.23 close on 2026-08-19."
    )
    return {
        "company_of_interest": "APLD",
        "market_report": signal.replace("Market", "Market"),
        "fundamentals_report": signal.replace("Market", "Fundamentals").replace("bearish", "neutral"),
        "news_report": signal.replace("Market", "News"),
        "sentiment_report": signal.replace("Market", "Sentiment").replace("bearish", "bullish"),
        "investment_plan": signal.replace("Market", "Research").replace("HOLD", "SELL").replace("bearish", "bearish"),
        "trader_investment_plan": signal.replace("Market", "Trading Plan").replace("HOLD", "SELL"),
        "final_trade_decision": (
            'DECISION_JSON: {"decision":"SELL","conviction":"high"}\n\n## Decision: SELL\n'
            "Reduce exposure due to weak technicals."
        ),
        "risk_debate_state": {
            "risky_history": "Aggressive Analyst: Upside remains if financing improves.",
            "safe_history": "Conservative Analyst: Protect capital until cash flow stabilizes.",
            "neutral_history": "Neutral Analyst: Reduce 60-80% and wait for confirmation.",
        },
        "investment_debate_state": {
            "bull_history": "Bull Analyst: " + ("Growth opportunity. " * 400),
            "bear_history": "Bear Analyst: " + ("Cash burn risk. " * 400),
        },
        "data_provenance": [{"vendor": "yfinance", "method": "get_stock_data"}],
    }


class ReportQcTests(unittest.TestCase):
    def test_apply_report_qc_does_not_flag_signal_json_after_strip(self):
        state = _sample_state()
        payload = build_report_payload(
            state=state,
            ticker="APLD",
            analysis_date="2026-08-19",
            decision="SELL",
            config=DEFAULT_CONFIG.copy(),
        )
        for field in ("market_report", "fundamentals_report", "news_report", "sentiment_report"):
            self.assertNotIn("SIGNAL_JSON:", getattr(payload, field))

        warnings = apply_report_qc(payload, state=state)
        self.assertNotIn("signal_json_missing", warnings)
        self.assertNotIn("decision_json_missing", warnings)

    def test_apply_report_qc_flags_missing_signal_json_from_raw_state(self):
        state = _sample_state()
        state["news_report"] = "News narrative without structured signal output."
        payload = build_report_payload(
            state=state,
            ticker="APLD",
            analysis_date="2026-08-19",
            decision="SELL",
            config=DEFAULT_CONFIG.copy(),
        )
        warnings = apply_report_qc(payload, state=state)
        self.assertIn("signal_json_missing", warnings)

    def test_trading_plan_uses_trader_plan_not_research_judge(self):
        state = _sample_state()
        state["trader_investment_plan"] = "Trader plan body " + ("x" * 1200)
        state["investment_plan"] = "Research judge body " + ("y" * 7000)

        payload = build_report_payload(
            state=state,
            ticker="APLD",
            analysis_date="2026-08-19",
            decision="SELL",
            config=DEFAULT_CONFIG.copy(),
        )
        self.assertIn("Trader plan body", payload.trading_plan)
        self.assertNotIn("Research judge body", payload.trading_plan)

        warnings = apply_report_qc(payload, state=state)
        self.assertNotIn("trading_plan_truncated", warnings)

    def test_risk_assessment_built_from_risk_debate_state(self):
        state = _sample_state()
        text = _build_risk_assessment_text(state)
        self.assertIn("AGGRESSIVE:", text)
        self.assertIn("CONSERVATIVE:", text)
        self.assertIn("NEUTRAL:", text)

    def test_confidence_penalizes_high_disagreement(self):
        mixed = extract_confidence_score(_sample_state(), "SELL")
        aligned_state = _sample_state()
        aligned_state["sentiment_report"] = aligned_state["sentiment_report"].replace("bullish", "bearish")
        aligned = extract_confidence_score(aligned_state, "SELL")
        self.assertLessEqual(mixed, aligned)

    def test_compute_state_warnings_dedupes_and_clears_false_signal_missing(self):
        from tradingagents.reporting.database import _compute_state_warnings

        state = _sample_state()
        state["report_warnings"] = ["signal_json_missing", "news_report_truncated"]
        warnings = _compute_state_warnings(state)
        self.assertNotIn("signal_json_missing", warnings)
        self.assertIn("news_report_truncated", warnings)

    def test_citations_missing_haircuts_confidence(self):
        """Citation haircut only — keep SIGNAL_JSON so other factors stay equal."""
        bare = (
            'SIGNAL_JSON: {"section":"Market","stance":"bearish","confidence":0.78,'
            '"key_factors":["Price below EMA"]}\n'
            "Narrative without a source, dollar print, or date stamp."
        )
        grounded = bare + " Source: https://www.sec.gov/example"
        missing_state = _sample_state()
        cited_state = _sample_state()
        for field in ("market_report", "fundamentals_report", "news_report", "sentiment_report"):
            missing_state[field] = bare
            cited_state[field] = grounded
        missing = extract_confidence_score(missing_state, "SELL")
        cited = extract_confidence_score(cited_state, "SELL")
        self.assertLess(missing, cited)

    def test_apply_report_qc_flags_deep_research_incomplete(self):
        state = _sample_state()
        state["data_provenance"] = [
            {"vendor": "perplexity", "method": "get_deep_research", "status": "success"},
            {"vendor": "yfinance", "method": "get_stock_data", "status": "success"},
        ]
        payload = build_report_payload(
            state=state,
            ticker="APLD",
            analysis_date="2026-08-19",
            decision="SELL",
            config=DEFAULT_CONFIG.copy(),
        )
        warnings = apply_report_qc(payload, state=state)
        self.assertIn("deep_research_incomplete", warnings)

    def test_apply_report_qc_clears_deep_research_when_sec_snapshot_succeeds(self):
        state = _sample_state()
        state["data_provenance"] = [
            {"vendor": "perplexity", "method": "get_deep_research", "status": "success"},
            {"vendor": "cache", "method": "get_sec_filings_snapshot", "status": "success", "cache_hit": True},
        ]
        payload = build_report_payload(
            state=state,
            ticker="APLD",
            analysis_date="2026-08-19",
            decision="SELL",
            config=DEFAULT_CONFIG.copy(),
        )
        warnings = apply_report_qc(payload, state=state)
        self.assertNotIn("deep_research_incomplete", warnings)

    def test_apply_report_qc_keeps_incomplete_when_sec_snapshot_errors(self):
        state = _sample_state()
        state["data_provenance"] = [
            {"vendor": "perplexity", "method": "get_deep_research", "status": "success"},
            {"vendor": "perplexity", "method": "get_sec_filings_snapshot", "status": "error"},
        ]
        payload = build_report_payload(
            state=state,
            ticker="APLD",
            analysis_date="2026-08-19",
            decision="SELL",
            config=DEFAULT_CONFIG.copy(),
        )
        warnings = apply_report_qc(payload, state=state)
        self.assertIn("deep_research_incomplete", warnings)

    def test_sanitize_options_drops_implausible_max_pain_and_tiny_iv(self):
        from tradingagents.dataflows.yfinance_extended import sanitize_options_snapshot

        out = sanitize_options_snapshot(
            {"atm_iv": 0.05, "max_pain": 110.0, "current_price": 290.0},
            spot=290.0,
        )
        self.assertIsNone(out["atm_iv"])
        self.assertIsNone(out["max_pain"])


if __name__ == "__main__":
    unittest.main()
