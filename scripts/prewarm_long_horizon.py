#!/usr/bin/env python3
"""
Prewarm long-horizon expanded-universe caches.

This command is intended to reduce latency for subsequent long-horizon scans by
warming:
1) ticker metadata cache (`ticker_metadata` table via resolve_and_cache)
2) centralized ticker info cache (`ticker_info_full` in data cache)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.dataflows.yfinance_extended import get_ticker_info
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting.database import ResearchDatabase
from tradingagents.screening.long_horizon import LongHorizonService
from tradingagents.screening.ticker_resolver import resolve_and_cache


def _csv_to_list(raw: str) -> List[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def _prewarm_info(symbols: List[str], max_workers: int) -> Tuple[int, int]:
    if not symbols:
        return 0, 0
    ok = 0
    failed = 0
    workers = max(1, min(int(max_workers), len(symbols)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(get_ticker_info, sym): sym for sym in symbols}
        for future in as_completed(futures):
            try:
                info = future.result() or {}
                if info:
                    ok += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
    return ok, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Prewarm long-horizon expanded-universe caches.")
    parser.add_argument("--db-path", default="research.db", help="Path to SQLite database")
    parser.add_argument("--max-tickers", type=int, default=600, help="Cap tickers to prewarm")
    parser.add_argument(
        "--sources",
        default="built-in,user,auto",
        help="Expanded-universe source types (comma-separated)",
    )
    parser.add_argument(
        "--exclude-prefixes",
        default="Movers:",
        help="Watchlist name prefixes to exclude (comma-separated)",
    )
    parser.add_argument("--metadata-workers", type=int, default=12, help="Metadata resolver workers")
    parser.add_argument("--metadata-max-age-days", type=int, default=7, help="Freshness threshold for metadata cache")
    parser.add_argument("--info-workers", type=int, default=12, help="Ticker info prewarm workers")
    parser.add_argument("--skip-info", action="store_true", help="Only prewarm ticker metadata, skip ticker info cache")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()

    config = deepcopy(DEFAULT_CONFIG)
    long_cfg = config.get("screening", {}).get("long_horizon", {})
    exp_cfg = long_cfg.get("expanded_universe", {})
    pol_cfg = long_cfg.get("universe_policy", {})

    exp_cfg["enabled"] = True
    exp_cfg["include_sources"] = _csv_to_list(args.sources) or ["built-in", "user", "auto"]
    exp_cfg["exclude_watchlist_prefixes"] = _csv_to_list(args.exclude_prefixes) or ["Movers:"]
    exp_cfg["max_tickers"] = max(10, int(args.max_tickers))

    pol_cfg["metadata_workers"] = max(1, int(args.metadata_workers))
    pol_cfg["metadata_max_age_days"] = max(1, int(args.metadata_max_age_days))
    pol_cfg["info_workers"] = max(1, int(args.info_workers))

    started = time.time()
    db = ResearchDatabase(args.db_path)
    service = LongHorizonService(db=db, config=config)

    symbols, universe_meta = service._resolve_universe_inputs(
        watchlist_id=None,
        tickers=None,
        use_expanded_universe=True,
    )
    symbols = symbols[: max(10, int(args.max_tickers))]

    if not symbols:
        payload = {
            "ok": False,
            "reason": "empty_universe",
            "input_count": 0,
            "duration_seconds": round(time.time() - started, 3),
            "universe": universe_meta,
        }
        print(json.dumps(payload, indent=2))
        return 1

    meta_started = time.time()
    metadata_map = resolve_and_cache(
        symbols,
        db,
        max_workers=max(1, int(args.metadata_workers)),
        max_age_days=max(1, int(args.metadata_max_age_days)),
    )
    metadata_duration = time.time() - meta_started

    info_ok = 0
    info_failed = 0
    info_duration = 0.0
    if not args.skip_info:
        info_started = time.time()
        info_ok, info_failed = _prewarm_info(symbols, max_workers=max(1, int(args.info_workers)))
        info_duration = time.time() - info_started

    payload: Dict[str, Any] = {
        "ok": True,
        "input_count": len(symbols),
        "metadata_resolved_count": len(metadata_map),
        "metadata_duration_seconds": round(metadata_duration, 3),
        "info_warmed_count": int(info_ok),
        "info_failed_count": int(info_failed),
        "info_duration_seconds": round(info_duration, 3),
        "total_duration_seconds": round(time.time() - started, 3),
        "skip_info": bool(args.skip_info),
        "universe": universe_meta,
        "config": {
            "sources": exp_cfg["include_sources"],
            "exclude_prefixes": exp_cfg["exclude_watchlist_prefixes"],
            "max_tickers": exp_cfg["max_tickers"],
            "metadata_workers": pol_cfg["metadata_workers"],
            "metadata_max_age_days": pol_cfg["metadata_max_age_days"],
            "info_workers": pol_cfg["info_workers"],
        },
    }

    if args.json:
        print(json.dumps(payload))
    else:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
