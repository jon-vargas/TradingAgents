#!/usr/bin/env python3
"""Enhanced scans on watchlists that match each operator book.

Usage:
    python -u scripts/qa/validate_matched_presets.py
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting.database import ResearchDatabase
from tradingagents.screening.engine import (
    ScreeningEngine,
    compute_factor_scorecard,
    primary_sleeve_present,
)
from tradingagents.screening.opportunity_score import compute_opportunity_score

# One live scan per watchlist. Extra presets are re-ranked off that snapshot.
MATCHES = [
    {"watchlist": "Dividend Aristocrats Top 50", "scan_preset": "dividend_income",
     "presets": ["dividend_income"]},
    {"watchlist": "Energy & Commodities", "scan_preset": "commodity_cyclical",
     "presets": ["commodity_cyclical"]},
    {"watchlist": "Healthcare & Life Sciences", "scan_preset": None,
     "presets": ["value_fisher", "quality_compounder", "earnings_play", "momentum_hunter", "smart_money_tracker"]},
    {"watchlist": "Sector ETFs", "scan_preset": "etf_technical",
     "presets": ["etf_technical"]},
    {"watchlist": "S&P 500 Top 100", "scan_preset": None,
     "presets": ["value_fisher", "quality_compounder", "long_horizon_12to36m", "earnings_play"]},
    {"watchlist": "Russell 2000 Top 100", "scan_preset": None,
     "presets": ["momentum_hunter", "smart_money_tracker", "short_squeeze"]},
]
SLEEVE = {
    "dividend_income": ["income_factor", "quality_factor", "valuation_gap"],
    "commodity_cyclical": ["relative_strength", "trend_strength", "smart_money", "volume_surge"],
    "etf_technical": ["relative_strength", "ma_crossover", "trend_strength", "volume_surge"],
    "value_fisher": ["valuation_gap", "quality_factor", "price_vs_target", "estimate_momentum"],
    "quality_compounder": ["quality_factor", "estimate_momentum", "rating_momentum", "valuation_gap"],
    "long_horizon_12to36m": ["valuation_gap", "estimate_momentum", "quality_factor", "rating_momentum"],
    "earnings_play": ["pead_drift", "earnings_proximity", "estimate_momentum", "options_sentiment"],
    "momentum_hunter": ["relative_strength", "volume_surge", "ma_crossover", "residual_momentum_12_1"],
    "smart_money_tracker": ["smart_money", "options_sentiment", "insider_buying"],
    "short_squeeze": ["short_pressure", "volume_surge", "options_sentiment", "relative_strength"],
}
KEY_SIGNALS = [
    "relative_strength", "ma_crossover", "trend_strength", "volume_surge",
    "quality_factor", "income_factor", "valuation_gap", "estimate_momentum",
    "rating_momentum", "pead_drift", "options_sentiment", "smart_money",
    "insider_buying", "earnings_proximity", "price_vs_target",
    "weekly_trend_alignment", "residual_momentum_12_1", "short_pressure",
]
PRESENCE = {"weekly_trend_alignment", "ma_crossover", "trend_strength", "residual_momentum_12_1"}


def _numeric_signals(signals: dict) -> dict:
    out = {}
    for k, v in (signals or {}).items():
        if str(k).startswith("_"):
            continue
        if v is None or isinstance(v, (int, float)):
            out[k] = v
    return out


def _norm_weights(raw: dict) -> dict:
    total = sum(raw.values()) or 1.0
    return {k: v / total for k, v in raw.items()}


def _mean(xs):
    return statistics.mean(xs) if xs else None


def _fmt(v, digits=2):
    if v is None:
        return "  n/a"
    return f"{v:>6.{digits}f}"


def coverage_stats(rows, key: str):
    vals = []
    none = 0
    zero = 0
    for r in rows:
        v = (r.signals or {}).get(key)
        if v is None:
            none += 1
        elif isinstance(v, (int, float)):
            vals.append(float(v))
            if float(v) == 0.0:
                zero += 1
    return {
        "n": len(vals),
        "none": none,
        "zero": zero,
        "mean": _mean(vals),
    }


def sleeve_mean(row, keys):
    vals = []
    for k in keys:
        v = (row.signals or {}).get(k)
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return _mean(vals)


def rescore(engine: ScreeningEngine, rows, preset: str, weights: dict):
    rescored = []
    for r in rows:
        signals = _numeric_signals(r.signals)
        composite = engine._compute_composite_score(signals, weights)
        composite_f = engine._compute_composite_fundamental_score(signals, weights)
        weekly = signals.get("weekly_trend_alignment")
        direction = engine._determine_direction(signals, weights, weekly_alignment=weekly)
        coverage = engine._compute_signal_coverage(signals, weights, presence_signals=PRESENCE)
        scorecard = compute_factor_scorecard(signals, macro_fit_score=r.macro_fit)
        opp, _ = compute_opportunity_score(
            composite, r.entry_quality, r.macro_fit,
            composite_fundamental=composite_f,
            weekly_alignment=weekly,
        )
        clone = deepcopy(r)
        clone.composite_score = round(composite, 2)
        clone.composite_fundamental = round(composite_f, 2)
        clone.direction = direction
        clone.signal_coverage_pct = coverage
        clone.factor_scorecard = scorecard
        meta = dict((clone.signals or {}).get("_screening_meta") or {})
        meta["resolved_preset"] = preset
        meta["weight_preset"] = preset
        meta["signal_coverage_pct"] = coverage
        meta["primary_sleeve_present"] = primary_sleeve_present(
            preset, signals, tier_reached=meta.get("tier_reached")
        )
        clone.signals = dict(clone.signals or {})
        clone.signals["_screening_meta"] = meta
        clone.signals["_qa_opportunity"] = opp
        rescored.append(clone)
    rescored.sort(
        key=lambda x: (
            0 if primary_sleeve_present(
                preset, x.signals,
                tier_reached=((x.signals or {}).get("_screening_meta") or {}).get("tier_reached"),
            ) else 1,
            -x.composite_score,
        )
    )
    for i, row in enumerate(rescored, 1):
        row.rank = i
    return rescored


def analyze(preset: str, rows):
    n = len(rows)

    def _ok(row):
        meta = (row.signals or {}).get("_screening_meta") or {}
        return primary_sleeve_present(
            preset, row.signals, tier_reached=meta.get("tier_reached") or getattr(row, "tier_reached", None)
        )

    gated = sum(1 for r in rows if not _ok(r))
    top = rows[:12]
    bottom = [r for r in rows if _ok(r)][-12:]
    sleeve = SLEEVE[preset]
    top_sleeve = [x for x in (sleeve_mean(r, sleeve) for r in top) if x is not None]
    bot_sleeve = [x for x in (sleeve_mean(r, sleeve) for r in bottom) if x is not None]
    dirs = Counter(r.direction for r in rows)
    cov = [r.signal_coverage_pct for r in rows if r.signal_coverage_pct is not None]
    twelve_one = coverage_stats(rows, "residual_momentum_12_1")
    vg = coverage_stats(rows, "valuation_gap")
    income = coverage_stats(rows, "income_factor")
    return {
        "preset": preset,
        "n": n,
        "gated_last": gated,
        "avg_score": _mean([r.composite_score for r in rows]),
        "top_avg": _mean([r.composite_score for r in top]),
        "directions": dict(dirs),
        "avg_coverage": _mean(cov),
        "sleeve_top": _mean(top_sleeve),
        "sleeve_bottom": _mean(bot_sleeve),
        "twelve_one": twelve_one,
        "valuation_gap": vg,
        "income_factor": income,
        "top12": [
            {
                "rank": r.rank,
                "ticker": r.ticker,
                "score": r.composite_score,
                "dir": r.direction,
                "gated": not _ok(r),
                "eq": r.entry_quality,
                "sleeve": sleeve_mean(r, sleeve),
                "quality": (r.factor_scorecard or {}).get("Quality"),
                "value": (r.factor_scorecard or {}).get("Value"),
                "income": (r.factor_scorecard or {}).get("Income"),
                "flow": (r.factor_scorecard or {}).get("Flow"),
                "mom": (r.factor_scorecard or {}).get("Momentum"),
                "tier": ((r.signals or {}).get("_screening_meta") or {}).get("tier_reached"),
                "signals": {k: (r.signals or {}).get(k) for k in sleeve},
            }
            for r in top
        ],
        "signals": {k: coverage_stats(rows, k) for k in KEY_SIGNALS},
    }


def print_report(watchlist: str, analysis: dict):
    a = analysis
    print()
    print("=" * 88)
    print(f"{watchlist} × {a['preset']}  n={a['n']}  gated_last={a['gated_last']}  "
          f"avg={a['avg_score']:.1f}  top12={a['top_avg']:.1f}  cov={a['avg_coverage']:.1f}%")
    print(f"  sleeve top={_fmt(a['sleeve_top'], 3)}  bottom={_fmt(a['sleeve_bottom'], 3)}  "
          f"12-1 n={a['twelve_one']['n']} none={a['twelve_one']['none']}  "
          f"VG n={a['valuation_gap']['n']} none={a['valuation_gap']['none']}  "
          f"inc n={a['income_factor']['n']} none={a['income_factor']['none']}")
    print("-" * 88)
    print(f"{'Rk':<4}{'Ticker':<8}{'Score':>7}{'Dir':<9}{'G':<2}{'EQ':>6} "
          f"{'Slv':>6}{'Q':>6}{'V':>6}{'Inc':>6}{'Flw':>6}{'Mom':>6}  Tier")
    for r in a["top12"]:
        flag = "!" if r["gated"] else " "
        print(
            f"{r['rank']:<4}{r['ticker']:<8}{r['score']:>7.2f}{r['dir']:<9}{flag:<2}"
            f"{_fmt(r['eq'], 1)} {_fmt(r['sleeve'], 2)}{_fmt(r['quality'], 1)}"
            f"{_fmt(r['value'], 1)}{_fmt(r['income'], 1)}{_fmt(r['flow'], 1)}"
            f"{_fmt(r['mom'], 1)}  {r['tier'] or '-'}"
        )


def persist(db, watchlist_id, preset, rows, source_run_id):
    payload = [asdict(r) for r in rows]
    criteria = json.dumps({
        "preset": preset,
        "strategy": "qa_matched_preset_validation",
        "source_run_id": source_run_id,
        "rescore": True,
    })
    run_id = db.save_screening_run(
        watchlist_id=watchlist_id,
        criteria=criteria,
        ticker_count=len(rows),
        results_count=len(rows),
        strategy="qa_matched_preset_validation",
    )
    db.save_screening_results(run_id, payload)
    return run_id


def main() -> int:
    db = ResearchDatabase("research.db")
    watchlists = {w["name"]: w for w in db.get_watchlists()}
    engine = ScreeningEngine(db=db)
    presets_cfg = DEFAULT_CONFIG["screening"]["presets"]
    all_analyses = []
    run_ids = {}
    print(f"Matched preset QA started {datetime.now().isoformat(timespec='seconds')}")

    for spec in MATCHES:
        name = spec["watchlist"]
        wl = watchlists.get(name)
        if not wl:
            print(f"SKIP missing watchlist: {name}")
            continue
        tickers = [t.strip().upper() for t in (wl.get("tickers") or "").split(",") if t.strip()]
        wl_id = int(wl["id"])
        scan_preset = spec["scan_preset"]
        print(f"\n>>> Scanning {name} id={wl_id} n={len(tickers)} preset={scan_preset or 'default'} enhanced=ON")
        results = engine.scan(
            tickers=tickers,
            preset=scan_preset,
            watchlist_id=wl_id,
            enable_enhanced=True,
            criteria_meta={"strategy": "qa_matched_signal_snapshot", "qa": True},
        )
        source_run_id = engine.last_run_id
        print(f"    snapshot run #{source_run_id}: {len(results)} scored")
        run_ids[f"{name}:snapshot"] = source_run_id
        snap_stats = {k: coverage_stats(results, k) for k in KEY_SIGNALS}

        for preset in spec["presets"]:
            weights = _norm_weights(dict(presets_cfg[preset]["weights"]))
            if scan_preset == preset:
                ranked = results
            else:
                ranked = rescore(engine, results, preset, weights)
            analysis = analyze(preset, ranked)
            analysis["watchlist"] = name
            analysis["snapshot_run_id"] = source_run_id
            analysis["snapshot_signals"] = snap_stats
            all_analyses.append(analysis)
            print_report(name, analysis)
            run_ids[f"{name}:{preset}"] = persist(db, wl_id, preset, ranked, source_run_id)

    out_path = ROOT_DIR / "docs" / "qa" / "matched_preset_validation.json"
    out_path.write_text(json.dumps({
        "run_ids": run_ids,
        "analyses": all_analyses,
    }, indent=2, default=str))
    print(f"\nWrote {out_path}")
    print("Persisted run ids:", run_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
