#!/bin/bash

# Quick Screener UI State Check
# ==============================

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔍 TradingAgents Screener UI - Quick Check"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# Check if server is running
echo "1️⃣  Checking server status..."
if curl -s -f -o /dev/null http://127.0.0.1:8000/ 2>/dev/null; then
    echo "   ✅ Server is running at http://127.0.0.1:8000"
else
    echo "   ❌ Server is not running!"
    echo "   Start it with: python -m webapp.app"
    exit 1
fi
echo

# Check auto-watchlists
echo "2️⃣  Checking for auto-generated watchlists..."
AUTO_COUNT=$(curl -s http://127.0.0.1:8000/api/watchlists | python3 -c "import sys, json; data = json.load(sys.stdin); print(sum(1 for w in data if w.get('source') == 'auto'))")

if [ "$AUTO_COUNT" -gt 0 ]; then
    echo "   ✅ Found $AUTO_COUNT auto-generated watchlist(s)"
    echo
    echo "   Details:"
    curl -s http://127.0.0.1:8000/api/watchlists | python3 -c "
import sys, json
from datetime import datetime

data = json.load(sys.stdin)
auto_wls = [w for w in data if w.get('source') == 'auto']

for wl in auto_wls:
    name = wl.get('name', 'Unnamed')
    wl_id = wl.get('id')
    ticker_count = wl.get('ticker_count', 0)
    created = wl.get('created_at', '')
    expires = wl.get('expires_at', '')
    
    # Calculate days until expiration
    if expires:
        try:
            exp_date = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            days_left = (exp_date - datetime.now(exp_date.tzinfo)).days
            exp_str = f'{days_left} days'
        except:
            exp_str = 'Unknown'
    else:
        exp_str = 'N/A'
    
    print(f'      - {name}')
    print(f'        ID: {wl_id}')
    print(f'        Tickers: {ticker_count}')
    print(f'        Expires in: {exp_str}')
    print()
"
else
    echo "   ⚠️  No auto-generated watchlists found"
    echo "   Run auto-discovery from the screener page to create them."
fi
echo

# Check auto-discovery status
echo "3️⃣  Checking Auto-Discovery card status..."
curl -s http://127.0.0.1:8000/api/screening/auto-discovery/status | python3 -c "
import sys, json

try:
    data = json.load(sys.stdin)
    state = data.get('state', 'unknown')
    budget = data.get('budget', {})
    last_run = data.get('last_run')
    
    print(f'   ✅ Status retrieved')
    print(f'      State: {state}')
    print(f'      Budget: {budget.get(\"remaining\", 0)} calls remaining')
    
    if last_run:
        print(f'      Last run: {last_run.get(\"timestamp\", \"N/A\")}')
        print(f'      Created: {last_run.get(\"watchlists_created\", 0)} watchlists')
        print(f'      Renewed: {last_run.get(\"watchlists_renewed\", 0)} watchlists')
    else:
        print(f'      Last run: None')
except:
    print('   ❌ Error parsing status')
"
echo

# Summary
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Summary"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

if [ "$AUTO_COUNT" -gt 0 ]; then
    echo "✅ Backend state is healthy"
    echo
    echo "🌐 To verify in the UI:"
    echo "   1. Open: http://127.0.0.1:8000/screener"
    echo "   2. Hard refresh: Ctrl+Shift+R (or Cmd+Shift+R on Mac)"
    echo "   3. Scroll to 'Watchlists' section"
    echo "   4. Look for the auto-watchlist in the table"
    echo
    echo "🔍 If not visible:"
    echo "   - Check browser console (F12) for JavaScript errors"
    echo "   - Use browser Find (Ctrl+F) to search for 'Auto:' or the watchlist name"
    echo "   - Run: python test_screener_ui.html (opens diagnostic dashboard)"
    echo
else
    echo "⚠️  No auto-watchlists exist yet"
    echo
    echo "📝 To create auto-watchlists:"
    echo "   1. Open: http://127.0.0.1:8000/screener"
    echo "   2. Find the 'Auto-Discovery (Manual)' card"
    echo "   3. Click 'Run Auto-Discovery Now'"
    echo "   4. Wait for completion"
    echo "   5. Refresh the page"
    echo
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
