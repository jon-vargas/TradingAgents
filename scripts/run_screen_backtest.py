"""Backtest a screening run by ID.

Usage: python scripts/run_screen_backtest.py <run_id>
   or: make screen-backtest RUN_ID=1
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.backtesting.engine import backtest_screening_run

run_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
if run_id == 0:
    print("RUN_ID is required.")
    sys.exit(1)

result = backtest_screening_run(run_id)

if result.get("error"):
    print(f"Error: {result['error']}")
    sys.exit(1)

print(f"Run #{run_id}: {result.get('tickers_evaluated', 0)} tickers evaluated")
print(f"Correlation (score vs 7d return): {result.get('correlation_7d', 'N/A')}")
print(f"Hit rate top-5 (7d): {result.get('hit_rate_top5_7d', 'N/A')}")
