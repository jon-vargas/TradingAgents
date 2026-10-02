#!/usr/bin/env python3
"""Smoke test: run scan-all against a small curated universe.

Defaults to the seeded ``Sector ETFs`` + ``NASDAQ 100`` watchlists, which are
guaranteed to be present after first-time bootstrap and total ~111 tickers —
small enough to run inside CI budget but exercises every layer of the
screening pipeline (Tier 1 OHLCV, Tier 2 .info enrichment, regime overlay,
percentile ranking).

Override via ``SMOKE_WATCHLISTS="A,B,C"`` to target a different set; missing
watchlists are skipped with a warning instead of failing the smoke run.

For the curated **Healthcare & Life Sciences** basket (~105 tickers), use a
higher budget (e.g. ``SMOKE_BUDGET_SEC=300``) or ``make qa-healthcare-watchlist-smoke``.

Intended for CI / stabilization-gate. Asserts the full screening pipeline
completes end-to-end within SMOKE_BUDGET_SEC without crashing on null-safe
comparisons, limiter integration, or watchlist materialization.

Exits 0 on success, non-zero on any of:
  - budget exceeded
  - zero results produced
  - any watchlist scan raised an uncaught exception

Usage:
    python scripts/qa/smoke_screen_all.py
    SMOKE_BUDGET_SEC=180 python scripts/qa/smoke_screen_all.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.reporting.database import ResearchDatabase
from tradingagents.screening.engine import ScreeningEngine


DEFAULT_SMOKE_WATCHLISTS = ["Sector ETFs", "NASDAQ 100"]


def _resolve_targets(db: ResearchDatabase, requested: list[str]) -> list[dict]:
    """Resolve requested watchlist names to populated targets.

    Falls back gracefully: missing watchlists are reported but do not abort
    the smoke run, so a fresh DB without the optional curated lists can still
    exercise the pipeline against whatever is present.
    """
    all_wl = {w["name"]: w for w in db.get_watchlists()}
    targets: list[dict] = []
    for name in requested:
        wl = all_wl.get(name)
        if not wl:
            print(f"[skip] watchlist '{name}' not found")
            continue
        tickers = [t.strip() for t in (wl.get("tickers") or "").split(",") if t.strip()]
        if tickers:
            targets.append({"name": name, "tickers": tickers})
        else:
            print(f"[skip] watchlist '{name}' has no tickers (pending refresh?)")
    return targets


def main() -> int:
    budget_sec = float(os.environ.get("SMOKE_BUDGET_SEC", "180"))
    db_path = os.environ.get("DB_PATH", "research.db")
    raw_override = os.environ.get("SMOKE_WATCHLISTS", "").strip()
    requested = (
        [n.strip() for n in raw_override.split(",") if n.strip()]
        if raw_override
        else list(DEFAULT_SMOKE_WATCHLISTS)
    )

    db = ResearchDatabase(db_path)
    targets = _resolve_targets(db, requested)

    if not targets:
        print(
            "No target watchlists populated. Run 'make builtins-refresh-apply' "
            "or set SMOKE_WATCHLISTS to an existing watchlist name."
        )
        return 2

    total_tickers = sum(len(t["tickers"]) for t in targets)
    print(f"Smoke: scanning {len(targets)} watchlist(s), {total_tickers} tickers, budget={budget_sec}s")

    engine = ScreeningEngine(db=db)
    total_results = 0
    started = time.monotonic()
    failures: list[tuple[str, str]] = []
    for wl in targets:
        try:
            t0 = time.monotonic()
            rows = engine.scan(tickers=wl["tickers"])
            elapsed = time.monotonic() - t0
            total_results += len(rows or [])
            print(f"  [ok]  {wl['name']}: {len(rows or [])} results in {elapsed:.1f}s")
        except Exception as exc:
            failures.append((wl["name"], str(exc)))
            print(f"  [err] {wl['name']}: {exc}")

    total_elapsed = time.monotonic() - started
    over_budget = total_elapsed > budget_sec

    print(
        f"Smoke complete: results={total_results} elapsed={total_elapsed:.1f}s"
        f" budget={budget_sec:.0f}s failures={len(failures)}"
    )
    if failures:
        return 3
    if total_results == 0:
        print("No results produced — smoke failed")
        return 4
    if over_budget:
        print(f"Budget exceeded ({total_elapsed:.1f}s > {budget_sec}s)")
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
