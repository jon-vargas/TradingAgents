"""Unit tests for the ETF/ADR universe + risk-score dimension shipped with
the "broaden universe + risk score" pass. These lock in three independent
behaviors:

1.  ``classify_asset_class`` correctly labels ETFs, ADRs, and domestic
    equities from a ``yfinance .info``-shaped payload, and degrades to
    ``"unknown"`` when the payload is missing ``quoteType``.
2.  ``resolve_profile_and_preset`` short-circuits to ``etf_technical`` for
    any ``asset_class == "etf"`` regardless of sector / market-cap / beta,
    and still flows ADRs through the regular equity decision tree.
3.  ``ScreeningEngine._compute_risk_score`` blends the present components,
    renormalizes weights when inputs are missing, and honors the
    ETF short-circuit for leverage / profitability / short_pressure.

The goal is to pin the contract so later changes to the underlying
formulas (e.g. tweaking the volatility window or the baseline floors)
don't silently break downstream UI / API consumers that expect a
risk_score in ``[0, 100]`` with a dict of components.
"""
import unittest

import pandas as pd


class TestClassifyAssetClass(unittest.TestCase):
    def test_etf_via_quote_type(self):
        from tradingagents.screening.ticker_resolver import classify_asset_class
        self.assertEqual(classify_asset_class({"quoteType": "ETF"}), "etf")
        self.assertEqual(classify_asset_class({"quoteType": "MUTUALFUND"}), "etf")
        self.assertEqual(classify_asset_class({"quoteType": "CLOSEDENDFUND"}), "etf")
        self.assertEqual(
            classify_asset_class({"quoteType": "EQUITY", "longName": "SPAC and New Issue ETF"}),
            "etf",
        )

    def test_adr_via_country(self):
        from tradingagents.screening.ticker_resolver import classify_asset_class
        self.assertEqual(
            classify_asset_class({"quoteType": "EQUITY", "country": "Japan"}),
            "adr",
        )
        self.assertEqual(
            classify_asset_class({"quoteType": "EQUITY", "country": "United Kingdom"}),
            "adr",
        )

    def test_adr_via_long_name(self):
        from tradingagents.screening.ticker_resolver import classify_asset_class
        self.assertEqual(
            classify_asset_class({
                "quoteType": "EQUITY",
                "longName": "Taiwan Semi American Depositary Receipt",
            }),
            "adr",
        )

    def test_us_equity_stays_equity(self):
        from tradingagents.screening.ticker_resolver import classify_asset_class
        self.assertEqual(
            classify_asset_class({"quoteType": "EQUITY", "country": "United States"}),
            "equity",
        )
        # Common US token variants should also be treated as domestic.
        self.assertEqual(
            classify_asset_class({"quoteType": "EQUITY", "country": "USA"}),
            "equity",
        )

    def test_missing_quote_type_is_unknown(self):
        from tradingagents.screening.ticker_resolver import classify_asset_class
        self.assertEqual(classify_asset_class({}), "unknown")
        self.assertEqual(classify_asset_class({"quoteType": ""}), "unknown")
        self.assertEqual(classify_asset_class(None), "unknown")


