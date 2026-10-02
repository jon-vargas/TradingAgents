#!/usr/bin/env python3
"""
TradingAgents Setup Verification Script
Run this to check if your environment is properly configured.
"""

import sys
import os


def print_header(text):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}\n")


def check_python_version():
    print_header("Checking Python Version")
    version = sys.version_info
    print(f"Python version: {version.major}.{version.minor}.{version.micro}")
    print(f"Python executable: {sys.executable}")

    if version.major == 3 and version.minor >= 10:
        print("PASS  Python version is compatible (3.10+)")
        return True
    else:
        print("FAIL  Python version should be 3.10 or higher")
        return False


def check_dependencies():
    print_header("Checking Required Dependencies")

    # module_name -> pip package name
    dependencies = {
        # Core LLM
        "langchain_openai": "langchain-openai",
        "langchain_experimental": "langchain-experimental",
        "langchain_anthropic": "langchain-anthropic",
        "langchain_google_genai": "langchain-google-genai",
        "langgraph": "langgraph",
        "perplexity": "perplexityai",
        # Data
        "pandas": "pandas",
        "numpy": "numpy",
        "yfinance": "yfinance",
        "chromadb": "chromadb",
        "stockstats": "stockstats",
        "finnhub": "finnhub-python",
        # News / social
        "praw": "praw",
        "feedparser": "feedparser",
        "requests": "requests",
        # Web UI
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "jinja2": "jinja2",
        "aiofiles": "aiofiles",
        # PDF / reports
        "tiktoken": "tiktoken",
        # CLI
        "typer": "typer",
        "rich": "rich",
        "questionary": "questionary",
        # Utilities
        "dotenv": "python-dotenv",
        "dateutil": "python-dateutil",
    }

    all_good = True
    for module, package in dependencies.items():
        try:
            __import__(module)
            print(f"  PASS  {package}")
        except ImportError:
            print(f"  FAIL  {package} — NOT INSTALLED")
            all_good = False

    # WeasyPrint is optional (PDF generation)
    try:
        __import__("weasyprint")
        print(f"  PASS  weasyprint")
    except ImportError:
        print(f"  WARN  weasyprint — not installed (PDF export disabled)")
        print(f"        Install with: pip install weasyprint")

    return all_good


def check_environment_variables():
    print_header("Checking Environment Variables")

    # Check if .env file exists
    env_file = ".env"
    if not os.path.exists(env_file):
        print(f"FAIL  .env file not found")
        print(f"      Create it: cp .env.example .env")
        return False
    else:
        print(f"PASS  .env file exists")

    # Load environment variables
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        print("FAIL  python-dotenv not installed, cannot load .env file")
        return False

    all_good = True

    # Required keys
    required_keys = {
        "OPENAI_API_KEY": {
            "placeholder": "your_openai_api_key_here",
            "help": "https://platform.openai.com/api-keys",
            "required": True,
        },
        "FINNHUB_API_KEY": {
            "placeholder": "your_finnhub_api_key_here",
            "help": "https://finnhub.io/register",
            "required": True,
        },
    }

    # Optional keys
    optional_keys = {
        "ALPHA_VANTAGE_API_KEY": {
            "placeholder": "your_alpha_vantage_api_key_here",
            "help": "https://www.alphavantage.co/support/#api-key",
        },
        "PERPLEXITY_API_KEY": {
            "placeholder": "your_perplexity_api_key_here",
            "help": "https://www.perplexity.ai/settings/api",
        },
        "ANTHROPIC_API_KEY": {
            "placeholder": "your_anthropic_api_key_here",
            "help": "https://console.anthropic.com/",
        },
        "GOOGLE_API_KEY": {
            "placeholder": "your_google_api_key_here",
            "help": "https://aistudio.google.com/apikey",
        },
    }

    for key, info in required_keys.items():
        val = os.getenv(key)
        if val and val != info["placeholder"]:
            print(f"  PASS  {key} is set")
        else:
            print(f"  FAIL  {key} is not set or using placeholder")
            print(f"        Get your key from: {info['help']}")
            all_good = False

    for key, info in optional_keys.items():
        val = os.getenv(key)
        if val and val != info["placeholder"]:
            print(f"  PASS  {key} is set")
        else:
            print(f"  INFO  {key} not set (optional)")

    return all_good


