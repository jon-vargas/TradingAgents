#!/usr/bin/env python3
import argparse
import csv
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.reporting import ResearchDatabase
from tradingagents.backtesting.metrics import compute_researcher_attribution, summarize_analyses


def write_csv(output_path: Path, rows):
    if not rows:
        output_path.write_text("", encoding="utf-8")
        return
    with output_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(rows[0].keys())
        for row in rows:
            writer.writerow(list(row))


def _select_analyses_for_run(db: ResearchDatabase, run) -> list:
    if run.start_date or run.end_date:
        return db.get_analyses_by_date_range(
            start_date=run.start_date or None,
            end_date=run.end_date or None,
            ticker=run.ticker or None,
            limit=run.limit_count or None,
        )
    if run.ticker:
        return db.get_analyses_by_ticker(run.ticker, run.limit_count or 50)
    return db.get_recent_analyses(run.limit_count or 50)


def _format_attribution(attribution: dict) -> str:
    if not attribution:
        return "n/a"

    def _one(label, row):
        row = row or {}
        n = row.get("n", 0)
        skipped = row.get("skipped", 0)
        return (
            f"{label} {row.get('win_rate', 0):.2%} "
            f"({row.get('wins', 0)}W/{row.get('losses', 0)}L n={n} skip={skipped})"
        )

    return (
        f"{_one('Bull', attribution.get('bull'))} / "
        f"{_one('Bear', attribution.get('bear'))} / "
        f"{_one('Research', attribution.get('research'))}"
    )


def write_html(output_path: Path, runs, *, filters: str, db: ResearchDatabase):
    total = len(runs)
    avg_return = 0.0
    avg_win = 0.0
    avg_acc = 0.0
    if total:
        avg_return = sum(run.avg_return or 0.0 for run in runs) / total
        avg_win = sum(run.win_rate or 0.0 for run in runs) / total
        avg_acc = sum(run.accuracy or 0.0 for run in runs) / total

    table_rows = []
    for run in runs:
        analyses = _select_analyses_for_run(db, run)
        attribution = compute_researcher_attribution(analyses)
        attribution_text = _format_attribution(attribution)
        book = summarize_analyses(analyses)
        signed_7d = book.get("avg_signed_return_7d")
        vol_7d = book.get("return_vol_7d")
        range_label = f"{run.start_date or '-'} → {run.end_date or '-'}"
        costs = f"{run.slippage_bps or 0:.1f}/{run.transaction_cost_bps or 0:.1f} bps"
        signed_label = f"{signed_7d:.2%}" if signed_7d is not None else "n/a"
        vol_label = f"{vol_7d:.2f}" if vol_7d is not None else "n/a"
        persisted_signed = run.avg_signed_return_7d
        if persisted_signed is not None:
            signed_label = f"{persisted_signed:.2%}"
        table_rows.append(
            "<tr>"
            f"<td>{run.run_at}</td>"
            f"<td>{run.ticker or '-'}</td>"
            f"<td>{range_label}</td>"
            f"<td>{run.lookahead_days or '-'}</td>"
            f"<td>{costs}</td>"
            f"<td>{run.updated_count}/{run.skipped_count}</td>"
            f"<td>{attribution_text}</td>"
            f"<td>{signed_label}</td>"
            f"<td>{run.avg_return:.2%}</td>"
            f"<td>{run.win_rate:.2%}</td>"
            f"<td>{run.accuracy:.2%}</td>"
            f"<td>{vol_label}</td>"
            "</tr>"
        )

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Backtest Runs Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; padding: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
    th {{ background: #f5f5f5; }}
    .meta {{ color: #555; font-size: 14px; }}
  </style>
</head>
<body>
  <h1>Backtest Runs Report</h1>
  <p class="meta">
    Filters: {filters} |
    Total runs: {total} |
    Avg return: {avg_return:.2%} |
    Avg win rate: {avg_win:.2%} |
    Avg accuracy: {avg_acc:.2%}
  </p>
  <p class="meta">
    KPI Summary: avg_return={avg_return:.2%}, avg_win_rate={avg_win:.2%}, avg_accuracy={avg_acc:.2%}
  </p>
  <table>
    <thead>
      <tr>
        <th>Run At</th>
        <th>Ticker</th>
        <th>Date Range</th>
        <th>Lookahead</th>
        <th>Costs</th>
        <th>Updated/Skipped</th>
        <th>Attribution (stance vs tape)</th>
        <th>Signed 7d</th>
        <th>Tape 7d</th>
        <th>Win Rate</th>
        <th>Accuracy</th>
        <th>7d Ret/Vol</th>
      </tr>
    </thead>
    <tbody>
      {''.join(table_rows)}
    </tbody>
  </table>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate backtest run summary report.")
    parser.add_argument("--db-path", help="Path to research.db", default=None)
    parser.add_argument("--output-dir", default="research_output", help="Output directory")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--ticker", help="Filter by ticker symbol")
    parser.add_argument("--run-start", help="Filter runs on/after YYYY-MM-DD")
    parser.add_argument("--run-end", help="Filter runs on/before YYYY-MM-DD")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    exports_dir = output_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    db = ResearchDatabase(args.db_path)
    runs = db.get_backtest_runs(
        limit=args.limit,
        ticker=args.ticker,
        run_start_date=args.run_start,
        run_end_date=args.run_end,
    )
    rows = [run.__dict__ for run in runs]
    filter_parts = []
    if args.ticker:
        filter_parts.append(f"ticker={args.ticker}")
    if args.run_start:
        filter_parts.append(f"run_start={args.run_start}")
    if args.run_end:
        filter_parts.append(f"run_end={args.run_end}")
    filter_parts.append(f"limit={args.limit}")
    filters = ", ".join(filter_parts) if filter_parts else "none"

    csv_path = exports_dir / "backtest_runs.csv"
    html_path = exports_dir / "backtest_runs.html"
    write_csv(csv_path, rows)
    write_html(html_path, runs, filters=filters, db=db)

    print(f"Backtest runs CSV saved to: {csv_path}")
    print(f"Backtest runs HTML saved to: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
