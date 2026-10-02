#!/usr/bin/env python3
"""Prewarm OHLCV + yfinance info cache for the scan-all union universe.

Populates the same DataCache keys as ``ScreeningEngine._fetch_batch_ohlcv``
(per-ticker JSON + optional chunk entries) so the first large ``scan-all`` run
after a built-in refresh does not cold-start on 3k+ tickers.

Safe to run manually or as a scheduled job. Honors YFinanceLimiter between
batches and skips batches when the breaker is open.

Usage:
    python scripts/prewarm_screen_all.py                    # all buckets
    python scripts/prewarm_screen_all.py --batch-size 60
    python scripts/prewarm_screen_all.py --limit 500
    python scripts/prewarm_screen_all.py --watchlists "S&P 500,Russell 2000"

Environment:
    DB_PATH overrides the DB file (default: research.db)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, List

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import yfinance as yf

from tradingagents.dataflows.screening_ohlcv_cache import (
    SCREENING_OHLCV_CHUNK_SIZE,
    load_ticker_bars,
    lookback_from_config,
    session_tag_for,
    warm_chunk_ohlcv,
)
from tradingagents.dataflows.yfinance_limiter import get_yfinance_limiter
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting.database import ResearchDatabase
from tradingagents.screening.engine import _DOWNLOAD_LOCK

logger = logging.getLogger("prewarm_screen_all")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _collect_universe(db: ResearchDatabase, watchlist_filter: List[str] | None) -> List[str]:
    watchlists = db.get_builtin_watchlists()
    if watchlist_filter:
        wanted = {w.strip() for w in watchlist_filter}
        watchlists = [w for w in watchlists if w["name"] in wanted]
    seen: set[str] = set()
    universe: List[str] = []
    for wl in watchlists:
        raw = wl.get("tickers") or ""
        for t in raw.split(","):
            sym = t.strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                universe.append(sym)
    return universe


def _chunks(symbols: Iterable[str], size: int) -> Iterable[List[str]]:
    buf: List[str] = []
    for s in symbols:
        buf.append(s)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def _download_batch(tickers, start_date, end_date, is_single):
    limiter = get_yfinance_limiter()
    if limiter.is_open() or not limiter.acquire(block=True):
        return None
    tickers_str = " ".join(tickers)
    try:
        with _DOWNLOAD_LOCK:
            if is_single:
                df = yf.download(
                    tickers_str,
                    start=start_date.strftime("%Y-%m-%d"),
                    end=end_date.strftime("%Y-%m-%d"),
                    progress=False,
                    auto_adjust=True,
                    multi_level_index=False,
                    timeout=45,
                )
            else:
                df = yf.download(
                    tickers_str,
                    start=start_date.strftime("%Y-%m-%d"),
                    end=end_date.strftime("%Y-%m-%d"),
                    group_by="ticker",
                    progress=False,
                    auto_adjust=True,
                    timeout=45,
                )
        if df is not None and not df.empty:
            limiter.record_success()
            return df
        limiter.record_success()
    except Exception as exc:
        if limiter.record_if_rate_limited(exc):
            logger.warning("Batch rate-limited: %s", exc)
        else:
            logger.warning("Batch download failed: %s", exc)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Prewarm OHLCV for the scan-all union universe.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=SCREENING_OHLCV_CHUNK_SIZE,
        help="Tickers per warm_chunk (default: %(default)s, matches engine)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Cap total tickers prewarmed (0 = no cap)")
    parser.add_argument(
        "--date",
        default="",
        help="As-of scan date YYYY-MM-DD (default: today UTC)",
    )
    parser.add_argument(
        "--watchlists",
        default="",
        help="Comma-separated built-in watchlist names to include. Empty = all.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.25,
        help="Inter-batch sleep in seconds (matches engine chunk_sleep default)",
    )
    args = parser.parse_args()

    db_path = os.environ.get("DB_PATH", "research.db")
    db = ResearchDatabase(db_path)
    screening_config = DEFAULT_CONFIG.get("screening") or {}

    scan_date = args.date.strip() or datetime.utcnow().strftime("%Y-%m-%d")

    watchlist_filter = [w for w in args.watchlists.split(",") if w.strip()]
    universe = _collect_universe(db, watchlist_filter or None)
    if args.limit > 0:
        universe = universe[: args.limit]
    if not universe:
        logger.warning("No tickers to prewarm — universe is empty.")
        return 0

    logger.info(
        "Prewarming %d tickers in batches of %d (as_of=%s)",
        len(universe),
        max(1, args.batch_size),
        scan_date,
    )

    limiter = get_yfinance_limiter()
    total_success = 0
    total_failed = 0
    total_deferred = 0
    started = time.monotonic()

    for i, batch in enumerate(_chunks(universe, max(1, args.batch_size))):
        if limiter.is_open():
            cooldown = limiter.cooldown_remaining()
            logger.warning(
                "Batch %d: limiter open (%.1fs remaining) — sleeping then retrying once",
                i + 1,
                cooldown,
            )
            time.sleep(max(1.0, cooldown))
            if limiter.is_open():
                total_deferred += len(batch)
                continue

        from tradingagents.dataflows.cache import get_cache

        cache = get_cache()
        warm_chunk_ohlcv(
            batch,
            scan_date,
            screening_config,
            _download_batch,
            cache=cache,
        )
        lookback = lookback_from_config(screening_config)
        session_tag = session_tag_for(scan_date, screening_config)
        hits, misses = load_ticker_bars(cache, batch, scan_date, session_tag, lookback)
        batch_ok = 0
        for sym in batch:
            if sym.upper() in hits:
                db.record_ticker_success(sym)
                batch_ok += 1
            else:
                db.record_ticker_failure(sym, reason="prewarm_empty")
        if batch_ok == 0:
            total_failed += len(batch)
            continue

        total_success += batch_ok
        total_failed += len(batch) - batch_ok

        logger.info(
            "Batch %d: cached %d/%d (running totals: ok=%d, fail=%d, deferred=%d)",
            i + 1,
            batch_ok,
            len(batch),
            total_success,
            total_failed,
            total_deferred,
        )

        pause = max(args.sleep, limiter.cooldown_remaining())
        if pause > 0:
            time.sleep(pause)

    elapsed = time.monotonic() - started
    logger.info(
        "Prewarm complete in %.1fs: ok=%d, fail=%d, deferred=%d, universe=%d",
        elapsed,
        total_success,
        total_failed,
        total_deferred,
        len(universe),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
