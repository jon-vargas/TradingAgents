#!/usr/bin/env python3
import argparse
from pathlib import Path

from tradingagents.reporting.report_diff import diff_reports


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff two report HTML files.")
    parser.add_argument("--file-a", help="Path to first report HTML")
    parser.add_argument("--file-b", help="Path to second report HTML")
    parser.add_argument("--ticker", help="Ticker symbol (used with --date-a/--date-b)")
    parser.add_argument("--date-a", help="First report date YYYY-MM-DD")
    parser.add_argument("--date-b", help="Second report date YYYY-MM-DD")
    parser.add_argument("--output-dir", default="research_output", help="Report output directory")
    parser.add_argument("--output", help="Optional path to save diff")
    args = parser.parse_args()

    try:
        output = diff_reports(
            file_a=args.file_a,
            file_b=args.file_b,
            ticker=args.ticker,
            date_a=args.date_a,
            date_b=args.date_b,
            output_dir=args.output_dir,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Diff saved to {args.output}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
