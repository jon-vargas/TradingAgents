"""OpenAI Chat Completions extra_body for GPT-5.6 tool compatibility."""
import unittest

from tradingagents.graph.trading_graph import _openai_chat_completions_extra_body


class TestOpenAIChatCompletionsExtraBody(unittest.TestCase):
    def test_quick_luna_disables_reasoning_for_tools(self):
        extra = _openai_chat_completions_extra_body(
            "gpt-5.6-luna",
            for_tool_calling=True,
            prompt_cache_key="run-1",
        )
        self.assertEqual(extra["reasoning_effort"], "none")
        self.assertEqual(extra["prompt_cache_key"], "run-1")

    def test_deep_terra_keeps_default_reasoning(self):
        extra = _openai_chat_completions_extra_body(
            "gpt-5.6-terra",
            for_tool_calling=False,
            prompt_cache_key="run-1",
        )
        self.assertNotIn("reasoning_effort", extra)
        self.assertEqual(extra["prompt_cache_key"], "run-1")

    def test_legacy_quick_model_unchanged(self):
        extra = _openai_chat_completions_extra_body(
            "gpt-4o-mini",
            for_tool_calling=True,
        )
        self.assertNotIn("reasoning_effort", extra)


if __name__ == "__main__":
    unittest.main()
