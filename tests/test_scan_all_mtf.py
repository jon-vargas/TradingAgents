"""Regression coverage for Scan All weekly MTF enrichment."""
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from unittest.mock import patch

from tradingagents.screening.engine import ScreeningEngine
from tradingagents.screening.scan_all_mtf import (
    apply_mtf_confluence_flags,
    collect_leaderboard_mtf_targets,
    hydrate_weekly_mtf,
    merge_hydrated_rows,
    needs_weekly_hydrate,
)
from tradingagents.screening.context_packet import build_context_packet, format_screening_context


WEEKLY_BULLISH = {
    "ticker": "AAA",
    "fetched_at": "2026-07-20T00:00:00",
    "weekly_close": 100.0,
    "weekly_rsi": 55.0,
    "weekly_macd_histogram": 1.5,
    "weekly_sma10": 95.0,
    "weekly_sma20": 90.0,
    "confluence_score": 0.7,
}


class TestScanAllWeeklyMTF(unittest.TestCase):
    def test_legacy_zero_without_marker_requires_hydration(self):
        self.assertTrue(needs_weekly_hydrate({
            "signals": {"weekly_trend_alignment": 0.0, "_screening_meta": {}},
        }))
        self.assertFalse(needs_weekly_hydrate({
            "signals": {
                "weekly_trend_alignment": 0.0,
                "_screening_meta": {"weekly_computed": True},
            },
        }))

    @patch("tradingagents.screening.scan_all_mtf.get_weekly_technicals")
    def test_hydration_respects_cap_and_merges_detail(self, mock_weekly):
        mock_weekly.side_effect = lambda ticker, **_kwargs: {
            **WEEKLY_BULLISH,
            "ticker": ticker,
        }
        rows = [{"ticker": f"T{i}", "signals": {}, "direction": "bullish"} for i in range(4)]
        result = hydrate_weekly_mtf(rows, cap=2)
        self.assertEqual(mock_weekly.call_count, 2)
        self.assertEqual(result.hydrated_tickers, {"T0", "T1"})
        self.assertTrue(result.rows[0]["signals"]["_screening_meta"]["weekly_computed"])
        self.assertEqual(result.rows[0]["signals"]["weekly_trend_alignment"], 0.7)
        self.assertEqual(result.rows[0]["weekly_detail"]["label"], "BULLISH")
        self.assertNotIn("weekly_trend_alignment", result.rows[2]["signals"])

    @patch("tradingagents.screening.scan_all_mtf.get_weekly_technicals")
    def test_insufficient_history_remains_unknown(self, mock_weekly):
        mock_weekly.return_value = {
            "ticker": "NEW",
            "fetched_at": "2026-07-20T00:00:00",
            "as_of_date": "latest",
        }
        result = hydrate_weekly_mtf(
            [{"ticker": "NEW", "signals": {}, "direction": "bullish"}],
            cap=1,
        )
        row = result.rows[0]
        self.assertFalse(row["signals"]["_screening_meta"]["weekly_computed"])
        self.assertNotIn("weekly_trend_alignment", row["signals"])
        self.assertEqual(result.insufficient_history_tickers, {"NEW"})

    def test_counter_trend_flag(self):
        rows = apply_mtf_confluence_flags([{
            "ticker": "AAA",
            "direction": "bullish",
            "signals": {"weekly_trend_alignment": 0.2},
        }])
        self.assertEqual(rows[0]["mtf_confluence"], "counter_trend")
        self.assertTrue(rows[0]["weekly_counter_trend"])

    @patch("tradingagents.screening.scan_all_mtf.get_weekly_technicals")
    def test_display_backfill_hydrates_post_resort_leader(self, mock_weekly):
        mock_weekly.side_effect = lambda ticker, **_kwargs: {
            **WEEKLY_BULLISH,
            "ticker": ticker,
        }
        rows = [
            {"ticker": "LEADER", "signals": {}, "direction": "bullish", "market_cap_tier": "mid"},
            {"ticker": "OTHER", "signals": {"weekly_trend_alignment": 0.8, "_screening_meta": {"weekly_computed": True}}, "direction": "bullish", "market_cap_tier": "large"},
        ]
        targets = collect_leaderboard_mtf_targets(rows, top=1, stratified_top_k=1)
        self.assertEqual([r["ticker"] for r in targets], ["LEADER"])
        backfill = hydrate_weekly_mtf(targets, cap=1)
        merged = merge_hydrated_rows(rows, backfill)
        self.assertEqual(merged[0]["signals"]["weekly_trend_alignment"], 0.7)
        self.assertEqual(mock_weekly.call_count, 1)

    def test_computed_bearish_weekly_counts_as_coverage(self):
        coverage = ScreeningEngine._compute_signal_coverage(
            {"weekly_trend_alignment": 0.0},
            {"weekly_trend_alignment": 1.0},
            presence_signals={"weekly_trend_alignment"},
        )
        missing = ScreeningEngine._compute_signal_coverage(
            {"weekly_trend_alignment": None},
            {"weekly_trend_alignment": 1.0},
            presence_signals={"weekly_trend_alignment"},
        )
        self.assertEqual(coverage, 100.0)
        self.assertEqual(missing, 0.0)

    def test_context_packet_includes_mtf_fields(self):
        packet = build_context_packet({
            "ticker": "AAA",
            "opportunity_score": 62,
            "composite_score": 60,
            "direction": "bullish",
            "signals": {"weekly_trend_alignment": 0.2},
            "mtf_confluence": "counter_trend",
            "weekly_counter_trend": True,
            "weekly_detail": {"detail": "Weekly RSI 41"},
            "avg_dollar_volume_usd": 5_000_000,
            "watchlist_breadth": 3,
            "batch_percentile": 92,
        }, run_id=7, preset="growth")
        summary = format_screening_context(packet)
        self.assertIn("MTF confluence: counter_trend", summary)
        self.assertIn("counter-trend", summary)
        self.assertIn("Watchlist breadth: 3", summary)


