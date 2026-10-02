from typing import Annotated, Optional
from datetime import datetime
import time

# Import from vendor-specific modules
from .local import get_YFin_data, get_finnhub_news, get_finnhub_company_insider_sentiment, get_finnhub_company_insider_transactions, get_simfin_balance_sheet, get_simfin_cashflow, get_simfin_income_statements, get_reddit_global_news, get_reddit_company_news
from .y_finance import get_YFin_data_online, get_stock_stats_indicators_window, get_balance_sheet as get_yfinance_balance_sheet, get_cashflow as get_yfinance_cashflow, get_income_statement as get_yfinance_income_statement, get_insider_transactions as get_yfinance_insider_transactions, get_insider_sentiment as get_yfinance_insider_sentiment, get_yfinance_fundamentals
from .google import get_google_news as _get_google_news_original
from .openai import get_stock_news_openai as _get_stock_news_openai_original, get_global_news_openai, get_fundamentals_openai
from .alpha_vantage import (
    get_stock as get_alpha_vantage_stock,
    get_indicator as get_alpha_vantage_indicator,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_income_statement as get_alpha_vantage_income_statement,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_global_news as get_alpha_vantage_global_news
)
from .alpha_vantage_common import AlphaVantageRateLimitError
from .finnhub_api import (
    get_news as get_finnhub_api_news,
    get_global_news as get_finnhub_api_global_news,
    FinnhubRateLimitError
)

# Perplexity API for deep research
from .perplexity_api import (
    get_news as get_perplexity_news,
    get_global_news as get_perplexity_global_news,
    get_deep_research as get_perplexity_deep_research,
    get_earnings_analysis as get_perplexity_earnings,
    get_sec_filings_summary as get_perplexity_sec,
    get_sec_filings_snapshot as get_perplexity_sec_snapshot,
    get_earnings_transcript_snapshot as get_perplexity_earnings_transcript,
    get_catalyst_pipeline as get_perplexity_catalyst_pipeline,
    discover_opportunities as _perplexity_discover_opportunities,
    PerplexityRateLimitError,
    PerplexityAPIError
)

# Smart caching layer
from .cache import get_cache, DataCache

# Usage tracking
from .usage_tracker import get_tracker, UsageTracker
from .provenance import record_event

# Configuration and routing logic
from .config import get_config
from .vendor_errors import (
    VendorUnavailableError,
    format_vendor_unavailable,
    is_vendor_unavailable_payload,
)

import logging
logger = logging.getLogger("tradingagents.dataflows.interface")

# Wrapper functions to make function signatures compatible
def get_stock_news_openai(ticker, start_date, end_date):
    """Wrapper for OpenAI news to convert ticker to query parameter."""
    # Use ticker as the query (OpenAI expects 'query' parameter)
    return _get_stock_news_openai_original(ticker, start_date, end_date)

def get_google_news(ticker, start_date, end_date):
    """Wrapper for Google news to convert (ticker, start_date, end_date) to (query, curr_date, look_back_days)."""
    # Calculate look_back_days
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    look_back_days = (end - start).days
    
    # Google news expects: (query, curr_date, look_back_days)
    return _get_google_news_original(ticker, end_date, look_back_days)

# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": [
            "get_stock_data"
        ]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": [
            "get_indicators"
        ]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News (public/insiders, original/processed)",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_sentiment",
            "get_insider_transactions",
        ]
    }
}

VENDOR_LIST = [
    "local",
    "yfinance",
    "openai",
    "google",
    "finnhub",
    "alpha_vantage",
    "perplexity"
]