class TestResolveProfileAndPreset(unittest.TestCase):
    def test_etf_short_circuits_to_technical_preset(self):
        from tradingagents.screening.ticker_resolver import resolve_profile_and_preset
        profile, preset = resolve_profile_and_preset(
            sector="Technology",
            market_cap_tier="mega",
            is_profitable=True,
            has_dividend=False,
            beta_tier="medium",
            asset_class="etf",
        )
        self.assertEqual(preset, "etf_technical")
        self.assertEqual(profile, "etf_baseline")

    def test_etf_short_circuit_ignores_sector_signal(self):
        # Regardless of sector/beta/profitability, an ETF must end up on
        # the etf_technical preset — the short-circuit is the whole point.
        from tradingagents.screening.ticker_resolver import resolve_profile_and_preset
        for sector in ("Energy", "Healthcare", "Financial Services", "Utilities"):
            _, preset = resolve_profile_and_preset(
                sector=sector,
                market_cap_tier="small",
                is_profitable=False,
                has_dividend=True,
                beta_tier="extreme",
                asset_class="etf",
            )
            self.assertEqual(preset, "etf_technical", f"sector={sector}")

    def test_adr_flows_through_normal_equity_tree(self):
        # ADRs behave like equities (they have PE, analyst coverage, etc.),
        # so they should NOT land on etf_technical.
        from tradingagents.screening.ticker_resolver import resolve_profile_and_preset
        _, preset = resolve_profile_and_preset(
            sector="Technology",
            market_cap_tier="mega",
            is_profitable=True,
            has_dividend=False,
            beta_tier="medium",
            asset_class="adr",
        )
        self.assertNotEqual(preset, "etf_technical")

    def test_marine_shipping_is_cyclical_even_when_sector_is_industrials(self):
        from tradingagents.screening.ticker_resolver import resolve_profile_and_preset

        profile, preset = resolve_profile_and_preset(
            sector="Industrials",
            industry="Marine Shipping",
            market_cap_tier="mid",
            is_profitable=True,
            has_dividend=True,
            beta_tier="unknown",
        )
        self.assertEqual(profile, "commodity_cyclical")
        self.assertEqual(preset, "commodity_cyclical")

    def test_airline_is_not_dividend_income(self):
        from tradingagents.screening.ticker_resolver import resolve_profile_and_preset

        profile, preset = resolve_profile_and_preset(
            sector="Industrials",
            industry="Airlines",
            market_cap_tier="large",
            is_profitable=True,
            has_dividend=True,
            beta_tier="high",
        )
        self.assertEqual(profile, "large_cap_core")
        self.assertEqual(preset, "value_fisher")
        self.assertNotEqual(profile, "dividend_income")


