"""Run a single-ticker analysis from the command line.

Usage: python scripts/run_analyze.py NVDA [--date 2026-02-12] [--mode standard] [--risk-profile growth] [--investment-profile growth_equity]
   or: make analyze TICKER=NVDA DATE=2026-02-12 MODE=standard RISK=growth PROFILE=growth_equity
"""
import argparse
import sys
from datetime import date as date_lib
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

load_dotenv()

from tradingagents.default_config import DEFAULT_CONFIG, get_config_for_mode
from tradingagents.research import ResearchAgent


def main() -> int:
    parser = argparse.ArgumentParser(description="Run single-ticker analysis.")
    parser.add_argument("ticker", help="Stock ticker symbol (e.g. NVDA)")
    parser.add_argument("--date", default=date_lib.today().isoformat(), help="Analysis date YYYY-MM-DD")
    parser.add_argument("--mode", choices=["quick", "standard", "deep"], default="standard", help="Research depth")
    parser.add_argument("--risk-profile", choices=["aggressive", "growth", "conservative"], default="growth", help="Risk profile")
    parser.add_argument("--output-dir", default="research_output", help="Output directory for reports")
    parser.add_argument(
        "--investment-profile",
        choices=["growth_equity", "value_income", "speculative_tech", "macro_sensitive", "event_driven"],
        default=None,
        help="Investment profile for analysis focus directives",
    )
    args = parser.parse_args()

    ticker = args.ticker.strip().upper()
    if not ticker:
        print("Ticker is required.")
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
    print(f"Analyzing {ticker} | Date: {args.date} | Mode: {args.mode} | Risk: {args.risk_profile} | Profile: {profile_label}")
    print()

    agent = ResearchAgent(
        config=config,
        output_dir=args.output_dir,
        auto_report=True,
        auto_save=True,
        debug=True,
    )

    result = agent.analyze(
        ticker,
        args.date,
        investment_profile=inv_profile,
        investment_profile_key=args.investment_profile or None,
    )

    print()
    print(f"Decision: {result.decision}")
    print(f"Confidence: {result.confidence}%")
    print(f"Analysis ID: {result.analysis_id}")
    if result.html_path:
        print(f"Report: {result.html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
