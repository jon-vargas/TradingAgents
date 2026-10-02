"""Acceptance tests for Institutional Screener Enhancement plan (Phases 0–6)."""
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting import generate_html_report
from tradingagents.screening.alert_evaluator import AlertEvaluator
from tradingagents.screening.context_packet import build_context_packet, format_screening_context
from tradingagents.screening.engine import FACTOR_FAMILIES, ScreeningEngine
from tradingagents.screening.scan_all_overlays import apply_scan_all_overlays
from webapp.app import _enrich_screening_rows


class TestPhase0AlertCalendarFirst(unittest.TestCase):
    def test_earnings_alert_uses_calendar_without_live_fetch(self):
        db = MagicMock()
        db.get_upcoming_events.return_value = [{
            "ticker": "AAPL",
            "event_date": "2026-07-25",
            "days_to_event": 5,
            "event_type": "earnings",
        }]
        ev = AlertEvaluator(db=db)
        with patch(
            "tradingagents.dataflows.yfinance_extended.get_earnings_profile"
        ) as live:
            triggered, msg, data = ev._check_earnings_approaching(
                "AAPL", {"days_threshold": 7}, {}
            )
        self.assertTrue(triggered)
        self.assertEqual(data.get("source"), "calendar")
        self.assertIn("earnings", msg.lower())
        live.assert_not_called()


class TestPhase6CHandoffReport(unittest.TestCase):
    def test_screening_summary_rendered_in_html_report(self):
        packet = build_context_packet(
            {
                "ticker": "AAPL",
                "opportunity_score": 72.5,
                "composite_score": 68.0,
                "direction": "bullish",
                "rank": 3,
                "preset": "growth_momentum",
            },
            run_id=42,
            preset="growth_momentum",
        )
        summary = format_screening_context(packet)
        state = {
            "screening_summary_text": summary,
            "screening_context": packet,
            "final_trade_decision": "HOLD",
        }
        html = generate_html_report(
            state=state,
            ticker="AAPL",
            analysis_date="2026-07-20",
            decision="HOLD",
            config=DEFAULT_CONFIG.copy(),
            duration_seconds=10.0,
            output_path=None,
        )
        self.assertIn('id="screening-summary"', html)
        self.assertIn("Scan run #42", html)
        self.assertIn("72.5", html)


class TestPhase6CPresetAndFullChain(unittest.TestCase):
    """IMP-2 strengthening: (a) the rendered #screening-summary block must
    surface the *preset* used for the run (not just score/rank), and (b) the
    full API-shaped chain — DB-persisted screening run/results -> context
    packet construction (mirrors ``analyze_top_n_from_screening``) -> HTML
    report — must carry the preset end to end without dropping it."""

    def test_preset_appears_in_rendered_summary(self):
        packet = build_context_packet(
            {
                "ticker": "AAPL",
                "opportunity_score": 72.5,
                "composite_score": 68.0,
                "direction": "bullish",
                "rank": 3,
            },
            run_id=42,
            preset="growth_momentum",
        )
        summary = format_screening_context(packet)
        state = {
            "screening_summary_text": summary,
            "screening_context": packet,
            "final_trade_decision": "HOLD",
        }
        html = generate_html_report(
            state=state, ticker="AAPL", analysis_date="2026-07-20", decision="HOLD",
            config=DEFAULT_CONFIG.copy(), duration_seconds=10.0, output_path=None,
        )
        self.assertIn("Preset: growth_momentum", html)

    def test_full_db_to_report_chain_preserves_preset(self):
        import tempfile as _tempfile
        from tradingagents.reporting.database import ResearchDatabase

        with _tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/research.db"
            db = ResearchDatabase(db_path)

            criteria = json.dumps({"weights": {}, "preset": "value_fisher", "regime": "neutral"})
            run_id = db.save_screening_run(
                watchlist_id=None, criteria=criteria, ticker_count=1, results_count=1,
            )
            db.save_screening_results(run_id, [{
                "ticker": "MSFT", "composite_score": 61.0, "direction": "bullish",
                "signals": {"valuation_gap": 0.7}, "rank": 1,
                "entry_quality": 55.0, "macro_fit": 50.0, "composite_fundamental": 60.0,
            }])

            # Replicate the exact chain analyze_top_n_from_screening() performs:
            # DB read -> preset extraction from criteria JSON -> context packet.
            run_meta = db.get_screening_run(run_id) or {}
            crit = json.loads(run_meta.get("criteria") or "{}")
            preset_name = crit.get("preset") or "default"
            self.assertEqual(preset_name, "value_fisher")

            results = db.get_screening_results(run_id)
            row = next(r for r in results if str(r.get("ticker", "")).upper() == "MSFT")
            pkt = build_context_packet(row, run_id=run_id, preset=str(preset_name))
            pkt = dict(pkt)
            pkt["summary_text"] = format_screening_context(pkt)
            self.assertIn("Preset: value_fisher", pkt["summary_text"])

            state = {
                "screening_summary_text": pkt["summary_text"],
                "screening_context": pkt,
                "final_trade_decision": "HOLD",
            }
            html = generate_html_report(
                state=state, ticker="MSFT", analysis_date="2026-07-20", decision="HOLD",
                config=DEFAULT_CONFIG.copy(), duration_seconds=5.0, output_path=None,
            )
            self.assertIn('id="screening-summary"', html)
            self.assertIn(f"Scan run #{run_id}", html)
            self.assertIn("Preset: value_fisher", html)


