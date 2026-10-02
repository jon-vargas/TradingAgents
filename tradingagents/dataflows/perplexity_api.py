"""
Perplexity API integration — Agent API (responses.create) with citations.

See: https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/how-to
Sonar Chat Completions (legacy) only when PERPLEXITY_API_MODE=sonar through 2026-09-27.
"""

import os
import json
import logging
from datetime import date, datetime, timezone
from typing import Optional, Dict, Any, List, Union, Sequence

from tradingagents.dataflows.cache import get_cache

logger = logging.getLogger("tradingagents.dataflows.perplexity_api")

_SONAR_SUNSET = date(2026, 9, 27)
_SEC_DOMAIN_DEFAULTS = ("sec.gov", "www.sec.gov")

try:
    from perplexity import Perplexity
    PERPLEXITY_SDK_AVAILABLE = True
except ImportError:
    PERPLEXITY_SDK_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class PerplexityAPIError(Exception):
    """Exception raised for Perplexity API errors."""
    pass


class PerplexityRateLimitError(Exception):
    """Exception raised when Perplexity API rate/credit limit is exceeded."""
    pass


def _format_search_after_date(value: Optional[str]) -> Optional[str]:
    """Convert ISO application dates to Perplexity's required MM/DD/YYYY."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%m/%d/%Y")
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid Perplexity after-date filter: %r", value)
        return None


def get_api_key() -> str:
    """Retrieve the API key for Perplexity from environment variables."""
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        raise ValueError("PERPLEXITY_API_KEY environment variable is not set.")
    return api_key


def _api_mode() -> str:
    """Return ``agent`` (default) or legacy ``sonar`` before sunset."""
    mode = (os.getenv("PERPLEXITY_API_MODE") or "agent").strip().lower()
    if mode == "sonar":
        if date.today() > _SONAR_SUNSET:
            logger.warning(
                "PERPLEXITY_API_MODE=sonar ignored after %s; using Agent API",
                _SONAR_SUNSET.isoformat(),
            )
            return "agent"
        return "sonar"
    return "agent"


def get_agent_client() -> "Perplexity":
    """Perplexity Agent API client (``responses.create``)."""
    if not PERPLEXITY_SDK_AVAILABLE:
        raise ImportError(
            "Perplexity SDK required. Install with: pip install perplexityai"
        )
    return Perplexity(api_key=get_api_key())


def get_sonar_client() -> "OpenAI":
    """Legacy Sonar Chat Completions client (pre-sunset rollback only)."""
    if not OPENAI_AVAILABLE:
        raise ImportError("OpenAI SDK required for Sonar rollback. Install with: pip install openai")
    return OpenAI(api_key=get_api_key(), base_url="https://api.perplexity.ai")


def get_client() -> "Perplexity":
    """Primary client — Agent API."""
    return get_agent_client()


def _resolve_preset(model: str) -> str:
    """Map legacy Sonar model ids to Agent API presets."""
    key = (model or "").strip().lower()
    if key == "sonar":
        return "fast"
    if key in ("sonar-pro", "sonar_pro"):
        return "low"
    if key in ("sonar-reasoning-pro", "sonar_reasoning_pro"):
        return "medium"
    if key in ("sonar-deep-research", "sonar_deep_research"):
        return "high"
    if key in ("fast", "low", "medium", "high"):
        return key
    logger.warning("Unknown Perplexity model %r; defaulting preset to low", model)
    return "low"


def _agent_model_for_tier(model: str) -> str:
    """Explicit Agent API model when presets cannot carry custom search filters."""
    key = (model or "").strip().lower()
    if key.startswith("perplexity/"):
        return model.strip()
    if key == "sonar":
        return "perplexity/sonar"
    return "perplexity/sonar"


def _append_primary_source_url(text: str, url: Optional[str]) -> str:
    """Agent API has no file_url; steer search with the URL in the prompt."""
    if not url or not str(url).strip():
        return text
    return f"{text.rstrip()}\n\nPrimary source URL (use this document when possible): {url.strip()}"


def _normalize_user_input(
    query: str,
    user_content: Optional[Union[str, List[Dict[str, Any]]]],
) -> str:
    if user_content is None:
        return query
    if isinstance(user_content, str):
        return user_content
    parts: List[str] = []
    file_url = ""
    for block in user_content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "file_url":
            payload = block.get("file_url") or {}
            file_url = str(payload.get("url") or "")
    merged = "\n".join(p for p in parts if p).strip() or query
    return _append_primary_source_url(merged, file_url or None)


def _build_web_search_tool(
    *,
    web_search_options: Optional[Dict[str, Any]] = None,
    search_domain_filter: Optional[List[str]] = None,
    search_mode: Optional[str] = None,
) -> Dict[str, Any]:
    filters: Dict[str, Any] = {}
    domains = list(search_domain_filter or [])
    if (search_mode or "").lower() == "sec":
        for host in _SEC_DOMAIN_DEFAULTS:
            if host not in domains:
                domains.append(host)
    if domains:
        filters["search_domain_filter"] = domains

    opts = dict(web_search_options or {})
    context_size = opts.pop("search_context_size", None)
    user_location = opts.pop("user_location", None)
    for key, value in opts.items():
        if value is not None:
            filters[key] = value

    tool: Dict[str, Any] = {"type": "web_search"}
    if filters:
        tool["filters"] = filters
    if context_size:
        tool["search_context_size"] = context_size
    if isinstance(user_location, dict) and user_location:
        tool["user_location"] = user_location
    return tool


def _requires_explicit_search_tools(
    search_mode: Optional[str],
    web_search_options: Optional[Dict[str, Any]],
    search_domain_filter: Optional[List[str]],
) -> bool:
    if (search_mode or "").lower() == "sec":
        return True
    if search_domain_filter:
        return True
    if web_search_options:
        return True
    return False


def _extract_output_text(response: Any) -> str:
    if response is None:
        return ""
    if hasattr(response, "output_text"):
        return str(response.output_text or "")
    if hasattr(response, "choices"):
        try:
            return str(response.choices[0].message.content or "")
        except (AttributeError, IndexError, TypeError):
            pass
    return ""


def _search_result_to_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return {
            "title": item.get("title") or "Source",
            "url": item.get("url") or "",
            "date": item.get("date") or item.get("last_updated"),
        }
    return {
        "title": getattr(item, "title", None) or "Source",
        "url": getattr(item, "url", None) or "",
        "date": getattr(item, "date", None) or getattr(item, "last_updated", None),
    }


def _extract_search_results_from_output(response: Any) -> Optional[List[Dict[str, Any]]]:
    legacy = _extract_search_results_sonar(response)
    if legacy:
        return legacy
    output = getattr(response, "output", None)
    if not output:
        data = response.model_dump() if hasattr(response, "model_dump") else {}
        output = data.get("output") if isinstance(data, dict) else None
    if not output:
        return None
    collected: List[Dict[str, Any]] = []
    for item in output:
        item_type = getattr(item, "type", None)
        if item_type is None and isinstance(item, dict):
            item_type = item.get("type")
        if item_type != "search_results":
            continue
        results = getattr(item, "results", None)
        if results is None and isinstance(item, dict):
            results = item.get("results")
        if not results:
            continue
        for row in results:
            collected.append(_search_result_to_dict(row))
    return collected or None


def _extract_search_results_sonar(response: Any) -> Optional[List[Dict[str, Any]]]:
    if response is None:
        return None
    if hasattr(response, "model_dump"):
        data = response.model_dump()
    else:
        data = getattr(response, "__dict__", {})
    if isinstance(data, dict):
        if data.get("search_results"):
            return data.get("search_results")
        if data.get("data", {}).get("search_results"):
            return data.get("data", {}).get("search_results")
    return None


def _extract_search_results(response: Any) -> Optional[List[Dict[str, Any]]]:
    return _extract_search_results_from_output(response)


def _format_search_results(search_results: Optional[List[Dict[str, Any]]]) -> str:
    if not search_results:
        return ""

    lines = ["\n\nSources:"]
    for result in search_results:
        title = result.get("title") or "Source"
        url = result.get("url") or ""
        date = result.get("date")
        date_str = f" ({date})" if date else ""
        if url:
            lines.append(f"- {title}{date_str}: {url}")
        else:
            lines.append(f"- {title}{date_str}")
    return "\n".join(lines)


def _extract_json_payload(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    trimmed = text.strip()
    try:
        data = json.loads(trimmed)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = trimmed[start:end + 1]
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _extract_json_array(text: str) -> Optional[List[Dict[str, Any]]]:
    """Extract a JSON array from LLM response text.

    Handles both clean JSON and responses wrapped in markdown/prose.
    Returns None if no valid array can be extracted.
    """
    if not text:
        return None
    trimmed = text.strip()
    try:
        data = json.loads(trimmed)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    start = trimmed.find("[")
    end = trimmed.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(trimmed[start:end + 1])
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


def _handle_request_error(e: Exception) -> None:
    error_str = str(e).lower()
    if "rate" in error_str or "limit" in error_str or "credit" in error_str:
        raise PerplexityRateLimitError(f"Perplexity rate/credit limit exceeded: {e}") from e
    if "timeout" in error_str or "timed out" in error_str:
        raise PerplexityAPIError(f"Perplexity request timed out (60s): {e}") from e
    raise PerplexityAPIError(f"Perplexity API error: {e}") from e


def _make_request_sonar(
    query: str,
    model: str,
    temperature: float,
    max_tokens: int,
    search_mode: Optional[str],
    search_domain: Optional[str],
    web_search_options: Optional[Dict[str, Any]],
    reasoning_effort: Optional[str],
    include_sources: bool,
    user_content: Optional[Union[str, List[Dict[str, Any]]]],
    search_domain_filter: Optional[List[str]],
    system_prompt: Optional[str],
) -> str:
    client = get_sonar_client()
    user_payload = user_content if user_content is not None else query
    default_system = (
        "You are a professional financial research analyst. "
        "Provide accurate, well-cited information with specific numbers and dates. "
        "Always cite your sources. Be concise but comprehensive."
    )
    request_args: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt if system_prompt is not None else default_system},
            {"role": "user", "content": user_payload},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    extra_body: Dict[str, Any] = {}
    if search_mode:
        extra_body["search_mode"] = search_mode
    if search_domain:
        extra_body["search_domain"] = search_domain
    if web_search_options:
        extra_body["web_search_options"] = web_search_options
    if reasoning_effort:
        extra_body["reasoning_effort"] = reasoning_effort
    if search_domain_filter:
        extra_body["search_domain_filter"] = search_domain_filter
    if extra_body:
        request_args["extra_body"] = extra_body
    try:
        response = client.chat.completions.create(**request_args, timeout=60.0)
        content = _extract_output_text(response)
        if include_sources:
            sources = _extract_search_results_sonar(response)
            content = f"{content}{_format_search_results(sources)}"
        return content
    except (PerplexityRateLimitError, PerplexityAPIError):
        raise
    except Exception as e:
        _handle_request_error(e)
    return ""


def _make_request_agent(
    query: str,
    model: str,
    temperature: float,
    max_tokens: int,
    search_mode: Optional[str],
    web_search_options: Optional[Dict[str, Any]],
    reasoning_effort: Optional[str],
    include_sources: bool,
    user_content: Optional[Union[str, List[Dict[str, Any]]]],
    search_domain_filter: Optional[List[str]],
    system_prompt: Optional[str],
) -> str:
    client = get_agent_client()
    user_text = _normalize_user_input(query, user_content)
    default_system = (
        "You are a professional financial research analyst. "
        "Provide accurate, well-cited information with specific numbers and dates. "
        "Always cite your sources. Be concise but comprehensive."
    )
    instructions = system_prompt if system_prompt is not None else default_system
    explicit = _requires_explicit_search_tools(
        search_mode, web_search_options, search_domain_filter
    )
    request_args: Dict[str, Any] = {
        "input": user_text,
        "instructions": instructions,
        "max_output_tokens": max_tokens,
    }
    if explicit:
        # Preset requests reject temperature (API 400). Explicit-model calls accept it.
        request_args["temperature"] = temperature
        request_args["model"] = _agent_model_for_tier(model)
        request_args["tools"] = [
            _build_web_search_tool(
                web_search_options=web_search_options,
                search_domain_filter=search_domain_filter,
                search_mode=search_mode,
            )
        ]
    else:
        request_args["preset"] = _resolve_preset(model)
    if reasoning_effort:
        request_args["reasoning"] = {"effort": reasoning_effort}
    try:
        response = client.responses.create(**request_args, timeout=60.0)
        content = _extract_output_text(response)
        if include_sources:
            sources = _extract_search_results_from_output(response)
            content = f"{content}{_format_search_results(sources)}"
        return content
    except (PerplexityRateLimitError, PerplexityAPIError):
        raise
    except Exception as e:
        _handle_request_error(e)
    return ""


def _make_request(
    query: str,
    model: str = "sonar-pro",
    temperature: float = 0.2,
    max_tokens: int = 2000,
    search_mode: Optional[str] = None,
    search_domain: Optional[str] = None,
    web_search_options: Optional[Dict[str, Any]] = None,
    reasoning_effort: Optional[str] = None,
    include_sources: bool = True,
    user_content: Optional[Union[str, List[Dict[str, Any]]]] = None,
    search_domain_filter: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
) -> str:
    """
    Make a request to Perplexity (Agent API by default).

    Legacy ``model`` values ``sonar`` / ``sonar-pro`` map to Agent presets
    ``fast`` / ``low``. SEC filing calls use explicit ``web_search`` tools.
    """
    if search_domain and not search_domain_filter:
        search_domain_filter = [search_domain]
    if _api_mode() == "sonar":
        return _make_request_sonar(
            query,
            model,
            temperature,
            max_tokens,
            search_mode,
            search_domain,
            web_search_options,
            reasoning_effort,
            include_sources,
            user_content,
            search_domain_filter,
            system_prompt,
        )
    return _make_request_agent(
        query,
        model,
        temperature,
        max_tokens,
        search_mode,
        web_search_options,
        reasoning_effort,
        include_sources,
        user_content,
        search_domain_filter,
        system_prompt,
    )


# =============================================================================
# OPTIMIZED RESEARCH QUERIES
# =============================================================================

def get_deep_research(ticker: str, company_name: str = None) -> str:
    """
    Comprehensive stock research in a SINGLE API call.
    Combines 5+ data points for maximum efficiency.
    
    Args:
        ticker: Stock symbol (e.g., "NFLX")
        company_name: Optional company name for better results
        
    Returns:
        Comprehensive research report with citations
    """
    company = company_name or ticker
    
    query = f"""
