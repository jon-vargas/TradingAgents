#!/usr/bin/env python3
"""One enhanced scan of My Watchlist, then re-rank with remaining presets.

Usage:
    python scripts/qa/validate_watchlist_presets.py
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
    FACTOR_FAMILIES,
    ScreeningEngine,
    compute_factor_scorecard,
)
from tradingagents.screening.opportunity_score import compute_opportunity_score

WATCHLIST_NAME = "My Watchlist"
THESIS_NAMES = ["RKLB", "ASTS", "PL", "POET", "JOBY", "PLTR", "LUNR", "IONQ"]
PRESETS = [
    "momentum_hunter",
    "value_fisher",
    "earnings_play",
    "smart_money_tracker",
    "short_squeeze",
    "quality_compounder",
    "long_horizon_12to36m",
    "dividend_income",
]
SLEEVE = {
    "momentum_hunter": ["relative_strength", "ma_crossover", "volume_surge", "trend_strength"],
    "value_fisher": ["valuation_gap", "quality_factor", "price_vs_target", "estimate_momentum"],
    "earnings_play": ["estimate_momentum", "pead_drift", "options_sentiment", "earnings_proximity"],
    "smart_money_tracker": ["smart_money", "options_sentiment", "insider_buying"],
    "short_squeeze": ["volume_surge", "options_sentiment", "relative_strength", "residual_momentum_12_1"],
    "quality_compounder": ["quality_factor", "estimate_momentum", "rating_momentum", "valuation_gap"],
    "long_horizon_12to36m": ["valuation_gap", "estimate_momentum", "quality_factor", "rating_momentum"],
    "dividend_income": ["income_factor", "quality_factor", "valuation_gap"],
}
KEY_SIGNALS = [
    "relative_strength",
    "ma_crossover",
    "trend_strength",
    "volume_surge",
    "quality_factor",
    "income_factor",
    "valuation_gap",
    "estimate_momentum",
    "rating_momentum",
    "pead_drift",
    "options_sentiment",
    "smart_money",
    "insider_buying",
    "earnings_proximity",
    "price_vs_target",
    "weekly_trend_alignment",
    "residual_momentum_12_1",
]
PRESENCE = {"weekly_trend_alignment", "ma_crossover", "trend_strength"}


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
        clone.signals = dict(clone.signals or {})
        clone.signals["_screening_meta"] = meta
        clone.signals["_qa_opportunity"] = opp
        rescored.append(clone)
    rescored.sort(key=lambda x: x.composite_score, reverse=True)
    for i, row in enumerate(rescored, 1):
        row.rank = i
    return rescored


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
        "p90": statistics.quantiles(vals, n=10)[-1] if len(vals) >= 10 else (max(vals) if vals else None),
    }


def sleeve_mean(row, keys):
    vals = []
    for k in keys:
        v = (row.signals or {}).get(k)
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return _mean(vals)


def analyze(preset: str, rows, weights):
    n = len(rows)
    top = rows[:15]
    bottom = rows[-15:] if n >= 30 else rows[-min(10, n):]
    sleeve = SLEEVE[preset]
    top_sleeve = [sleeve_mean(r, sleeve) for r in top]
    bot_sleeve = [sleeve_mean(r, sleeve) for r in bottom]
    top_sleeve = [x for x in top_sleeve if x is not None]
    bot_sleeve = [x for x in bot_sleeve if x is not None]
    dirs = Counter(r.direction for r in rows)
    cov = [r.signal_coverage_pct for r in rows if r.signal_coverage_pct is not None]
    thesis = {}
    by_ticker = {r.ticker: r for r in rows}
    for t in THESIS_NAMES:
        row = by_ticker.get(t)
        if row:
            thesis[t] = {
                "rank": row.rank,
                "score": row.composite_score,
                "dir": row.direction,
                "sleeve": sleeve_mean(row, sleeve),
                "tier": ((row.signals or {}).get("_screening_meta") or {}).get("tier_reached"),
            }
    sig_stats = {k: coverage_stats(rows, k) for k in KEY_SIGNALS}
    return {
        "preset": preset,
        "n": n,
        "avg_score": _mean([r.composite_score for r in rows]),
        "top_avg": _mean([r.composite_score for r in top]),
        "directions": dict(dirs),
        "avg_coverage": _mean(cov),
        "sleeve_top": _mean(top_sleeve),
        "sleeve_bottom": _mean(bot_sleeve),
        "sleeve_spread": (
            (_mean(top_sleeve) - _mean(bot_sleeve))
            if top_sleeve and bot_sleeve else None
        ),
        "top15": [
            {
                "rank": r.rank,
                "ticker": r.ticker,
                "score": r.composite_score,
                "dir": r.direction,
                "eq": r.entry_quality,
                "risk": r.risk_score,
                "opp": (r.signals or {}).get("_qa_opportunity"),
                "sleeve": sleeve_mean(r, sleeve),
                "quality": (r.factor_scorecard or {}).get("Quality"),
                "value": (r.factor_scorecard or {}).get("Value"),
                "income": (r.factor_scorecard or {}).get("Income"),
                "flow": (r.factor_scorecard or {}).get("Flow"),
                "mom": (r.factor_scorecard or {}).get("Momentum"),
                "rev": (r.factor_scorecard or {}).get("Revisions"),
                "tier": ((r.signals or {}).get("_screening_meta") or {}).get("tier_reached"),
                "signals": {k: (r.signals or {}).get(k) for k in sleeve},
            }
            for r in top
        ],
        "thesis": thesis,
        "signals": sig_stats,
    }


def print_report(analysis: dict):
    a = analysis
    print()
    print("=" * 88)
    print(f"{a['preset']}  n={a['n']}  avg={a['avg_score']:.1f}  top15={a['top_avg']:.1f}  "
          f"cov={a['avg_coverage']:.1f}%  dirs={a['directions']}")
    print(f"  sleeve top={_fmt(a['sleeve_top'], 3)}  bottom={_fmt(a['sleeve_bottom'], 3)}  "
          f"spread={_fmt(a['sleeve_spread'], 3)}")
    print("-" * 88)
    print(f"{'Rk':<4}{'Ticker':<8}{'Score':>7}{'Dir':<9}{'EQ':>6}{'Risk':>6}{'Opp':>6} "
          f"{'Slv':>6}{'Q':>6}{'V':>6}{'Inc':>6}{'Flw':>6}{'Mom':>6}{'Rev':>6}  Tier")
    for r in a["top15"]:
        print(
            f"{r['rank']:<4}{r['ticker']:<8}{r['score']:>7.2f}{r['dir']:<9}"
            f"{_fmt(r['eq'], 1)}{_fmt(r['risk'], 1)}{_fmt(r['opp'], 1)} "
            f"{_fmt(r['sleeve'], 2)}{_fmt(r['quality'], 1)}{_fmt(r['value'], 1)}"
            f"{_fmt(r['income'], 1)}{_fmt(r['flow'], 1)}{_fmt(r['mom'], 1)}{_fmt(r['rev'], 1)}  "
            f"{r['tier'] or '-'}"
        )
    if a["thesis"]:
        print("  thesis ranks:", ", ".join(
            f"{t}#{v['rank']}({v['score']:.1f})" for t, v in a["thesis"].items()
        ))


def persist_rescore(db: ResearchDatabase, watchlist_id: int, preset: str, rows, source_run_id: int):
    payload = []
    for r in rows:
        d = asdict(r)
        d.pop("_qa_opportunity", None)
        payload.append(d)
    criteria = json.dumps({
        "preset": preset,
        "strategy": "qa_watchlist_preset_validation",
        "source_run_id": source_run_id,
        "rescore": True,
    })
    run_id = db.save_screening_run(
        watchlist_id=watchlist_id,
        criteria=criteria,
        ticker_count=len(rows),
        results_count=len(rows),
        strategy="qa_watchlist_preset_validation",
    )
    db.save_screening_results(run_id, payload)
    return run_id


def main() -> int:
    db = ResearchDatabase("research.db")
    watchlists = db.get_watchlists()
    wl = next((w for w in watchlists if w["name"] == WATCHLIST_NAME), None)
    if not wl:
        print(f"Watchlist '{WATCHLIST_NAME}' not found")
        return 1
    tickers = [t.strip().upper() for t in (wl.get("tickers") or "").split(",") if t.strip()]
    watchlist_id = int(wl["id"])
    print(f"Scanning {WATCHLIST_NAME} id={watchlist_id} n={len(tickers)} enhanced=ON")
    print(f"Started {datetime.now().isoformat(timespec='seconds')}")

    engine = ScreeningEngine(db=db)
    results = engine.scan(
        tickers=tickers,
        preset=None,
        watchlist_id=watchlist_id,
        enable_enhanced=True,
        criteria_meta={"strategy": "qa_watchlist_signal_snapshot", "qa": True},
    )
    source_run_id = engine.last_run_id
    print(f"Snapshot run #{source_run_id}: {len(results)} scored")

    presets_cfg = DEFAULT_CONFIG["screening"]["presets"]
    analyses = []
    run_ids = {"snapshot": source_run_id}
    for name in PRESETS:
        weights = _norm_weights(dict(presets_cfg[name]["weights"]))
        ranked = rescore(engine, results, name, weights)
        analysis = analyze(name, ranked, weights)
        analyses.append(analysis)
        print_report(analysis)
        run_ids[name] = persist_rescore(db, watchlist_id, name, ranked, source_run_id)

    # Cross-preset rank disagreement on overlapping top 20
    print()
    print("=" * 88)
    print("CROSS-PRESET TOP-15 OVERLAP (Jaccard)")
    tops = {a["preset"]: {r["ticker"] for r in a["top15"]} for a in analyses}
    names = [a["preset"] for a in analyses]
    header = f"{'':<24}" + "".join(f"{n[:10]:>11}" for n in names)
    print(header)
    for a in names:
        line = f"{a:<24}"
        for b in names:
            inter = len(tops[a] & tops[b])
            union = len(tops[a] | tops[b]) or 1
            line += f"{inter / union:>11.2f}"
        print(line)

    out = {
        "watchlist": WATCHLIST_NAME,
        "watchlist_id": watchlist_id,
        "source_run_id": source_run_id,
        "run_ids": run_ids,
        "n_scored": len(results),
        "n_input": len(tickers),
        "snapshot_signal_stats": {k: coverage_stats(results, k) for k in KEY_SIGNALS},
        "analyses": analyses,
    }
    out_path = ROOT_DIR / "docs" / "qa" / "watchlist_preset_validation.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nWrote {out_path}")
    print("Persisted run ids:", run_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
