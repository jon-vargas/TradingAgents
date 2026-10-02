#!/usr/bin/env python3
"""
Test script to verify QA fixes for report generation.
Tests: Latest models, smart truncation, markdown conversion, Perplexity integration.
"""

import os
import re
import sys
import time
from datetime import datetime
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

# Load environment variables
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Verify API keys
print("=" * 60)
print("🔑 API KEY CHECK")
print("=" * 60)
openai_key = os.getenv("OPENAI_API_KEY")
perplexity_key = os.getenv("PERPLEXITY_API_KEY")
print(f"  OPENAI_API_KEY: {'✅ Set' if openai_key else '❌ Missing'}")
print(f"  PERPLEXITY_API_KEY: {'✅ Set' if perplexity_key else '❌ Missing'}")

if not openai_key:
    print("\n❌ OPENAI_API_KEY is required. Exiting.")
    sys.exit(1)

print("\n" + "=" * 60)
print("📊 STARTING QA TEST: ASTS Deep Analysis")
print("=" * 60)

# Import after env check
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting import generate_html_report

# Configure for deep mode with latest models
config = DEFAULT_CONFIG.copy()

# Use latest OpenAI GPT-5.2 chat models
config["llm_provider"] = "openai"
config["deep_think_llm"] = "gpt-5.2-chat-latest"  # Advanced chat model
config["quick_think_llm"] = "gpt-5.2-chat-latest"  # Same model for consistency

# Enable deep analysis mode
config["analysis_mode"] = "deep"
config["use_perplexity"] = True
config["max_debate_rounds"] = 2
config["max_risk_discuss_rounds"] = 2

print(f"\n📋 Configuration:")
print(f"  • LLM Provider: {config['llm_provider']}")
print(f"  • Deep Think Model: {config['deep_think_llm']}")
print(f"  • Quick Think Model: {config['quick_think_llm']}")
print(f"  • Analysis Mode: {config['analysis_mode']}")
print(f"  • Perplexity Enabled: {config['use_perplexity']}")
print(f"  • Debate Rounds: {config['max_debate_rounds']}")

# Initialize graph
print("\n🔧 Initializing TradingAgentsGraph...")
ta = TradingAgentsGraph(debug=True)

# Apply config
ta.config = config

# Run analysis
ticker = "ASTS"
analysis_date = datetime.now().strftime("%Y-%m-%d")

print(f"\n🚀 Running deep analysis for {ticker} on {analysis_date}...")
start_time = time.time()

try:
    # Run the analysis
    final_state, decision = ta.propagate(ticker, analysis_date)
    
    duration = time.time() - start_time
    
    print(f"\n✅ Analysis complete in {duration:.1f} seconds")
    print(f"📊 Decision: {decision}")
    
    # Generate report with fixes
    print("\n📝 Generating HTML report with QA fixes...")
    
    output_dir = "./research_output"
    os.makedirs(output_dir, exist_ok=True)
    
    report_path = os.path.join(output_dir, f"{ticker}_{analysis_date}_qa_test_report.html")
    
    generate_html_report(
        state=final_state,
        ticker=ticker,
        analysis_date=analysis_date,
        decision=decision,
        config=config,
        duration_seconds=duration,
        output_path=report_path
    )
    
    print(f"\n✅ Report saved to: {report_path}")
    
    # Verify report content
    print("\n" + "=" * 60)
    print("🔍 QA VERIFICATION")
    print("=" * 60)
    
    with open(report_path, 'r') as f:
        html_content = f.read()
    
    # Check 1: Model info
    if "gpt-5.2" in html_content:
        print("  ✅ Model (gpt-5.2) referenced in report")
    else:
        print("  ⚠️ Model info not found in report")
    
    # Check 2: No raw markdown in executive summary
    if "**Conservative" in html_content or "**Aggressive" in html_content:
        print("  ⚠️ Raw markdown still present in executive summary")
    else:
        print("  ✅ Executive summary markdown cleaned")
    
    # Check 3: Tables converted
    if "<table" in html_content:
        print("  ✅ Markdown tables converted to HTML")
    else:
        print("  ⚠️ No HTML tables found (may not have been in source)")
    
    # Check 4: Sections not truncated mid-sentence
    truncation_issues = html_content.count("...")
    if truncation_issues < 3:
        print(f"  ✅ Minimal truncation markers ({truncation_issues})")
    else:
        print(f"  ⚠️ Multiple truncation markers found ({truncation_issues})")

    # Check 4b: Risk cards end with full sentences
    risk_contents = re.findall(r'class="risk-content">([^<]+)</div>', html_content)
    risk_bad = [text for text in risk_contents if not re.search(r'[.!?]$', text.strip())]
    if risk_bad:
        print(f"  ⚠️ Risk cards with incomplete sentences: {len(risk_bad)}")
    else:
        print("  ✅ Risk cards end with full sentences")
    
    # Check 5: Confidence score
    if 'class="metric-value">85%' in html_content:
        print("  ⚠️ Confidence still showing default 85%")
    else:
        print("  ✅ Dynamic confidence score calculated")
    
    # Check 6: Perplexity data
    if "citation" in html_content.lower() or "source:" in html_content.lower():
        print("  ✅ Perplexity citations found in report")
    else:
        print("  ⚠️ No Perplexity citations detected")
    
    print("\n" + "=" * 60)
    print(f"🎉 TEST COMPLETE - Report: {report_path}")
    print("=" * 60)

except Exception as e:
    duration = time.time() - start_time
    print(f"\n❌ Error after {duration:.1f}s: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