For {company} ({ticker}), provide a comprehensive investment research report:

## 1. Latest Earnings Analysis
- Most recent quarterly revenue, EPS, and YoY growth rates
- Key beats or misses vs analyst expectations
- Notable items from the earnings call

## 2. Management Guidance & Outlook
- Forward guidance for next quarter/year
- Key strategic initiatives mentioned
- Any changes to previous guidance

## 3. Analyst Consensus
- Current buy/hold/sell rating distribution
- Average and range of price targets
- Recent rating changes (upgrades/downgrades)

## 4. Recent SEC Filings & Insider Activity
- Key risk factors from latest 10-K/10-Q
- Recent insider buying or selling (Form 4)
- Any material 8-K filings

## 5. Competitive Position
- Market share trends
- How the company compares to main competitors
- Recent competitive developments

Provide specific numbers, dates, and cite all sources. Focus on the most recent and relevant information.
"""
    
    logger.debug("Perplexity API - Deep research for %s", ticker)
    result = _make_request(query, model="sonar-pro", max_tokens=3000)
    logger.debug("Perplexity API - Received %d chars of research", len(result))
    
    return f"## Deep Research Report: {ticker}\n\n{result}"


def get_earnings_analysis(ticker: str, company_name: str = None) -> str:
    """
    Focused earnings call and financial performance analysis.
    
    Args:
        ticker: Stock symbol
        company_name: Optional company name
        
    Returns:
        Earnings analysis with citations
    """
    company = company_name or ticker
    
    query = f"""
