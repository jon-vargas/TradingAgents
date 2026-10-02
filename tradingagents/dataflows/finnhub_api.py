"""
Finnhub Live API Integration

Provides real-time news data from Finnhub's API.
Free tier: 60 requests/minute.

API Documentation: https://finnhub.io/docs/api
"""

import os
import logging
import requests
import json
from datetime import datetime, timedelta
from typing import Optional, Set

from tradingagents.dataflows.cache import get_cache

logger = logging.getLogger("tradingagents.dataflows.finnhub_api")

API_BASE_URL = "https://finnhub.io/api/v1"
_US_SYMBOL_SET_CACHE_KEY = "finnhub_symbol_set:US"
_US_SYMBOL_SET_TTL_SECONDS = 24 * 60 * 60


class FinnhubRateLimitError(Exception):
    """Exception raised when Finnhub API rate limit is exceeded."""
    pass


class FinnhubAPIError(Exception):
    """Exception raised for general Finnhub API errors."""
    pass


def get_api_key() -> str:
    """Retrieve the API key for Finnhub from environment variables."""
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        raise ValueError("FINNHUB_API_KEY environment variable is not set.")
    return api_key


def _make_api_request(endpoint: str, params: dict) -> dict | list:
    """
    Helper function to make Finnhub API requests and handle responses.
    
    Args:
        endpoint: API endpoint (e.g., "/company-news", "/news")
        params: Query parameters (excluding token)
        
    Returns:
        Parsed JSON response
        
    Raises:
        FinnhubRateLimitError: When API rate limit is exceeded
        FinnhubAPIError: For other API errors
    """
    # Add API token to params
    api_params = params.copy()
    api_params["token"] = get_api_key()
    
    url = f"{API_BASE_URL}{endpoint}"
    
    try:
        response = requests.get(url, params=api_params, timeout=30)
        
        # Check for rate limit (429) or other HTTP errors
        if response.status_code == 429:
            raise FinnhubRateLimitError("Finnhub API rate limit exceeded (60 requests/minute)")
        
        response.raise_for_status()
        
        # Parse JSON response
        data = response.json()
        
        # Check for API error messages
        if isinstance(data, dict) and "error" in data:
            error_msg = data.get("error", "Unknown error")
            if "limit" in error_msg.lower():
                raise FinnhubRateLimitError(f"Finnhub rate limit: {error_msg}")
            raise FinnhubAPIError(f"Finnhub API error: {error_msg}")
        
        return data
        
    except requests.exceptions.Timeout:
        raise FinnhubAPIError("Finnhub API request timed out")
    except requests.exceptions.RequestException as e:
        raise FinnhubAPIError(f"Finnhub API request failed: {e}")


def _format_news_response(articles: list, ticker: str = None, start_date: str = None, end_date: str = None) -> str:
    """
    Format Finnhub news articles into a readable string format.
    
    Args:
        articles: List of article dictionaries from Finnhub
        ticker: Optional ticker symbol for header
        start_date: Optional start date for header
        end_date: Optional end date for header
        
    Returns:
        Formatted string with news articles
    """
    if not articles:
        return f"No news found for {ticker or 'market'}"
    
    # Build header
    if ticker:
        header = f"## {ticker} News"
        if start_date and end_date:
            header += f" ({start_date} to {end_date})"
    else:
        header = "## Market News"
        if start_date and end_date:
            header += f" ({start_date} to {end_date})"
    
    formatted_articles = []
    for article in articles:
        # Convert Unix timestamp to readable date
        timestamp = article.get("datetime", 0)
        if timestamp:
            article_date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        else:
            article_date = "Unknown date"
        
        headline = article.get("headline", "No headline")
        summary = article.get("summary", "No summary available")
        source = article.get("source", "Unknown source")
        url = article.get("url", "")
        
        # Format each article
        article_str = f"### {headline}\n"
        article_str += f"**Date:** {article_date} | **Source:** {source}\n\n"
        article_str += f"{summary}\n"
        if url:
            article_str += f"\n[Read more]({url})\n"
        
        formatted_articles.append(article_str)
    
    return header + f" ({len(articles)} articles):\n\n" + "\n---\n".join(formatted_articles)


