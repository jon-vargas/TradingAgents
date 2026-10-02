"""Discovery hopper: templates, listing gates, overlap, prompt packs."""
import unittest

from tradingagents.dataflows.perplexity_api import _extract_json_array
from tradingagents.screening.discovery import (
    DISCOVERY_TEMPLATES,
    VALID_PROMPT_PACKS,
    annotate_desk_overlap,
    build_discovery_prompt,
    build_watchlist_ticker_index,
    evaluate_book_fit,
    evaluate_listing,
    extract_ticker_candidates,
    get_discovery_template,
    infer_fit_policy,
    is_plausible_ticker,
    list_discovery_templates,
    market_cap_in_bounds,
    normalize_dividend_yield,
    parse_market_cap_filter,
)
from tradingagents.screening.theme_detector import _THEME_TEMPLATES


class TestDiscoveryCatalog(unittest.TestCase):
    def test_templates_use_visible_books(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        presets = DEFAULT_CONFIG["screening"]["presets"]
        visible = {k for k, v in presets.items() if v.get("operator_visible")}
        for tmpl in DISCOVERY_TEMPLATES:
            self.assertIn(tmpl.preset, visible, tmpl.id)
            self.assertIn(tmpl.prompt_pack, VALID_PROMPT_PACKS, tmpl.id)
            self.assertIn(tmpl.asset_policy, {"equity", "etf", "adr", "any"})
            self.assertIn(tmpl.fit_policy, {"", "household", "income", "etf", "momentum"}, tmpl.id)

    def test_templates_cover_visible_books(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        presets = DEFAULT_CONFIG["screening"]["presets"]
        visible = {k for k, v in presets.items() if v.get("operator_visible")}
        covered = {t.preset for t in DISCOVERY_TEMPLATES}
        self.assertFalse(
            visible - covered,
            f"operator-visible books without a Discover chip: {sorted(visible - covered)}",
        )

    def test_get_and_list(self):
        listed = list_discovery_templates()
        self.assertGreaterEqual(len(listed), 8)
        self.assertEqual(listed[0]["id"], DISCOVERY_TEMPLATES[0].id)
        self.assertIsNone(get_discovery_template("nope"))
        self.assertEqual(get_discovery_template("squeeze_setups").preset, "short_squeeze")
        self.assertEqual(get_discovery_template("micro_momentum").market_cap_filter, "over $25M under $300M")
        self.assertEqual(get_discovery_template("small_cap_momentum").market_cap_filter, "over $500M under $2B")
        self.assertEqual(get_discovery_template("mid_cap_momentum").market_cap_filter, "over $2B under $8B")
        self.assertEqual(get_discovery_template("biotech_catalysts").market_cap_filter, "over $25M under $5B")
        self.assertEqual(get_discovery_template("value_revisions").fit_policy, "household")
        self.assertEqual(get_discovery_template("adr_leaders").fit_policy, "household")
        self.assertEqual(get_discovery_template("energy_producers").sector_allowlist, ("Energy",))
        self.assertIsNone(get_discovery_template("small_mid_momentum"))
        self.assertEqual(get_discovery_template("smart_money_flow").preset, "smart_money_tracker")
        self.assertEqual(get_discovery_template("dividend_growers").fit_policy, "income")

    def test_auto_discovery_presets_are_screening_keys(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        presets = DEFAULT_CONFIG["screening"]["presets"]
        for tmpl in _THEME_TEMPLATES:
            self.assertIn(tmpl.preset, presets, tmpl.id)


class TestMarketCapAndTickers(unittest.TestCase):
    def test_parse_market_cap_filter(self):
        self.assertIsNone(parse_market_cap_filter(""))
        self.assertIsNone(parse_market_cap_filter("any"))
        self.assertEqual(parse_market_cap_filter("under $500M"), (None, 500_000_000.0))
        self.assertEqual(parse_market_cap_filter("under $2B"), (None, 2_000_000_000.0))
        self.assertEqual(parse_market_cap_filter("over $10B")[0], 10_000_000_000.0)
        self.assertEqual(
            parse_market_cap_filter("over $50M under $500M"),
            (50_000_000.0, 500_000_000.0),
        )
        self.assertEqual(
            parse_market_cap_filter("over $500M under $2B"),
            (500_000_000.0, 2_000_000_000.0),
        )
        self.assertEqual(
            parse_market_cap_filter("$500M-$2B"),
            (500_000_000.0, 2_000_000_000.0),
        )
        micro = parse_market_cap_filter("over $50M under $500M")
        self.assertTrue(market_cap_in_bounds(80_000_000.0, micro))  # GANX/UMAC-class
        self.assertFalse(market_cap_in_bounds(20_000_000.0, micro))
        self.assertFalse(market_cap_in_bounds(5_100_000_000.0, micro))  # ACHR-class
        self.assertTrue(
            market_cap_in_bounds(5_100_000_000.0, parse_market_cap_filter("over $2B under $8B"))
        )
        self.assertFalse(
            market_cap_in_bounds(9_400_000_000.0, parse_market_cap_filter("over $2B under $8B"))
        )
        self.assertTrue(market_cap_in_bounds(1.2e9, parse_market_cap_filter("under $2B")))
        self.assertFalse(market_cap_in_bounds(3e9, parse_market_cap_filter("under $2B")))
        self.assertFalse(market_cap_in_bounds(1e9, parse_market_cap_filter("over $2B")))

    def test_plausible_ticker_and_regex_harvest(self):
        self.assertTrue(is_plausible_ticker("AAPL"))
        self.assertTrue(is_plausible_ticker("BRK.B"))
        self.assertTrue(is_plausible_ticker("AI"))  # C3.ai — valid listing, only skipped in regex harvest
        self.assertFalse(is_plausible_ticker("TOOLONG"))
        text = "Look at NVDA and NASDAQ plus THE AI STOCK story for SMCI."
        found = extract_ticker_candidates(text, max_results=10)
        self.assertIn("NVDA", found)
        self.assertIn("SMCI", found)
        self.assertNotIn("NASDAQ", found)
        self.assertNotIn("AI", found)
        self.assertNotIn("STOCK", found)
        self.assertNotIn("THE", found)


class TestListingGates(unittest.TestCase):
    def test_equity_pass_and_otc_reject(self):
        ok = evaluate_listing(
            {"shortName": "Apple", "quoteType": "EQUITY", "exchange": "NMS", "marketCap": 3e12},
            asset_policy="equity",
        )
        self.assertTrue(ok["ok"])
        otc = evaluate_listing(
            {"shortName": "Shell Co", "quoteType": "EQUITY", "exchange": "PNK", "marketCap": 8e6},
            asset_policy="equity",
        )
        self.assertFalse(otc["ok"])
        self.assertIn("OTC", otc["reason"])

    def test_etf_policy_and_mcap_filter(self):
        etf = evaluate_listing(
            {"shortName": "Sector SPDR", "quoteType": "ETF", "exchange": "PCX", "marketCap": 4e10},
            asset_policy="etf",
        )
        self.assertTrue(etf["ok"])
        equity_as_etf = evaluate_listing(
            {"shortName": "Apple", "quoteType": "EQUITY", "exchange": "NMS", "marketCap": 3e12},
            asset_policy="etf",
        )
        self.assertFalse(equity_as_etf["ok"])
        too_big = evaluate_listing(
            {"shortName": "Mega", "quoteType": "EQUITY", "exchange": "NYQ", "marketCap": 80e9},
            asset_policy="equity",
            mcap_bounds=parse_market_cap_filter("under $10B"),
        )
        self.assertFalse(too_big["ok"])
        self.assertEqual(too_big["reason"], "outside market-cap filter")
        domestic = evaluate_listing(
            {
                "shortName": "AMD",
                "quoteType": "EQUITY",
                "exchange": "NMS",
                "marketCap": 8e11,
                "country": "United States",
            },
            asset_policy="adr",
        )
        self.assertFalse(domestic["ok"])
        self.assertIn("domestic", domestic["reason"])
        adr = evaluate_listing(
            {
                "shortName": "UMC",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 2e10,
                "country": "Taiwan",
            },
            asset_policy="adr",
        )
        self.assertTrue(adr["ok"])

    def test_ranged_cap_rejects_ceiling_hug(self):
        mid = parse_market_cap_filter("over $500M under $2B")
        hug = evaluate_listing(
            {"shortName": "Worthington", "quoteType": "EQUITY", "exchange": "NYQ", "marketCap": 1.93e9},
            asset_policy="equity",
            mcap_bounds=mid,
        )
        self.assertFalse(hug["ok"])
        self.assertEqual(hug["reason"], "hugging top of cap band")
        interior = evaluate_listing(
            {"shortName": "Interior", "quoteType": "EQUITY", "exchange": "NMS", "marketCap": 1.2e9},
            asset_policy="equity",
            mcap_bounds=mid,
        )
        self.assertTrue(interior["ok"])
        # Open-ended "under $10B" has no floor, so hug does not apply.
        under_only = evaluate_listing(
            {"shortName": "NearCeiling", "quoteType": "EQUITY", "exchange": "NMS", "marketCap": 9.5e9},
            asset_policy="equity",
            mcap_bounds=parse_market_cap_filter("under $10B"),
        )
        self.assertTrue(under_only["ok"])

    def test_missing_exchange_still_ok_for_us_equity(self):
        gate = evaluate_listing(
            {"shortName": "Foo", "quoteType": "EQUITY", "marketCap": 2e9},
            asset_policy="equity",
        )
        self.assertTrue(gate["ok"])


class TestOverlapAndPrompt(unittest.TestCase):
    def test_overlap_index(self):
        index = build_watchlist_ticker_index([
            {"name": "NASDAQ 100", "tickers": "AAPL, MSFT, NVDA"},
            {"name": "My Watchlist", "tickers": "AAPL, XYZ"},
        ])
        rows = annotate_desk_overlap(
            [{"ticker": "aapl", "validated": True}, {"ticker": "NEWCO", "validated": True}],
            index,
        )
        self.assertTrue(rows[0]["on_desk"])
        self.assertEqual(rows[0]["novelty"], "on_desk")
        self.assertIn("NASDAQ 100", rows[0]["desk_watchlists"])
        self.assertFalse(rows[1]["on_desk"])
        self.assertEqual(rows[1]["novelty"], "new")

    def test_prompt_pack_mentions_json_array(self):
        system, user = build_discovery_prompt(
            theme="AI infrastructure",
            criteria="enterprise wins",
            market_cap_filter="under $10B",
            prompt_pack="equity_search",
        )
        self.assertIn("JSON array", system)
        self.assertIn("under $10B", user)
        self.assertIn("AI infrastructure", user)
        self.assertIn("household mega-caps", user)
        system_m, user_m = build_discovery_prompt(
            theme="small and mid-cap early momentum",
            market_cap_filter="under $10B",
            prompt_pack="momentum_breakout",
        )
        self.assertIn("early tape strength", system_m)
        self.assertIn("already doubled", user_m)
        self.assertIn("never pad", user_m)

    def test_extract_json_array_ignores_sources_suffix(self):
        raw = '[{"ticker":"AAPL","company":"Apple"}]\n\nSources:\n- Note [1]'
        body = raw.split("\n\nSources:", 1)[0]
        parsed = _extract_json_array(body)
        self.assertEqual(parsed[0]["ticker"], "AAPL")


class TestDiscoverOpportunitiesMocked(unittest.TestCase):
    def test_json_path_applies_listing_gates(self):
        from unittest.mock import patch
        from tradingagents.dataflows import perplexity_api as px

        payload = (
            '[{"ticker":"VECO","company":"Veeco","rationale":"theme match"},'
            '{"ticker":"PINK","company":"Pink","rationale":"otc"}]\n\nSources:\n- x'
        )
        infos = {
            "VECO": {"shortName": "Veeco", "quoteType": "EQUITY", "exchange": "NMS", "marketCap": 3e9},
            "PINK": {"shortName": "Pink", "quoteType": "EQUITY", "exchange": "PNK", "marketCap": 8e6},
        }

        with patch.object(px, "_make_request", return_value=payload), patch(
            "tradingagents.dataflows.yfinance_extended.get_ticker_info",
            side_effect=lambda sym: infos.get(sym, {}),
        ):
            result = px.discover_opportunities(
                theme="US mega-cap software",
                max_results=10,
                asset_policy="equity",
            )
        by_ticker = {r["ticker"]: r for r in result["tickers"]}
        self.assertTrue(by_ticker["VECO"]["validated"])
        self.assertFalse(by_ticker["PINK"]["validated"])
        self.assertIn("OTC", by_ticker["PINK"]["rejected_reason"])
        self.assertFalse(result["used_regex_fallback"])


class TestBookFit(unittest.TestCase):
    def test_income_rejects_nvda_and_low_yield(self):
        nvda = evaluate_book_fit(
            "NVDA",
            {"shortName": "NVIDIA", "dividendYield": 0.0003},
            fit_policy="income",
        )
        self.assertFalse(nvda["ok"])
        self.assertIn("token-dividend", nvda["reason"])
        thin = evaluate_book_fit(
            "MSFT",
            {"shortName": "Microsoft", "dividendYield": 0.006},
            fit_policy="income",
        )
        self.assertFalse(thin["ok"])
        self.assertIn("yield below", thin["reason"])
        pg = evaluate_book_fit(
            "PG",
            {"shortName": "Procter", "dividendYield": 0.024},
            fit_policy="income",
        )
        self.assertTrue(pg["ok"])

    def test_household_and_etf_fit(self):
        self.assertFalse(evaluate_book_fit("AAPL", {}, fit_policy="household")["ok"])
        self.assertFalse(evaluate_book_fit("TSM", {}, fit_policy="household")["ok"])
        self.assertFalse(evaluate_book_fit("ABBV", {}, fit_policy="household")["ok"])
        software = evaluate_book_fit(
            "RPD",
            {"shortName": "Rapid7", "sector": "Technology"},
            fit_policy="household",
            sector_allowlist=("Energy",),
        )
        self.assertFalse(software["ok"])
        self.assertIn("sector", software["reason"])
        eandp = evaluate_book_fit(
            "MTDR",
            {"shortName": "Matador", "sector": "Energy"},
            fit_policy="household",
            sector_allowlist=("Energy",),
        )
        self.assertTrue(eandp["ok"])
        self.assertTrue(evaluate_book_fit("APLD", {}, fit_policy="household")["ok"])
        self.assertTrue(evaluate_book_fit("AAPL", {}, fit_policy="")["ok"])
        tqqq = evaluate_book_fit(
            "TQQQ",
            {"shortName": "ProShares UltraPro QQQ"},
            fit_policy="etf",
        )
        self.assertFalse(tqqq["ok"])
        smh = evaluate_book_fit(
            "SMH",
            {"shortName": "VanEck Semiconductor ETF"},
            fit_policy="etf",
        )
        self.assertTrue(smh["ok"])

    def test_momentum_rejects_extended_and_snap(self):
        self.assertFalse(evaluate_book_fit("SNAP", {}, fit_policy="momentum")["ok"])
        self.assertFalse(evaluate_book_fit("AAPL", {}, fit_policy="momentum")["ok"])
        early = evaluate_book_fit(
            "MTLS",
            {
                "shortName": "Materialise",
                "fiftyTwoWeekChange": 0.18,
                "currentPrice": 10.0,
                "fiftyTwoWeekHigh": 11.0,
            },
            fit_policy="momentum",
        )
        self.assertTrue(early["ok"])
        qmco = evaluate_book_fit(
            "QMCO",
            {
                "shortName": "Quantum",
                "fiftyTwoWeekChange": 2.10,
                "currentPrice": 20.0,
                "fiftyTwoWeekHigh": 20.1,
            },
            fit_policy="momentum",
        )
        self.assertFalse(qmco["ok"])
        self.assertEqual(qmco["reason"], "already extended")
        washed = evaluate_book_fit(
            "SWMR",
            {
                "shortName": "Swarmer",
                "fiftyTwoWeekChange": 0.40,
                "currentPrice": 5.0,
                "fiftyTwoWeekHigh": 10.0,
            },
            fit_policy="momentum",
        )
        self.assertTrue(washed["ok"])
        reit = evaluate_book_fit(
            "STRW",
            {"shortName": "Strawberry Fields REIT, Inc.", "industry": "REIT - Healthcare Facilities"},
            fit_policy="momentum",
        )
        self.assertFalse(reit["ok"])
        self.assertEqual(reit["reason"], "REIT")
        cef = evaluate_book_fit(
            "PFN",
            {"shortName": "PIMCO Income Strategy Fund II", "industry": "Asset Management", "quoteType": "EQUITY"},
            fit_policy="momentum",
        )
        self.assertFalse(cef["ok"])
        self.assertEqual(cef["reason"], "fund/CEF/ETF")
        self.assertEqual(
            infer_fit_policy(None, prompt_pack="momentum_breakout"),
            "momentum",
        )

    def test_yield_normalization_and_infer(self):
        self.assertAlmostEqual(normalize_dividend_yield(0.024), 0.024)
        self.assertAlmostEqual(normalize_dividend_yield(2.4), 0.024)
        self.assertIsNone(normalize_dividend_yield(None))
        self.assertEqual(
            infer_fit_policy(get_discovery_template("dividend_growers")),
            "income",
        )
        self.assertEqual(
            infer_fit_policy(None, prompt_pack="equity_search", asset_policy="equity"),
            "household",
        )
        self.assertEqual(
            infer_fit_policy(
                None,
                prompt_pack="income_quality",
                theme="High-quality compounders with stable revisions",
            ),
            "",
        )
        self.assertEqual(
            infer_fit_policy(
                None,
                prompt_pack="income_quality",
                theme="Dividend growers with covered payouts",
            ),
            "income",
        )

    def test_json_path_applies_income_fit(self):
        from unittest.mock import patch
        from tradingagents.dataflows import perplexity_api as px

        payload = '[{"ticker":"NVDA","company":"NVIDIA","rationale":"grower"},{"ticker":"PG","company":"P&G","rationale":"aristocrat"}]'
        infos = {
            "NVDA": {
                "shortName": "NVIDIA",
                "quoteType": "EQUITY",
                "exchange": "NMS",
                "marketCap": 5e12,
                "dividendYield": 0.0003,
            },
            "PG": {
                "shortName": "Procter",
                "quoteType": "EQUITY",
                "exchange": "NYQ",
                "marketCap": 3e11,
                "dividendYield": 0.024,
            },
        }
        with patch.object(px, "_make_request", return_value=payload), patch(
            "tradingagents.dataflows.yfinance_extended.get_ticker_info",
            side_effect=lambda sym: infos.get(sym, {}),
        ):
            result = px.discover_opportunities(
                theme="Dividend growers with covered payouts",
                max_results=10,
                prompt_pack="income_quality",
                asset_policy="equity",
                template_id="dividend_growers",
            )
        by_ticker = {r["ticker"]: r for r in result["tickers"]}
        self.assertEqual(result["fit_policy"], "income")
        self.assertFalse(by_ticker["NVDA"]["validated"])
        self.assertIn("token-dividend", by_ticker["NVDA"]["rejected_reason"])
        self.assertTrue(by_ticker["PG"]["validated"])


class TestMomentumHopperFlags(unittest.TestCase):
    def test_is_momentum_discovery_context(self):
        from tradingagents.screening.discovery import is_momentum_discovery_context

        self.assertTrue(is_momentum_discovery_context("micro_momentum"))
        self.assertTrue(is_momentum_discovery_context("", fit_policy="momentum"))
        self.assertTrue(is_momentum_discovery_context("", prompt_pack="momentum_breakout"))
        self.assertFalse(is_momentum_discovery_context("dividend_growers"))
        self.assertFalse(is_momentum_discovery_context("squeeze_setups"))

    def test_annotate_momentum_hopper_flags_skips_invalid(self):
        from tradingagents.screening.discovery import annotate_momentum_hopper_flags

        rows = annotate_momentum_hopper_flags([
            {"ticker": "BAD", "validated": False},
            {"ticker": "", "validated": True},
        ])
        self.assertNotIn("momentum_hopper", rows[0])
        self.assertNotIn("momentum_hopper", rows[1])

    def test_annotate_momentum_hopper_flags_mocks(self):
        from unittest.mock import patch

        from tradingagents.screening.discovery import annotate_momentum_hopper_flags

        fake_info = {
            "regularMarketPrice": 12.0,
            "averageVolume": 2_000_000,
            "marketCap": 800_000_000,
            "exchange": "NMS",
            "floatShares": 40_000_000,
        }
        fake_enrich = {
            "form4_p_buy_count": 2,
            "binary_event_within_days": 3,
            "runway_months": 14.0,
            "going_concern": False,
        }
        with patch("tradingagents.dataflows.yfinance_extended.get_ticker_info", return_value=fake_info):
            with patch("tradingagents.screening.early_momentum_enrich.enrich_ticker", return_value=fake_enrich):
                rows = annotate_momentum_hopper_flags([{"ticker": "JOBY", "validated": True}])
        hop = rows[0]["momentum_hopper"]
        self.assertTrue(hop["gate_pass"])
        self.assertEqual(hop["form4_p_buy_count"], 2)
        self.assertEqual(hop["binary_event_within_days"], 3)
        self.assertAlmostEqual(hop["runway_months"], 14.0)


class TestWatchlistTickerHygiene(unittest.TestCase):
    def test_spaced_token_is_rejected_not_glued(self):
        from tradingagents.screening.discovery import parse_watchlist_tickers

        valid, rejected = parse_watchlist_tickers("AAPL, NB SYY, MSFT")
        self.assertEqual(valid, ["AAPL", "MSFT"])
        self.assertEqual(rejected, ["NB SYY"])
        self.assertNotIn("NBSYY", valid)

    def test_implausible_mega_skips_household_names(self):
        from tradingagents.screening.discovery import is_implausible_mega_identity

        self.assertFalse(is_implausible_mega_identity("AAPL", 3.5e12))
        self.assertFalse(is_implausible_mega_identity("MU", 1.10e12))
        self.assertFalse(is_implausible_mega_identity("BRK-B", 1.06e12))
        self.assertFalse(is_implausible_mega_identity("BRK.B", 1.06e12))
        self.assertTrue(is_implausible_mega_identity("SPCX", 1.76e12))
        self.assertTrue(is_implausible_mega_identity("SKHY", 1.16e12))
        self.assertFalse(is_implausible_mega_identity("PGY", 2e9))

    def test_small_cap_growth_aliases_to_momentum_hunter(self):
        from tradingagents.screening.discovery import canonical_preset_name

        self.assertEqual(canonical_preset_name("small_cap_growth"), "momentum_hunter")
        self.assertEqual(canonical_preset_name("momentum_hunter"), "momentum_hunter")


if __name__ == "__main__":
    unittest.main()