Analyze {company} ({ticker})'s most recent earnings:

1. **Financial Results**: Revenue, EPS, margins vs expectations
2. **Key Metrics**: Subscriber/user growth, ARPU, or relevant KPIs
3. **Management Commentary**: Notable quotes from the earnings call
4. **Guidance**: Forward-looking statements and targets
5. **Analyst Reactions**: How analysts responded to the results

Include specific numbers and cite sources.
"""
    
    logger.debug("Perplexity API - Earnings analysis for %s", ticker)
    result = _make_request(query, model="sonar-pro", max_tokens=2000)
    
    return f"## Earnings Analysis: {ticker}\n\n{result}"


def get_sec_filings_summary(
    ticker: str,
    company_name: str = None,
    after_date: Optional[str] = None,
    filing_url: Optional[str] = None,
) -> str:
    """
    Summary of recent SEC filings and insider activity.
    
    Args:
        ticker: Stock symbol
        company_name: Optional company name
        
    Returns:
        SEC filings summary with citations
    """
    company = company_name or ticker
    
    query = f"""
Summarize recent SEC filings for {company} ({ticker}):

1. **10-K/10-Q Highlights**: Key risk factors and business updates
2. **Insider Transactions**: Recent Form 4 filings (buys/sells by executives)
3. **Institutional Holdings**: Notable 13F changes
4. **Material Events**: Any recent 8-K filings

