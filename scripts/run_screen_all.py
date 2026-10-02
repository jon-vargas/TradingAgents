"""Scan ALL watchlists sequentially from the command line.

Each watchlist is scanned using its default preset. Results are written to the
database just like individual scans. A 2-second pause between watchlists
prevents yfinance rate-throttling.

Usage:
  python scripts/run_screen_all.py [--top 10] [--db research.db]
  make screen-all
"""
import argparse
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.screening import ScreeningEngine
from tradingagents.screening.opportunity_score import compute_opportunity_score
from tradingagents.reporting.database import ResearchDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan all watchlists.")
    parser.add_argument("--top", type=int, default=10, help="Show top N results per watchlist (default: 10)")
    parser.add_argument("--db", type=str, default="research.db", help="Database path")
    args = parser.parse_args()

    db = ResearchDatabase(args.db)
    watchlists = db.get_watchlists()
    scannable = []
    for wl in watchlists:
        tickers = [t.strip() for t in (wl.get("tickers") or "").split(",") if t.strip()]
        if tickers:
            scannable.append({"id": wl["id"], "name": wl["name"], "tickers": tickers,
                              "preset": wl.get("default_preset")})

    if not scannable:
        print("No watchlists with tickers found.")
        return 1

    total_tickers = sum(len(w["tickers"]) for w in scannable)
    print(f"Batch Scan: {len(scannable)} watchlists, {total_tickers} total tickers")
    print("=" * 60)

    engine = ScreeningEngine(db=db)
    grand_total = 0
    failed = []

    for i, wl in enumerate(scannable):
        preset_label = wl["preset"] or "default"
        print(f"\n[{i + 1}/{len(scannable)}] {wl['name']} ({len(wl['tickers'])} tickers, preset: {preset_label})")
        try:
            results = engine.scan(
                tickers=wl["tickers"],
                watchlist_id=wl["id"],
                preset=wl.get("preset"),
            )
            grand_total += len(results)

            top_n = min(args.top, len(results))
            for r in results[:top_n]:
                eq = f"{r.entry_quality:.1f}" if r.entry_quality is not None else "N/A"
                rs = f"{r.risk_score:.1f}" if r.risk_score is not None else "N/A"
                ac = (r.asset_class or "eq")[:4].upper()
                opp, _ = compute_opportunity_score(
                    r.composite_score,
                    r.entry_quality,
                    r.macro_fit,
                    composite_fundamental=getattr(r, "composite_fundamental", None),
                )
                print(
                    f"  {r.rank:<4} {r.ticker:<8} [{ac}] OPP:{opp:>5.1f}  "
                    f"{r.composite_score:>6.2f}  EQ:{eq:<5} RISK:{rs:<5} {r.direction}"
                )
            if len(results) > top_n:
                print(f"  ... +{len(results) - top_n} more")

        except Exception as exc:
            print(f"  FAILED: {exc}")
            failed.append(wl["name"])

        if i < len(scannable) - 1:
            time.sleep(2)

    print("\n" + "=" * 60)
    print(f"Complete: {len(scannable) - len(failed)}/{len(scannable)} watchlists, {grand_total} tickers scored")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
