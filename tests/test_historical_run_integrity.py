import unittest
from unittest.mock import patch

from tradingagents.dataflows.run_date import is_historical_run, parse_trade_date, run_as_of_date
from tradingagents.dataflows.instrument_identity import resolve_instrument_context_for_run
from tradingagents.dataflows.interface import route_to_vendor


class TestHistoricalRunIntegrity(unittest.TestCase):
    def test_parse_and_historical(self):
        self.assertEqual(parse_trade_date("2020-01-15").isoformat(), "2020-01-15")
        self.assertTrue(is_historical_run("2020-01-15", today="2026-09-29"))
        self.assertFalse(is_historical_run("2026-09-29", today="2026-09-29"))
        self.assertEqual(run_as_of_date("2020-01-15"), "2020-01-15")
        self.assertFalse(is_historical_run("30"))

    def test_indicator_run_date_is_curr_date_not_lookback(self):
        from tradingagents.dataflows.interface import _run_date_from_call

        self.assertEqual(
            _run_date_from_call("get_indicators", ("PTRN", "rsi", "2026-10-02", 30), {}),
            "2026-10-02",
        )

    @patch("tradingagents.dataflows.instrument_identity._load_ticker_metadata_row", return_value=None)
    @patch("tradingagents.dataflows.instrument_identity.resolve_instrument_identity")
    def test_skips_live_identity_on_historical(self, mock_resolve, _meta):
        mock_resolve.return_value = {"company_name": "Live Co"}
        _identity, context = resolve_instrument_context_for_run("AAPL", "2020-01-15")
        mock_resolve.assert_not_called()
        self.assertIn("Historical run", context)

    def test_metadata_attached_only_when_not_after_trade_date(self):
        from tradingagents.dataflows.instrument_identity import identity_from_metadata_as_of

        row = {"sector": "Technology", "industry": "Software", "last_updated": "2019-12-01"}
        self.assertEqual(
            identity_from_metadata_as_of(row, "2020-01-15").get("sector"),
            "Technology",
        )
        later = {**row, "last_updated": "2024-06-01"}
        self.assertEqual(identity_from_metadata_as_of(later, "2020-01-15"), {})

    @patch("tradingagents.dataflows.interface.get_config")
    def test_live_only_tool_blocked(self, mock_cfg):
        mock_cfg.return_value = {"enable_cache": False, "enable_usage_tracking": False}
        out = route_to_vendor(
            "get_deep_research",
            "AAPL",
            "Apple",
            curr_date="2020-01-15",
        )
        self.assertIn("Not available for historical run date", str(out))


if __name__ == "__main__":
    unittest.main()