Focus on the most significant and recent items. Cite specific filing dates and sources.
"""

    logger.debug("Perplexity API - SEC filings for %s", ticker)
    web_search_options: Dict[str, Any] = {"search_context_size": "medium"}
    search_after_date = _format_search_after_date(after_date)
    if search_after_date:
        web_search_options["search_after_date_filter"] = search_after_date

    prompt = _append_primary_source_url(query, filing_url)

    result = _make_request(
        query=prompt,
        model="sonar-pro",
        max_tokens=1500,
        search_mode="sec",
        web_search_options=web_search_options,
    )

    return f"## SEC Filings Summary: {ticker}\n\n{result}"


def get_sec_filings_snapshot(
    ticker: str,
    company_name: str = None,
    after_date: Optional[str] = None,
    filing_url: Optional[str] = None,
) -> str:
    """
    Structured snapshot of recent SEC filings (10-K/10-Q/8-K) with source URLs.
    Returns JSON for downstream ingestion and storage.
    """
    company = company_name or ticker
    date_clause = f"after {after_date}" if after_date else "in the last 12 months"

    query = f"""
Return JSON only (no markdown, no prose). For {company} ({ticker}), build a filings snapshot {date_clause}.

Schema:
{{
  "ticker": "{ticker}",
  "company": "{company}",
  "as_of": "{datetime.now(timezone.utc).date().isoformat()}",
  "filings": [
    {{
      "form": "10-K|10-Q|8-K",
      "filing_date": "YYYY-MM-DD",
      "period_end": "YYYY-MM-DD",
      "url": "https://...",
      "highlights": ["..."],
      "risk_factors": ["..."],
      "material_events": ["..."]
    }}
  ],
  "insider_activity": [
    {{
      "name": "",
      "role": "",
      "transaction_type": "buy|sell",
      "date": "YYYY-MM-DD",
      "shares": "",
      "value": "",
      "url": "https://..."
    }}
  ],
  "notes": ""
}}

