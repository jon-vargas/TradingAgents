#!/usr/bin/env python3
import argparse
import datetime
import sys
from datetime import date as date_lib
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

load_dotenv()

from tradingagents.default_config import DEFAULT_CONFIG, get_config_for_mode
from tradingagents.research import ResearchAgent


def _parse_tickers(values):
    tickers = []
    seen = set()
    for raw in values:
        for part in raw.split(","):
            ticker = part.strip().upper()
            if ticker and ticker not in seen:
                tickers.append(ticker)
                seen.add(ticker)
    return tickers


def main() -> int:
    parser = argparse.ArgumentParser(description="Run batch analyses for multiple tickers.")
    parser.add_argument("--tickers", nargs="*", default=[], help="Tickers (space or comma separated)")
    parser.add_argument("--tickers-file", help="Path to a newline-separated ticker list")
    parser.add_argument("--date", default=date_lib.today().isoformat(), help="Analysis date YYYY-MM-DD")
    parser.add_argument("--delay", type=int, default=60, help="Delay between analyses (60s+ recommended for rate limits)")
    parser.add_argument("--mode", choices=["quick", "standard", "deep"], default="standard")
    parser.add_argument("--risk-profile", choices=["aggressive", "growth", "conservative"], default="growth", help="Risk profile")
    parser.add_argument("--output-dir", default="research_output")
    parser.add_argument("--summary-csv", default=None)
    parser.add_argument("--summary-html", default=None)
    parser.add_argument(
        "--investment-profile",
        choices=["growth_equity", "value_income", "speculative_tech", "macro_sensitive", "event_driven"],
        default=None,
        help="Investment profile for analysis focus directives",
    )
    args = parser.parse_args()

    tickers = _parse_tickers(args.tickers)
    if args.tickers_file:
        path = Path(args.tickers_file)
        if path.exists():
            file_values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            tickers.extend(_parse_tickers(file_values))

    if not tickers:
        print("No tickers provided. Use --tickers or --tickers-file.")
        return 1
    try:
        datetime.date.fromisoformat(args.date)
    except ValueError:
        print("Invalid --date. Use YYYY-MM-DD.")
        return 1

    config = get_config_for_mode(args.mode, DEFAULT_CONFIG)
    config["risk_profile"] = args.risk_profile

    # Resolve investment profile
    inv_profile = None
    if args.investment_profile:
        profiles = config.get("investment_profiles", {})
        inv_profile = profiles.get(args.investment_profile)
        if not inv_profile:
            print(f"WARNING: Profile '{args.investment_profile}' not found in config, using default.")

    profile_label = args.investment_profile or "auto"
    print(f"Mode: {args.mode} | Risk: {args.risk_profile} | Profile: {profile_label} | Delay: {args.delay}s")
    agent = ResearchAgent(
        config=config,
        output_dir=args.output_dir,
        auto_report=True,
        auto_save=True,
        debug=True,
    )

    print(f"Starting batch analysis for {len(tickers)} tickers on {args.date}...")
    agent.batch_analyze(
        tickers=tickers,
        date=args.date,
        delay_seconds=args.delay,
        save_summary=True,
        summary_path=args.summary_csv,
        summary_html_path=args.summary_html,
        investment_profile=inv_profile,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
