#!/usr/bin/env python3
"""Prune historical runtime_metrics samples to refresh p95 reporting.

Usage:
    python scripts/prune_runtime_metrics.py --dry-run
    python scripts/prune_runtime_metrics.py --metric scan_all_duration_seconds --older-than-days 7
    python scripts/prune_runtime_metrics.py --older-than-days 30 --confirm

Defaults to a dry-run preview unless ``--confirm`` is passed. Use this after a
significant perf fix lands so the QA-gate p95 metric (``scan_all_p95_sec``)
converges to the new baseline instead of being dragged upward by samples from
regressed builds.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.reporting.database import ResearchDatabase  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default="research.db")
    p.add_argument(
        "--metric",
        default=None,
        help="Specific metric_key to prune (default: all keys).",
    )
    p.add_argument(
        "--older-than-days",
        type=int,
        default=14,
        help="Delete samples older than this many days (default: 14).",
    )
    p.add_argument(
        "--keep-newest",
        type=int,
        default=None,
        help=(
            "Alternative mode: keep only the N most-recent samples for the "
            "specified metric and delete everything else. Useful for resetting "
            "a noisy p95 baseline after a perf fix lands. Requires --metric."
        ),
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete rows. Without this flag the script previews the count only.",
    )
    return p.parse_args()


def _preview_count(db: ResearchDatabase, metric: str | None, older_than_days: int) -> int:
    days = max(1, int(older_than_days))
    with db._connect() as conn:
        if metric:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM runtime_metrics
                WHERE metric_key = ? AND created_at < datetime('now', ?)
                """,
                (str(metric).strip(), f"-{days} day"),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM runtime_metrics WHERE created_at < datetime('now', ?)",
                (f"-{days} day",),
            ).fetchone()
    return int(row["c"] if row else 0)


def _keep_newest(
    db: ResearchDatabase,
    metric: str,
    keep: int,
    confirm: bool,
) -> int:
    keep = max(0, int(keep))
    with db._connect() as conn:
        rows = conn.execute(
            """
            SELECT id, duration_seconds, created_at
            FROM runtime_metrics
            WHERE metric_key = ?
            ORDER BY created_at DESC
            """,
            (str(metric).strip(),),
        ).fetchall()
        all_ids = [int(r["id"]) for r in rows]
        keep_ids = set(all_ids[:keep])
        delete_ids = [i for i in all_ids if i not in keep_ids]
        print(
            f"[prune-runtime-metrics] keep-newest mode: {len(rows)} total, "
            f"keeping {len(keep_ids)}, would delete {len(delete_ids)}."
        )
        if delete_ids:
            print("Samples to be deleted:")
            kept = {int(r["id"]): r for r in rows}
            for i in delete_ids:
                row = kept[i]
                print(f"  - id={i}  {row['duration_seconds']}s  @ {row['created_at']}")
        if not confirm or not delete_ids:
            return 0
        placeholders = ",".join(["?"] * len(delete_ids))
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM runtime_metrics WHERE id IN ({placeholders})",
            delete_ids,
        )
        deleted = int(cur.rowcount or 0)
        conn.commit()
        return deleted


def main() -> int:
    args = _parse_args()
    db = ResearchDatabase(args.db_path)

    if args.keep_newest is not None:
        if not args.metric:
            print("--keep-newest requires --metric (mode is per-metric).")
            return 2
        deleted = _keep_newest(db, args.metric, args.keep_newest, args.confirm)
        if not args.confirm:
            print("Dry-run only. Re-run with --confirm to actually delete.")
        else:
            print(f"Deleted {deleted} sample(s).")
        return 0

    eligible = _preview_count(db, args.metric, args.older_than_days)
    scope = f"metric_key='{args.metric}'" if args.metric else "all metric_keys"
    print(
        f"[prune-runtime-metrics] {eligible} sample(s) older than "
        f"{args.older_than_days} day(s) for {scope}"
    )
    if eligible == 0:
        print("Nothing to do.")
        return 0
    if not args.confirm:
        print("Dry-run only. Re-run with --confirm to actually delete.")
        return 0
    deleted = db.prune_runtime_metrics(metric_key=args.metric, older_than_days=args.older_than_days)
    print(f"Deleted {deleted} sample(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