Use source URLs for each filing and transaction. If unknown, use null.
"""

    logger.debug("Perplexity API - SEC filings snapshot for %s", ticker)
    web_search_options: Dict[str, Any] = {"search_context_size": "medium"}
    search_after_date = _format_search_after_date(after_date)
    if search_after_date:
        web_search_options["search_after_date_filter"] = search_after_date

    prompt = _append_primary_source_url(query, filing_url)

    response = _make_request(
        query=prompt,
        model="sonar-pro",
        max_tokens=4000,
        search_mode="sec",
        web_search_options=web_search_options,
        include_sources=False,
    )

    payload = _extract_json_payload(response)
    if payload:
        return f"SEC_FILINGS_SNAPSHOT_JSON:\n{json.dumps(payload, indent=2)}"
    return f"SEC_FILINGS_SNAPSHOT_RAW:\n{response}"


def get_earnings_transcript_snapshot(
    ticker: str,
    company_name: str = None,
    transcript_url: Optional[str] = None,
) -> str:
    """
    Structured snapshot of the most recent earnings transcript with guidance extraction.
    Returns JSON for downstream ingestion and storage.
    """
    company = company_name or ticker

    query = f"""
Return JSON only (no markdown, no prose). For {company} ({ticker}), parse the latest earnings call transcript.

