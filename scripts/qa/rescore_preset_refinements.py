#!/usr/bin/env python3
"""Rescore matched-universe snapshots after identity refinements.

Does not call Yahoo. Hydrates short_pressure, earnings window, and mega-growth
meta from persisted rows + ticker_metadata.

Usage:
    python -u scripts/qa/rescore_preset_refinements.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import fields
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting.database import ResearchDatabase
from tradingagents.screening.engine import ScreeningEngine, ScreeningResult, _EARNINGS_WINDOW_DAYS
from scripts.qa.validate_matched_presets import (
    _norm_weights,
    analyze,
    persist,
    print_report,
    rescore,
)

# Old proximity used a 60-day ramp. Map onto the 21-day identity window.
_OLD_PROX_WINDOW = 60.0
_PROX_KEEP = 1.0 - (_EARNINGS_WINDOW_DAYS / _OLD_PROX_WINDOW)

JOBS = [
    {"snapshot": 1375, "watchlist_id": 19, "watchlist": "Russell 2000 Top 100",
     "preset": "short_squeeze"},
    {"snapshot": 1370, "watchlist_id": 3, "watchlist": "S&P 500 Top 100",
     "preset": "earnings_play"},
    {"snapshot": 1370, "watchlist_id": 3, "watchlist": "S&P 500 Top 100",
     "preset": "value_fisher"},
    {"snapshot": 1364, "watchlist_id": 20, "watchlist": "Dividend Aristocrats Top 50",
     "preset": "quality_compounder"},
    {"snapshot": 1370, "watchlist_id": 3, "watchlist": "S&P 500 Top 100",
     "preset": "long_horizon_12to36m"},
]


def _parse_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def load_rows(db: ResearchDatabase, run_id: int) -> list:
    allowed = {f.name for f in fields(ScreeningResult)}
    rows = []
    for d in db.get_screening_results(run_id):
        sig = _parse_json(d.get("signals")) or {}
        deltas = _parse_json(d.get("signal_deltas")) or {}
        fs = d.get("factor_scorecard")
        if not fs and isinstance(sig, dict) and sig.get("_factor_scorecard"):
            fs = _parse_json(sig["_factor_scorecard"])
        rc = _parse_json(d.get("risk_components"))
        payload = {}
        for k in allowed:
            if k == "signals":
                payload[k] = sig if isinstance(sig, dict) else {}
            elif k == "signal_deltas":
                payload[k] = deltas if isinstance(deltas, dict) else {}
            elif k == "factor_scorecard":
                payload[k] = fs
            elif k == "risk_components":
                payload[k] = rc if isinstance(rc, dict) else None
            elif k in d:
                payload[k] = d[k]
        rows.append(ScreeningResult(**payload))
    return rows


def hydrate(rows, metadata: dict) -> None:
    for row in rows:
        signals = dict(row.signals or {})
        meta = dict(signals.get("_screening_meta") or {})
        tmeta = metadata.get(row.ticker.upper()) or {}
        if not meta.get("market_cap_tier"):
            meta["market_cap_tier"] = tmeta.get("market_cap_tier")
        if not meta.get("sector"):
            meta["sector"] = tmeta.get("sector")
        meta["ticker"] = row.ticker.upper()
        if signals.get("short_pressure") is None:
            sp = (row.risk_components or {}).get("short_pressure") if row.risk_components else None
            if isinstance(sp, (int, float)):
                signals["short_pressure"] = min(max(float(sp) / 100.0, 0.0), 1.0)
        prox = signals.get("earnings_proximity")
        if isinstance(prox, (int, float)) and float(prox) < _PROX_KEEP:
            signals["earnings_proximity"] = None
        elif prox == 0:
            signals["earnings_proximity"] = None
        signals["_screening_meta"] = meta
        row.signals = signals


def main() -> int:
    db = ResearchDatabase("research.db")
    engine = ScreeningEngine(db=db)
    presets_cfg = DEFAULT_CONFIG["screening"]["presets"]
    analyses = []
    run_ids = {}
    for job in JOBS:
        rows = load_rows(db, job["snapshot"])
        meta = db.get_ticker_metadata_bulk([r.ticker for r in rows])
        hydrate(rows, meta)
        weights = _norm_weights(dict(presets_cfg[job["preset"]]["weights"]))
        ranked = rescore(engine, rows, job["preset"], weights)
        analysis = analyze(job["preset"], ranked)
        analysis["watchlist"] = job["watchlist"]
        analysis["snapshot_run_id"] = job["snapshot"]
        print_report(job["watchlist"], analysis)
        new_id = persist(db, job["watchlist_id"], job["preset"], ranked, job["snapshot"])
        print(f"  persisted rescore run #{new_id}")
        run_ids[f"{job['watchlist']}:{job['preset']}"] = new_id
        analyses.append(analysis)

    out = ROOT_DIR / "docs" / "qa" / "preset_refinement_rescore.json"
    out.write_text(json.dumps({"run_ids": run_ids, "analyses": analyses}, indent=2, default=str))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