def check_tradingagents_import():
    print_header("Checking TradingAgents Package")

    checks = [
        ("tradingagents.graph.trading_graph", "TradingAgentsGraph"),
        ("tradingagents.research", "ResearchAgent"),
        ("tradingagents.screening.engine", "ScreeningEngine"),
        ("tradingagents.reporting", "ResearchDatabase"),
    ]

    all_good = True
    for module_path, class_name in checks:
        try:
            mod = __import__(module_path, fromlist=[class_name])
            getattr(mod, class_name)
            print(f"  PASS  {class_name}")
        except (ImportError, AttributeError) as e:
            print(f"  FAIL  {class_name}: {e}")
            all_good = False

    return all_good


def check_dataflow_modules():
    print_header("Checking Dataflow Modules")

    modules = [
        ("tradingagents.dataflows.y_finance", "get_YFin_data_online", "yfinance dataflow"),
        ("tradingagents.dataflows.yfinance_extended", "get_analyst_ratings", "yfinance extended"),
        ("tradingagents.dataflows.yfinance_extended", "get_estimate_revisions", "estimate revisions"),
        ("tradingagents.dataflows.yfinance_extended", "get_earnings_quality", "earnings quality"),
        ("tradingagents.dataflows.yfinance_extended", "compute_intrinsic_value", "intrinsic value (DCF)"),
        ("tradingagents.dataflows.cache", "get_cache", "data cache"),
        ("tradingagents.dataflows.risk_metrics", "compute_risk_metrics", "risk metrics"),
        ("tradingagents.dataflows.perplexity_api", "get_catalyst_pipeline", "perplexity catalysts"),
    ]

    all_good = True
    for module_path, func_name, label in modules:
        try:
            mod = __import__(module_path, fromlist=[func_name])
            getattr(mod, func_name)
            print(f"  PASS  {label}")
        except (ImportError, AttributeError) as e:
            print(f"  FAIL  {label}: {e}")
            all_good = False

    return all_good


def check_database():
    print_header("Checking Database")

    db_path = "research.db"
    if os.path.exists(db_path):
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        print(f"  PASS  {db_path} exists ({size_mb:.1f} MB)")
    else:
        print(f"  INFO  {db_path} not found (will be created on first run)")

    # Check SQLite availability
    try:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        print(f"  PASS  SQLite3 available (WAL mode supported)")
    except Exception as e:
        print(f"  FAIL  SQLite3: {e}")
        return False

    return True


def main():
    print("""
============================================================
        TradingAgents Setup Verification
============================================================
    """)

    results = []

    results.append(("Python Version", check_python_version()))
    results.append(("Dependencies", check_dependencies()))
    results.append(("Environment Variables", check_environment_variables()))
    results.append(("TradingAgents Import", check_tradingagents_import()))
    results.append(("Dataflow Modules", check_dataflow_modules()))
    results.append(("Database", check_database()))

    # Summary
    print_header("Verification Summary")

    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {status}  {name}")
        if not passed:
            all_passed = False

    print(f"\n{'='*60}\n")

    if all_passed:
        print("SUCCESS — Your environment is properly configured!\n")
        print("Quick start:")
        print("  make ui             Start the web UI")
        print("  make analyze TICKER=NVDA")
        print("  make watchlists     List available watchlists")
        print("  make help           See all commands")
        return 0
    else:
        print("SETUP INCOMPLETE — Please fix the issues above\n")
        print("Common solutions:")
        print("  1. Install dependencies:  pip install -r requirements.txt")
        print("  2. Create .env file:      cp .env.example .env")
        print("  3. Add your API keys to .env")
        print("  4. Run again:             make verify")
        return 1


if __name__ == "__main__":
    sys.exit(main())
