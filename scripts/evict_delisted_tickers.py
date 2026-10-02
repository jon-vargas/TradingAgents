"""Evict likely-delisted tickers from ticker_metadata based on ticker_health.

Consults the per-ticker failure telemetry produced by the screening engine
(recorded whenever a bulk OHLCV download returns no rows for a given symbol)
and removes tickers that have accumulated enough failures without a recent
success. By default this runs in DRY-RUN mode and only prints the candidates;
pass --apply to actually delete rows.

Usage:
    python scripts/evict_delisted_tickers.py                 # dry-run, default thresholds
    python scripts/evict_delisted_tickers.py --apply         # perform deletion
    python scripts/evict_delisted_tickers.py --min-failures 5 --min-days 3 --apply

Environment:
    DB_PATH overrides the DB file (default: research.db)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting.database import ResearchDatabase


def main() -> int:
    cfg = (DEFAULT_CONFIG.get("screening", {}) or {}).get("ticker_health", {}) or {}
    parser = argparse.ArgumentParser(description="Evict stale / delisted tickers based on ticker_health.")
    parser.add_argument(
        "--min-failures",
        type=int,
        default=int(cfg.get("min_failures", 3)),
        help="Minimum failure_count required to qualify (default: %(default)s)",
    )
    parser.add_argument(
        "--min-days",
        type=int,
        default=int(cfg.get("min_days_since_success", 2)),
        help="Minimum days since last success (default: %(default)s)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually evict; without this flag the script runs in dry-run mode.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON payload instead of human-readable output.",
    )
    args = parser.parse_args()

    db_path = os.environ.get("DB_PATH", "research.db")
    db = ResearchDatabase(db_path)

    result = db.evict_stale_tickers(
        min_failures=args.min_failures,
        min_days_since_success=args.min_days,
        dry_run=not args.apply,
    )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    candidate_count = result["candidate_count"]
    evicted_count = result["evicted_count"]
    mode = "DRY RUN" if result["dry_run"] else "APPLIED"

    print(f"[{mode}] min_failures>={args.min_failures}, min_days_since_success>={args.min_days}")
    print(f"Candidates: {candidate_count}")
    if candidate_count:
        preview = result["candidates"][:20]
        more = "" if candidate_count <= 20 else f" (+{candidate_count - 20} more)"
        print(f"  {', '.join(preview)}{more}")
    if result["dry_run"]:
        print("Re-run with --apply to delete ticker_metadata rows for these tickers.")
    else:
        print(f"Evicted: {evicted_count} row(s) from ticker_metadata")
    return 0


if __name__ == "__main__":
    sys.exit(main())
