import logging
from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.config import get_config

logger = logging.getLogger("tradingagents.agents.utils.news_data_tools")

# Import token management utilities
try:
    from tradingagents.utils.token_management import optimize_analyst_input, count_tokens
    TOKEN_MANAGEMENT_AVAILABLE = True
except ImportError:
    TOKEN_MANAGEMENT_AVAILABLE = False
    logger.warning("Token management not available. Install tiktoken: pip install tiktoken")

# Import Perplexity deep research functions
try:
    from tradingagents.dataflows.interface import (
        get_deep_research as _get_deep_research,
        get_earnings_deep_dive as _get_earnings_deep_dive,
        get_sec_deep_dive as _get_sec_deep_dive,
        get_sec_filings_snapshot as _get_sec_filings_snapshot,
        get_earnings_transcript_snapshot as _get_earnings_transcript_snapshot,
    )
    PERPLEXITY_AVAILABLE = True
except ImportError:
    PERPLEXITY_AVAILABLE = False


def _news_cap(method_name: str, default: int) -> int:
    try:
        cfg = get_config()
        cap = cfg.get("token_budget", {}).get("tools", {}).get(method_name, default)
        return max(800, int(cap))
    except Exception:
        return default


@tool
def get_news(
    ticker: Annotated[str, "Ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve news data for a given ticker symbol.
    Uses the configured news_data vendor.
    Args:
        ticker (str): Ticker symbol
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted string containing news data (optimized for token usage)
    """
    from tradingagents.dataflows.run_date import coerce_tool_date

    end_date = coerce_tool_date(end_date)
    start_date = coerce_tool_date(start_date, as_lookback_from=end_date)
    result = route_to_vendor("get_news", ticker, start_date, end_date)
    max_tokens = _news_cap("get_news", 3000)
    
    # Optimize to prevent token limit issues
    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > max_tokens:
            logger.info("News data: %d tokens - optimizing...", tokens)
            result = optimize_analyst_input(str(result), data_type="news", max_tokens=max_tokens)
    
    return result

@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "Number of days to look back"] = 7,
    limit: Annotated[int, "Maximum number of articles to return"] = 5,
) -> str:
    """
    Retrieve global news data.
    Uses the configured news_data vendor.
    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back (default 7)
        limit (int): Maximum number of articles to return (default 5)
    Returns:
        str: A formatted string containing global news data (optimized for token usage)
    """
    from tradingagents.dataflows.run_date import coerce_tool_date

    curr_date = coerce_tool_date(curr_date)
    result = route_to_vendor("get_global_news", curr_date, look_back_days, limit)
    max_tokens = _news_cap("get_global_news", 2000)
    
    # Optimize to prevent token limit issues
    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > max_tokens:
            logger.info("Global news data: %d tokens - optimizing...", tokens)
            result = optimize_analyst_input(str(result), data_type="news", max_tokens=max_tokens)
    
    return result

@tool
def get_insider_sentiment(
    ticker: Annotated[str, "ticker symbol for the company"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """
    Retrieve insider sentiment information about a company.
    Resolves to yfinance (get_insider_sentiment). The news_data vendor category
    (default: finnhub) does not register insider methods, so routing falls through
    to the yfinance implementation.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A report of insider sentiment data
    """
    try:
        result = route_to_vendor("get_insider_sentiment", ticker, curr_date)
        max_tokens = _news_cap("get_insider_sentiment", 1200)
        if TOKEN_MANAGEMENT_AVAILABLE:
            tokens = count_tokens(str(result))
            if tokens > max_tokens:
                logger.info("Insider sentiment: %d tokens - optimizing...", tokens)
                result = optimize_analyst_input(str(result), data_type="fundamentals", max_tokens=max_tokens)
        return result
    except Exception as e:
        logger.warning("Insider sentiment unavailable for %s: %s", ticker, e)
        return f"[Insider sentiment data unavailable for {ticker} — proceed with other available data sources]"

@tool
def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """
    Retrieve insider transaction information about a company.
    Resolves to alpha_vantage or yfinance (get_insider_transactions). The news_data
    vendor category (default: finnhub) does not register insider methods, so routing
    falls through to alpha_vantage first, then yfinance.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A report of insider transaction data
    """
    try:
        result = route_to_vendor("get_insider_transactions", ticker, curr_date)
        max_tokens = _news_cap("get_insider_transactions", 1500)
        if TOKEN_MANAGEMENT_AVAILABLE:
            tokens = count_tokens(str(result))
            if tokens > max_tokens:
                logger.info("Insider transactions: %d tokens - optimizing...", tokens)
                result = optimize_analyst_input(str(result), data_type="fundamentals", max_tokens=max_tokens)
        return result
    except Exception as e:
        logger.warning("Insider transactions unavailable for %s: %s", ticker, e)
        return f"[Insider transaction data unavailable for {ticker} — proceed with other available data sources]"


# =============================================================================
# PERPLEXITY DEEP RESEARCH TOOLS (for "deep" analysis mode)
# =============================================================================

@tool
def get_deep_research(
    ticker: Annotated[str, "Ticker symbol"],
    company_name: Annotated[str, "Company name (optional, improves results)"] = None,
) -> str:
    """
    Get comprehensive deep research from Perplexity AI with citations.
    
    This tool provides institutional-grade research including:
    - Latest earnings analysis with specific numbers
    - Management guidance and outlook
    - Analyst consensus and price targets
    - SEC filings and insider activity
    - Competitive positioning
    
    Use this for thorough due diligence. Results include source citations.
    
    Args:
        ticker (str): Stock ticker symbol (e.g., "NFLX")
        company_name (str): Optional company name for better results
        
    Returns:
        str: Comprehensive research report with citations
    """
    if not PERPLEXITY_AVAILABLE:
        return "[Deep research unavailable - Perplexity module not loaded]"
    
    config = get_config()
    if not config.get("use_perplexity", False):
        return "[Deep research skipped - not enabled in current analysis mode. Use 'deep' mode for Perplexity research.]"
    
    result = _get_deep_research(ticker, company_name)
    
    # Optimize if needed
    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > 12000:
            logger.info("Deep research: %d tokens - optimizing...", tokens)
            result = optimize_analyst_input(str(result), data_type="research", max_tokens=12000)
    
    return result


@tool
def get_earnings_analysis(
    ticker: Annotated[str, "Ticker symbol"],
    company_name: Annotated[str, "Company name (optional)"] = None,
) -> str:
    """
    Get detailed earnings call analysis from Perplexity AI.
    
    Provides:
    - Financial results vs expectations
    - Key metrics and KPIs
    - Management commentary highlights
    - Forward guidance
    - Analyst reactions
    
    Args:
        ticker (str): Stock ticker symbol
        company_name (str): Optional company name
        
    Returns:
        str: Earnings analysis with citations
    """
    if not PERPLEXITY_AVAILABLE:
        return "[Earnings analysis unavailable - Perplexity module not loaded]"
    
    config = get_config()
    if not config.get("use_perplexity", False):
        return "[Earnings analysis skipped - not enabled in current analysis mode]"

    from tradingagents.dataflows.perplexity_budget import overlap_prose_tools_blocked
    if overlap_prose_tools_blocked():
        return "[Earnings analysis skipped - covered by deep research and prefetched snapshots]"
    
    result = _get_earnings_deep_dive(ticker, company_name)
    
    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > 8000:
            result = optimize_analyst_input(str(result), data_type="research", max_tokens=8000)
    
    return result


@tool
def get_sec_analysis(
    ticker: Annotated[str, "Ticker symbol"],
    company_name: Annotated[str, "Company name (optional)"] = None,
) -> str:
    """
    Get SEC filings analysis from Perplexity AI.
    
    Provides:
    - 10-K/10-Q highlights and risk factors
    - Recent Form 4 insider transactions
    - Institutional holdings changes (13F)
    - Material 8-K events
    
    Args:
        ticker (str): Stock ticker symbol
        company_name (str): Optional company name
        
    Returns:
        str: SEC filings summary with citations
    """
    if not PERPLEXITY_AVAILABLE:
        return "[SEC analysis unavailable - Perplexity module not loaded]"
    
    config = get_config()
    if not config.get("use_perplexity", False):
        return "[SEC analysis skipped - not enabled in current analysis mode]"

    from tradingagents.dataflows.perplexity_budget import overlap_prose_tools_blocked
    if overlap_prose_tools_blocked():
        return "[SEC analysis skipped - covered by deep research and prefetched snapshots]"
    
    result = _get_sec_deep_dive(ticker, company_name)
    
    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > 6000:
            result = optimize_analyst_input(str(result), data_type="research", max_tokens=6000)
    
    return result


@tool
def get_sec_filings_snapshot(
    ticker: Annotated[str, "Ticker symbol"],
    company_name: Annotated[str, "Company name (optional)"] = None,
    after_date: Annotated[str, "Only include filings after YYYY-MM-DD (optional)"] = None,
    filing_url: Annotated[str, "Optional direct filing URL"] = None,
) -> str:
    """
    Get structured SEC filings snapshot (10-K/10-Q/8-K) from Perplexity AI.
    Returns JSON-formatted snapshot for ingestion.
    """
    if not PERPLEXITY_AVAILABLE:
        return "[SEC filings snapshot unavailable - Perplexity module not loaded]"

    config = get_config()
    if not config.get("use_perplexity", False):
        return "[SEC filings snapshot skipped - not enabled in current analysis mode]"

    result = _get_sec_filings_snapshot(ticker, company_name, after_date, filing_url)

    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > 8000:
            result = optimize_analyst_input(str(result), data_type="research", max_tokens=8000)

    return result


@tool
def get_earnings_transcript_snapshot(
    ticker: Annotated[str, "Ticker symbol"],
    company_name: Annotated[str, "Company name (optional)"] = None,
    transcript_url: Annotated[str, "Optional transcript URL"] = None,
) -> str:
    """
    Get structured earnings transcript snapshot with guidance extraction.
    Returns JSON-formatted snapshot for ingestion.
    """
    if not PERPLEXITY_AVAILABLE:
        return "[Earnings transcript snapshot unavailable - Perplexity module not loaded]"

    config = get_config()
    if not config.get("use_perplexity", False):
        return "[Earnings transcript snapshot skipped - not enabled in current analysis mode]"

    result = _get_earnings_transcript_snapshot(ticker, company_name, transcript_url)

    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > 8000:
            result = optimize_analyst_input(str(result), data_type="research", max_tokens=8000)

    return result
