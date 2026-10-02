#!/usr/bin/env python3
"""
Simulate Browser Rendering - Check what the UI should display
"""

import requests
from datetime import datetime

BASE_URL = "http://127.0.0.1:8000"

def simulate_ui_rendering():
    print("=" * 80)
    print("SIMULATING BROWSER UI RENDERING")
    print("=" * 80)
    print()
    
    # 1. Fetch watchlists (what the browser does on page load)
    print("1️⃣  Fetching watchlists via API (GET /api/watchlists)...")
    try:
        resp = requests.get(f"{BASE_URL}/api/watchlists")
        resp.raise_for_status()
        watchlists = resp.json()
        print(f"   ✅ Received {len(watchlists)} watchlists")
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return
    
    print()
    
    # 2. Filter auto-watchlists (what JavaScript does)
    print("2️⃣  Filtering for auto-generated watchlists...")
    auto_wls = [w for w in watchlists if w.get("source") == "auto"]
    print(f"   ✅ Found {len(auto_wls)} auto-watchlist(s)")
    
    if not auto_wls:
        print("   ⚠️  No auto-watchlists found!")
        return
    
    print()
    
    # 3. Simulate rendering each auto-watchlist row
    print("3️⃣  Simulating table row rendering...")
    print()
    
    for wl in auto_wls:
        name = wl.get("name", "Unnamed")
        wl_id = wl.get("id")
        source = wl.get("source")
        ticker_count = wl.get("ticker_count", 0)
        created_at = wl.get("created_at", "")
        expires_at = wl.get("expires_at", "")
        theme_rationale = wl.get("theme_rationale", "")
        tickers = wl.get("tickers", "").split(",") if wl.get("tickers") else []
        default_preset = wl.get("default_preset", "")
        default_profile = wl.get("default_investment_profile", "")
        
        # Calculate preview
        preview = ", ".join(tickers[:5])
        if len(tickers) > 5:
            preview += f" +{len(tickers) - 5} more"
        
        # Calculate days left until expiration
        if expires_at:
            try:
                exp_date = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                now = datetime.now(exp_date.tzinfo)
                days_left = (exp_date - now).days
            except:
                days_left = None
        else:
            days_left = None
        
        # Calculate created ago
        if created_at:
            try:
                created_date = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                now = datetime.now(created_date.tzinfo)
                days_ago = (now - created_date).days
            except:
                days_ago = None
        else:
            days_ago = None
        
        print(f"   📋 Watchlist Row for ID={wl_id}:")
        print(f"      Name: {name}")
        print(f"      Source: {source}")
        print(f"      Ticker Count: {ticker_count}")
        print(f"      Ticker Preview: {preview}")
        print()
        
        print(f"   🏷️  Badges that should appear:")
        
        # Preset badge
        if default_preset:
            preset_display = default_preset.replace('_', ' ')
            print(f"      - Preset: {preset_display} (amber background)")
        
        # Profile badge
        if default_profile:
            profile_display = default_profile.replace('_', ' ')
            print(f"      - Profile: {profile_display} (accent color)")
        
        # Auto badge
        print(f"      - Auto (purple/indigo #6366f1, white text)")
        
        # Expiration badge
        if days_left is not None:
            if days_left <= 7:
                print(f"      - Expires in {days_left}d (amber background)")
            else:
                print(f"      - Renews in {days_left}d (accent color)")
        
        # Info icon with tooltip
        if theme_rationale:
            print(f"      - Info icon (ⓘ) with tooltip: {theme_rationale}")
        
        print()
        
        # Created ago text
        if days_ago is not None:
            print(f"   📅 Created-ago text: Created {days_ago}d ago")
        
        print()
        
        print(f"   🔘 Action buttons that should appear:")
        print(f"      - View (all tickers)")
        print(f"      - Clone (as editable copy)")
        print(f"      - Export (as CSV)")
        print(f"      - Pin (keep permanently, purple button #6366f1)")
        print(f"      - Remove (delete button, red)")
        print()
        
        print(f"   📊 Expected DOM attributes:")
        print(f"      <tr data-wl-id=\"{wl_id}\">")
        print(f"        <td><strong>{name}</strong> [badges] [lifecycle]</td>")
        print(f"        <td><small>{preview}</small></td>")
        print(f"        <td>{ticker_count}</td>")
        print(f"        <td><span class=\"badge\">{source}</span></td>")
        print(f"        <td>[action buttons]</td>")
        print(f"      </tr>")
        
        print()
        print("-" * 80)
        print()
    
    # 4. Check Auto-Discovery card
    print("4️⃣  Checking Auto-Discovery card status...")
    try:
        resp = requests.get(f"{BASE_URL}/api/screening/auto-discovery/status")
        resp.raise_for_status()
        ad_status = resp.json()
        
        state = ad_status.get("state", "unknown")
        budget = ad_status.get("budget", {})
        last_run = ad_status.get("last_run")
        
        print(f"   ✅ Auto-discovery status received")
        print()
        print(f"   📊 Card should display:")
        print(f"      State badge: {state}")
        print(f"      Budget badge: {budget.get('remaining', 0)} calls remaining")
        
        if last_run:
            print(f"      Status text: Last run at {last_run.get('timestamp', 'N/A')}")
            print(f"      Summary: {last_run.get('watchlists_created', 0)} created, {last_run.get('watchlists_renewed', 0)} renewed")
        else:
            print(f"      Status text: No runs yet.")
        
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print()
    print("=" * 80)
    print("SIMULATION COMPLETE")
    print("=" * 80)
    print()
    print("🔍 WHAT TO CHECK IN THE ACTUAL UI:")
    print()
    print("1. Open http://127.0.0.1:8000/screener in your browser")
    print("2. Do a hard refresh: Ctrl+Shift+R (Windows/Linux) or Cmd+Shift+R (Mac)")
    print("3. Scroll to the 'Watchlists' section")
    print("4. Look for the table with watchlist rows")
    print("5. Find the row matching the details above")
    print("6. Verify all badges and buttons are present")
    print()
    print("If the row is NOT visible:")
    print("- Open browser DevTools (F12)")
    print("- Go to Console tab")
    print("- Look for JavaScript errors")
    print("- Go to Network tab")
    print("- Check if /api/watchlists request succeeded")
    print("- Go to Elements tab")
    print("- Search for 'watchlists-body' element")
    print("- Check if the tr element with the watchlist exists in the DOM")
    print()

if __name__ == "__main__":
    simulate_ui_rendering()
