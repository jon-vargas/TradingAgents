#!/usr/bin/env python3
"""Backfill ``asset_class`` + ``country`` on existing ``ticker_metadata`` rows.

Background
----------
The universe-broadening pass added ``asset_class`` (equity/etf/adr/unknown)
and ``country`` columns to ``ticker_metadata`` via additive migration, but
existing rows sat at NULL. ``resolve_and_cache`` naturally fills them in
over a 7-day window via its stale-ticker refresh, but during that window
ETFs silently flow through the equity decision tree (wrong preset, wrong
risk-score branch, extra Tier 2 404 noise). This script force-refreshes
all unclassified rows so the fix takes effect immediately instead of
gradually.

Usage
-----
    python scripts/backfill_asset_class.py [--db research.db] [--limit N]
                                            [--workers 8] [--dry-run]

By default it processes all rows where ``asset_class IS NULL`` in chunks
that honor the yfinance circuit breaker. ``--dry-run`` only prints the
count and a sample without touching yfinance.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import List

# Ensure project root is importable when this script is run from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_asset_class")


def _find_unclassified(db_path: str, limit: int | None) -> List[str]:
    """Return tickers with NULL ``asset_class`` in ``ticker_metadata``."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        sql = (
            "SELECT ticker FROM ticker_metadata "
            "WHERE asset_class IS NULL OR asset_class = '' "
            "ORDER BY last_updated ASC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [row[0] for row in cur.execute(sql).fetchall()]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="research.db", help="Path to research.db")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max rows to process (default: all unclassified)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="yfinance fetch concurrency (lower = gentler on rate limits)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=200,
        help="Tickers per resolve_and_cache batch (keeps the circuit breaker happy)",
    )
    parser.add_argument(
        "--chunk-sleep",
        type=float,
        default=3.0,
        help="Seconds to sleep between chunks (breathing room for yfinance)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts only, do not hit yfinance",
    )
    args = parser.parse_args()

    tickers = _find_unclassified(args.db, args.limit)
    logger.info(
        "Found %d ticker_metadata rows without asset_class in %s",
        len(tickers),
        args.db,
    )
    if not tickers:
        logger.info("Nothing to backfill — all rows are classified.")
        return 0

    if args.dry_run:
        sample = tickers[:15]
        logger.info("Dry-run: sample=%s", sample)
        return 0

    # Import here so --dry-run doesn't pull in the full stack.
    from tradingagents.reporting.database import get_db
    from tradingagents.screening.ticker_resolver import resolve_and_cache

    db = get_db(args.db)

    total = len(tickers)
    processed = 0
    start = time.time()
    # Force every ticker through the refresh path by setting max_age_days=0.
    # That's what actually makes resolve_and_cache re-fetch and write back
    # the new asset_class/country fields — without it, rows that are within
    # the 7-day window are considered fresh and returned from cache (which
    # is exactly the state we're trying to repair).
    for i in range(0, total, args.chunk_size):
        batch = tickers[i : i + args.chunk_size]
        t0 = time.time()
        try:
            resolve_and_cache(
                batch,
                db,
                max_workers=args.workers,
                max_age_days=0,
            )
        except Exception as e:
            # Don't let a batch failure block the rest — log and continue.
            logger.warning("Batch starting at %d failed: %s", i, e)
        elapsed = time.time() - t0
        processed += len(batch)
        pct = processed / total * 100
        logger.info(
            "Processed %d/%d (%.1f%%) in %.1fs — batch %d-%d",
            processed,
            total,
            pct,
            elapsed,
            i,
            i + len(batch) - 1,
        )
        if i + args.chunk_size < total and args.chunk_sleep > 0:
            time.sleep(args.chunk_sleep)

    elapsed_total = time.time() - start
    logger.info("Backfill complete in %.1fs", elapsed_total)

    # Final audit
    remaining = _find_unclassified(args.db, None)
    logger.info(
        "After backfill: %d rows still unclassified (first 10: %s)",
        len(remaining),
        remaining[:10],
    )

    # Summary by class
    conn = sqlite3.connect(args.db)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT asset_class, COUNT(*) AS n FROM ticker_metadata GROUP BY asset_class ORDER BY n DESC"
        ).fetchall()
        logger.info("Post-backfill asset_class distribution:")
        for row in rows:
            logger.info("  %-12s n=%d", row["asset_class"] or "<null>", row["n"])
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