class _ConsolidatedFakeDB:
    def __init__(self):
        self.runs = [
            {
                "id": 2, "watchlist_id": 2, "watchlist_name": "Growth",
                "run_at": "2026-07-20T12:05:00",
                "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","preset":"default"}',
            },
            {
                "id": 1, "watchlist_id": 1, "watchlist_name": "Core",
                "run_at": "2026-07-20T12:00:00",
                "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","preset":"default"}',
            },
        ]
        base = [
            {"ticker": "AAA", "composite_score": 72, "entry_quality": 70, "macro_fit": 65, "direction": "bullish", "rank": 1, "signals": {}},
            {"ticker": "BBB", "composite_score": 65, "entry_quality": 60, "macro_fit": 60, "direction": "bullish", "rank": 2, "signals": {}},
            {"ticker": "CCC", "composite_score": 55, "entry_quality": 55, "macro_fit": 55, "direction": "neutral", "rank": 3, "signals": {}},
        ]
        self.results = {1: deepcopy(base), 2: deepcopy(base)}

    def get_screening_runs(self, limit=50):
        return self.runs[:limit]

    def get_latest_screening_runs_per_watchlist(self, min_results=1):
        out = []
        for run in self.runs:
            if not run.get("watchlist_id"):
                continue
            count = run.get("results_count")
            if count is None:
                count = len(self.results.get(run.get("id"), []) or [])
                if count == 0:
                    count = 1
            if int(count or 0) < int(min_results):
                continue
            out.append(run)
        return sorted(
            out,
            key=lambda row: (str(row.get("run_at") or ""), int(row.get("id") or 0)),
            reverse=True,
        )

    def get_screening_results(self, run_id):
        return deepcopy(self.results[run_id])

    def get_ticker_metadata_bulk(self, tickers):
        return {
            ticker: {"sector": "Technology", "market_cap_tier": "large", "avg_dollar_volume_usd": 10_000_000}
            for ticker in tickers
        }

    def get_upcoming_events(self, **_kwargs):
        return []

    def record_runtime_metric(self, *_args, **_kwargs):
        return 1

    def get_latest_watchlist_run_after(self, watchlist_id, after_run_at, min_results=1):
        return None