def get_news(ticker: str, start_date: str, end_date: str, limit: int = 50) -> str:
    """
    Get company-specific news from Finnhub.
    
    Uses the /company-news endpoint to retrieve news for a specific stock symbol.
    
    Args:
        ticker: Stock symbol (e.g., "AAPL", "NFLX")
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        limit: Maximum number of articles to return (default 50)
        
    Returns:
        Formatted string containing news articles with headlines, dates, sources, and summaries
        
    Raises:
        FinnhubRateLimitError: When rate limit is exceeded
        FinnhubAPIError: For other API errors
    """
    params = {
        "symbol": ticker.upper(),
        "from": start_date,
        "to": end_date,
    }
    
    logger.debug(f"Finnhub API - Fetching company news for {ticker} from {start_date} to {end_date}")
    
    articles = _make_api_request("/company-news", params)
    
    # Limit the number of articles
    if isinstance(articles, list) and len(articles) > limit:
        articles = articles[:limit]
    
    logger.debug(f"Finnhub API - Retrieved {len(articles) if isinstance(articles, list) else 0} articles")
    
    return _format_news_response(articles, ticker, start_date, end_date)


def get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 50, category: str = "general") -> str:
    """
    Get general market news from Finnhub.
    
    Uses the /news endpoint to retrieve market-wide news.
    Categories: general, forex, crypto, merger
    
    Args:
        curr_date: Current date in YYYY-MM-DD format
        look_back_days: Number of days to look back (for header display only - 
                        Finnhub /news returns latest articles regardless)
        limit: Maximum number of articles to return (default 50)
        category: News category - "general", "forex", "crypto", or "merger"
        
    Returns:
        Formatted string containing market news articles
        
    Raises:
        FinnhubRateLimitError: When rate limit is exceeded
        FinnhubAPIError: For other API errors
    """
    # Calculate start date for display purposes
    end_date = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = end_date - timedelta(days=look_back_days)
    start_date_str = start_date.strftime("%Y-%m-%d")
    
    params = {
        "category": category,
    }
    
    logger.debug(f"Finnhub API - Fetching {category} market news")
    
    articles = _make_api_request("/news", params)
    
    # Filter articles by date if possible (Finnhub returns latest regardless)
    if isinstance(articles, list):
        # Filter by date range
        filtered_articles = []
        start_timestamp = start_date.timestamp()
        end_timestamp = end_date.timestamp()
        
        for article in articles:
            article_time = article.get("datetime", 0)
            if start_timestamp <= article_time <= end_timestamp:
                filtered_articles.append(article)
        
        # If filtering removed all articles, just use the latest ones
        if not filtered_articles:
            filtered_articles = articles
        
        # Limit results
        if len(filtered_articles) > limit:
            filtered_articles = filtered_articles[:limit]
        
        articles = filtered_articles
    
    logger.debug(f"Finnhub API - Retrieved {len(articles) if isinstance(articles, list) else 0} market news articles")
    
    return _format_news_response(articles, None, start_date_str, curr_date)


def get_company_profile(ticker: str) -> dict:
    """
    Get company profile/basic info from Finnhub.
    
    Args:
        ticker: Stock symbol
        
    Returns:
        Dictionary containing company profile data
    """
    params = {
        "symbol": ticker.upper(),
    }
    
    return _make_api_request("/stock/profile2", params)


def get_us_symbol_set() -> Set[str]:
    """Fetch US symbols from Finnhub /stock/symbol (24h cached)."""
    cache = get_cache()
    cached = cache.get("fundamentals", _US_SYMBOL_SET_CACHE_KEY)
    if isinstance(cached, list):
        return {str(s).upper() for s in cached if str(s).strip()}
    if isinstance(cached, set):
        return {str(s).upper() for s in cached if str(s).strip()}

    try:
        payload = _make_api_request("/stock/symbol", {"exchange": "US"})
    except Exception as exc:
        logger.warning("Finnhub /stock/symbol failed: %s", exc)
        return set()

    out: Set[str] = set()
    rows = payload if isinstance(payload, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or "." in symbol:
            symbol = symbol.replace(".", "-")
        if not symbol:
            continue
        sym_type = str(row.get("type") or "").strip().lower()
        if sym_type and sym_type not in {"common stock", "etf", "adr", "preferred", "reit"}:
            # keep the output focused on tradable equity-like instruments
            continue
        out.add(symbol)

    cache.set(
        "fundamentals",
        _US_SYMBOL_SET_CACHE_KEY,
        data=sorted(out),
        ttl=_US_SYMBOL_SET_TTL_SECONDS,
    )
    return out