# Mapping of methods to their vendor-specific implementations
VENDOR_METHODS = {
    # core_stock_apis
    "get_stock_data": {
        "alpha_vantage": get_alpha_vantage_stock,
        "yfinance": get_YFin_data_online,
        "local": get_YFin_data,
    },
    # technical_indicators
    "get_indicators": {
        "alpha_vantage": get_alpha_vantage_indicator,
        "yfinance": get_stock_stats_indicators_window,
        "local": get_stock_stats_indicators_window
    },
    # fundamental_data
    "get_fundamentals": {
        "alpha_vantage": get_alpha_vantage_fundamentals,
        "openai": get_fundamentals_openai,
        "yfinance": get_yfinance_fundamentals,
    },
    "get_balance_sheet": {
        "alpha_vantage": get_alpha_vantage_balance_sheet,
        "yfinance": get_yfinance_balance_sheet,
        "local": get_simfin_balance_sheet,
    },
    "get_cashflow": {
        "alpha_vantage": get_alpha_vantage_cashflow,
        "yfinance": get_yfinance_cashflow,
        "local": get_simfin_cashflow,
    },
    "get_income_statement": {
        "alpha_vantage": get_alpha_vantage_income_statement,
        "yfinance": get_yfinance_income_statement,
        "local": get_simfin_income_statements,
    },
    # news_data
    "get_news": {
        "finnhub": get_finnhub_api_news,  # Finnhub live API (free tier: 60 req/min)
        "perplexity": get_perplexity_news,  # Perplexity AI-powered news with citations
        "alpha_vantage": get_alpha_vantage_news,
        "openai": get_stock_news_openai,
        "google": get_google_news,
        "local": [get_finnhub_news, get_reddit_company_news, get_google_news],
    },
    "get_global_news": {
        "finnhub": get_finnhub_api_global_news,  # Finnhub live API (free tier: 60 req/min)
        "perplexity": get_perplexity_global_news,  # Perplexity AI-powered global news
        "alpha_vantage": get_alpha_vantage_global_news,
        "openai": get_global_news_openai,
        "local": get_reddit_global_news
    },
    "get_insider_sentiment": {
        "yfinance": get_yfinance_insider_sentiment,
        "local": get_finnhub_company_insider_sentiment,
    },
    "get_insider_transactions": {
        "alpha_vantage": get_alpha_vantage_insider_transactions,
        "yfinance": get_yfinance_insider_transactions,
        "local": get_finnhub_company_insider_transactions,
    },
}

def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")


def _method_to_cache_type(method: str) -> str:
    """Map tool method names to cache TTL categories."""
    mapping = {
        "get_stock_data": "price",
        "get_indicators": "technicals",
        "get_fundamentals": "fundamentals",
        "get_balance_sheet": "balance_sheet",
        "get_cashflow": "cashflow",
        "get_income_statement": "income_statement",
        "get_news": "news",
        "get_global_news": "global_news",
        "get_insider_sentiment": "fundamentals",
        "get_insider_transactions": "fundamentals",
    }
    return mapping.get(method, method)

