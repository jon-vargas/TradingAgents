"""Show alert rules and recent alert history.

Usage: python scripts/show_alerts.py
   or: make alerts
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.reporting.database import ResearchDatabase

db = ResearchDatabase("research.db")
rules = db.get_all_rules()
unread = db.get_unread_count()

print(f"Alert Rules: {len(rules)} ({sum(1 for r in rules if r['is_active'])} active)")
print(f"Unread Alerts: {unread}")

if rules:
    print()
    print(f"{'ID':<5} {'Ticker':<8} {'Type':<22} {'Active':<8}")
    print("-" * 45)
    for r in rules:
        status = "Yes" if r["is_active"] else "No"
        print(f"{r['id']:<5} {r['ticker']:<8} {r['alert_type']:<22} {status:<8}")

print()
history = db.get_alert_history(limit=5)
if history:
    print("Recent Alerts:")
    for a in history:
        read = "read" if a["is_read"] else "UNREAD"
        print(f"  [{read}] {a['ticker']} - {a['message'][:60]}")
else:
    print("No alert history yet.")