class TestPhase6AValuationMultiMetric(unittest.TestCase):
    def test_cheap_ps_raises_multi_metric_score(self):
        engine = ScreeningEngine.__new__(ScreeningEngine)
        engine._screening_config = DEFAULT_CONFIG.get("screening", {})
        live = {
            "forward_pe": {"Technology": 30.0},
            "price_to_sales": {"Technology": 8.0},
            "ev_to_ebitda": {"Technology": 20.0},
        }
        multi, detail = engine._signal_valuation_gap_multi(
            forward_pe=35.0,
            price_to_sales=2.0,
            ev_to_ebitda=18.0,
            sector="Technology",
            live_medians_all=live,
        )
        single = engine._signal_valuation_gap(35.0, "Technology", live["forward_pe"])
        self.assertGreaterEqual(multi, single + 0.10)
        self.assertIn("weights_used", detail)


class TestPhase0HSevenFamilies(unittest.TestCase):
    def test_factor_families_count_and_catalyst(self):
        self.assertGreaterEqual(len(FACTOR_FAMILIES), 9)
        self.assertIn("Catalyst", FACTOR_FAMILIES)
        self.assertIn("Flow", FACTOR_FAMILIES)
        self.assertIn("Income", FACTOR_FAMILIES)
        self.assertEqual(FACTOR_FAMILIES["Quality"], ["quality_factor"])
        self.assertIn("earnings_proximity", FACTOR_FAMILIES["Catalyst"])
        self.assertNotIn("earnings_proximity", FACTOR_FAMILIES["Revisions"])


def _synthetic_ohlcv_batch(tickers, n_days=80):
    """Build a fake ``_fetch_batch_ohlcv`` return value: per-ticker OHLCV
    Series long enough to clear the engine's 20-day minimum, with a mild
    upward drift so Tier 1 signals compute without NaN/empty-window errors."""
    idx = pd.date_range("2026-01-01", periods=n_days, freq="B")
    batch = {}
    for i, t in enumerate(tickers):
        base = 50.0 + i * 10
        drift = np.linspace(0, 5, n_days)
        noise = np.sin(np.linspace(0, 6, n_days)) * 1.5
        close = pd.Series(base + drift + noise, index=idx)
        high = close + 1.0
        low = close - 1.0
        volume = pd.Series([1_000_000 + 5000 * i] * n_days, index=idx)
        spy_close = pd.Series(400.0 + np.linspace(0, 3, n_days), index=idx)
        batch[t] = {
            "close": close, "high": high, "low": low, "volume": volume,
            "info": {}, "spy_close": spy_close,
        }
    return batch


