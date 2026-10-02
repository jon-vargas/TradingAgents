import csv
import io
import unittest

from tradingagents.utils.tradingview_links import (
    exchange_from_info,
    is_valid_tradingview_symbol_token,
    normalize_exchange,
    resolve_exchange_from_info,
    tradingview_chart_url,
    tradingview_symbol_token,
    tradingview_symbol_url,
    tradingview_txt_content,
)
from webapp.app import _render_tradingview_companion_csv


class TradingViewLinksTests(unittest.TestCase):
    def test_normalize_exchange_maps_vendor_variants(self):
        self.assertEqual(normalize_exchange("NMS"), "NASDAQ")
        self.assertEqual(normalize_exchange("NASDAQGS"), "NASDAQ")
        self.assertEqual(normalize_exchange("NYQ"), "NYSE")
        self.assertEqual(normalize_exchange("ASE"), "AMEX")
        self.assertEqual(normalize_exchange("OTCMKTS"), "OTC")

    def test_exchange_from_info_uses_preferred_fields(self):
        info = {"exchangeShortName": "NYSE", "exchange": "NYQ"}
        self.assertEqual(exchange_from_info(info), "NYSE")
        self.assertEqual(exchange_from_info({"exchange": "NMS"}), "NASDAQ")
        self.assertEqual(exchange_from_info({}), "NASDAQ")

    def test_symbol_token_and_url(self):
        self.assertEqual(tradingview_symbol_token("crm", "nyq"), "NYSE:CRM")
        self.assertEqual(
            tradingview_symbol_url("CRM", "NYSE"),
            "https://www.tradingview.com/symbols/NYSE-CRM/",
        )
        self.assertEqual(
            tradingview_symbol_token("NASDAQ:AAPL", "NYSE"),
            "NASDAQ:AAPL",
        )
        self.assertEqual(
            tradingview_chart_url("HIMS", "NYSE", chart_id="TxiihJ7N"),
            "https://www.tradingview.com/chart/TxiihJ7N/?symbol=NYSE%3AHIMS",
        )

    def test_txt_content(self):
        content = tradingview_txt_content(["NYSE:CRM", "", "NASDAQ:AAPL", " "])
        self.assertEqual(content, "NYSE:CRM,NASDAQ:AAPL")

    def test_strict_token_drops_unallowed_exchange(self):
        self.assertEqual(tradingview_symbol_token("AAPL", "Euronext", strict=True), "")

    def test_strict_token_drops_missing_exchange(self):
        self.assertEqual(tradingview_symbol_token("AAPL", None, strict=True), "")

    def test_is_valid_tradingview_symbol_token(self):
        self.assertTrue(is_valid_tradingview_symbol_token("NASDAQ:AAPL"))
        self.assertTrue(is_valid_tradingview_symbol_token("TSE:7203"))
        self.assertFalse(is_valid_tradingview_symbol_token("EURONEXT:ENPH"))
        self.assertFalse(is_valid_tradingview_symbol_token("AAPL"))

    def test_resolve_exchange_disambiguates_tse_for_japan(self):
        info = {
            "exchangeShortName": "TSE",
            "country": "Japan",
            "currency": "JPY",
            "exchangeTimezoneName": "Asia/Tokyo",
        }
        res = resolve_exchange_from_info("7203", info)
        self.assertEqual(res["exchange"], "TSE")

    def test_resolve_exchange_disambiguates_tse_for_canada(self):
        info = {
            "exchangeShortName": "TSE",
            "country": "Canada",
            "currency": "CAD",
            "exchangeTimezoneName": "America/Toronto",
        }
        res = resolve_exchange_from_info("7203", info)
        self.assertEqual(res["exchange"], "TSX")


class TradingViewCompanionCsvTests(unittest.TestCase):
    def test_companion_csv_shape(self):
        csv_text = _render_tradingview_companion_csv(
            [
                {
                    "ticker": "CRM",
                    "exchange": "NYSE",
                    "tv_symbol": "NYSE:CRM",
                    "composite_score": 67.6,
                    "direction": "bullish",
                    "watchlist_name": "Dow Jones 30",
                    "run_id": 12,
                    "macro_fit": 63.2,
                    "rank": 1,
                }
            ]
        )
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["ticker"], "CRM")
        self.assertEqual(row["exchange"], "NYSE")
        self.assertEqual(row["tv_symbol"], "NYSE:CRM")
        self.assertEqual(row["watchlist_name"], "Dow Jones 30")
        self.assertEqual(row["run_id"], "12")


if __name__ == "__main__":
    unittest.main()
