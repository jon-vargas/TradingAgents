#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.backtesting import BacktestEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="Run backtesting against stored analyses.")
    parser.add_argument("--ticker", help="Filter by ticker symbol")
    parser.add_argument("--limit", type=int, default=50, help="Max analyses to evaluate")
    parser.add_argument("--lookahead", nargs="*", type=int, default=[7, 14, 30], help="Lookahead days")
    parser.add_argument("--start-date", help="Filter analyses on/after YYYY-MM-DD")
    parser.add_argument("--end-date", help="Filter analyses on/before YYYY-MM-DD")
    parser.add_argument("--slippage-bps", type=float, default=0.0, help="Slippage in bps (each side)")
    parser.add_argument("--transaction-cost-bps", type=float, default=0.0, help="Transaction cost in bps (each side)")
    args = parser.parse_args()

    engine = BacktestEngine()
    results = engine.run(
        ticker=args.ticker,
        limit=args.limit,
        lookahead_days=args.lookahead,
        start_date=args.start_date,
        end_date=args.end_date,
        slippage_bps=args.slippage_bps,
        transaction_cost_bps=args.transaction_cost_bps,
    )

    print(f"Backtest complete: {results['updated']} updated, {results['skipped']} skipped")
    if results.get("skip_reasons"):
        print("Skip reasons:")
        for reason, count in sorted(results["skip_reasons"].items()):
            print(f"  {reason}: {count}")
    print(f"Win rate (signed 7d): {results.get('win_rate', 0):.2%}")
    print(f"7d directional accuracy: {results.get('accuracy', 0):.2%}")
    signed = results.get("avg_signed_return_7d")
    print(f"Signed return 7d (HOLD excluded): {signed:.2%}" if signed is not None else "Signed return 7d: n/a")
    print(f"Tape return 7d (unsigned): {results.get('avg_return', 0):.2%}")
    vol = results.get("return_vol_7d")
    print(f"7d return/vol: {vol:.4f}" if vol is not None else "7d return/vol: n/a (need n>=10)")
    if results.get("backtest_run_id"):
        print(f"Backtest run ID: {results.get('backtest_run_id')}")
    if results.get("slippage_bps") or results.get("transaction_cost_bps"):
        print(f"Costs applied: slippage={results.get('slippage_bps', 0):.2f}bps, "
              f"transaction={results.get('transaction_cost_bps', 0):.2f}bps")
    attribution = results.get("agent_attribution", {})
    if attribution:
        def _row(name, payload):
            payload = payload or {}
            n = payload.get("n", 0)
            skipped = payload.get("skipped", 0)
            return (
                f"  {name}: {payload.get('win_rate', 0):.2%} "
                f"({payload.get('wins', 0)}W/{payload.get('losses', 0)}L, n={n}, skipped={skipped})"
            )
        print("Researcher stance vs 7d tape:")
        print(_row("Bull", attribution.get("bull")))
        print(_row("Bear", attribution.get("bear")))
        print(_row("Research", attribution.get("research")))

    try:
        from tradingagents.backtesting import compute_calibration
        calibration = compute_calibration()
        print("Calibration bins (avg_confidence, accuracy, count):")
        for avg_conf, accuracy, count in calibration.get("bins", []):
            if avg_conf < 0 or accuracy < 0:
                print(f"  n/a -> n/a ({count})")
            else:
                print(f"  {avg_conf:>6.2f}% -> {accuracy:>5.1%} ({count})")
    except Exception as exc:
        print(f"Calibration unavailable: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