class TestScanAllConsolidation(unittest.TestCase):
    def test_sector_medians_response_is_present(self):
        import webapp.app as appmod

        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        with patch.object(appmod, "get_db", return_value=_ConsolidatedFakeDB()), patch.object(
            appmod, "DEFAULT_CONFIG", cfg
        ):
            data = appmod.get_scan_all_consolidated(top=3, bottom=2, db_path="test.db")

        self.assertIn("Technology", data["sector_medians"])
        self.assertEqual(data["source_mode"], "per_watchlist")
        self.assertEqual(len(data["top_opportunities"]), 3)

    def test_empty_tagged_batch_does_not_replace_prior_board(self):
        import webapp.app as appmod

        class _PoisonedDB(_ConsolidatedFakeDB):
            def __init__(self):
                super().__init__()
                empty = {
                    "criteria": '{"strategy":"screen_all_union","source_mode":"union_buckets","preset":"default"}',
                    "results_count": 0,
                }
                self.runs = [
                    {
                        "id": 99, "watchlist_id": 1, "watchlist_name": "Core",
                        "run_at": "2026-08-17T20:10:00", **empty,
                    },
                    {
                        "id": 98, "watchlist_id": 2, "watchlist_name": "Growth",
                        "run_at": "2026-08-17T20:10:01", **empty,
                    },
                    {
                        "id": 2, "watchlist_id": 2, "watchlist_name": "Growth",
                        "run_at": "2026-07-20T12:05:00", "results_count": 3,
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","preset":"default"}',
                    },
                    {
                        "id": 1, "watchlist_id": 1, "watchlist_name": "Core",
                        "run_at": "2026-07-20T12:00:00", "results_count": 3,
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","preset":"default"}',
                    },
                ]
                self.results[99] = []
                self.results[98] = []

        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        with patch.object(appmod, "get_db", return_value=_PoisonedDB()), patch.object(
            appmod, "DEFAULT_CONFIG", cfg
        ):
            data = appmod.get_scan_all_consolidated(top=3, bottom=2, db_path="test.db")

        self.assertEqual(data["source_mode"], "per_watchlist")
        self.assertEqual(len(data["top_opportunities"]), 3)

    def test_fresher_single_watchlist_run_overlays_stale_batch_row(self):
        import webapp.app as appmod

        class _OverlayDB(_ConsolidatedFakeDB):
            def __init__(self):
                super().__init__()
                self.runs = [
                    {
                        "id": 10,
                        "watchlist_id": 1,
                        "watchlist_name": "Core",
                        "run_at": "2026-08-18T17:06:00",
                        "results_count": 3,
                        "criteria": '{"source_mode":"single_watchlist","strategy":"standard","preset":"default"}',
                    },
                    {
                        "id": 2,
                        "watchlist_id": 2,
                        "watchlist_name": "Growth",
                        "run_at": "2026-07-20T12:05:00",
                        "results_count": 3,
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","preset":"default"}',
                    },
                    {
                        "id": 1,
                        "watchlist_id": 1,
                        "watchlist_name": "Core",
                        "run_at": "2026-07-20T12:00:00",
                        "results_count": 3,
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","preset":"default"}',
                    },
                ]
                self.results[10] = [
                    {
                        "ticker": "PGY",
                        "composite_score": 90,
                        "entry_quality": 85,
                        "macro_fit": 80,
                        "direction": "bullish",
                        "rank": 1,
                        "signals": {},
                    },
                    {
                        "ticker": "BBB",
                        "composite_score": 65,
                        "entry_quality": 60,
                        "macro_fit": 60,
                        "direction": "bullish",
                        "rank": 2,
                        "signals": {},
                    },
                    {
                        "ticker": "CCC",
                        "composite_score": 55,
                        "entry_quality": 55,
                        "macro_fit": 55,
                        "direction": "neutral",
                        "rank": 3,
                        "signals": {},
                    },
                ]

            def get_latest_watchlist_run_after(self, watchlist_id, after_run_at, min_results=1):
                candidates = [
                    r for r in self.runs
                    if r.get("watchlist_id") == watchlist_id
                    and r.get("run_at", "") > after_run_at
                    and int(r.get("results_count") or 0) >= int(min_results)
                ]
                if not candidates:
                    return None
                return max(candidates, key=lambda r: r.get("run_at", ""))

        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        with patch.object(appmod, "get_db", return_value=_OverlayDB()), patch.object(
            appmod, "DEFAULT_CONFIG", cfg
        ):
            data = appmod.get_scan_all_consolidated(top=3, bottom=2, db_path="test.db")

        self.assertEqual(len(data["single_run_overlays"]), 1)
        self.assertEqual(data["single_run_overlays"][0]["overlay_run_id"], 10)
        self.assertEqual(data["source_mode"], "mixed")
        self.assertIsNone(data.get("universe_scanned"))
        core_summary = next(
            s for s in data["watchlist_summaries"] if s["watchlist_id"] == 1
        )
        growth_summary = next(
            s for s in data["watchlist_summaries"] if s["watchlist_id"] == 2
        )
        self.assertEqual(core_summary["run_id"], 10)
        self.assertEqual(core_summary["source"], "overlay")
        self.assertEqual(growth_summary["source"], "scan_all")
        self.assertEqual(data["source_mix"]["overlay"], 1)
        self.assertEqual(data["source_mix"]["scan_all"], 1)
        self.assertEqual(data["source_mix"]["single"], 0)
        top_tickers = [row["ticker"] for row in data["top_opportunities"]]
        self.assertIn("PGY", top_tickers)