Schema:
{{
  "ticker": "{ticker}",
  "company": "{company}",
  "period": "FY2025 Q4",
  "call_date": "YYYY-MM-DD",
  "guidance": [
    {{
      "metric": "revenue|eps|margin|capex|fcf|other",
      "range": "",
      "timeframe": "next quarter|full year",
      "context": ""
    }}
  ],
  "kpis": [
    {{
      "name": "",
      "value": "",
      "period": "",
      "context": ""
    }}
  ],
  "key_quotes": [
    {{
      "speaker": "",
      "quote": "",
      "topic": ""
    }}
  ],
  "source_urls": []
}}

Include citation URLs in source_urls. If unavailable, use an empty list.
"""

    logger.debug("Perplexity API - Earnings transcript snapshot for %s", ticker)
    prompt = _append_primary_source_url(query, transcript_url)

    response = _make_request(
        query=prompt,
        model="sonar-pro",
        max_tokens=1600,
        include_sources=False,
    )

    payload = _extract_json_payload(response)
    if payload:
        return f"EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:\n{json.dumps(payload, indent=2)}"
    return f"EARNINGS_TRANSCRIPT_SNAPSHOT_RAW:\n{response}"


def get_news_with_analysis(ticker: str, days: int = 7) -> str:
    """
    Recent news with AI analysis and sentiment.
    Use as Finnhub fallback or for deeper news analysis.
    
    Args:
        ticker: Stock symbol
        days: Number of days to look back
        
    Returns:
        News summary with analysis and citations
    """
    query = f"""
Summarize the most important news for {ticker} from the past {days} days:

1. **Key Headlines**: Most significant news stories
2. **Market Impact**: How the news affected the stock price
3. **Sentiment Analysis**: Overall positive/negative/neutral assessment
4. **Analyst Commentary**: Expert opinions on the news
5. **What to Watch**: Upcoming events or catalysts

Prioritize market-moving news. Cite all sources with dates.
"""
    
    logger.debug("Perplexity API - News analysis for %s", ticker)
    result = _make_request(query, model="sonar", max_tokens=1500)
    
    return f"## News Analysis: {ticker} (Last {days} Days)\n\n{result}"


# =============================================================================
# VENDOR-COMPATIBLE INTERFACE
# =============================================================================

def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """
    Vendor-compatible news function for interface.py integration.
    
    Args:
        ticker: Stock symbol
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        
    Returns:
        News summary with citations
    """
    # Calculate days between dates
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        days = (end - start).days
    except Exception as e:
        logger.warning("Failed to parse date range, defaulting to 7 days: %s", e)
        days = 7
    
    return get_news_with_analysis(ticker, days=max(1, days))


def get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 50) -> str:
    """
    Vendor-compatible global news function for interface.py integration.
    
    Args:
        curr_date: Current date (YYYY-MM-DD)
        look_back_days: Days to look back
        limit: Ignored (Perplexity handles its own limits)
        
    Returns:
        Global market news summary with citations
    """
    query = f"""
Summarize the most important global market news from the past {look_back_days} days:

1. **Major Market Moves**: Significant index movements and why
2. **Economic Data**: Key releases (jobs, inflation, GDP, etc.)
3. **Central Bank Actions**: Fed, ECB, or other policy updates
4. **Geopolitical Events**: Market-relevant global developments
5. **Sector Highlights**: Notable sector rotations or themes

