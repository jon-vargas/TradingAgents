import copy
import os
import tempfile
import unittest


class TestWatchlistUniverseRestructure(unittest.TestCase):
    def test_builtin_registry_contains_phase_indexes(self):
        from tradingagents.reporting.database import ResearchDatabase

        names = {row.get("name"): row for row in ResearchDatabase._builtin_watchlist_registry()}
        self.assertIn("S&P 500", names)
        self.assertIn("S&P 400", names)
        self.assertIn("Russell 2000", names)
        self.assertTrue(bool(names["S&P 500"].get("registry_backed")))

    def test_healthcare_life_sciences_builtin_registry(self):
        from tradingagents.reporting.database import ResearchDatabase

        names = {row.get("name"): row for row in ResearchDatabase._builtin_watchlist_registry()}
        self.assertIn("Healthcare & Life Sciences", names)
        row = names["Healthcare & Life Sciences"]
        tickers = [t.strip().upper() for t in (row.get("tickers") or "").split(",") if t.strip()]
        self.assertGreaterEqual(len(tickers), 100)
        self.assertEqual(len(tickers), len(set(tickers)))
        self.assertEqual(row.get("category"), "curated_thematic")
        for sym in ("UNH", "LLY", "ISRG", "MRNA", "TMO", "WELL"):
            self.assertIn(sym, tickers)

    def test_ai_infrastructure_builtin_is_focus_only(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase

        names = {row.get("name"): row for row in ResearchDatabase._builtin_watchlist_registry()}
        row = names["AI & AI Infrastructure"]
        tickers = [t.strip().upper() for t in (row.get("tickers") or "").split(",") if t.strip()]
        metals = {
            t.strip().upper()
            for t in (names["AI Infrastructure Metals"].get("tickers") or "").split(",")
            if t.strip()
        }
        self.assertEqual(len(tickers), 125)
        self.assertEqual(len(tickers), len(set(tickers)))
        self.assertEqual(row.get("target_size"), 125)
        self.assertEqual(row.get("category"), "curated_thematic")
        self.assertIsNone(row.get("default_preset"))
        for sym in ("NVDA", "TSM", "ASML", "VRT", "EQIX", "DLR", "ANET", "CRWV", "PLTR", "MSFT", "GLW", "LUMN", "BE", "KEYS", "STRL", "CARR", "JCI", "BDC", "SMTC"):
            self.assertIn(sym, tickers)
        for sym in ("FCX", "CCJ", "ALB", "MP", "SMH", "AAPL", "PANW", "PTRN", "VZ"):
            self.assertNotIn(sym, tickers)
        self.assertEqual(sorted(set(tickers) & metals), ["BWXT", "CEG", "OKLO", "SMR", "VST"])
        excluded = DEFAULT_CONFIG["screening"]["scheduler"]["postclose"]["exclude_watchlist_names"]
        self.assertIn("AI & AI Infrastructure", excluded)
        self.assertNotIn("AI & AI Infrastructure", DEFAULT_CONFIG["screening"]["scheduler"]["postclose"]["priority_watchlist_names"])

    def test_enrich_watchlist_registry_fields_healthcare_category(self):
        from tradingagents.reporting.database import ResearchDatabase

        raw = {
            "id": 99,
            "name": "Healthcare & Life Sciences",
            "source": "built-in",
            "tickers": "UNH,LLY",
        }
        enriched = ResearchDatabase.enrich_watchlist_registry_fields(raw)
        self.assertEqual(enriched.get("category"), "curated_thematic")
        self.assertEqual(enriched.get("cadence"), "monthly_validation")
        self.assertEqual(enriched.get("target_size"), 105)
        user = {"id": 1, "name": "My picks", "source": "user", "tickers": "AAPL"}
        self.assertEqual(ResearchDatabase.enrich_watchlist_registry_fields(user), user)

    def test_broad_index_watchlists_use_adaptive_presets(self):
        from tradingagents.reporting.database import ResearchDatabase

        names = {row.get("name"): row for row in ResearchDatabase._builtin_watchlist_registry()}
        adaptive_indices = (
            "NASDAQ 100",
            "S&P 400",
            "Russell 2000 Top 100",
            "S&P 600",
            "Russell 2000",
        )
        for wl_name in adaptive_indices:
            with self.subTest(watchlist=wl_name):
                row = names[wl_name]
                self.assertIsNone(row.get("default_preset"))
                self.assertEqual(row.get("defaults_version"), "v3")

        self.assertEqual(names["Dividend Aristocrats Top 50"].get("default_preset"), "dividend_income")
        self.assertEqual(names["ETFs - Factor & Style"].get("default_preset"), "etf_technical")

    def test_v3_migration_clears_pinned_index_presets(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="preset_v3_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            with db._connect() as conn:
                conn.execute(
                    """
                    UPDATE watchlists
                    SET default_preset = 'momentum_hunter', defaults_version = 'v2'
                    WHERE source = 'built-in' AND name = 'NASDAQ 100'
                    """
                )
                conn.commit()
            before = db.get_builtin_watchlist_by_name("NASDAQ 100")
            self.assertEqual((before or {}).get("default_preset"), "momentum_hunter")

            db._seed_watchlists()
            after = db.get_builtin_watchlist_by_name("NASDAQ 100")
            self.assertIsNone((after or {}).get("default_preset"))
            self.assertEqual((after or {}).get("defaults_version"), "v3")
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass
    def test_index_constituents_materialize_registry_watchlist(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="registry_materialize_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            saved = db.save_index_constituents(
                index_key="sp500_full",
                rows=[
                    {"ticker": "MSFT", "liquidity_rank": 1, "market_cap": 3_000_000_000_000},
                    {"ticker": "AAPL", "liquidity_rank": 2, "market_cap": 2_900_000_000_000},
                    {"ticker": "NVDA", "liquidity_rank": 3, "market_cap": 2_800_000_000_000},
                ],
                source="seed",
            )
            self.assertEqual(saved, 3)

            result = db.materialize_registry_watchlist("S&P 500", "sp500_full", target_size=2)
            self.assertTrue(result.get("updated"))
            wl = db.get_builtin_watchlist_by_name("S&P 500")
            self.assertIsNotNone(wl)
            self.assertEqual((wl or {}).get("tickers"), "MSFT,AAPL")
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_compute_watchlist_universe_metrics_contract(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="registry_metrics_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            metrics = db.compute_watchlist_universe_metrics()
            self.assertIn("unique_tickers_baseline", metrics)
            self.assertIn("hydration_required_capacity", metrics)
            self.assertIn("hydration_cap_utilization", metrics)
            self.assertIn("top_overlap_pairs", metrics)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_watchlist_parity_diff_contract(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="registry_parity_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            with db._connect() as conn:
                conn.execute(
                    "UPDATE watchlists SET tickers = ? WHERE source = 'built-in' AND name = ?",
                    ("AAPL,MSFT,NVDA", "S&P 500 Top 100"),
                )
                conn.execute(
                    "UPDATE watchlists SET tickers = ? WHERE source = 'built-in' AND name = ?",
                    ("AAPL,MSFT,GOOGL", "S&P 500"),
                )
                conn.commit()
            diff = db.watchlist_parity_diff("S&P 500 Top 100", "S&P 500")
            self.assertEqual(diff.get("left_count"), 3)
            self.assertEqual(diff.get("right_count"), 3)
            self.assertEqual(diff.get("overlap_count"), 2)
            self.assertAlmostEqual(float(diff.get("churn_pct")), round(2 / 3, 4), places=4)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_partial_metadata_save_preserves_resolved_preset(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="partial_meta_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            db.save_ticker_metadata(
                "NVDA",
                sector="Technology",
                market_cap_tier="mega",
                resolved_profile="high_growth",
                resolved_preset="momentum_hunter",
                asset_class="equity",
            )
            # Simulate engine ADV liquidity partial write (sector + ADV only).
            db.save_ticker_metadata(
                "NVDA",
                sector="Technology",
                avg_dollar_volume_usd=12_500_000_000.0,
                asset_class="equity",
            )
            row = db.get_ticker_metadata("NVDA")
            self.assertIsNotNone(row)
            self.assertEqual(row.get("resolved_preset"), "momentum_hunter")
            self.assertEqual(row.get("resolved_profile"), "high_growth")
            self.assertEqual(row.get("market_cap_tier"), "mega")
            self.assertAlmostEqual(float(row.get("avg_dollar_volume_usd") or 0), 12_500_000_000.0)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_get_stale_tickers_treats_incomplete_metadata_as_stale(self):
        from datetime import datetime, timedelta
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="stale_meta_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            now = datetime.now().isoformat()
            with db._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO ticker_metadata
                        (ticker, sector, last_updated, resolved_preset, market_cap_tier)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("PARTIAL", "Technology", now, None, None),
                )
                conn.execute(
                    """
                    INSERT INTO ticker_metadata
                        (ticker, sector, last_updated, resolved_preset, market_cap_tier)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("COMPLETE", "Technology", now, "momentum_hunter", "large"),
                )
                conn.commit()

            stale = db.get_stale_tickers(["PARTIAL", "COMPLETE"], max_age_days=7)
            self.assertIn("PARTIAL", stale)
            self.assertNotIn("COMPLETE", stale)

            old = (datetime.now() - timedelta(days=10)).isoformat()
            with db._connect() as conn:
                conn.execute(
                    "UPDATE ticker_metadata SET last_updated = ? WHERE ticker = ?",
                    (old, "COMPLETE"),
                )
                conn.commit()
            stale_old = db.get_stale_tickers(["COMPLETE"], max_age_days=7)
            self.assertEqual(stale_old, ["COMPLETE"])
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_registry_write_guard_rejects_invalid_source(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="registry_guard_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            with self.assertRaises(ValueError):
                db.save_index_constituents(
                    index_key="sp500_full",
                    rows=[{"ticker": "AAPL", "source": "auto"}],
                    source="auto",
                )
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_long_horizon_cap_sort_and_drop_meta(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        fd, db_path = tempfile.mkstemp(prefix="lh_cap_meta_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            db.save_ticker_metadata("AAA", liquidity_rank=3, market_cap=100)
            db.save_ticker_metadata("BBB", liquidity_rank=1, market_cap=90)
            db.save_ticker_metadata("CCC", liquidity_rank=2, market_cap=80)

            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg["screening"]["long_horizon"]["universe_policy"]["max_universe_size"] = 2
            svc = LongHorizonService(db=db, config=cfg)
            symbols, meta = svc._resolve_universe_inputs(
                watchlist_id=None,
                tickers=["AAA", "BBB", "CCC"],
                use_expanded_universe=False,
            )
            self.assertEqual(symbols, ["BBB", "CCC"])
            self.assertEqual(int(meta.get("dropped_by_cap_count") or 0), 1)
            self.assertIn("AAA", meta.get("dropped_by_cap_sample", []))
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()

