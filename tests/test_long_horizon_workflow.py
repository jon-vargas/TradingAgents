import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi import HTTPException


class TestLongHorizonWorkflow(unittest.TestCase):
    def test_long_horizon_config_guardrails_present(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        cfg = DEFAULT_CONFIG.get("screening", {}).get("long_horizon", {})
        self.assertTrue(cfg.get("manual_only", True))
        self.assertIn("long_6to12m", cfg.get("horizons", {}))
        self.assertIn("long_12to36m", cfg.get("horizons", {}))
        self.assertGreaterEqual(int(cfg.get("underwriting", {}).get("max_underwrite_jobs_per_run", 0)), 1)
        self.assertGreaterEqual(int(cfg.get("idempotency_window_seconds", 0)), 60)
        self.assertIn("execution_profile", cfg)

    def test_long_horizon_runs_filters_strategy_only(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="long_horizon_runs_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            watchlist_id = int(db.create_watchlist(name="LH Runs WL", tickers="AAPL,MSFT", description="test"))
            now = datetime(2026, 3, 24, 20, 0, 0).isoformat()
            with db._connect() as conn:
                conn.execute(
                    "INSERT INTO screening_runs (watchlist_id, run_at, criteria, ticker_count, results_count) VALUES (?, ?, ?, ?, ?)",
                    (watchlist_id, now, json.dumps({"strategy": "long_horizon", "horizon": "long_6to12m"}), 2, 2),
                )
                run1 = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.execute(
                    "INSERT INTO screening_runs (watchlist_id, run_at, criteria, ticker_count, results_count) VALUES (?, ?, ?, ?, ?)",
                    (watchlist_id, now, json.dumps({"strategy": "movers"}), 2, 2),
                )
                conn.commit()
            db.save_long_horizon_run_meta(run1, {"weights_hash": "abc123"})
            rows = db.get_long_horizon_runs(limit=10, offset=0)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["criteria"]["strategy"], "long_horizon")
            self.assertIn("run_meta", rows[0])
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_require_long_horizon_strategy_guard(self):
        from tradingagents.reporting.database import ResearchDatabase
        from webapp.app import _require_long_horizon_strategy_run

        fd, db_path = tempfile.mkstemp(prefix="lh_guard_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            wl_id = int(db.create_watchlist(name="LH Guard WL", tickers="AAA,BBB", description="test"))
            with db._connect() as conn:
                conn.execute(
                    "INSERT INTO screening_runs (watchlist_id, run_at, criteria, ticker_count, results_count) VALUES (?, ?, ?, ?, ?)",
                    (wl_id, datetime(2026, 3, 24, 20, 0, 0).isoformat(), json.dumps({"strategy": "momentum"}), 2, 2),
                )
                run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                conn.commit()
            with self.assertRaises(HTTPException) as ctx:
                _require_long_horizon_strategy_run(db, run_id)
            self.assertEqual(ctx.exception.status_code, 404)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.compute_risk_metrics")
    def test_allocation_is_deterministic(self, mock_risk):
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        mock_risk.return_value = {"beta": 1.0}
        fd, db_path = tempfile.mkstemp(prefix="lh_alloc_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            db.save_ticker_metadata("AAPL", sector="Technology", market_cap_tier="large")
            db.save_ticker_metadata("MSFT", sector="Technology", market_cap_tier="large")
            db.save_ticker_metadata("XOM", sector="Energy", market_cap_tier="large")
            svc = LongHorizonService(db=db)
            rows = [
                {"ticker": "AAPL", "composite_score": 0.9},
                {"ticker": "MSFT", "composite_score": 0.9},
                {"ticker": "XOM", "composite_score": 0.7},
            ]
            out1, alloc1 = svc._apply_risk_overlay_and_allocate(rows, top_n=3)
            out2, alloc2 = svc._apply_risk_overlay_and_allocate(rows, top_n=3)
            self.assertEqual(out1, out2)
            self.assertEqual(alloc1, alloc2)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_expanded_universe_resolution_uses_configured_sources(self):
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        fd, db_path = tempfile.mkstemp(prefix="lh_universe_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            w1 = int(db.create_watchlist(name="Builtins A", tickers="AAPL,MSFT", description=""))
            w2 = int(db.create_watchlist(name="User A", tickers="NVDA,MSFT", description=""))
            w3 = int(db.create_watchlist(name="Movers: 2026-03-30", tickers="TSLA,AMD", description=""))
            with db._connect() as conn:
                conn.execute("UPDATE watchlists SET source = 'built-in' WHERE id = ?", (w1,))
                conn.execute("UPDATE watchlists SET source = 'user' WHERE id = ?", (w2,))
                conn.execute("UPDATE watchlists SET source = 'movers' WHERE id = ?", (w3,))
                conn.commit()

            svc = LongHorizonService(db=db)
            symbols, meta = svc._resolve_universe_inputs(
                watchlist_id=None,
                tickers=None,
                use_expanded_universe=True,
            )
            self.assertTrue({"AAPL", "MSFT", "NVDA"}.issubset(set(symbols)))
            self.assertEqual(meta.get("source"), "expanded_universe")
            self.assertGreaterEqual(int(meta.get("expanded_universe_watchlists_used") or 0), 2)
            self.assertIn("built-in", meta.get("expanded_universe_source_breakdown", {}))
            self.assertNotIn("movers", meta.get("expanded_universe_source_breakdown", {}))
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_underwrite_timeout_returns_degraded_packets(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        fd, db_path = tempfile.mkstemp(prefix="lh_underwrite_timeout_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            wl_id = int(db.create_watchlist(name="LH Underwrite WL", tickers="AAPL,MSFT", description=""))
            run_id = db.save_screening_run(
                watchlist_id=wl_id,
                criteria=json.dumps({"strategy": "long_horizon", "horizon": "long_6to12m"}),
                ticker_count=2,
                results_count=2,
            )
            db.save_screening_results(
                run_id,
                [
                    {"ticker": "AAPL", "composite_score": 52.0, "direction": "bullish", "signals": {}, "rank": 1},
                    {"ticker": "MSFT", "composite_score": 49.0, "direction": "bullish", "signals": {}, "rank": 2},
                ],
            )

            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            uw = cfg["screening"]["long_horizon"]["underwriting"]
            uw["enabled"] = True
            uw["llm_augment_top_k"] = 2
            uw["llm_call_timeout_seconds"] = 1
            uw["underwrite_timeout_seconds"] = 10
            uw["underwrite_retry_budget"] = 0

            svc = LongHorizonService(db=db, config=cfg)

            def _slow_packet(*args, **kwargs):
                time.sleep(2)
                return {
                    "run_id": run_id,
                    "ticker": kwargs["row"]["ticker"],
                    "degraded": False,
                    "error_code": None,
                    "token_estimate": 100,
                    "base_case": "ok",
                    "bull_case": "ok",
                    "bear_case": "ok",
                    "disconfirming_signals": [],
                    "monitoring_checklist": [],
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }

            with patch.object(LongHorizonService, "_build_underwriting_packet", side_effect=_slow_packet):
                result = svc.underwrite_top(run_id=run_id, top_n=2)

            self.assertTrue(result.get("degraded"))
            self.assertEqual(result.get("count"), 2)
            self.assertTrue(any(item.get("error_code") == "dependency_error" for item in result.get("items", [])))
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.compute_risk_metrics")
    @patch("tradingagents.screening.long_horizon.ScreeningEngine")
    @patch("tradingagents.screening.long_horizon.LongHorizonService._apply_universe_policy")
    def test_expanded_universe_uses_two_pass_execution(self, mock_apply_policy, mock_engine_cls, mock_risk):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.engine import ScreeningResult
        from tradingagents.screening.long_horizon import LongHorizonService

        mock_risk.return_value = {"beta": 1.0}
        mock_apply_policy.return_value = (
            ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"],
            [],
        )

        fake_engine = Mock()
        fast_results = [
            ScreeningResult(ticker="NVDA", composite_score=88.0, direction="bullish", signals={}),
            ScreeningResult(ticker="AAPL", composite_score=86.0, direction="bullish", signals={}),
            ScreeningResult(ticker="MSFT", composite_score=82.0, direction="bullish", signals={}),
        ]
        deep_results = [
            ScreeningResult(ticker="NVDA", composite_score=90.0, direction="bullish", signals={}),
            ScreeningResult(ticker="AAPL", composite_score=89.0, direction="bullish", signals={}),
        ]
        fake_engine.scan.side_effect = [fast_results, deep_results]
        mock_engine_cls.return_value = fake_engine

        fd, db_path = tempfile.mkstemp(prefix="lh_two_pass_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            w1 = int(db.create_watchlist(name="Builtins A", tickers="AAPL,MSFT,NVDA,GOOGL,AMZN", description=""))
            with db._connect() as conn:
                conn.execute("UPDATE watchlists SET source = 'built-in' WHERE id = ?", (w1,))
                conn.commit()

            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            exec_cfg = cfg["screening"]["long_horizon"]["execution_profile"]
            exec_cfg["expanded_fast_path_enabled"] = True
            exec_cfg["two_pass_min_universe_size"] = 3
            exec_cfg["fast_pass_top_k"] = 2

            svc = LongHorizonService(db=db, config=cfg)
            out = svc.run_scan(
                horizon="long_6to12m",
                top_n=2,
                watchlist_id=None,
                tickers=None,
                use_expanded_universe=True,
            )
            self.assertEqual(fake_engine.scan.call_count, 2)
            first_kwargs = fake_engine.scan.call_args_list[0].kwargs
            second_kwargs = fake_engine.scan.call_args_list[1].kwargs
            self.assertEqual(len(first_kwargs.get("tickers", [])), 5)
            self.assertEqual(set(second_kwargs.get("tickers", [])), {"NVDA", "AAPL"})
            self.assertEqual(out.get("meta", {}).get("execution_profile", {}).get("mode"), "two_pass")
            self.assertEqual(out.get("meta", {}).get("execution_profile", {}).get("deep_pass_input_count"), 2)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.get_ticker_info")
    @patch("tradingagents.screening.long_horizon.resolve_and_cache")
    def test_universe_policy_prefilters_low_market_cap_before_info(self, mock_resolve_and_cache, mock_get_ticker_info):
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        fd, db_path = tempfile.mkstemp(prefix="lh_policy_prefilter_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            svc = LongHorizonService(db=db)
            mock_resolve_and_cache.return_value = {
                "SMALL": {"market_cap": 500_000_000},
                "LARGE": {"market_cap": 50_000_000_000},
            }
            mock_get_ticker_info.return_value = {
                "quoteType": "EQUITY",
                "exchange": "NMS",
                "longName": "Large Corp",
                "averageVolume": 5_000_000,
                "currentPrice": 120.0,
                "marketCap": 50_000_000_000,
            }

            eligible, excluded = svc._apply_universe_policy(
                tickers=["SMALL", "LARGE"],
                policy={
                    "min_price": 5.0,
                    "min_market_cap": 2_000_000_000,
                    "min_avg_volume": 750_000,
                    "exclude_otc": True,
                    "exclude_etf": True,
                    "allow_adr": False,
                },
            )
            self.assertEqual(eligible, ["LARGE"])
            self.assertTrue(any(e["ticker"] == "SMALL" for e in excluded))
            # SMALL should be filtered via cached metadata prefilter and skip info lookup.
            called_symbols = {c.args[0] for c in mock_get_ticker_info.call_args_list if c.args}
            self.assertNotIn("SMALL", called_symbols)
            self.assertIn("LARGE", called_symbols)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.get_ticker_info")
    @patch("tradingagents.screening.long_horizon.resolve_and_cache")
    def test_universe_policy_excludes_leveraged(self, mock_resolve_and_cache, mock_get_ticker_info):
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        fd, db_path = tempfile.mkstemp(prefix="lh_policy_leveraged_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            svc = LongHorizonService(db=db)
            mock_resolve_and_cache.return_value = {
                "UPRO": {"market_cap": 10_000_000_000},
            }
            mock_get_ticker_info.return_value = {
                "quoteType": "EQUITY",
                "exchange": "NMS",
                "longName": "UltraPro 3x S&P Proxy",
                "averageVolume": 5_000_000,
                "currentPrice": 50.0,
                "marketCap": 10_000_000_000,
            }
            eligible, excluded = svc._apply_universe_policy(
                tickers=["UPRO"],
                policy={
                    "min_price": 5.0,
                    "min_market_cap": 2_000_000_000,
                    "min_avg_volume": 750_000,
                    "exclude_otc": True,
                    "exclude_etf": False,
                    "exclude_leveraged": True,
                    "allow_adr": False,
                },
            )
            self.assertEqual(eligible, [])
            self.assertTrue(any("excluded_leveraged" in (e.get("reason_codes") or []) for e in excluded))
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.compute_risk_metrics")
    def test_allocation_enforces_large_cap_min(self, mock_risk):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        mock_risk.return_value = {"beta": 1.0}
        fd, db_path = tempfile.mkstemp(prefix="lh_alloc_large_min_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            db.save_ticker_metadata("AAA", sector="Technology", market_cap_tier="large")
            db.save_ticker_metadata("BBB", sector="Energy", market_cap_tier="small")
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["screening"]["long_horizon"]["allocation"]["market_cap_band_min"]["large_or_above"] = 0.60
            cfg["screening"]["long_horizon"]["allocation"]["max_single_name_weight"] = 0.95
            cfg["screening"]["long_horizon"]["allocation"]["hard_constraints"] = ["liquidity_floor"]
            svc = LongHorizonService(db=db, config=cfg)
            rows = [
                {"ticker": "AAA", "composite_score": 0.3},
                {"ticker": "BBB", "composite_score": 0.7},
            ]
            out, alloc = svc._apply_risk_overlay_and_allocate(rows, top_n=2)
            large_weight = sum(float(r.get("target_weight", 0)) for r in out if r.get("market_cap_tier") in {"large", "mega"})
            self.assertGreaterEqual(round(large_weight, 4), 0.45)
            self.assertIn("enforced_market_cap_band_min", set(alloc.get("reason_codes", [])))
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.LongHorizonService._build_underwriting_packet")
    def test_underwrite_shared_token_budget_with_concurrency(self, mock_packet):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        fd, db_path = tempfile.mkstemp(prefix="lh_underwrite_budget_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            wl_id = int(db.create_watchlist(name="LH Budget WL", tickers="AAPL,MSFT", description=""))
            run_id = db.save_screening_run(
                watchlist_id=wl_id,
                criteria=json.dumps({"strategy": "long_horizon", "horizon": "long_6to12m"}),
                ticker_count=2,
                results_count=2,
            )
            db.save_screening_results(
                run_id,
                [
                    {"ticker": "AAPL", "composite_score": 52.0, "direction": "bullish", "signals": {}, "rank": 1},
                    {"ticker": "MSFT", "composite_score": 49.0, "direction": "bullish", "signals": {}, "rank": 2},
                ],
            )

            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            uw = cfg["screening"]["long_horizon"]["underwriting"]
            uw["enabled"] = True
            uw["llm_augment_top_k"] = 2
            uw["max_underwrite_concurrency"] = 2
            uw["underwrite_token_budget_per_run"] = 1000
            uw["underwrite_retry_budget"] = 0
            uw["llm_call_timeout_seconds"] = 10

            def _packet(*args, **kwargs):
                return {
                    "run_id": run_id,
                    "ticker": kwargs["row"]["ticker"],
                    "degraded": False,
                    "error_code": None,
                    "token_estimate": 900,
                    "base_case": "ok",
                    "bull_case": "ok",
                    "bear_case": "ok",
                    "disconfirming_signals": [],
                    "monitoring_checklist": [],
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }

            mock_packet.side_effect = _packet
            svc = LongHorizonService(db=db, config=cfg)
            result = svc.underwrite_top(run_id=run_id, top_n=2)
            self.assertEqual(result.get("count"), 2)
            # Shared budget should cap one of the two packets.
            self.assertTrue(any(item.get("error_code") == "budget_exceeded" for item in result.get("items", [])))
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.compute_sector_momentum")
    @patch("tradingagents.screening.long_horizon.get_macro_snapshot")
    def test_regime_snapshot_includes_macro_features(self, mock_macro, mock_sector):
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        mock_macro.return_value = {
            "index_trend": "bull",
            "index_stress": "quiet",
            "market_regime": "bull",
            "index_regime": "bull_quiet",
            "index_regime_label": "Bull / Quiet",
            "credit_stress": "normal",
            "sector_breadth": "healthy",
        }
        mock_sector.return_value = {"top3": ["XLK", "XLI", "XLF"]}

        fd, db_path = tempfile.mkstemp(prefix="lh_regime_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            svc = LongHorizonService(db=db)
            snap = svc._regime_snapshot()
            self.assertEqual(snap.get("market_regime"), "bull")
            self.assertEqual(snap.get("index_regime_label"), "Bull / Quiet")
            self.assertEqual(snap.get("regime"), "bull")
            self.assertEqual(snap.get("credit_stress"), "normal")
            self.assertEqual(snap.get("sector_momentum_top3"), ["XLK", "XLI", "XLF"])
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass


    @patch("tradingagents.screening.long_horizon.compute_risk_metrics")
    def test_unknown_cap_tier_is_not_treated_as_small(self, mock_risk):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        mock_risk.return_value = {"beta": 1.0}
        fd, db_path = tempfile.mkstemp(prefix="lh_unknown_tier_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            db.save_ticker_metadata("XOM", sector="Energy", market_cap=500e9, market_cap_tier=None)
            db.save_ticker_metadata("AAA", sector="Technology", market_cap_tier="small")
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["screening"]["long_horizon"]["allocation"]["market_cap_band_max"]["small_or_below"] = 0.05
            cfg["screening"]["long_horizon"]["allocation"]["max_single_name_weight"] = 0.95
            svc = LongHorizonService(db=db, config=cfg)
            out, alloc = svc._apply_risk_overlay_and_allocate(
                [
                    {"ticker": "XOM", "composite_score": 0.6},
                    {"ticker": "AAA", "composite_score": 0.4},
                ],
                top_n=2,
            )
            xom = next(r for r in out if r["ticker"] == "XOM")
            self.assertEqual(xom.get("market_cap_tier"), "mega")
            self.assertGreater(float(xom.get("target_weight") or 0), 0.05)
            self.assertNotIn("market_cap_band_max", xom.get("constraint_hits") or [])
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.compute_risk_metrics")
    @patch("tradingagents.screening.long_horizon.ScreeningEngine")
    @patch("tradingagents.screening.long_horizon.LongHorizonService._apply_universe_policy")
    def test_run_scan_persists_portfolio_risk(self, mock_apply_policy, mock_engine_cls, mock_risk):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.engine import ScreeningResult
        from tradingagents.screening.long_horizon import LongHorizonService

        mock_risk.return_value = {"beta": 1.0}
        mock_apply_policy.return_value = (["AAPL", "MSFT"], [])
        fake_engine = Mock()
        fake_engine.scan.return_value = [
            ScreeningResult(ticker="AAPL", composite_score=80.0, direction="bullish", signals={"quality_factor": 0.7}),
            ScreeningResult(ticker="MSFT", composite_score=70.0, direction="bullish", signals={"quality_factor": 0.6}),
        ]
        mock_engine_cls.return_value = fake_engine

        fd, db_path = tempfile.mkstemp(prefix="lh_persist_risk_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            db.save_ticker_metadata("AAPL", sector="Technology", market_cap_tier="large")
            db.save_ticker_metadata("MSFT", sector="Technology", market_cap_tier="large")
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["screening"]["long_horizon"]["execution_profile"]["expanded_fast_path_enabled"] = False
            svc = LongHorizonService(db=db, config=cfg)
            out = svc.run_scan(horizon="long_6to12m", top_n=2, tickers=["AAPL", "MSFT"])
            self.assertIn("portfolio_risk", out.get("meta") or {})
            stored = db.get_long_horizon_run_meta(out["run_id"])
            self.assertIn("portfolio_risk", stored.get("meta_json") or {})
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_underwrite_prefers_allocated_weights(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        fd, db_path = tempfile.mkstemp(prefix="lh_uw_alloc_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            wl_id = int(db.create_watchlist(name="LH UW Alloc", tickers="AAPL,MSFT,NVDA", description=""))
            run_id = db.save_screening_run(
                watchlist_id=wl_id,
                criteria=json.dumps({"strategy": "long_horizon", "horizon": "long_6to12m", "preset": "long_horizon_6to12m"}),
                ticker_count=3,
                results_count=3,
            )
            db.save_screening_results(
                run_id,
                [
                    {"ticker": "NVDA", "composite_score": 90.0, "direction": "bullish", "signals": {}, "rank": 1},
                    {"ticker": "AAPL", "composite_score": 70.0, "direction": "bullish", "signals": {}, "rank": 2},
                    {"ticker": "MSFT", "composite_score": 60.0, "direction": "bullish", "signals": {}, "rank": 3},
                ],
            )
            db.save_long_horizon_run_details(run_id, [
                {"ticker": "AAPL", "composite_score": 70.0, "target_weight": 0.08, "sector": "Technology"},
                {"ticker": "MSFT", "composite_score": 60.0, "target_weight": 0.06, "sector": "Technology"},
                {"ticker": "NVDA", "composite_score": 90.0, "target_weight": 0.0, "sector": "Technology"},
            ])
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["screening"]["long_horizon"]["underwriting"]["enabled"] = False
            cfg["screening"]["long_horizon"]["underwriting"]["llm_augment_top_k"] = 2
            svc = LongHorizonService(db=db, config=cfg)
            result = svc.underwrite_top(run_id=run_id, top_n=2)
            tickers = [item["ticker"] for item in result.get("items", [])]
            self.assertEqual(tickers, ["AAPL", "MSFT"])
            self.assertNotIn("NVDA", tickers)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.get_ticker_info")
    @patch("tradingagents.screening.long_horizon.compute_risk_metrics")
    def test_overlay_hydrates_cap_and_excludes_etf_from_info(self, mock_risk, mock_info):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        mock_risk.return_value = {"beta": 1.0}

        def _info(sym):
            if sym == "SPCX":
                return {"quoteType": "ETF", "longName": "SPAC and New Issue ETF", "sector": "Industrials"}
            return {"quoteType": "EQUITY", "marketCap": 80_000_000_000, "beta": 1.05, "sector": "Healthcare"}

        mock_info.side_effect = _info
        fd, db_path = tempfile.mkstemp(prefix="lh_hydrate_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            db.save_ticker_metadata("UNH", sector="Healthcare")
            db.save_ticker_metadata("SPCX", sector="Industrials")
            svc = LongHorizonService(db=db, config=json.loads(json.dumps(DEFAULT_CONFIG)))
            out, alloc = svc._apply_risk_overlay_and_allocate(
                [
                    {"ticker": "UNH", "composite_score": 60.0, "signals": {"quality_factor": 0.5}},
                    {"ticker": "SPCX", "composite_score": 80.0, "signals": {"quality_factor": 0.7}},
                ],
                top_n=2,
                preset="long_horizon_6to12m",
            )
            unh = next(r for r in out if r["ticker"] == "UNH")
            spcx = next(r for r in out if r["ticker"] == "SPCX")
            self.assertEqual(unh.get("market_cap_tier"), "large")
            self.assertAlmostEqual(float(unh.get("beta") or 0), 1.05)
            self.assertGreater(float(unh.get("target_weight") or 0), 0)
            self.assertEqual(float(spcx.get("target_weight") or 0), 0.0)
            self.assertIn("excluded_etf", spcx.get("allocation_reason_codes") or [])
            self.assertNotIn("SPCX", [w["ticker"] for w in alloc.get("weights", [])])
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.compute_risk_metrics")
    def test_overlay_gates_implausible_mega_and_extreme_beta(self, mock_risk):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        mock_risk.return_value = {"beta": 1.0}
        fd, db_path = tempfile.mkstemp(prefix="lh_gate_hygiene_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            db.save_ticker_metadata("SPCX", sector="Industrials", market_cap=1_900_000_000_000, market_cap_tier="mega", beta=3.6)
            db.save_ticker_metadata("CORZ", sector="Technology", market_cap=8_000_000_000, market_cap_tier="mid", beta=5.6)
            db.save_ticker_metadata("UNH", sector="Healthcare", market_cap=300_000_000_000, market_cap_tier="mega", beta=0.85)
            svc = LongHorizonService(db=db, config=json.loads(json.dumps(DEFAULT_CONFIG)))
            out, alloc = svc._apply_risk_overlay_and_allocate(
                [
                    {"ticker": "SPCX", "composite_score": 80.0, "signals": {"quality_factor": 0.7}},
                    {"ticker": "CORZ", "composite_score": 70.0, "signals": {"quality_factor": 0.6}},
                    {"ticker": "UNH", "composite_score": 60.0, "signals": {"quality_factor": 0.5}},
                ],
                top_n=3,
                preset="long_horizon_6to12m",
            )
            by_ticker = {r["ticker"]: r for r in out}
            self.assertEqual(float(by_ticker["SPCX"]["target_weight"]), 0.0)
            self.assertIn("excluded_data_error", by_ticker["SPCX"]["allocation_reason_codes"])
            self.assertEqual(float(by_ticker["CORZ"]["target_weight"]), 0.0)
            self.assertIn("excluded_extreme_beta", by_ticker["CORZ"]["allocation_reason_codes"])
            self.assertGreater(float(by_ticker["UNH"]["target_weight"]), 0)
            self.assertEqual([w["ticker"] for w in alloc["weights"]], ["UNH"])
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    @patch("tradingagents.screening.long_horizon.compute_risk_metrics")
    def test_allocate_rebuilds_from_screening_results_when_details_sparse(self, mock_risk):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.reporting.database import ResearchDatabase
        from tradingagents.screening.long_horizon import LongHorizonService

        mock_risk.return_value = {"beta": 1.0}
        fd, db_path = tempfile.mkstemp(prefix="lh_alloc_rebuild_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            for ticker, sector in (("AAPL", "Technology"), ("MSFT", "Technology"), ("JNJ", "Healthcare"),
                                  ("PG", "Consumer Defensive"), ("XOM", "Energy")):
                db.save_ticker_metadata(ticker, sector=sector, market_cap_tier="large")
            run_id = db.save_screening_run(
                watchlist_id=None,
                criteria=json.dumps({
                    "strategy": "long_horizon",
                    "horizon": "long_6to12m",
                    "preset": "long_horizon_6to12m",
                    "requested_top_n": 5,
                }),
                ticker_count=5,
                results_count=5,
            )
            db.save_screening_results(run_id, [
                {"ticker": "AAPL", "composite_score": 70.0, "direction": "bullish",
                 "signals": {"quality_factor": 0.7}, "rank": 1},
                {"ticker": "MSFT", "composite_score": 65.0, "direction": "bullish",
                 "signals": {"quality_factor": 0.6}, "rank": 2},
                {"ticker": "JNJ", "composite_score": 60.0, "direction": "bullish",
                 "signals": {"quality_factor": 0.5}, "rank": 3},
                {"ticker": "PG", "composite_score": 55.0, "direction": "bullish",
                 "signals": {"quality_factor": 0.4}, "rank": 4},
                {"ticker": "XOM", "composite_score": 50.0, "direction": "bullish",
                 "signals": {"quality_factor": 0.3}, "rank": 5},
            ])
            db.save_long_horizon_run_details(run_id, [
                {"ticker": "AAPL", "composite_score": 70.0, "target_weight": 0.08, "sector": "Technology"},
                {"ticker": "MSFT", "composite_score": 65.0, "target_weight": 0.08, "sector": "Technology"},
            ])
            svc = LongHorizonService(db=db, config=json.loads(json.dumps(DEFAULT_CONFIG)))
            out = svc.allocate(run_id)
            allocated = [w["ticker"] for w in out["allocation"]["weights"] if w["weight"] > 0]
            self.assertGreaterEqual(len(allocated), 4)
            stored = db.get_long_horizon_run_details(run_id)
            self.assertGreaterEqual(len(stored), 4)
            self.assertIn("portfolio_risk", out)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_six_to_twelve_horizon_uses_distinct_preset(self):
        from tradingagents.default_config import DEFAULT_CONFIG

        horizons = DEFAULT_CONFIG["screening"]["long_horizon"]["horizons"]
        self.assertEqual(horizons["long_6to12m"]["preset"], "long_horizon_6to12m")
        self.assertEqual(horizons["long_12to36m"]["preset"], "long_horizon_12to36m")
        w6 = DEFAULT_CONFIG["screening"]["presets"]["long_horizon_6to12m"]["weights"]
        w36 = DEFAULT_CONFIG["screening"]["presets"]["long_horizon_12to36m"]["weights"]
        self.assertGreater(w6["residual_momentum_12_1"], w36["residual_momentum_12_1"])
        self.assertLess(w6["quality_factor"], w36["quality_factor"])


if __name__ == "__main__":
    unittest.main()