class _FakeDBForScanTest:
    """Minimal DB stub tracking exactly the calls the Phase 3 ADV hard gate
    needs to verify: ticker_metadata writes (no second yfinance fetch — the
    engine reuses cached ``.info`` from the same batch) and the persisted
    screening-run criteria JSON."""

    def __init__(self):
        self.saved_metadata_calls = []
        self.saved_criteria = None

    def get_evicted_tickers(self, since_days=None):
        return set()

    def get_screening_runs(self, limit=5):
        return []

    def get_screening_results(self, run_id):
        return []

    def get_watchlist(self, watchlist_id):
        return None

    def save_ticker_metadata(self, ticker, **kwargs):
        self.saved_metadata_calls.append((ticker, kwargs))

    def save_screening_run(self, watchlist_id, criteria, ticker_count, results_count):
        self.saved_criteria = json.loads(criteria)
        return 999

    def save_screening_results(self, run_id, results):
        pass


class TestPhase3LiquidityADVWritePath(unittest.TestCase):
    """IMP-1 hard gate: after one scan, >=80% of ranked rows have a
    non-null avg_dollar_volume_usd write with NO second yfinance ADV fetch
    (the write reuses the same .info payload already fetched for Tier 2 /
    risk scoring), and the null-ADV rate is logged in run criteria JSON."""

    def _run_scan_with_mocks(self, tickers, info_overrides=None):
        db = _FakeDBForScanTest()
        engine = ScreeningEngine(config=DEFAULT_CONFIG, db=db)

        ohlcv_batch = _synthetic_ohlcv_batch(tickers)
        ohlcv_fetch_calls = {"count": 0}

        def _fake_fetch_batch_ohlcv(chunk, date):
            ohlcv_fetch_calls["count"] += 1
            return {t: ohlcv_batch[t] for t in chunk if t in ohlcv_batch}

        info_fetch_calls = {"count": 0}

        def _fake_fetch_info_cached(chunk, date):
            info_fetch_calls["count"] += 1
            info_fetch_calls.setdefault("tickers", []).extend(chunk)
            out = {}
            for t in chunk:
                base_info = {
                    "marketCap": 5_000_000_000,
                    "averageVolume": 2_000_000,
                    "sector": "Technology",
                }
                if info_overrides and t in info_overrides:
                    base_info.update(info_overrides[t])
                out[t] = base_info
            return out

        with patch.object(engine, "_fetch_batch_ohlcv", side_effect=_fake_fetch_batch_ohlcv), \
             patch.object(engine, "_fetch_info_cached", side_effect=_fake_fetch_info_cached), \
             patch.object(engine, "_get_regime", return_value="neutral"), \
             patch.object(engine, "_compute_macro_overlay", return_value={}), \
             patch.object(engine, "_load_prior_signals", return_value={}):
            results = engine.scan(
                tickers=tickers,
                date="2026-07-20",
                preset="momentum_hunter",
                watchlist_id=1,
                enable_enhanced=False,
            )
        return engine, db, results, ohlcv_fetch_calls, info_fetch_calls

    def test_adv_write_coverage_no_second_fetch(self):
        tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        engine, db, results, ohlcv_calls, info_calls = self._run_scan_with_mocks(tickers)

        self.assertEqual(len(results), 5, "all 5 synthetic tickers should score")

        # No second yfinance ADV-specific fetch: .info is fetched exactly
        # once per chunk (Tier 2 funnel phase) and reused for both risk
        # scoring AND the ADV write — assert the mocked info fetch was
        # called with every ticker exactly once (not twice for the same
        # ticker, which would indicate a redundant ADV-only fetch pass).
        fetched_tickers = info_calls.get("tickers", [])
        self.assertEqual(
            sorted(fetched_tickers), sorted(tickers),
            "info should be fetched exactly once per ticker (no duplicate ADV fetch pass)",
        )
        self.assertEqual(
            len(fetched_tickers), len(set(fetched_tickers)),
            "each ticker's .info must be fetched only once — a second fetch pass would duplicate entries",
        )

        # >=80% non-null avg_dollar_volume_usd write coverage.
        written_tickers = {t for t, kwargs in db.saved_metadata_calls if kwargs.get("avg_dollar_volume_usd") is not None}
        coverage_pct = 100.0 * len(written_tickers) / len(tickers)
        self.assertGreaterEqual(coverage_pct, 80.0, f"ADV write coverage only {coverage_pct:.0f}%")

        # null_adv_rate_pct must be logged in the persisted run criteria JSON.
        self.assertIsNotNone(db.saved_criteria)
        self.assertIn("null_adv_rate_pct", db.saved_criteria)
        self.assertLessEqual(db.saved_criteria["null_adv_rate_pct"], 20.0)

    def test_penalize_mode_reranks_illiquid_row_below_liquid_peer(self):
        """Fixture-level proof (independent of the engine mocks above) that
        Phase 3's penalize mode actually demotes an illiquid row below a
        liquid peer that would otherwise rank behind it on raw composite."""
        rows = [
            {
                "ticker": "ILLIQUID", "composite_score": 78.0, "entry_quality": 70.0,
                "macro_fit": 55.0, "composite_fundamental": 75.0,
                "ticker_metadata": {"avg_dollar_volume_usd": 100_000.0},  # far below floor
                "signals": {},
            },
            {
                "ticker": "LIQUID", "composite_score": 72.0, "entry_quality": 66.0,
                "macro_fit": 52.0, "composite_fundamental": 70.0,
                "ticker_metadata": {"avg_dollar_volume_usd": 50_000_000.0},  # well above floor
                "signals": {},
            },
        ]
        cfg = {
            "liquidity_policy": {"scan_all_min_dollar_adv_usd": 2_000_000},
            "liquidity_gate_mode": "penalize",
            "event_blackout": {"enabled": False},
            "coverage_penalty": {"mode": "warn"},
        }
        out = apply_scan_all_overlays(rows, screening_config=cfg, db=None)
        by_ticker = {r["ticker"]: r for r in out}
        self.assertFalse(by_ticker["ILLIQUID"]["liquidity_pass"])
        self.assertTrue(by_ticker["LIQUID"]["liquidity_pass"])
        # Before the penalty, ILLIQUID's raw opportunity score edges out
        # LIQUID's (slightly higher composite/EQ/macro across the board).
        # After the liquidity penalty, it must fall below the liquid peer.
        self.assertGreater(
            by_ticker["LIQUID"]["opportunity_score"],
            by_ticker["ILLIQUID"]["opportunity_score"],
            "penalize mode should re-rank the illiquid row below its liquid peer",
        )
        ill = by_ticker["ILLIQUID"]
        self.assertTrue(ill["opp_liquidity_penalty_applied"])
        self.assertIsNotNone(ill["opp_pre_penalty_score"])
        self.assertLess(ill["opportunity_score"], ill["opp_pre_penalty_score"])

    def test_risk_penalty_stamps_flag_and_demotes_high_risk_row(self):
        rows = [
            {
                "ticker": "RISKY", "composite_score": 80.0, "entry_quality": 72.0,
                "macro_fit": 58.0, "composite_fundamental": 76.0,
                "risk_score": 78.0,
                "ticker_metadata": {"avg_dollar_volume_usd": 50_000_000.0},
                "signals": {},
            },
            {
                "ticker": "SAFER", "composite_score": 76.0, "entry_quality": 70.0,
                "macro_fit": 55.0, "composite_fundamental": 72.0,
                "risk_score": 42.0,
                "ticker_metadata": {"avg_dollar_volume_usd": 50_000_000.0},
                "signals": {},
            },
        ]
        cfg = {
            "liquidity_policy": {"scan_all_min_dollar_adv_usd": 2_000_000},
            "liquidity_gate_mode": "penalize",
            "event_blackout": {"enabled": False},
            "coverage_penalty": {"mode": "warn"},
            "risk_penalty": {"enabled": True, "threshold": 70, "multiplier": 0.9},
        }
        out = apply_scan_all_overlays(rows, screening_config=cfg, db=None)
        by_ticker = {r["ticker"]: r for r in out}
        risky = by_ticker["RISKY"]
        safer = by_ticker["SAFER"]
        self.assertTrue(risky["opp_risk_penalty_applied"])
        self.assertFalse(safer.get("opp_risk_penalty_applied"))
        self.assertLess(risky["opportunity_score"], risky["opp_pre_penalty_score"])
        self.assertGreater(safer["opportunity_score"], risky["opportunity_score"])

    def test_coverage_penalty_stamps_flag_when_enabled(self):
        rows = [
            {
                "ticker": "THIN", "composite_score": 70.0, "entry_quality": 65.0,
                "macro_fit": 50.0, "composite_fundamental": 68.0,
                "signal_coverage_pct": 52.0,
                "ticker_metadata": {"avg_dollar_volume_usd": 50_000_000.0},
                "signals": {},
            },
        ]
        cfg = {
            "liquidity_policy": {"scan_all_min_dollar_adv_usd": 2_000_000},
            "liquidity_gate_mode": "penalize",
            "event_blackout": {"enabled": False},
            "coverage_penalty": {"mode": "penalize", "threshold": 60, "multiplier": 0.95},
            "risk_penalty": {"enabled": False},
        }
        out = apply_scan_all_overlays(rows, screening_config=cfg, db=None)[0]
        self.assertTrue(out["opp_coverage_penalty_applied"])
        self.assertLess(out["opportunity_score"], out["opp_pre_penalty_score"])

    def test_scan_all_coverage_override_penalizes_when_global_is_warn(self):
        rows = [
            {
                "ticker": "THIN", "composite_score": 70.0, "entry_quality": 65.0,
                "macro_fit": 50.0, "composite_fundamental": 68.0,
                "signal_coverage_pct": 41.0,
                "ticker_metadata": {"avg_dollar_volume_usd": 50_000_000.0},
                "signals": {},
            },
        ]
        cfg = {
            "liquidity_policy": {"scan_all_min_dollar_adv_usd": 2_000_000},
            "liquidity_gate_mode": "penalize",
            "event_blackout": {"enabled": False},
            "coverage_penalty": {"mode": "warn", "threshold": 60, "multiplier": 0.95},
            "scan_all": {"coverage_penalty": {"mode": "penalize", "threshold": 60, "multiplier": 0.90}},
            "risk_penalty": {"enabled": False},
        }
        out = apply_scan_all_overlays(rows, screening_config=cfg, db=None)[0]
        self.assertTrue(out["opp_coverage_penalty_applied"])
        self.assertAlmostEqual(out["opportunity_score"], round(out["opp_pre_penalty_score"] * 0.90, 1))

    def test_hunter_ma_zero_does_not_outrank_confirmed_tape(self):
        rows = [
            {
                "ticker": "SPCX",
                "composite_score": 40.0,
                "entry_quality": 70.0,
                "macro_fit": 75.0,
                "composite_fundamental": 74.0,
                "resolved_preset": "momentum_hunter",
                "ticker_metadata": {
                    "avg_dollar_volume_usd": 50_000_000.0,
                    "market_cap": 1.76e12,
                },
                "signals": {
                    "ma_crossover": 0.0,
                    "_screening_meta": {"resolved_preset": "momentum_hunter"},
                },
            },
            {
                "ticker": "PGY",
                "composite_score": 55.0,
                "entry_quality": 68.0,
                "macro_fit": 72.0,
                "composite_fundamental": 48.0,
                "resolved_preset": "momentum_hunter",
                "ticker_metadata": {
                    "avg_dollar_volume_usd": 50_000_000.0,
                    "market_cap": 2_000_000_000.0,
                },
                "signals": {
                    "ma_crossover": 1.0,
                    "_screening_meta": {"resolved_preset": "momentum_hunter"},
                },
            },
        ]
        cfg = {
            "liquidity_policy": {"scan_all_min_dollar_adv_usd": 2_000_000},
            "liquidity_gate_mode": "penalize",
            "event_blackout": {"enabled": False},
            "coverage_penalty": {"mode": "warn"},
            "risk_penalty": {"enabled": False},
        }
        out = apply_scan_all_overlays(rows, screening_config=cfg, db=None)
        by_ticker = {r["ticker"]: r for r in out}
        self.assertFalse(by_ticker["PGY"]["used_fundamental_composite"])
        self.assertTrue(by_ticker["SPCX"]["ma_crossover_dampener_applied"])
        self.assertTrue(by_ticker["SPCX"]["opp_implausible_identity_applied"])
        self.assertGreater(by_ticker["PGY"]["opportunity_score"], by_ticker["SPCX"]["opportunity_score"])

    def test_implausible_mega_stays_on_board_but_sorts_last(self):
        rows = [
            {
                "ticker": "SPCX",
                "composite_score": 80.0,
                "entry_quality": 70.0,
                "macro_fit": 70.0,
                "composite_fundamental": 80.0,
                "ticker_metadata": {
                    "avg_dollar_volume_usd": 50_000_000.0,
                    "market_cap": 1.76e12,
                },
                "signals": {},
            },
            {
                "ticker": "AAPL",
                "composite_score": 50.0,
                "entry_quality": 50.0,
                "macro_fit": 50.0,
                "composite_fundamental": 50.0,
                "ticker_metadata": {
                    "avg_dollar_volume_usd": 50_000_000.0,
                    "market_cap": 3.5e12,
                },
                "signals": {},
            },
            {
                "ticker": "MU",
                "composite_score": 50.0,
                "entry_quality": 50.0,
                "macro_fit": 50.0,
                "composite_fundamental": 50.0,
                "ticker_metadata": {
                    "avg_dollar_volume_usd": 50_000_000.0,
                    "market_cap": 1.10e12,
                },
                "signals": {},
            },
        ]
        cfg = {
            "liquidity_policy": {"scan_all_min_dollar_adv_usd": 2_000_000},
            "liquidity_gate_mode": "penalize",
            "event_blackout": {"enabled": False},
            "coverage_penalty": {"mode": "warn"},
            "risk_penalty": {"enabled": False},
        }
        out = apply_scan_all_overlays(rows, screening_config=cfg, db=None)
        by_ticker = {r["ticker"]: r for r in out}
        self.assertTrue(by_ticker["SPCX"]["opp_implausible_identity_applied"])
        self.assertFalse(by_ticker["AAPL"].get("opp_implausible_identity_applied"))
        self.assertFalse(by_ticker["MU"].get("opp_implausible_identity_applied"))
        self.assertGreater(by_ticker["AAPL"]["opportunity_score"], by_ticker["SPCX"]["opportunity_score"])
        self.assertGreater(by_ticker["MU"]["opportunity_score"], by_ticker["SPCX"]["opportunity_score"])

    def test_small_cap_growth_alias_uses_hunter_blender(self):
        rows = [
            {
                "ticker": "TIGR",
                "composite_score": 52.0,
                "entry_quality": 60.0,
                "macro_fit": 70.0,
                "composite_fundamental": 80.0,
                "resolved_preset": "small_cap_growth",
                "ticker_metadata": {"avg_dollar_volume_usd": 50_000_000.0},
                "signals": {"ma_crossover": 1.0, "_screening_meta": {"resolved_preset": "small_cap_growth"}},
            },
        ]
        cfg = {
            "liquidity_policy": {"scan_all_min_dollar_adv_usd": 2_000_000},
            "liquidity_gate_mode": "penalize",
            "event_blackout": {"enabled": False},
            "coverage_penalty": {"mode": "warn"},
            "risk_penalty": {"enabled": False},
        }
        out = apply_scan_all_overlays(rows, screening_config=cfg, db=None)[0]
        self.assertEqual(out["resolved_preset"], "momentum_hunter")
        self.assertFalse(out["used_fundamental_composite"])


