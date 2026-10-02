"""Tests for instrument identity, verified snapshot, AV look-ahead, and date-first prompts."""
import inspect
import json
import unittest
from unittest.mock import patch

import pandas as pd

from tradingagents.agents.analysts import (
    fundamentals_analyst,
    market_analyst,
    news_analyst,
    social_media_analyst,
)
from tradingagents.agents.utils.agent_utils import create_msg_delete
from tradingagents.dataflows.alpha_vantage_fundamentals import _filter_reports_by_date
from tradingagents.dataflows.instrument_identity import (
    _clean_identity_value,
    build_instrument_context,
    get_instrument_context_from_state,
    resolve_instrument_identity,
)
from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot
from tradingagents.graph.propagation import Propagator


class TestInstrumentIdentity(unittest.TestCase):
    def test_clean_identity_rejects_placeholders(self):
        self.assertIsNone(_clean_identity_value(None))
        self.assertIsNone(_clean_identity_value("n/a"))
        self.assertEqual(_clean_identity_value("  Apple Inc.  "), "Apple Inc.")

    @patch("tradingagents.dataflows.yfinance_extended.get_ticker_info")
    def test_resolve_identity_maps_fields(self, mock_info):
        mock_info.return_value = {
            "longName": "Apple Inc.",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "exchange": "NMS",
            "quoteType": "EQUITY",
        }
        identity = resolve_instrument_identity("aapl")
        self.assertEqual(identity["company_name"], "Apple Inc.")
        self.assertEqual(identity["sector"], "Technology")
        self.assertEqual(identity["exchange"], "NMS")
        self.assertEqual(identity["quote_type"], "EQUITY")

    @patch("tradingagents.dataflows.yfinance_extended.get_ticker_info")
    def test_resolve_identity_fails_open(self, mock_info):
        mock_info.side_effect = RuntimeError("rate limited")
        self.assertEqual(resolve_instrument_identity("AAPL"), {})

    def test_build_context_includes_do_not_substitute(self):
        text = build_instrument_context(
            "AAPL",
            {"company_name": "Apple Inc.", "sector": "Technology"},
        )
        self.assertIn("`AAPL`", text)
        self.assertIn("Apple Inc.", text)
        self.assertIn("Do not substitute", text)

    def test_state_prefers_stored_context(self):
        stored = "The instrument to analyze is `NVDA`."
        self.assertEqual(
            get_instrument_context_from_state(
                {"instrument_context": stored, "company_of_interest": "WRONG"}
            ),
            stored,
        )

    def test_state_fallback_is_ticker_only_no_lookup(self):
        text = get_instrument_context_from_state({"company_of_interest": "MSFT"})
        self.assertIn("`MSFT`", text)
        self.assertNotIn("Resolved identity", text)

    def test_propagator_stores_identity_fields(self):
        state = Propagator().create_initial_state(
            "AAPL",
            "2026-08-17",
            instrument_context="ctx",
            instrument_identity={"company_name": "Apple Inc."},
        )
        self.assertEqual(state["instrument_context"], "ctx")
        self.assertEqual(state["instrument_identity"]["company_name"], "Apple Inc.")

    def test_msg_delete_placeholder_is_not_bare_continue(self):
        from langchain_core.messages import HumanMessage

        msg = HumanMessage(content="prior", id="1")
        out = create_msg_delete()(
            {
                "messages": [msg],
                "company_of_interest": "AAPL",
                "trade_date": "2026-08-17",
                "instrument_context": "The instrument to analyze is `AAPL`.",
            }
        )
        placeholder = out["messages"][-1]
        self.assertNotEqual(placeholder.content, "Continue")
        self.assertIn("AAPL", placeholder.content)
        self.assertIn("2026-08-17", placeholder.content)


class TestDateFirstPrompts(unittest.TestCase):
    def _assert_date_leads(self, module):
        src = inspect.getsource(module)
        self.assertIn("Today's date is {current_date}", src)
        self.assertIn("{instrument_context}", src)
        date_idx = src.index("Today's date is {current_date}")
        sys_idx = src.index("{system_message}")
        self.assertLess(date_idx, sys_idx)
        self.assertNotIn("For your reference, the current date", src)

    def test_market_prompt_leads_with_date(self):
        self._assert_date_leads(market_analyst)

    def test_news_prompt_leads_with_date(self):
        self._assert_date_leads(news_analyst)

    def test_social_prompt_leads_with_date(self):
        self._assert_date_leads(social_media_analyst)

    def test_fundamentals_prompt_leads_with_date(self):
        self._assert_date_leads(fundamentals_analyst)

    def test_market_analyst_lists_verified_snapshot_tool(self):
        src = inspect.getsource(market_analyst)
        self.assertIn("get_verified_market_snapshot", src)


class TestAlphaVantageLookahead(unittest.TestCase):
    def test_filters_future_fiscal_periods(self):
        payload = json.dumps(
            {
                "annualReports": [
                    {"fiscalDateEnding": "2024-12-31", "totalAssets": "1"},
                    {"fiscalDateEnding": "2026-12-31", "totalAssets": "2"},
                ],
                "quarterlyReports": [
                    {"fiscalDateEnding": "2025-06-30"},
                    {"fiscalDateEnding": "2026-09-30"},
                ],
            }
        )
        filtered = json.loads(_filter_reports_by_date(payload, "2026-08-17"))
        self.assertEqual(
            [r["fiscalDateEnding"] for r in filtered["annualReports"]],
            ["2024-12-31"],
        )
        self.assertEqual(
            [r["fiscalDateEnding"] for r in filtered["quarterlyReports"]],
            ["2025-06-30"],
        )

    def test_non_json_and_unset_date_pass_through(self):
        raw = "not-json"
        self.assertEqual(_filter_reports_by_date(raw, "2026-08-17"), raw)
        payload = json.dumps({"annualReports": [{"fiscalDateEnding": "2026-12-31"}]})
        self.assertEqual(_filter_reports_by_date(payload, None), payload)
        self.assertEqual(_filter_reports_by_date({"annualReports": []}, "2026-08-17"), {"annualReports": []})


class TestVerifiedMarketSnapshot(unittest.TestCase):
    def test_snapshot_uses_latest_row_on_or_before_date(self):
        csv_text = (
            "# Stock data for AAPL from 2025-07-14 to 2026-08-17\n"
            "Date,Open,High,Low,Close,Volume\n"
            "2026-08-14,10,11,9,10.5,1000\n"
            "2026-08-17,11,12,10,11.5,1100\n"
        )
        with patch(
            "tradingagents.dataflows.market_data_validator.get_YFin_data_online",
            return_value=csv_text,
        ):
            out = build_verified_market_snapshot("AAPL", "2026-08-17", look_back_days=2)
        self.assertIn("Latest trading row used: 2026-08-17", out)
        self.assertIn("11.50", out)
        self.assertIn("source of truth", out)

    def test_snapshot_unavailable_on_no_data(self):
        with patch(
            "tradingagents.dataflows.market_data_validator.get_YFin_data_online",
            return_value="No data found for symbol 'ZZZZ' between 2025-07-14 and 2026-08-17",
        ):
            out = build_verified_market_snapshot("ZZZZ", "2026-08-17")
        self.assertIn("Unavailable", out)
        self.assertIn("No data found", out)


if __name__ == "__main__":
    unittest.main()