Focus on market-moving events. Cite sources with dates.
"""
    
    logger.debug("Perplexity API - Global market news")
    result = _make_request(query, model="sonar", max_tokens=1500)
    
    return f"## Global Market News (Last {look_back_days} Days)\n\n{result}"


def get_fundamentals(ticker: str, curr_date: str) -> str:
    """
    Vendor-compatible fundamentals function for interface.py integration.
    Uses Perplexity for comprehensive fundamental analysis.
    
    Args:
        ticker: Stock symbol
        curr_date: Current date (YYYY-MM-DD)
        
    Returns:
        Fundamental analysis with citations
    """
    query = f"""
Provide fundamental analysis for {ticker} as of {curr_date}:

1. **Valuation Metrics**: P/E, P/S, P/B, EV/EBITDA ratios
2. **Growth Metrics**: Revenue growth, EPS growth, margins
3. **Financial Health**: Debt/equity, current ratio, cash position
4. **Profitability**: ROE, ROA, operating margins
5. **Dividend Info**: Yield, payout ratio (if applicable)

Compare to industry averages where relevant. Cite sources.
"""
    
    logger.debug("Perplexity API - Fundamentals for %s", ticker)
    result = _make_request(query, model="sonar-pro", max_tokens=2000)
    
    return f"## Fundamental Analysis: {ticker}\n\n{result}"


# =============================================================================
# TARGETED RESEARCH QUERIES (with caching)
# =============================================================================

def get_catalyst_pipeline(ticker: str, company_name: str = "") -> str:
    """Research upcoming catalysts and events that could move the stock price."""
    name_str = f" ({company_name})" if company_name else ""
    query = (
        f"What are the key upcoming catalysts for {ticker}{name_str} stock in the next 3-6 months? "
        f"Include: earnings dates, product launches, FDA decisions, regulatory milestones, "
        f"contract announcements, management changes, index inclusion/exclusion, "
        f"and any pending litigation or M&A activity. "
        f"Rank catalysts by potential price impact (high/medium/low)."
    )
    
    try:
        result = _make_request(
            query=query,
            model="sonar",
            temperature=0.1,
            max_tokens=1500,
        )
        return result
    except Exception as e:
        logger.warning("Catalyst pipeline query failed for %s: %s", ticker, e)
        return ""


# =============================================================================
# TICKER DISCOVERY
# =============================================================================

def _validate_tickers(
    tickers: List[Dict[str, Any]],
    max_workers: int = 8,
    *,
    asset_policy: str = "equity",
    market_cap_filter: str = "",
    fit_policy: str = "",
    sector_allowlist: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """Validate ticker entries against cached yfinance info in parallel.

    Each entry gets ``validated=True/False`` plus listing metadata. Valid
    rows overwrite ``company`` from Yahoo and add ``market_cap_actual``.
    """
    from concurrent.futures import ThreadPoolExecutor

    from tradingagents.dataflows.yfinance_extended import get_ticker_info
    from tradingagents.screening.discovery import (
        evaluate_book_fit,
        evaluate_listing,
        is_plausible_ticker,
        normalize_ticker_symbol,
        parse_market_cap_filter,
    )

    bounds = parse_market_cap_filter(market_cap_filter)

    def _check(entry: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(entry or {})
        sym = normalize_ticker_symbol(row.get("ticker"))
        row["ticker"] = sym
        if not is_plausible_ticker(sym):
            row["validated"] = False
            row["rejected_reason"] = "implausible ticker"
            return row
        try:
            info = get_ticker_info(sym) or {}
            gate = evaluate_listing(info, asset_policy=asset_policy, mcap_bounds=bounds)
            row["quote_type"] = gate.get("quote_type") or ""
            row["exchange"] = gate.get("exchange") or ""
            if gate.get("company"):
                row["company"] = gate["company"]
            if gate.get("market_cap") is not None:
                row["market_cap_actual"] = gate["market_cap"]
            if not gate.get("ok"):
                row["validated"] = False
                row["rejected_reason"] = gate.get("reason") or "listing check failed"
                return row
            fit = evaluate_book_fit(
                sym, info, fit_policy=fit_policy, sector_allowlist=sector_allowlist,
            )
            if not fit.get("ok"):
                row["validated"] = False
                row["rejected_reason"] = fit.get("reason") or "book-fit check failed"
                return row
            row["validated"] = True
            row.pop("rejected_reason", None)
        except Exception as exc:
            row["validated"] = False
            row["rejected_reason"] = f"listing lookup failed ({exc.__class__.__name__})"
        return row

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_check, t) for t in tickers]
        return [f.result() for f in futures]


def discover_opportunities(
    theme: str,
    criteria: str = "",
    max_results: int = 20,
    market_cap_filter: str = "",
    model: str = "sonar-pro",
    prompt_pack: str = "equity_search",
    asset_policy: str = "equity",
    template_id: str = "",
) -> Dict[str, Any]:
    """Use Perplexity to discover tickers matching an investment theme.

    Args:
        model: Perplexity model to use. ``"sonar-pro"`` (default) for manual
            discovery; ``"sonar"`` for cheaper auto-discovery.
        prompt_pack: Catalog pack (``equity_search``, ``etf_search``, ...).
        asset_policy: Listing gate (``equity``, ``etf``, ``adr``, ``any``).
    """
    from tradingagents.screening.discovery import (
        build_discovery_prompt,
        extract_ticker_candidates,
        get_discovery_template,
        infer_fit_policy,
        normalize_ticker_symbol,
    )

    tmpl = get_discovery_template(template_id)
    if tmpl:
        theme = theme or tmpl.theme
        criteria = criteria or tmpl.criteria
        market_cap_filter = market_cap_filter or tmpl.market_cap_filter
        prompt_pack = prompt_pack or tmpl.prompt_pack
        asset_policy = asset_policy or tmpl.asset_policy

    fit_policy = infer_fit_policy(
        tmpl,
        prompt_pack=prompt_pack,
        asset_policy=asset_policy,
        theme=theme,
    )
    sector_allowlist = tuple(tmpl.sector_allowlist) if tmpl else ()

    system_prompt, user_prompt = build_discovery_prompt(
        theme=theme,
        criteria=criteria,
        market_cap_filter=market_cap_filter,
        max_results=max_results,
        prompt_pack=prompt_pack,
        asset_policy=asset_policy,
    )

    logger.info(
        "Perplexity discovery: theme=%r pack=%s policy=%s model=%s max=%d",
        theme, prompt_pack, asset_policy, model, max_results,
    )

    try:
        raw = _make_request(
            query=user_prompt,
            model=model,
            temperature=0.1,
            max_tokens=3000,
            include_sources=True,
            system_prompt=system_prompt,
        )
    except (PerplexityRateLimitError, PerplexityAPIError):
        raise
    except Exception as e:
        logger.error("Discovery failed unexpectedly: %s", e)
        return {"tickers": [], "theme": theme, "raw_response": str(e), "source": "perplexity"}

    json_body = raw.split("\n\nSources:", 1)[0] if raw else ""
    parsed = _extract_json_array(json_body)
    used_regex_fallback = False

    if not parsed:
        symbols = extract_ticker_candidates(json_body or raw or "", max_results=max_results)
        parsed = [{
            "ticker": s,
            "company": "",
            "sector": "",
            "market_cap_approx": "",
            "rationale": "",
        } for s in symbols]
        used_regex_fallback = True
        logger.warning("JSON parse failed for discovery; extracted %d symbols via regex", len(parsed))

    cleaned = []
    seen = set()
    for row in parsed:
        if not isinstance(row, dict):
            continue
        sym = normalize_ticker_symbol(row.get("ticker"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        row = dict(row)
        row["ticker"] = sym
        cleaned.append(row)
        if len(cleaned) >= max_results:
            break

    validated = _validate_tickers(
        cleaned,
        asset_policy=asset_policy,
        market_cap_filter=market_cap_filter,
        fit_policy=fit_policy,
        sector_allowlist=sector_allowlist,
    )

    return {
        "tickers": validated,
        "theme": theme,
        "criteria": criteria,
        "market_cap_filter": market_cap_filter,
        "prompt_pack": prompt_pack,
        "asset_policy": asset_policy,
        "fit_policy": fit_policy,
        "template_id": template_id or None,
        "raw_response": raw,
        "source": "perplexity",
        "used_regex_fallback": used_regex_fallback,
    }
