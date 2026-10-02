import unittest

from tradingagents.reporting.attribution import _extract_signal_json, strip_signal_json, lint_signal_json


class SignalJsonGuardrailsTests(unittest.TestCase):
    def test_strip_signal_json_removes_valid_block(self):
        text = "Intro line.\nSIGNAL_JSON: {\"section\":\"News\",\"stance\":\"bullish\",\"confidence\":0.8}\nFooter."
        cleaned = strip_signal_json(text)
        self.assertIn("Intro line.", cleaned)
        self.assertIn("Footer.", cleaned)
        self.assertNotIn("SIGNAL_JSON", cleaned)

    def test_strip_signal_json_removes_malformed_block(self):
        text = "Intro line.\nSIGNAL_JSON: {\"section\":\"News\" \"stance\":\"bullish\"\nFooter."
        cleaned = strip_signal_json(text)
        self.assertEqual(cleaned, "Intro line.")

    def test_extract_signal_json_handles_malformed(self):
        text = "Intro line.\nSIGNAL_JSON: {\"section\":\"News\" \"stance\":\"bullish\""
        parsed = _extract_signal_json(text)
        self.assertEqual(parsed, {})

    def test_lint_signal_json_flags_schema_issues(self):
        text = "Intro line.\nSIGNAL_JSON: {\"section\":\"News\",\"stance\":\"bullish\",\"confidence\":0.8,\"key_factors\":[\"A\"]}"
        self.assertEqual(lint_signal_json(text, expected_section="News"), [])

        bad_text = "Intro line.\nSIGNAL_JSON: {\"section\":\"Other\",\"stance\":\"maybe\",\"confidence\":120}"
        issues = lint_signal_json(bad_text, expected_section="News")
        self.assertIn("section_mismatch", issues)
        self.assertIn("invalid_stance", issues)
        self.assertIn("invalid_confidence", issues)
        self.assertIn("invalid_key_factors", issues)


class TestOutputBudgetAndSignalLead(unittest.TestCase):
    def test_standard_mode_output_budget_covers_structured_footers(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        llm = DEFAULT_CONFIG["token_budget"]["llm"]
        self.assertGreaterEqual(llm["quick_max_output_tokens"], 1600)
        self.assertGreaterEqual(llm["deep_max_output_tokens"], 2200)

    def test_analysts_are_instructed_to_lead_with_signal_json(self):
        from tradingagents.agents.utils.agent_utils import structured_signal_instruction
        text = structured_signal_instruction("Market")
        self.assertIn("Start the final written report", text)
        self.assertIn('SIGNAL_JSON: {"section":"Market"', text)
        self.assertIn("dated data point or named source", text)
        self.assertNotIn("At the very end, append", text)


if __name__ == "__main__":
    unittest.main()
