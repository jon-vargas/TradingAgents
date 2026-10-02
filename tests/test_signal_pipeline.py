"""Unit tests for the signal aggregation and processing pipeline."""
import unittest
from unittest.mock import patch, MagicMock


class TestStanceToScore(unittest.TestCase):
    def test_bullish_tokens(self):
        from tradingagents.graph.signal_aggregator import _stance_to_score
        for stance in ["bullish", "BUY", "positive", "Outperform", "BULLISH"]:
            self.assertEqual(_stance_to_score(stance), 1.0, f"Failed for: {stance}")

    def test_bearish_tokens(self):
        from tradingagents.graph.signal_aggregator import _stance_to_score
        for stance in ["bearish", "SELL", "negative", "Underperform", "BEARISH"]:
            self.assertEqual(_stance_to_score(stance), -1.0, f"Failed for: {stance}")

    def test_neutral_tokens(self):
        from tradingagents.graph.signal_aggregator import _stance_to_score
        for stance in ["neutral", "HOLD", "mixed", ""]:
            self.assertEqual(_stance_to_score(stance), 0.0, f"Failed for: {stance}")

    def test_empty_and_none(self):
        from tradingagents.graph.signal_aggregator import _stance_to_score
        self.assertEqual(_stance_to_score(""), 0.0)
        self.assertEqual(_stance_to_score(None), 0.0)


class TestNormalizeConfidence(unittest.TestCase):
    def test_scale_0_to_1(self):
        from tradingagents.graph.signal_aggregator import _normalize_confidence
        self.assertAlmostEqual(_normalize_confidence(0.75), 0.75)

    def test_scale_0_to_100(self):
        from tradingagents.graph.signal_aggregator import _normalize_confidence
        self.assertAlmostEqual(_normalize_confidence(75), 0.75)

    def test_none_returns_default(self):
        from tradingagents.graph.signal_aggregator import _normalize_confidence
        self.assertAlmostEqual(_normalize_confidence(None), 0.5)

    def test_negative_clamped(self):
        from tradingagents.graph.signal_aggregator import _normalize_confidence
        self.assertGreaterEqual(_normalize_confidence(-0.5), 0.0)


class TestCompositeLabel(unittest.TestCase):
    def test_neutral_band(self):
        from tradingagents.graph.signal_aggregator import _composite_label
        self.assertEqual(_composite_label(0.0), "NEUTRAL")
        self.assertEqual(_composite_label(0.04), "NEUTRAL")
        self.assertEqual(_composite_label(-0.04), "NEUTRAL")

    def test_bullish(self):
        from tradingagents.graph.signal_aggregator import _composite_label
        label = _composite_label(0.5)
        self.assertIn("BULLISH", label)

    def test_bearish(self):
        from tradingagents.graph.signal_aggregator import _composite_label
        label = _composite_label(-0.5)
        self.assertIn("BEARISH", label)


class TestProfileWeightOverlays(unittest.TestCase):
    def test_default_weights_remain_unchanged(self):
        from tradingagents.graph.signal_aggregator import (
            DATA_WEIGHTS,
            LLM_WEIGHTS,
            _resolve_weights,
        )

        llm, data, metadata = _resolve_weights({}, transcript_available=False)
        self.assertEqual(llm, LLM_WEIGHTS)
        self.assertEqual(data, DATA_WEIGHTS)
        self.assertIsNone(metadata["weight_overlay_key"])

    def test_high_growth_overlay_preserves_60_40_with_or_without_transcript(self):
        from tradingagents.graph.signal_aggregator import _resolve_weights

        state = {"investment_profile": {"profile_key": "high_growth"}}
        _, without_transcript, without_meta = _resolve_weights(
            state, transcript_available=False
        )
        with_llm, with_transcript, with_meta = _resolve_weights(
            state, transcript_available=True
        )
        self.assertAlmostEqual(sum(with_llm.values()), 0.60)
        self.assertAlmostEqual(sum(without_transcript.values()), 0.40)
        self.assertAlmostEqual(sum(with_transcript.values()), 0.40)
        self.assertNotIn("transcript_kpi", without_transcript)
        self.assertEqual(without_meta["weight_overlay_key"], "high_growth")
        self.assertEqual(with_meta["weight_overlay_key"], "high_growth")


class TestProcessSignal(unittest.TestCase):
    def test_decision_json_extraction(self):
        from tradingagents.graph.signal_processing import _extract_decision_json
        text = 'Some analysis...\nDECISION_JSON: {"decision":"BUY","conviction":"high"}'
        result = _extract_decision_json(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "BUY")

    def test_regex_fallback(self):
        from tradingagents.graph.signal_processing import _extract_decision_from_text
        text = "FINAL TRANSACTION PROPOSAL: **SELL**"
        self.assertEqual(_extract_decision_from_text(text), "SELL")

    def test_invalid_decision_json(self):
        from tradingagents.graph.signal_processing import _extract_decision_json
        self.assertIsNone(_extract_decision_json("no json here"))
        self.assertIsNone(_extract_decision_json(""))
        self.assertIsNone(_extract_decision_json(None))

    def test_explicit_decision_ignores_narrative_buy_mention(self):
        from tradingagents.graph.signal_processing import extract_explicit_decision
        text = (
            "**Decision: SELL**\n\n"
            "A future reassessment toward HOLD or BUY would require cash-flow improvement."
        )
        self.assertEqual(extract_explicit_decision(text), "SELL")

    def test_explicit_decision_from_proposal(self):
        from tradingagents.graph.signal_processing import extract_explicit_decision
        self.assertEqual(
            extract_explicit_decision("FINAL TRANSACTION PROPOSAL: **HOLD**\nLong analysis."),
            "HOLD",
        )


class TestSignalInferenceAndCitations(unittest.TestCase):
    def test_infers_market_signal_from_proposal_without_json(self):
        from tradingagents.graph.signal_aggregator import _extract_llm_signals
        state = {
            "market_report": "FINAL TRANSACTION PROPOSAL: **SELL**\nPrice broke the 50-day SMA.",
            "fundamentals_report": "FINAL TRANSACTION PROPOSAL: **BUY**\nRevenue grew 40%.",
        }
        signals, missing = _extract_llm_signals(state)
        self.assertEqual(signals["Market"]["score"], -1.0)
        self.assertTrue(signals["Market"]["inferred"])
        self.assertEqual(signals["Fundamentals"]["score"], 1.0)
        self.assertNotIn("Market", missing)
        self.assertNotIn("Fundamentals", missing)

    def test_citation_detector_accepts_url_and_grounded_numbers(self):
        from tradingagents.reporting.pdf_generator import _has_citation
        self.assertTrue(_has_citation("See https://example.com/filing"))
        self.assertTrue(_has_citation("Source [1] supports the claim."))
        self.assertTrue(_has_citation("Close was $18.01 on 2026-08-18."))
        self.assertTrue(_has_citation("Data basis: Yahoo Finance daily bars through August 24, 2026."))
        self.assertTrue(_has_citation("Support held at $925 on August 24, 2026."))
        self.assertFalse(_has_citation("The stock looks mixed without numbers."))


if __name__ == "__main__":
    unittest.main()