def get_vendor(category: str, method: str = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level.
    """
    config = get_config()

    # Check tool-level configuration first (if method provided)
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # Fall back to category-level configuration
    return config.get("data_vendors", {}).get(category, "default")

_LIVE_ONLY_METHODS = frozenset(
    {
        "get_deep_research",
        "get_insider_sentiment",
        "get_insider_transactions",
    }
)


def _run_date_from_call(method: str, args: tuple, kwargs: dict) -> str | None:
    for key in ("curr_date", "end_date", "trade_date", "as_of_date"):
        val = kwargs.get(key)
        if val:
            return str(val)[:10]
    if method in {"get_stock_data", "get_YFin_data"} and len(args) >= 3:
        return str(args[2])[:10]
    # get_indicators(symbol, indicator, curr_date, look_back_days)
    # args[3] is the lookback window (often 30), not a calendar date.
    if method == "get_indicators" and len(args) >= 3:
        return str(args[2])[:10]
    return None


def _historical_unavailable_message(method: str, run_date: str) -> str:
    return f"[Not available for historical run date {run_date}] ({method})"


def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to appropriate vendor implementation with caching, usage tracking, and fallback support."""
    config = get_config()
    from .run_date import is_historical_run

    run_date = _run_date_from_call(method, args, kwargs)
    if run_date and is_historical_run(run_date) and method in _LIVE_ONLY_METHODS:
        return _historical_unavailable_message(method, run_date)
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    provenance_enabled = config.get("enable_provenance", False)
    provenance_max_events = config.get("provenance_max_events", 500)

    args_repr = [str(arg) for arg in args]
    kwargs_repr = {key: str(val) for key, val in kwargs.items()}
    
    # Initialize cache and tracker if enabled
    cache = get_cache() if config.get("enable_cache", True) else None
    tracker = get_tracker() if config.get("enable_usage_tracking", True) else None

    # Check cache first
    if cache:
        cache_type = _method_to_cache_type(method)
        cache_key_args = [method] + list(args) + [f"{k}={v}" for k, v in sorted(kwargs.items())]
        if run_date and is_historical_run(run_date):
            cache_key_args.append(f"historical={run_date}")
        cached_result = cache.get(cache_type, *cache_key_args)
        if cached_result is not None and not is_vendor_unavailable_payload(cached_result):
            logger.debug("Cache hit for %s - returning cached result", method)
            if provenance_enabled:
                record_event(
                    {
                        "method": method,
                        "vendor": "cache",
                        "category": category,
                        "cache_hit": True,
                        "status": "success",
                        "duration_ms": 0,
                        "args": args_repr,
                        "kwargs": kwargs_repr,
                    },
                    max_events=provenance_max_events,
                )
            return cached_result

    # Handle comma-separated vendors
    primary_vendors = [v.strip() for v in vendor_config.split(',')]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    # Get all available vendors for this method for fallback
    all_available_vendors = list(VENDOR_METHODS[method].keys())
    
    # Create fallback vendor list: primary vendors first, then remaining vendors as fallbacks
    fallback_vendors = primary_vendors.copy()
    for vendor in all_available_vendors:
        if vendor not in fallback_vendors:
            fallback_vendors.append(vendor)

    primary_str = " → ".join(primary_vendors)
    fallback_str = " → ".join(fallback_vendors)
    logger.debug("%s - Primary: [%s] | Full fallback order: [%s]", method, primary_str, fallback_str)

    # Track results and execution state
    results = []
    vendor_attempt_count = 0
    any_primary_vendor_attempted = False
    successful_vendor = None
    last_vendor_error = None
    last_vendor_attempted = None

    for vendor in fallback_vendors:
        if vendor not in VENDOR_METHODS[method]:
            if vendor in primary_vendors:
                logger.info("Vendor '%s' not supported for method '%s', falling back to next vendor", vendor, method)
            continue
        
        # Check usage limits before attempting vendor
        if tracker and not tracker.can_use(vendor):
            logger.warning("Vendor '%s' at usage limit, skipping to next vendor", vendor)
            if provenance_enabled:
                record_event(
                    {
                        "method": method,
                        "vendor": vendor,
                        "category": category,
                        "cache_hit": False,
                        "status": "skipped",
                        "reason": "usage_limit",
                        "args": args_repr,
                        "kwargs": kwargs_repr,
                    },
                    max_events=provenance_max_events,
                )
            continue

        vendor_impl = VENDOR_METHODS[method][vendor]
        is_primary_vendor = vendor in primary_vendors
        vendor_attempt_count += 1
        last_vendor_attempted = vendor
        attempt_start = time.time()

        # Track if we attempted any primary vendor
        if is_primary_vendor:
            any_primary_vendor_attempted = True

        vendor_type = "PRIMARY" if is_primary_vendor else "FALLBACK"
        logger.debug("Attempting %s vendor '%s' for %s (attempt #%d)", vendor_type, vendor, method, vendor_attempt_count)

        # Handle list of methods for a vendor
        if isinstance(vendor_impl, list):
            vendor_methods = [(impl, vendor) for impl in vendor_impl]
            logger.debug("Vendor '%s' has multiple implementations: %d functions", vendor, len(vendor_methods))
        else:
            vendor_methods = [(vendor_impl, vendor)]

        # Run methods for this vendor
        vendor_results = []
        vendor_error = None
        for impl_func, vendor_name in vendor_methods:
            try:
                logger.debug("Calling %s from vendor '%s'...", impl_func.__name__, vendor_name)
                result = impl_func(*args, **kwargs)
                vendor_results.append(result)
                
                # Record successful usage
                if tracker:
                    tracker.record_use(vendor_name)
                
                logger.info("%s from vendor '%s' completed successfully", impl_func.__name__, vendor_name)
                    
            except AlphaVantageRateLimitError as e:
                logger.warning("Alpha Vantage rate limit exceeded, falling back to next available vendor")
                logger.debug("Rate limit details: %s", e)
                vendor_error = str(e)
                # Continue to next vendor for fallback
                continue
            except FinnhubRateLimitError as e:
                logger.warning("Finnhub rate limit exceeded (60 req/min), falling back to next available vendor")
                logger.debug("Rate limit details: %s", e)
                vendor_error = str(e)
                # Continue to next vendor for fallback
                continue
            except PerplexityRateLimitError as e:
                logger.warning("Perplexity credit limit exceeded, falling back to next available vendor")
                logger.debug("Rate limit details: %s", e)
                vendor_error = str(e)
                # Continue to next vendor for fallback
                continue
            except PerplexityAPIError as e:
                logger.error("Perplexity API error, falling back to next available vendor")
                logger.debug("API error details: %s", e)
                vendor_error = str(e)
                continue
            except Exception as e:
                # Log error but continue with other implementations
                logger.error("%s from vendor '%s' failed: %s", impl_func.__name__, vendor_name, e)
                vendor_error = str(e)
                continue

        # Add this vendor's results
        if vendor_results:
            results.extend(vendor_results)
            successful_vendor = vendor
            result_summary = f"Got {len(vendor_results)} result(s)"
            logger.info("Vendor '%s' succeeded - %s", vendor, result_summary)
            if provenance_enabled:
                record_event(
                    {
                        "method": method,
                        "vendor": vendor,
                        "category": category,
                        "cache_hit": False,
                        "status": "success",
                        "primary": is_primary_vendor,
                        "duration_ms": int((time.time() - attempt_start) * 1000),
                        "results_count": len(vendor_results),
                        "args": args_repr,
                        "kwargs": kwargs_repr,
                    },
                    max_events=provenance_max_events,
                )
            
            # Stopping logic: Stop after first successful vendor for single-vendor configs
            # Multiple vendor configs (comma-separated) may want to collect from multiple sources
            if len(primary_vendors) == 1:
                logger.debug("Stopping after successful vendor '%s' (single-vendor config)", vendor)
                break
        else:
            logger.warning("Vendor '%s' produced no results", vendor)
            if vendor_error:
                last_vendor_error = vendor_error
            if provenance_enabled:
                record_event(
                    {
                        "method": method,
                        "vendor": vendor,
                        "category": category,
                        "cache_hit": False,
                        "status": "error",
                        "primary": is_primary_vendor,
                        "duration_ms": int((time.time() - attempt_start) * 1000),
                        "error": vendor_error,
                        "args": args_repr,
                        "kwargs": kwargs_repr,
                    },
                    max_events=provenance_max_events,
                )

    # Final result summary
    if not results:
        logger.error("All %d vendor attempts failed for method '%s'", vendor_attempt_count, method)
        last_vendor = last_vendor_attempted or (fallback_vendors[-1] if fallback_vendors else "unknown")
        reason = last_vendor_error or "all vendors failed"
        return format_vendor_unavailable(method, last_vendor, reason)
    else:
        logger.info("Method '%s' completed with %d result(s) from %d vendor attempt(s)", method, len(results), vendor_attempt_count)

    # Prepare final result
    if len(results) == 1:
        final_result = results[0]
    else:
        # Convert all results to strings and concatenate
        final_result = '\n'.join(str(result) for result in results)

    if is_vendor_unavailable_payload(final_result):
        return str(final_result)
    
    # Cache the result
    if cache and final_result and not is_vendor_unavailable_payload(final_result):
        cache_key_args = [method] + list(args) + [f"{k}={v}" for k, v in sorted(kwargs.items())]
        if run_date and is_historical_run(run_date):
            cache_key_args.append(f"historical={run_date}")
        cache_type = _method_to_cache_type(method)
        cache.set(cache_type, *cache_key_args, data=final_result)
        logger.debug("Cache set for %s - result cached", method)

    return final_result


# =============================================================================
# PERPLEXITY DEEP RESEARCH FUNCTIONS (for "deep" analysis mode)
# =============================================================================

def _optional_cache_suffix(value: Optional[str]) -> tuple:
    """Return cache key suffix only for non-empty optional args."""
    if value is None:
        return ()
    text = str(value).strip()
    if not text:
        return ()
    return (text,)


def _record_perplexity_live_use(tracker, method: str) -> None:
    from tradingagents.dataflows.perplexity_budget import register_live_call

    register_live_call(method)
    if tracker:
        tracker.record_use("perplexity", method=method)


def _can_make_perplexity_live_call(tracker, method: str) -> bool:
    from tradingagents.dataflows.perplexity_budget import has_live_budget

    if tracker and not tracker.can_use_deep_perplexity():
        return False
    return has_live_budget(method)


def _should_fetch_catalyst_live(tracker, cache, ticker: str) -> bool:
    """Skip catalyst when cached, or when the live slot is needed for research."""
    research_cached = bool(cache and cache.get("perplexity_research", ticker))
    if cache:
        cached = cache.get("catalyst_pipeline", ticker)
        if cached:
            return False
    if tracker:
        if not tracker.can_use_deep_perplexity():
            return False
        needed_for_research = 0 if research_cached else 1
        if tracker.get_deep_remaining() <= needed_for_research:
            return False
    from tradingagents.dataflows.perplexity_budget import has_live_budget

    return has_live_budget("catalyst", research_available=research_cached)


def get_deep_research(ticker: str, company_name: str = None) -> str:
    """
    Get comprehensive deep research from Perplexity.
    Use this for "deep" analysis mode to get earnings, SEC, competitive intel.
    
    Args:
        ticker: Stock symbol
        company_name: Optional company name
        
    Returns:
        Comprehensive research report with citations
    """
    config = get_config()
    tracker = get_tracker() if config.get("enable_usage_tracking", True) else None
    cache = get_cache() if config.get("enable_cache", True) else None
    provenance_enabled = config.get("enable_provenance", False)
    provenance_max_events = config.get("provenance_max_events", 500)
    
    # Check cache
    if cache:
        cached = cache.get("perplexity_research", ticker)
        if cached:
            logger.debug("Cache hit for deep research: %s", ticker)
            if provenance_enabled:
                record_event(
                    {
                        "method": "get_deep_research",
                        "vendor": "cache",
                        "category": "news_data",
                        "cache_hit": True,
                        "status": "success",
                        "duration_ms": 0,
                        "args": [ticker, company_name],
                        "kwargs": {},
                    },
                    max_events=provenance_max_events,
                )
            from tradingagents.dataflows.perplexity_budget import mark_research_satisfied

            mark_research_satisfied()
            return cached
    
    # Check usage limits
    if tracker and not tracker.can_use_deep_perplexity():
        logger.warning("Perplexity at monthly limit - skipping deep research")
        if provenance_enabled:
            record_event(
                {
                    "method": "get_deep_research",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "skipped",
                    "reason": "usage_limit",
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return f"[Deep research unavailable - Perplexity monthly limit reached]"
    
    if not _can_make_perplexity_live_call(tracker, "get_deep_research"):
        logger.warning("Perplexity per-run or monthly cap - skipping deep research")
        if provenance_enabled:
            record_event(
                {
                    "method": "get_deep_research",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "skipped",
                    "reason": "usage_limit",
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return "[Deep research unavailable - Perplexity call budget exhausted]"

    try:
        start_time = time.time()
        result = get_perplexity_deep_research(ticker, company_name)
        
        # Record usage and cache (live request only)
        _record_perplexity_live_use(tracker, "get_deep_research")
        if cache:
            cache.set("perplexity_research", ticker, data=result)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_deep_research",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "success",
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return result
    except (PerplexityRateLimitError, PerplexityAPIError) as e:
        logger.error("Perplexity error: %s", e)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_deep_research",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "error",
                    "duration_ms": 0,
                    "error": str(e),
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return f"[Deep research unavailable: {e}]"


def get_earnings_deep_dive(ticker: str, company_name: str = None) -> str:
    """Get detailed earnings analysis from Perplexity."""
    config = get_config()
    tracker = get_tracker() if config.get("enable_usage_tracking", True) else None
    cache = get_cache() if config.get("enable_cache", True) else None
    provenance_enabled = config.get("enable_provenance", False)
    provenance_max_events = config.get("provenance_max_events", 500)
    
    if cache:
        cached = cache.get("perplexity_earnings", ticker)
        if cached:
            if provenance_enabled:
                record_event(
                    {
                        "method": "get_earnings_deep_dive",
                        "vendor": "cache",
                        "category": "news_data",
                        "cache_hit": True,
                        "status": "success",
                        "duration_ms": 0,
                        "args": [ticker, company_name],
                        "kwargs": {},
                    },
                    max_events=provenance_max_events,
                )
            return cached
    
    if tracker and not tracker.can_use_deep_perplexity():
        if provenance_enabled:
            record_event(
                {
                    "method": "get_earnings_deep_dive",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "skipped",
                    "reason": "usage_limit",
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return f"[Earnings analysis unavailable - Perplexity monthly limit reached]"
    
    if not _can_make_perplexity_live_call(tracker, "get_earnings_deep_dive"):
        return "[Earnings analysis skipped - covered by deep research and prefetched snapshots]"
    
    try:
        start_time = time.time()
        result = get_perplexity_earnings(ticker, company_name)
        _record_perplexity_live_use(tracker, "get_earnings_deep_dive")
        if cache:
            cache.set("perplexity_earnings", ticker, data=result)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_earnings_deep_dive",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "success",
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return result
    except (PerplexityRateLimitError, PerplexityAPIError) as e:
        logger.error("Earnings deep dive failed for %s: %s", ticker, e)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_earnings_deep_dive",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "error",
                    "duration_ms": 0,
                    "error": str(e),
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return f"[Earnings analysis unavailable: {e}]"


def get_sec_deep_dive(ticker: str, company_name: str = None) -> str:
    """Get SEC filings analysis from Perplexity."""
    config = get_config()
    tracker = get_tracker() if config.get("enable_usage_tracking", True) else None
    cache = get_cache() if config.get("enable_cache", True) else None
    provenance_enabled = config.get("enable_provenance", False)
    provenance_max_events = config.get("provenance_max_events", 500)
    
    if cache:
        cached = cache.get("perplexity_sec", ticker)
        if cached:
            if provenance_enabled:
                record_event(
                    {
                        "method": "get_sec_deep_dive",
                        "vendor": "cache",
                        "category": "news_data",
                        "cache_hit": True,
                        "status": "success",
                        "duration_ms": 0,
                        "args": [ticker, company_name],
                        "kwargs": {},
                    },
                    max_events=provenance_max_events,
                )
            return cached
    
    if tracker and not tracker.can_use_deep_perplexity():
        if provenance_enabled:
            record_event(
                {
                    "method": "get_sec_deep_dive",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "skipped",
                    "reason": "usage_limit",
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return f"[SEC analysis unavailable - Perplexity monthly limit reached]"
    
    if not _can_make_perplexity_live_call(tracker, "get_sec_deep_dive"):
        return "[SEC analysis skipped - covered by deep research and prefetched snapshots]"
    
    try:
        start_time = time.time()
        result = get_perplexity_sec(ticker, company_name)
        _record_perplexity_live_use(tracker, "get_sec_deep_dive")
        if cache:
            cache.set("perplexity_sec", ticker, data=result)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_sec_deep_dive",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "success",
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return result
    except (PerplexityRateLimitError, PerplexityAPIError) as e:
        logger.error("SEC deep dive failed for %s: %s", ticker, e)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_sec_deep_dive",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "error",
                    "duration_ms": 0,
                    "error": str(e),
                    "args": [ticker, company_name],
                    "kwargs": {},
                },
                max_events=provenance_max_events,
            )
        return f"[SEC analysis unavailable: {e}]"


def get_sec_filings_snapshot(
    ticker: str,
    company_name: str = None,
    after_date: str = None,
    filing_url: str = None,
) -> str:
    """Get structured SEC filings snapshot from Perplexity."""
    config = get_config()
    tracker = get_tracker() if config.get("enable_usage_tracking", True) else None
    cache = get_cache() if config.get("enable_cache", True) else None
    provenance_enabled = config.get("enable_provenance", False)
    provenance_max_events = config.get("provenance_max_events", 500)

    cache_key_args = (ticker,) + _optional_cache_suffix(filing_url)

    if cache:
        cached = cache.get("perplexity_sec_snapshot", *cache_key_args)
        if cached:
            if provenance_enabled:
                record_event(
                    {
                        "method": "get_sec_filings_snapshot",
                        "vendor": "cache",
                        "category": "news_data",
                        "cache_hit": True,
                        "status": "success",
                        "duration_ms": 0,
                        "args": [ticker, company_name],
                        "kwargs": {"after_date": after_date, "filing_url": filing_url},
                    },
                    max_events=provenance_max_events,
                )
            return cached

    if tracker and not tracker.can_use_deep_perplexity():
        if provenance_enabled:
            record_event(
                {
                    "method": "get_sec_filings_snapshot",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "skipped",
                    "reason": "usage_limit",
                    "args": [ticker, company_name],
                    "kwargs": {"after_date": after_date, "filing_url": filing_url},
                },
                max_events=provenance_max_events,
            )
        return "[SEC filings snapshot unavailable - Perplexity monthly limit reached]"

    if not _can_make_perplexity_live_call(tracker, "get_sec_filings_snapshot"):
        return "[SEC filings snapshot skipped - already prefetched for this analysis run]"

    try:
        start_time = time.time()
        result = get_perplexity_sec_snapshot(ticker, company_name, after_date, filing_url)
        _record_perplexity_live_use(tracker, "get_sec_filings_snapshot")
        if cache:
            cache.set("perplexity_sec_snapshot", *cache_key_args, data=result)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_sec_filings_snapshot",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "success",
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "args": [ticker, company_name],
                    "kwargs": {"after_date": after_date, "filing_url": filing_url},
                },
                max_events=provenance_max_events,
            )
        return result
    except (PerplexityRateLimitError, PerplexityAPIError) as e:
        logger.error("SEC filings snapshot failed for %s: %s", ticker, e)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_sec_filings_snapshot",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "error",
                    "duration_ms": 0,
                    "error": str(e),
                    "args": [ticker, company_name],
                    "kwargs": {"after_date": after_date, "filing_url": filing_url},
                },
                max_events=provenance_max_events,
            )
        return f"[SEC filings snapshot unavailable: {e}]"


def get_earnings_transcript_snapshot(
    ticker: str,
    company_name: str = None,
    transcript_url: str = None,
) -> str:
    """Get structured earnings transcript snapshot from Perplexity."""
    config = get_config()
    tracker = get_tracker() if config.get("enable_usage_tracking", True) else None
    cache = get_cache() if config.get("enable_cache", True) else None
    provenance_enabled = config.get("enable_provenance", False)
    provenance_max_events = config.get("provenance_max_events", 500)

    cache_key_args = (ticker,) + _optional_cache_suffix(transcript_url)

    if cache:
        cached = cache.get("perplexity_earnings_transcript", *cache_key_args)
        if cached:
            if provenance_enabled:
                record_event(
                    {
                        "method": "get_earnings_transcript_snapshot",
                        "vendor": "cache",
                        "category": "news_data",
                        "cache_hit": True,
                        "status": "success",
                        "duration_ms": 0,
                        "args": [ticker, company_name],
                        "kwargs": {"transcript_url": transcript_url},
                    },
                    max_events=provenance_max_events,
                )
            return cached

    if tracker and not tracker.can_use_deep_perplexity():
        if provenance_enabled:
            record_event(
                {
                    "method": "get_earnings_transcript_snapshot",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "skipped",
                    "reason": "usage_limit",
                    "args": [ticker, company_name],
                    "kwargs": {"transcript_url": transcript_url},
                },
                max_events=provenance_max_events,
            )
        return "[Earnings transcript snapshot unavailable - Perplexity monthly limit reached]"

    if not _can_make_perplexity_live_call(tracker, "get_earnings_transcript_snapshot"):
        return "[Earnings transcript snapshot skipped - already prefetched for this analysis run]"

    try:
        start_time = time.time()
        result = get_perplexity_earnings_transcript(ticker, company_name, transcript_url)
        _record_perplexity_live_use(tracker, "get_earnings_transcript_snapshot")
        if cache:
            cache.set("perplexity_earnings_transcript", *cache_key_args, data=result)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_earnings_transcript_snapshot",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "success",
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "args": [ticker, company_name],
                    "kwargs": {"transcript_url": transcript_url},
                },
                max_events=provenance_max_events,
            )
        return result
    except (PerplexityRateLimitError, PerplexityAPIError) as e:
        logger.error("Earnings transcript snapshot failed for %s: %s", ticker, e)
        if provenance_enabled:
            record_event(
                {
                    "method": "get_earnings_transcript_snapshot",
                    "vendor": "perplexity",
                    "category": "news_data",
                    "cache_hit": False,
                    "status": "error",
                    "duration_ms": 0,
                    "error": str(e),
                    "args": [ticker, company_name],
                    "kwargs": {"transcript_url": transcript_url},
                },
                max_events=provenance_max_events,
            )
        return f"[Earnings transcript snapshot unavailable: {e}]"


def get_catalyst_pipeline(ticker: str, company_name: str = "") -> str:
    """Get upcoming catalyst pipeline from Perplexity with usage tracking."""
    config = get_config()
    tracker = get_tracker() if config.get("enable_usage_tracking", True) else None
    cache = get_cache() if config.get("enable_cache", True) else None

    if cache:
        cached = cache.get("catalyst_pipeline", ticker)
        if cached:
            return cached

    if not _should_fetch_catalyst_live(tracker, cache, ticker):
        logger.info(
            "Skipping live catalyst pipeline for %s (cached, budget reserved, or cap reached)",
            ticker,
        )
        return ""

    try:
        result = get_perplexity_catalyst_pipeline(ticker, company_name)
        if result:
            _record_perplexity_live_use(tracker, "catalyst")
            if cache:
                cache.set("catalyst_pipeline", ticker, data=result)
        return result
    except Exception as e:
        logger.error("Catalyst pipeline failed for %s: %s", ticker, e)
        return f"[Catalyst pipeline unavailable: {e}]"


# =============================================================================
# TICKER DISCOVERY (Perplexity-powered)
# =============================================================================

def discover_opportunities(
    theme: str,
    criteria: str = "",
    max_results: int = 20,
    market_cap_filter: str = "",
    model: str = "sonar-pro",
    skip_cache: bool = False,
    prompt_pack: str = "equity_search",
    asset_policy: str = "equity",
    template_id: str = "",
) -> dict:
    """Discover tickers matching an investment theme via Perplexity.

    Wraps :func:`perplexity_api.discover_opportunities` with caching (12h TTL)
    and usage tracking.  The cache key incorporates theme, criteria, market
    cap, prompt pack, and asset policy so different queries hit different
    cache entries.

    Args:
        model: Perplexity model to use (passed through to perplexity_api).
        skip_cache: When *True*, bypass the cache lookup (but still write to
            cache after fetching).  Used by the auto-discovery scheduler to
            ensure fresh data.
    """
    import hashlib

    config = get_config()
    tracker = get_tracker() if config.get("enable_usage_tracking", True) else None
    cache = get_cache() if config.get("enable_cache", True) else None

    cache_seed = (
        f"v9|{theme}|{criteria}|{market_cap_filter}|{max_results}|{model}|"
        f"{prompt_pack}|{asset_policy}|{template_id}"
    )
    cache_key = hashlib.md5(cache_seed.encode()).hexdigest()

    if cache and not skip_cache:
        cached = cache.get("discovery", cache_key)
        if cached is not None:
            logger.debug("Discovery cache hit for key=%s", cache_key)
            return cached

    if tracker and not tracker.can_use_discover():
        raise PerplexityRateLimitError(
            "Perplexity monthly limit reached. Check /api/usage for details."
        )

    result = _perplexity_discover_opportunities(
        theme=theme,
        criteria=criteria,
        max_results=max_results,
        market_cap_filter=market_cap_filter,
        model=model,
        prompt_pack=prompt_pack,
        asset_policy=asset_policy,
        template_id=template_id,
    )

    if tracker:
        tracker.record_use("perplexity", method="discover")
        tracker.record_discover_use()
    if cache:
        cache.set("discovery", cache_key, data=result, ttl=43200)

    return result