class TestCrossWatchlistLatestUnion(unittest.TestCase):
    def _cfg(self, appmod, **scan_all_overrides):
        cfg = deepcopy(appmod.DEFAULT_CONFIG)
        cfg["screening"]["scan_all"]["weekly_mtf"]["enabled"] = False
        cfg["screening"]["scan_all"]["board_max_age_days"] = 400
        cfg["screening"]["scan_all"].update(scan_all_overrides)
        return cfg

    def _opp_row(self, ticker, score=70):
        return {
            "ticker": ticker,
            "composite_score": score,
            "entry_quality": 60,
            "macro_fit": 60,
            "direction": "bullish",
            "rank": 1,
            "signals": {},
        }

    def test_opportunity_single_plus_scan_all_list_is_two_cards(self):
        import webapp.app as appmod

        opp_row = self._opp_row("PGY", 90)

        class _DB(_ConsolidatedFakeDB):
            def __init__(self):
                super().__init__()
                self.runs = [
                    {
                        "id": 20, "watchlist_id": 3, "watchlist_name": "My Watchlist",
                        "run_at": "2026-08-18T17:06:00", "results_count": 1,
                        "criteria": '{"source_mode":"single_watchlist","strategy":"standard","preset":"default"}',
                    },
                    {
                        "id": 2, "watchlist_id": 2, "watchlist_name": "Growth",
                        "run_at": "2026-07-20T12:05:00", "results_count": 3,
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","preset":"default"}',
                    },
                ]
                self.results = {
                    20: [deepcopy(opp_row)],
                    2: deepcopy(self.results[2]),
                }

        cfg = self._cfg(appmod)
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(appmod, "DEFAULT_CONFIG", cfg):
            data = appmod.get_scan_all_consolidated(top=5, bottom=2, db_path="test.db")
        self.assertEqual(len(data["watchlist_summaries"]), 1)
        sources = {s["watchlist_id"]: s["source"] for s in data["watchlist_summaries"]}
        self.assertNotIn(3, sources)
        self.assertEqual(sources[2], "scan_all")
        self.assertEqual(data["source_mode"], "per_watchlist")

    def test_one_opportunity_single_only_is_404(self):
        import webapp.app as appmod
        from fastapi import HTTPException

        class _DB(_ConsolidatedFakeDB):
            def __init__(self):
                super().__init__()
                self.runs = [
                    {
                        "id": 20, "watchlist_id": 3, "watchlist_name": "My Watchlist",
                        "run_at": "2026-08-18T17:06:00", "results_count": 1,
                        "criteria": '{"source_mode":"single_watchlist","strategy":"standard"}',
                    },
                ]
                self.results = {20: [TestCrossWatchlistLatestUnion._opp_row(self, "PGY", 90)]}

        cfg = self._cfg(appmod)
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(appmod, "DEFAULT_CONFIG", cfg):
            with self.assertRaises(HTTPException) as ctx:
                appmod.get_scan_all_consolidated(top=5, bottom=2, db_path="test.db")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_orphan_momentum_single_excluded_from_board(self):
        import webapp.app as appmod
        from fastapi import HTTPException

        class _DB(_ConsolidatedFakeDB):
            def __init__(self):
                super().__init__()
                self.runs = [{
                    "id": 30,
                    "watchlist_id": 3,
                    "watchlist_name": "Momentum candidates",
                    "run_at": "2026-08-18T17:06:00",
                    "strategy": "early_momentum",
                    "results_count": 1,
                    "criteria": '{"source_mode":"single_watchlist","strategy":"early_momentum","preset":"momentum_hunter"}',
                }]
                self.results = {
                    30: [{
                        "ticker": "AAA",
                        "composite_score": 61,
                        "direction": "bullish",
                        "rank": 1,
                        "signals": {
                            "_early_momentum": {
                                "score_final": 78.5,
                                "bucket": "confirmed",
                                "passed_gates": True,
                                "domains": {"A": 20, "B": 15, "C": 10, "D": 8, "E": 5},
                                "f_deduction": 1.5,
                            },
                        },
                    }],
                }

        cfg = self._cfg(appmod)
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(
            appmod, "DEFAULT_CONFIG", cfg
        ):
            with self.assertRaises(HTTPException) as ctx:
                appmod.get_scan_all_consolidated(
                    top=5, bottom=2, strategy="early_momentum", db_path="test.db",
                )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_momentum_overlay_on_fresher_single(self):
        import webapp.app as appmod

        class _DB(_ConsolidatedFakeDB):
            def __init__(self):
                super().__init__()
                self.runs = [
                    {
                        "id": 31,
                        "watchlist_id": 3,
                        "watchlist_name": "Momentum candidates",
                        "run_at": "2026-08-18T17:06:00",
                        "strategy": "early_momentum",
                        "results_count": 1,
                        "criteria": '{"source_mode":"single_watchlist","strategy":"early_momentum","preset":"momentum_hunter"}',
                    },
                    {
                        "id": 30,
                        "watchlist_id": 3,
                        "watchlist_name": "Momentum candidates",
                        "run_at": "2026-07-20T12:00:00",
                        "strategy": "early_momentum_union",
                        "results_count": 1,
                        "criteria": '{"source_mode":"per_watchlist","strategy":"early_momentum_union","early_momentum_all_job_id":"job-m"}',
                    },
                ]
                row = {
                    "ticker": "AAA",
                    "composite_score": 61,
                    "direction": "bullish",
                    "rank": 1,
                    "signals": {
                        "_early_momentum": {
                            "score_final": 78.5,
                            "bucket": "confirmed",
                            "passed_gates": True,
                            "domains": {"A": 20, "B": 15, "C": 10, "D": 8, "E": 5},
                            "f_deduction": 1.5,
                        },
                    },
                }
                self.results = {30: [dict(row)], 31: [dict(row, ticker="BBB", signals=dict(row["signals"]))]}

        cfg = self._cfg(appmod)
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(
            appmod, "DEFAULT_CONFIG", cfg
        ):
            data = appmod.get_scan_all_consolidated(
                top=5, bottom=2, strategy="early_momentum", db_path="test.db",
            )
        self.assertEqual(data["lens"], "momentum")
        self.assertEqual(data["source_mix"]["overlay"], 1)
        self.assertEqual(data["source_mix"]["single"], 0)
        self.assertEqual(data["watchlist_summaries"][0]["source"], "overlay")
        tickers = {r["ticker"] for r in data["top_opportunities"]}
        self.assertIn("BBB", tickers)

    def test_kill_switch_omits_singles(self):
        import webapp.app as appmod

        class _DB(_ConsolidatedFakeDB):
            def __init__(self):
                super().__init__()
                self.runs = [
                    {
                        "id": 10, "watchlist_id": 1, "watchlist_name": "Core",
                        "run_at": "2026-08-18T17:06:00", "results_count": 3,
                        "criteria": '{"source_mode":"single_watchlist","strategy":"standard","preset":"default"}',
                    },
                    {
                        "id": 2, "watchlist_id": 2, "watchlist_name": "Growth",
                        "run_at": "2026-07-20T12:05:00", "results_count": 3,
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","preset":"default"}',
                    },
                    {
                        "id": 1, "watchlist_id": 1, "watchlist_name": "Core",
                        "run_at": "2026-07-20T12:00:00", "results_count": 3,
                        "criteria": '{"strategy":"screen_all","source_mode":"per_watchlist","preset":"default"}',
                    },
                ]
                self.results[10] = [self._row("PGY")]

            def _row(self, ticker):
                return {
                    "ticker": ticker, "composite_score": 90, "entry_quality": 85,
                    "macro_fit": 80, "direction": "bullish", "rank": 1, "signals": {},
                }

        cfg = self._cfg(appmod, overlay_fresher_single_runs=False)
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(appmod, "DEFAULT_CONFIG", cfg):
            data = appmod.get_scan_all_consolidated(top=5, bottom=2, db_path="test.db")
        run_ids = {s["run_id"] for s in data["watchlist_summaries"]}
        self.assertNotIn(10, run_ids)
        self.assertEqual(run_ids, {1, 2})
        self.assertFalse(data["single_run_overlays"])
        self.assertEqual(data["source_mix"]["scan_all"], 2)

    def test_stale_runs_omitted_unless_latest_scan_all_job(self):
        import webapp.app as appmod

        job_new = "job-new"
        job_old = "job-old"

        class _DB(_ConsolidatedFakeDB):
            def __init__(self):
                super().__init__()
                self.runs = [
                    {
                        "id": 4, "watchlist_id": 1, "watchlist_name": "Fresh",
                        "run_at": "2026-08-18T12:00:00", "results_count": 1,
                        "criteria": f'{{"strategy":"screen_all","source_mode":"per_watchlist","screen_all_job_id":"{job_new}"}}',
                    },
                    {
                        "id": 3, "watchlist_id": 2, "watchlist_name": "SameJobOld",
                        "run_at": "2026-06-01T12:00:00", "results_count": 1,
                        "criteria": f'{{"strategy":"screen_all","source_mode":"per_watchlist","screen_all_job_id":"{job_new}"}}',
                    },
                    {
                        "id": 2, "watchlist_id": 3, "watchlist_name": "OtherJob",
                        "run_at": "2026-06-01T11:00:00", "results_count": 1,
                        "criteria": f'{{"strategy":"screen_all","source_mode":"per_watchlist","screen_all_job_id":"{job_old}"}}',
                    },
                    {
                        "id": 1, "watchlist_id": 4, "watchlist_name": "OldSingle",
                        "run_at": "2026-06-01T10:00:00", "results_count": 1,
                        "criteria": '{"source_mode":"single_watchlist","strategy":"standard"}',
                    },
                ]
                row = {
                    "ticker": "AAA", "composite_score": 70, "entry_quality": 60,
                    "macro_fit": 60, "direction": "bullish", "rank": 1, "signals": {},
                }
                self.results = {1: [dict(row)], 2: [dict(row, ticker="BBB")], 3: [dict(row, ticker="CCC")], 4: [dict(row, ticker="DDD")]}

        cfg = self._cfg(appmod, board_max_age_days=7)
        with patch.object(appmod, "get_db", return_value=_DB()), patch.object(
            appmod, "DEFAULT_CONFIG", cfg
        ), patch.object(appmod, "_board_now", return_value=datetime(2026, 8, 19, 12, 0, 0)):
            data = appmod.get_scan_all_consolidated(top=5, bottom=2, db_path="test.db")
        names = {s["watchlist_name"] for s in data["watchlist_summaries"]}
        self.assertEqual(names, {"Fresh", "SameJobOld"})
        self.assertNotIn("OtherJob", names)
        self.assertNotIn("OldSingle", names)

    def test_db_helper_returns_all_runs_not_global_latest(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, path = tempfile.mkstemp(prefix="board_latest_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(path)
            wl = int(db.create_watchlist(name="Core", tickers="AAA", description="t"))
            with db._connect() as conn:
                conn.execute(
                    """INSERT INTO screening_runs
                       (watchlist_id, run_at, strategy, criteria, ticker_count, results_count)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (wl, "2026-08-10T12:00:00", "screen_all", '{"strategy":"screen_all"}', 1, 1),
                )
                conn.execute(
                    """INSERT INTO screening_runs
                       (watchlist_id, run_at, strategy, criteria, ticker_count, results_count)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (wl, "2026-08-18T12:00:00", "reversal_buildup", '{"strategy":"reversal_buildup"}', 1, 1),
                )
                conn.execute(
                    """INSERT INTO screening_runs
                       (watchlist_id, run_at, strategy, criteria, ticker_count, results_count)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (wl, "2026-08-19T12:00:00", "screen_all", "{}", 0, 0),
                )
                conn.commit()
            rows = db.get_latest_screening_runs_per_watchlist(min_results=1)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["strategy"], "reversal_buildup")
            self.assertEqual(rows[1]["strategy"], "screen_all")
        finally:
            os.remove(path)

    def test_momentum_run_isolated_from_opportunity_board(self):
        import webapp.app as appmod

        mom_run = {"strategy": "early_momentum_union", "criteria": '{"early_momentum_all_job_id":"m1"}'}
        opp_run = {"strategy": "screen_all_union", "criteria": '{"screen_all_job_id":"o1"}'}
        self.assertTrue(appmod._run_matches_lens(mom_run, "momentum"))
        self.assertFalse(appmod._run_matches_lens(mom_run, "opportunity"))
        self.assertFalse(appmod._run_matches_lens(mom_run, "reversal"))
        self.assertTrue(appmod._run_matches_lens(opp_run, "opportunity"))
        self.assertFalse(appmod._is_scan_all_provenance_run(mom_run))
        self.assertTrue(appmod._is_lens_scan_all_run(mom_run, "momentum"))
        self.assertFalse(appmod._is_lens_scan_all_run(mom_run, "opportunity"))


if __name__ == "__main__":
    unittest.main()
