"""Tests for Wave 1 hardening: null-safe classifiers + initial-population churn.

Covers:
- _safe_num() coercion of yfinance-style mixed-type numeric fields
- classify_market_cap / classify_beta resilience to strings, NaN, bools
- resolve_ticker_metadata normalizing market_cap to int|None
- _compute_live_sector_medians tolerating string forward_pe values
- build_refresh_proposal emitting initial_population note (not churn warning)
  when old_tickers is empty AND source_mode_status == pending_first_refresh
"""
from __future__ import annotations

import math
import unittest
from unittest.mock import MagicMock, patch


class TestSafeNum(unittest.TestCase):
    def test_rejects_none_nan_inf_bool(self):
        from tradingagents.screening.ticker_resolver import _safe_num

        self.assertIsNone(_safe_num(None))
        self.assertIsNone(_safe_num(float("nan")))
        self.assertIsNone(_safe_num(float("inf")))
        self.assertIsNone(_safe_num(float("-inf")))
        self.assertIsNone(_safe_num(True))
        self.assertIsNone(_safe_num(False))

    def test_coerces_numeric_strings(self):
        from tradingagents.screening.ticker_resolver import _safe_num

        self.assertEqual(_safe_num("0.85"), 0.85)
        self.assertEqual(_safe_num("1,200"), 1200.0)
        self.assertEqual(_safe_num("5%"), 5.0)
        self.assertEqual(_safe_num("  42 "), 42.0)

    def test_rejects_sentinel_strings(self):
        from tradingagents.screening.ticker_resolver import _safe_num

        for token in ["N/A", "n/a", "none", "null", "Infinity", "-inf", ""]:
            self.assertIsNone(_safe_num(token))

    def test_passes_through_clean_floats(self):
        from tradingagents.screening.ticker_resolver import _safe_num

        self.assertEqual(_safe_num(1.5), 1.5)
        self.assertEqual(_safe_num(0), 0.0)
        self.assertEqual(_safe_num(-0.75), -0.75)


class TestClassifiers(unittest.TestCase):
    def test_classify_beta_null_safe_against_strings(self):
        from tradingagents.screening.ticker_resolver import classify_beta

        self.assertEqual(classify_beta(None), "unknown")
        self.assertEqual(classify_beta("N/A"), "unknown")
        self.assertEqual(classify_beta("Infinity"), "unknown")
        self.assertEqual(classify_beta(float("nan")), "unknown")
        self.assertEqual(classify_beta(True), "unknown")  # bool rejected
        # Valid numeric paths still resolve correctly
        self.assertEqual(classify_beta(-0.2), "defensive")
        self.assertEqual(classify_beta(0.5), "low")
        self.assertEqual(classify_beta(1.0), "medium")
        self.assertEqual(classify_beta(1.5), "high")
        self.assertEqual(classify_beta(2.5), "extreme")
        # String numerics get coerced
        self.assertEqual(classify_beta("1.1"), "medium")

    def test_classify_market_cap_null_safe(self):
        from tradingagents.screening.ticker_resolver import classify_market_cap

        self.assertEqual(classify_market_cap(None), "unknown")
        self.assertEqual(classify_market_cap("N/A"), "unknown")
        self.assertEqual(classify_market_cap(0), "unknown")
        self.assertEqual(classify_market_cap(-1), "unknown")
        self.assertEqual(classify_market_cap(float("nan")), "unknown")
        self.assertEqual(classify_market_cap(500e9), "mega")
        self.assertEqual(classify_market_cap(50e9), "large")
        self.assertEqual(classify_market_cap(5e9), "mid")
        self.assertEqual(classify_market_cap(500e6), "small")
        self.assertEqual(classify_market_cap(100e6), "micro")


class TestResolveTickerMetadata(unittest.TestCase):
    def test_normalizes_market_cap_to_int_or_none(self):
        from tradingagents.screening import ticker_resolver

        fake_info = {
            "quoteType": "EQUITY",
            "sector": "Technology",
            "industry": "Software",
            "marketCap": "abc",  # junk string — must degrade to None
            "trailingEPS": "N/A",
            "profitMargins": None,
            "netIncomeToCommon": None,
            "dividendYield": "",
            "beta": "Infinity",
            "exchange": "NMS",
        }
        with patch.object(ticker_resolver, "get_ticker_info", return_value=fake_info, create=True), \
             patch("tradingagents.dataflows.yfinance_extended.get_ticker_info", return_value=fake_info):
            result = ticker_resolver.resolve_ticker_metadata("XYZ")
        self.assertIsNotNone(result)
        self.assertIsNone(result["market_cap"])
        self.assertEqual(result["market_cap_tier"], "unknown")
        self.assertEqual(result["beta_tier"], "unknown")
        self.assertFalse(result["has_dividend"])
        self.assertFalse(result["is_profitable"])

    def test_coerces_valid_string_numbers(self):
        from tradingagents.screening import ticker_resolver

        fake_info = {
            "quoteType": "EQUITY",
            "sector": "Healthcare",
            "industry": "Drug Manufacturers",
            "marketCap": "250000000000",  # 250B as string
            "trailingEPS": "5.25",
            "dividendYield": "0.03",
            "beta": "0.65",
            "exchange": "NYQ",
        }
        with patch("tradingagents.dataflows.yfinance_extended.get_ticker_info", return_value=fake_info):
            result = ticker_resolver.resolve_ticker_metadata("XYZ")
        self.assertIsNotNone(result)
        self.assertEqual(result["market_cap"], 250_000_000_000)
        self.assertEqual(result["market_cap_tier"], "mega")
        self.assertTrue(result["is_profitable"])
        self.assertTrue(result["has_dividend"])
        self.assertEqual(result["beta_tier"], "low")


