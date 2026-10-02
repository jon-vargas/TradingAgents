"""List all available watchlists.

Usage: python scripts/list_watchlists.py
   or: make watchlists
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.reporting.database import ResearchDatabase

db = ResearchDatabase("research.db")
watchlists = db.get_watchlists()

print(f"{'ID':<5} {'Name':<32} {'Tickers':>8}  {'Source':<10} {'Category':<18} Description")
print("-" * 110)

for w in watchlists:
    tickers = w.get("tickers", "")
    count = len([t for t in tickers.split(",") if t.strip()]) if tickers else 0
    desc = (w.get("description") or "")[:36]
    cat = (w.get("category") or "")[:16]
    print(f"{w['id']:<5} {w['name']:<32} {count:>8}  {w.get('source', ''):10} {cat:<18} {desc}")

print(f"\nTotal: {len(watchlists)} watchlists")
print("\nUse with screening:  make screen-watchlist NAME=\"Dow Jones 30\"")
print("                     make screen-watchlist ID=5 PRESET=momentum_hunter")
