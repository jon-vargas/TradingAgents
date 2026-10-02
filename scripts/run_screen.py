"""Run a screening scan from the command line.

Supports three modes:
  1. Raw tickers:     python scripts/run_screen.py "AAPL MSFT NVDA" [preset]
  2. Watchlist by ID: python scripts/run_screen.py --id 5 [--preset momentum_hunter]
  3. Watchlist name:  python scripts/run_screen.py --name "Dow Jones 30" [--preset value_fisher]

Make targets:
  make screen WATCHLIST="AAPL MSFT NVDA" PRESET=momentum_hunter
  make screen-watchlist NAME="AI Infrastructure Metals" PRESET=momentum_hunter
  make screen-watchlist ID=5
"""
import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.screening import ScreeningEngine
from tradingagents.reporting.database import ResearchDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a screening scan.")
    parser.add_argument("tickers", nargs="*", default=[], help="Raw tickers (space or comma separated)")
    parser.add_argument("--id", type=int, default=0, help="Watchlist ID from database")
    parser.add_argument("--name", type=str, default="", help="Watchlist name from database")
    parser.add_argument("--preset", type=str, default=None, help="Screening preset (momentum_hunter, value_fisher, earnings_play)")
    parser.add_argument("--mode", type=str, default="standard", choices=["standard", "reversal_buildup", "base_coil"],
                        help="Scan mode: standard (composite rank) or reversal_buildup")
    parser.add_argument("--top", type=int, default=20, help="Show top N results (default: 20)")
    args = parser.parse_args()

    db = ResearchDatabase("research.db")
    tickers = []
    source_label = "custom"

    # Resolve tickers from watchlist ID
    if args.id:
        wl = db.get_watchlist(args.id)
        if not wl:
            print(f"Error: Watchlist ID {args.id} not found.")
            print("Run 'make watchlists' to see available watchlists.")
            return 1
        tickers = [t.strip() for t in wl["tickers"].split(",") if t.strip()]
        source_label = wl["name"]
    # Resolve tickers from watchlist name
    elif args.name:
        watchlists = db.get_watchlists()
        wl = next((w for w in watchlists if w["name"].lower() == args.name.lower()), None)
        if not wl:
            print(f"Error: Watchlist '{args.name}' not found.")
            print("Available watchlists:")
            for w in watchlists:
                print(f"  [{w['id']}] {w['name']}")
            return 1
        tickers = [t.strip() for t in wl["tickers"].split(",") if t.strip()]
        source_label = wl["name"]
    # Raw tickers from positional args
    elif args.tickers:
        raw = " ".join(args.tickers)
        tickers = [t.strip().upper() for t in raw.replace(",", " ").split() if t.strip()]
        source_label = "custom"
    else:
        print("Provide tickers, --id, or --name.")
        print("  python scripts/run_screen.py AAPL MSFT NVDA")
        print("  python scripts/run_screen.py --name 'Dow Jones 30'")
        print("  python scripts/run_screen.py --id 5 --preset momentum_hunter")
        return 1

    if not tickers:
        print("No tickers found in the specified source.")
        return 1

    preset_label = args.preset or "default"
    mode_label = args.mode or "standard"
    print(f"Screening: {source_label} ({len(tickers)} tickers) | Preset: {preset_label} | Mode: {mode_label}")
    print()

    engine = ScreeningEngine(db=db)
    if args.mode == "reversal_buildup":
        criteria_meta = {"strategy": "reversal_buildup"}
    elif args.mode == "base_coil":
        criteria_meta = {"strategy": "base_coil"}
    else:
        criteria_meta = None
    persist_id = 0
    if args.id:
        persist_id = args.id
    elif args.name:
        persist_id = int(wl.get("id") or 0) if wl else 0
    results = engine.scan(
        tickers=tickers,
        preset=args.preset,
        criteria_meta=criteria_meta,
        watchlist_id=persist_id or None,
    )

    top_n = min(args.top, len(results))
    if args.mode == "reversal_buildup":
        print(f"{'Rank':<5} {'Ticker':<8} {'Phase':<12} {'Side':<6} {'Buildup':>8} {'Score':>7} {'Direction':<10}")
        print("-" * 70)
        for r in results[:top_n]:
            payload = (r.signals or {}).get("_reversal_buildup") or {}
            phase = str(payload.get("phase") or "-")
            side = str(payload.get("side") or "-")
            buildup = payload.get("score")
            buildup_s = f"{float(buildup):.1f}" if buildup is not None else "N/A"
            print(
                f"{r.rank:<5} {r.ticker:<8} {phase:<12} {side:<6} {buildup_s:>8} "
                f"{r.composite_score:>7.2f} {r.direction:<10}"
            )
    elif args.mode == "base_coil":
        print(f"{'Rank':<5} {'Ticker':<8} {'Coil':>7} {'Days':>5} {'Squeeze':>8} {'Vol':>6} {'Trend':<6}")
        print("-" * 55)
        for r in results[:top_n]:
            payload = (r.signals or {}).get("_base_coil") or {}
            coil = payload.get("score")
            coil_s = f"{float(coil):.1f}" if coil is not None else "N/A"
            days = payload.get("days_in_box") if payload.get("days_in_box") is not None else "-"
            squeeze = payload.get("squeeze")
            squeeze_s = f"{float(squeeze):.2f}" if squeeze is not None else "N/A"
            vol = payload.get("vol_ratio")
            vol_s = f"{float(vol):.2f}" if vol is not None else "N/A"
            trend = "yes" if payload.get("trend_pass") else "no"
            print(
                f"{r.rank:<5} {r.ticker:<8} {coil_s:>7} {days:>5} {squeeze_s:>8} {vol_s:>6} {trend:<6}"
            )
    else:
        # Columns: rank | ticker | asset_class tag | score | entry | risk | direction.
        print(f"{'Rank':<5} {'Ticker':<8} {'Type':<4} {'Score':>7} {'Entry':>7} {'Risk':>6} {'Direction':<10}")
        print("-" * 55)
        for r in results[:top_n]:
            eq = f"{r.entry_quality:.1f}" if r.entry_quality is not None else "N/A"
            rs = f"{r.risk_score:.1f}" if r.risk_score is not None else "N/A"
            ac = (r.asset_class or "eq")[:4].upper()
            print(
                f"{r.rank:<5} {r.ticker:<8} {ac:<4} {r.composite_score:>7.2f} "
                f"{eq:>7} {rs:>6} {r.direction:<10}"
            )

    if len(results) > top_n:
        print(f"  ... and {len(results) - top_n} more")

    bullish = sum(1 for r in results if r.direction == "bullish")
    avg = sum(r.composite_score for r in results) / len(results) if results else 0
    eq_vals = [r.entry_quality for r in results if r.entry_quality is not None]
    avg_eq = sum(eq_vals) / len(eq_vals) if eq_vals else 0
    eq_label = f" | Avg entry: {avg_eq:.1f}" if eq_vals else ""
    risk_vals = [r.risk_score for r in results if r.risk_score is not None]
    avg_risk = sum(risk_vals) / len(risk_vals) if risk_vals else 0
    risk_label = f" | Avg risk: {avg_risk:.1f}" if risk_vals else ""
    # Asset-class distribution: quick sanity check that ETF watchlists come
    # through tagged correctly when you run them via CLI.
    from collections import Counter
    class_counts = Counter((r.asset_class or "equity").lower() for r in results)
    class_label = (
        " | " + ", ".join(f"{k}={v}" for k, v in class_counts.most_common())
        if class_counts and len(class_counts) > 1
        else ""
    )
    print(
        f"\n{len(results)} tickers scored | {bullish} bullish, "
        f"{len(results) - bullish} non-bullish | Avg score: {avg:.1f}"
        f"{eq_label}{risk_label}{class_label}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