class TestSectorMediansNullSafe(unittest.TestCase):
    def test_compute_live_sector_medians_skips_string_forward_pe(self):
        """Regression guard: yfinance may return forward_pe as 'Infinity'/None/str
        which previously raised TypeError inside sorted(pes). We must silently drop
        them without crashing the whole scan.
        """
        from tradingagents.screening.engine import ScreeningEngine

        engine = ScreeningEngine.__new__(ScreeningEngine)
        engine._screening_config = {"valuation_gap_metrics": {"live_median_min_peers": 3}}
        enhanced_data = {
            "AAA": {"valuation": {"forward_pe": 18.0, "sector": "Technology"}},
            "BBB": {"valuation": {"forward_pe": "Infinity", "sector": "Technology"}},
            "CCC": {"valuation": {"forward_pe": None, "sector": "Technology"}},
            "DDD": {"valuation": {"forward_pe": 22.0, "sector": "Technology"}},
            "EEE": {"valuation": {"forward_pe": 20.0, "sector": "Technology"}},
            "FFF": {"valuation": {"forward_pe": 15.0, "sector": None}},  # no sector
            "GGG": {"valuation": {"forward_pe": "N/A", "sector": "Healthcare"}},
        }
        medians = engine._compute_live_sector_medians(enhanced_data)
        # Now returns {metric: {sector: median}} — check forward_pe sub-dict
        fwd_pe_medians = medians.get("forward_pe", medians)  # tolerate both old and new shape
        self.assertIn("Technology", fwd_pe_medians)
        self.assertEqual(fwd_pe_medians["Technology"], 20.0)
        self.assertNotIn("Healthcare", fwd_pe_medians)  # only 1 numeric after filter

    def test_signal_valuation_gap_handles_string_forward_pe(self):
        from tradingagents.screening.engine import ScreeningEngine

        engine = ScreeningEngine.__new__(ScreeningEngine)
        # Must not raise even if the caller passes a raw string
        score = engine._signal_valuation_gap("N/A", "Technology", None)
        self.assertEqual(score, 0.0)
        score = engine._signal_valuation_gap(None, "Technology", None)
        self.assertEqual(score, 0.0)
        score = engine._signal_valuation_gap(15.0, "Technology", None)  # static fallback 28
        self.assertGreater(score, 0.0)


class TestInitialPopulationChurnSuppression(unittest.TestCase):
    def _make_db(self, existing_row):
        db = MagicMock()
        db._builtin_watchlist_registry.return_value = [
            {
                "name": "Russell 2000",
                "source": "built-in",
                "enabled": True,
                "source_mode": "objective",
                "target_size": 2000,
                "tickers": "AAA,BBB",
                "registry_backed": True,
                "index_key": "russell_2000",
            }
        ]
        db.get_builtin_watchlists.return_value = [existing_row]
        db.create_builtin_refresh_proposal.return_value = 99
        return db

    def _patch_targets(self):
        return patch.multiple(
            "tradingagents.screening.builtin_refresh",
            _fetch_index_symbols=lambda name: (["AAPL", "MSFT", "GOOG", "AMZN", "NVDA"], []),
            _validate_symbols_tiered=lambda symbols, **kwargs: (
                list(symbols), [], {}, {"tier1_validated": len(symbols), "tier2_validated": 0, "tier3_validated": 0}
            ),
            _hydrate_market_caps=lambda db, symbols, warnings: ({s: 1e11 for s in symbols}, 0),
        )

    def _captured_items(self, db):
        """Pull the items list out of the create_builtin_refresh_proposal call.

        build_refresh_proposal returns whatever get_builtin_refresh_proposal
        produces, which is a MagicMock in unit tests. The real items list is
        what was persisted via create_builtin_refresh_proposal().
        """
        create_call = db.create_builtin_refresh_proposal.call_args
        self.assertIsNotNone(create_call)
        return create_call.args[0] if create_call.args else create_call.kwargs["items"]

    def test_initial_population_emits_note_not_churn(self):
        from tradingagents.screening import builtin_refresh

        db = self._make_db({
            "id": 1,
            "name": "Russell 2000",
            "tickers": "",
            "source_mode_status": "pending_first_refresh",
        })
        with self._patch_targets():
            builtin_refresh.build_refresh_proposal(db)

        items = self._captured_items(db)
        russell = next(i for i in items if i["watchlist_name"] == "Russell 2000")
        warning_text = "\n".join(russell["warnings"])
        self.assertIn("initial_population", warning_text)
        self.assertNotIn("high churn", warning_text)

    def test_churn_warning_still_fires_for_established_list(self):
        from tradingagents.screening import builtin_refresh

        # Incumbent list of 2; candidate replaces both → 100%+ churn.
        db = self._make_db({
            "id": 1,
            "name": "Russell 2000",
            "tickers": "ZZZ,YYY",
            "source_mode_status": "materialized",
        })
        with self._patch_targets():
            builtin_refresh.build_refresh_proposal(db)

        items = self._captured_items(db)
        russell = next(i for i in items if i["watchlist_name"] == "Russell 2000")
        warning_text = "\n".join(russell["warnings"])
        self.assertIn("high churn", warning_text)
        self.assertNotIn("initial_population", warning_text)


if __name__ == "__main__":
    unittest.main()