class TestRiskScoreComputation(unittest.TestCase):
    """The risk score must always return a number in [0,100] and must never
    crash on partial data. These tests hammer the edges: empty price
    series, ETF short-circuit, and missing info dict."""

    def _engine(self):
        from tradingagents.screening.engine import ScreeningEngine
        # Bypass __init__ so we don't need a DB/config — only the static
        # helpers and _compute_risk_score (which doesn't read self state).
        return ScreeningEngine.__new__(ScreeningEngine)

    def _make_close(self, values):
        idx = pd.date_range(end="2024-01-01", periods=len(values), freq="D")
        return pd.Series(values, index=idx)

    def test_equity_full_data_returns_number_in_range(self):
        engine = self._engine()
        # 260 days of mild 1% daily noise around a $100 base
        import random
        random.seed(42)
        prices = [100.0]
        for _ in range(259):
            prices.append(prices[-1] * (1.0 + random.uniform(-0.01, 0.01)))
        close = self._make_close(prices)
        info = {
            "beta": 1.2,
            "averageVolume": 5_000_000,
            "debtToEquity": 80.0,
            "profitMargins": 0.15,
            "shortPercentOfFloat": 0.02,
        }
        score, components = engine._compute_risk_score(close, info, "equity")
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)
        # All equity components should be present given the fat info dict.
        for key in ("volatility", "drawdown", "liquidity", "beta",
                    "leverage", "profitability", "short_pressure",
                    "asset_class_baseline"):
            self.assertIn(key, components, f"missing component: {key}")

    def test_etf_skips_equity_only_components(self):
        engine = self._engine()
        prices = [100.0 + i * 0.1 for i in range(260)]
        close = self._make_close(prices)
        # Even though we supply a D/E and profit margin, ETF handling
        # should refuse to include them (these fields are not meaningful
        # for pooled vehicles, and yfinance's ETF values are misleading).
        info = {
            "beta": 1.0,
            "averageVolume": 80_000_000,
            "debtToEquity": 500.0,
            "profitMargins": -0.30,
            "shortPercentOfFloat": 0.50,
        }
        _, components = engine._compute_risk_score(close, info, "etf")
        self.assertNotIn("leverage", components)
        self.assertNotIn("profitability", components)
        self.assertNotIn("short_pressure", components)
        # But technical + baseline components must still fire.
        self.assertIn("volatility", components)
        self.assertIn("drawdown", components)
        self.assertIn("liquidity", components)
        self.assertIn("beta", components)
        self.assertIn("asset_class_baseline", components)

    def test_missing_info_still_returns_baseline(self):
        # An empty info dict means beta/leverage/etc. can't be computed,
        # but the asset-class baseline alone must carry the score through.
        engine = self._engine()
        close = self._make_close([100.0] * 260)  # flat line → zero vol
        score, components = engine._compute_risk_score(close, {}, "equity")
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)
        self.assertIn("asset_class_baseline", components)

    def test_asset_class_baseline_floor(self):
        # ETF baseline (20) < ADR baseline (40) < equity baseline (50).
        # If price data is totally missing, the score should track the
        # baseline for that asset class (the only present component).
        from tradingagents.screening.engine import ScreeningEngine
        self.assertEqual(ScreeningEngine._risk_asset_class_baseline("etf"), 20.0)
        self.assertEqual(ScreeningEngine._risk_asset_class_baseline("adr"), 40.0)
        self.assertEqual(ScreeningEngine._risk_asset_class_baseline("equity"), 50.0)
        self.assertEqual(
            ScreeningEngine._risk_asset_class_baseline("unknown"),
            55.0,
            "unknown tickers should carry an uncertainty premium",
        )

    def test_score_stays_within_range_on_extreme_inputs(self):
        # Crash all-in: 90% drawdown, 80% annualized vol, tiny liquidity,
        # huge leverage, deep net loss, massive short interest.
        engine = self._engine()
        prices = [100.0] * 200 + [10.0] * 60  # -90% cliff drop
        close = self._make_close(prices)
        info = {
            "beta": 3.5,
            "averageVolume": 1_000,        # ~$10k/day dollar volume
            "debtToEquity": 800.0,
            "profitMargins": -0.80,
            "shortPercentOfFloat": 0.50,
        }
        score, _ = engine._compute_risk_score(close, info, "equity")
        self.assertLessEqual(score, 100.0)
        self.assertGreaterEqual(score, 0.0)
        # And a boring-ETF ceiling: should be meaningfully lower.
        bland_prices = [100.0 + i * 0.01 for i in range(260)]
        bland_score, _ = engine._compute_risk_score(
            self._make_close(bland_prices),
            {"beta": 1.0, "averageVolume": 100_000_000},
            "etf",
        )
        self.assertLess(bland_score, score)


class TestSparseFundamentalsIncludesETF(unittest.TestCase):
    """``_has_sparse_fundamentals`` must treat ETFs as sparse so the engine
    skips the expensive Tier-2 auxiliary fetches (analyst ratings,
    estimate revisions, options chain, etc.) that don't exist for ETFs."""

    def _engine(self):
        from tradingagents.screening.engine import ScreeningEngine
        return ScreeningEngine.__new__(ScreeningEngine)

    def test_etf_quote_type_flags_sparse(self):
        engine = self._engine()
        self.assertTrue(engine._has_sparse_fundamentals({"quoteType": "ETF"}))
        self.assertTrue(engine._has_sparse_fundamentals({"quoteType": "MUTUALFUND"}))
        self.assertTrue(engine._has_sparse_fundamentals({"quoteType": "CLOSEDENDFUND"}))

    def test_healthy_equity_does_not_flag_sparse(self):
        engine = self._engine()
        info = {
            "quoteType": "EQUITY",
            "trailingPE": 25.3,
            "forwardPE": 22.1,
            "priceToBook": 5.0,
            "enterpriseValue": 1_000_000_000,
            "profitMargins": 0.20,
        }
        self.assertFalse(engine._has_sparse_fundamentals(info))


if __name__ == "__main__":
    unittest.main()