class TestPhase6BCoverageTierHardGate(unittest.TestCase):
    """IMP-10 hard gate: a Tier-1-only fixture row has signal_coverage_pct
    < 60 and tier_reached == 'tier1_only'; an enhanced-tier fixture row has
    signal_coverage_pct >= 80."""

    def test_tier1_only_row_has_low_coverage_and_correct_tier(self):
        weights = {s: 1.0 for s in ScreeningEngine.SIGNAL_NAMES}
        # Tier-1-only: only the 7 Tier 1 signals have real (non-zero) values;
        # every Tier 2 / enhanced signal is neutral-zero (as scan() sets for
        # eliminated/non-enhanced tickers), so present-weight is 7/16.
        signals = {s: 0.6 for s in ScreeningEngine.TIER1_SIGNALS}
        signals.update({s: 0.0 for s in ScreeningEngine.TIER2_SIGNALS})
        signals.update({s: 0.0 for s in ScreeningEngine.ENHANCED_SIGNALS})

        coverage = ScreeningEngine._compute_signal_coverage(signals, weights)
        tier = ScreeningEngine._resolve_tier_reached(
            "AAA", enhanced_tickers=set(), funnel_tickers=set(), aux_tickers=set(),
        )
        self.assertLess(coverage, 60.0, f"tier1-only coverage was {coverage}, expected <60")
        self.assertEqual(tier, "tier1_only")

    def test_enhanced_row_has_high_coverage(self):
        weights = {s: 1.0 for s in ScreeningEngine.SIGNAL_NAMES}
        # Enhanced: every signal (Tier 1 + Tier 2 + enhanced) has a real value.
        signals = {s: 0.6 for s in ScreeningEngine.SIGNAL_NAMES}

        coverage = ScreeningEngine._compute_signal_coverage(signals, weights)
        tier = ScreeningEngine._resolve_tier_reached(
            "BBB", enhanced_tickers={"BBB"}, funnel_tickers={"BBB"}, aux_tickers={"BBB"},
        )
        self.assertGreaterEqual(coverage, 80.0, f"enhanced coverage was {coverage}, expected >=80")
        self.assertEqual(tier, "enhanced")


class _EnrichmentDB:
    def __init__(self, metadata=None, events=None):
        self.metadata = metadata or {}
        self.events = events or []

    def get_ticker_metadata_bulk(self, tickers):
        return {ticker: self.metadata.get(ticker, {}) for ticker in tickers}

    def get_upcoming_events(self, **_kwargs):
        return self.events


class TestInstitutionalScreeningEnrichment(unittest.TestCase):
    def setUp(self):
        self.config = {
            "liquidity_policy": {"scan_all_min_dollar_adv_usd": 2_000_000},
            "liquidity_gate_mode": "penalize",
            "event_blackout": {"enabled": False, "days": 5, "exempt_presets": ["earnings_play"]},
            "coverage_penalty": {"mode": "warn"},
        }

    @staticmethod
    def _row(ticker, score=70.0):
        return {
            "ticker": ticker,
            "composite_score": score,
            "composite_fundamental": score,
            "entry_quality": 60.0,
            "macro_fit": 50.0,
            "signals": {"weekly_trend_alignment": 0.7},
        }

    def test_hydrates_adv_and_normalizes_unknown_adv(self):
        db = _EnrichmentDB({
            "LIQ": {"avg_dollar_volume_usd": 10_000_000, "sector": "Technology"},
            "ILL": {"avg_dollar_volume_usd": 100_000, "sector": "Technology"},
            "UNK": {"sector": "Technology"},
        })
        rows = [self._row("LIQ"), self._row("ILL"), self._row("UNK")]
        enriched = _enrich_screening_rows(rows, db=db, screening_config=self.config)
        by_ticker = {row["ticker"]: row for row in enriched}
        self.assertTrue(by_ticker["LIQ"]["liquidity_pass"])
        self.assertFalse(by_ticker["ILL"]["liquidity_pass"])
        self.assertIsNone(by_ticker["UNK"]["liquidity_pass"])

    def test_unknown_adv_is_retained_in_exclude_mode(self):
        db = _EnrichmentDB({"UNK": {"sector": "Technology"}})
        config = dict(self.config)
        config["liquidity_gate_mode"] = "exclude"
        enriched = _enrich_screening_rows(
            [self._row("UNK")], db=db, screening_config=config,
        )
        self.assertEqual(len(enriched), 1)
        self.assertIsNone(enriched[0]["liquidity_pass"])
        self.assertNotIn("liquidity_excluded", enriched[0])

    def test_earnings_play_preset_is_exempt_from_blackout(self):
        db = _EnrichmentDB(
            {"EARN": {"avg_dollar_volume_usd": 10_000_000, "sector": "Technology"}},
            [{"ticker": "EARN", "event_type": "earnings"}],
        )
        config = dict(self.config)
        config["event_blackout"] = {
            "enabled": True, "days": 5, "exempt_presets": ["earnings_play"],
        }
        enriched = _enrich_screening_rows(
            [self._row("EARN")],
            db=db,
            screening_config=config,
            preset="earnings_play",
        )
        self.assertEqual(enriched[0]["event_risk_flags"], [])

    def test_sector_relative_uses_post_overlay_scores_and_peer_threshold(self):
        db = _EnrichmentDB({
            "A": {"avg_dollar_volume_usd": 10_000_000, "sector": "Technology"},
            "B": {"avg_dollar_volume_usd": 10_000_000, "sector": "Technology"},
            "C": {"avg_dollar_volume_usd": 100_000, "sector": "Technology"},
            "D": {"avg_dollar_volume_usd": 10_000_000, "sector": "Energy"},
        })
        rows = [
            self._row("A", 70.0), self._row("B", 60.0),
            self._row("C", 80.0), self._row("D", 75.0),
        ]
        enriched = _enrich_screening_rows(rows, db=db, screening_config=self.config)
        by_ticker = {row["ticker"]: row for row in enriched}
        tech_scores = sorted(
            row["opportunity_score"] for row in enriched if row["sector"] == "Technology"
        )
        median = tech_scores[1]
        self.assertEqual(
            by_ticker["C"]["sector_relative_score"],
            round(by_ticker["C"]["opportunity_score"] - median, 1),
        )
        self.assertIsNone(by_ticker["D"]["sector_relative_score"])

    def test_enriched_opportunity_order_is_export_ready(self):
        db = _EnrichmentDB({
            "HIGH_ADV": {"avg_dollar_volume_usd": 10_000_000, "sector": "Technology"},
            "LOW_ADV": {"avg_dollar_volume_usd": 100_000, "sector": "Technology"},
        })
        enriched = _enrich_screening_rows(
            [self._row("LOW_ADV", 80.0), self._row("HIGH_ADV", 76.0)],
            db=db,
            screening_config=self.config,
        )
        ordered = sorted(enriched, key=lambda row: row["opportunity_score"], reverse=True)
        self.assertEqual(ordered[0]["ticker"], "HIGH_ADV")


if __name__ == "__main__":
    unittest.main()
