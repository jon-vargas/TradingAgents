import os
import logging
import requests
import pandas as pd
import json
from typing import Set
from datetime import datetime
from io import StringIO

from tradingagents.dataflows.cache import get_cache

logger = logging.getLogger("tradingagents.dataflows.alpha_vantage_common")

API_BASE_URL = "https://www.alphavantage.co/query"
_LISTING_STATUS_CACHE_KEY = "av_listing_status:active_us"
_LISTING_STATUS_TTL_SECONDS = 24 * 60 * 60

def get_api_key() -> str:
    """Retrieve the API key for Alpha Vantage from environment variables."""
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise ValueError("ALPHA_VANTAGE_API_KEY environment variable is not set.")
    return api_key

def format_datetime_for_api(date_input) -> str:
    """Convert various date formats to YYYYMMDDTHHMM format required by Alpha Vantage API."""
    if isinstance(date_input, str):
        # If already in correct format, return as-is
        if len(date_input) == 13 and 'T' in date_input:
            return date_input
        # Try to parse common date formats
        try:
            dt = datetime.strptime(date_input, "%Y-%m-%d")
            return dt.strftime("%Y%m%dT0000")
        except ValueError:
            try:
                dt = datetime.strptime(date_input, "%Y-%m-%d %H:%M")
                return dt.strftime("%Y%m%dT%H%M")
            except ValueError:
                raise ValueError(f"Unsupported date format: {date_input}")
    elif isinstance(date_input, datetime):
        return date_input.strftime("%Y%m%dT%H%M")
    else:
        raise ValueError(f"Date must be string or datetime object, got {type(date_input)}")

class AlphaVantageRateLimitError(Exception):
    """Exception raised when Alpha Vantage API rate limit is exceeded."""
    pass

def _make_api_request(function_name: str, params: dict) -> dict | str:
    """Helper function to make API requests and handle responses.
    
    Raises:
        AlphaVantageRateLimitError: When API rate limit is exceeded
    """
    # Create a copy of params to avoid modifying the original
    api_params = params.copy()
    api_params.update({
        "function": function_name,
        "apikey": get_api_key(),
        "source": "trading_agents",
    })
    
    # Handle entitlement parameter if present in params or global variable
    current_entitlement = globals().get('_current_entitlement')
    entitlement = api_params.get("entitlement") or current_entitlement
    
    if entitlement:
        api_params["entitlement"] = entitlement
    elif "entitlement" in api_params:
        # Remove entitlement if it's None or empty
        api_params.pop("entitlement", None)
    
    response = requests.get(API_BASE_URL, params=api_params)
    response.raise_for_status()

    response_text = response.text
    
    # Check if response is JSON (error responses are typically JSON)
    try:
        response_json = json.loads(response_text)
        # Check for rate limit error
        if "Information" in response_json:
            info_message = response_json["Information"]
            if "rate limit" in info_message.lower() or "api key" in info_message.lower():
                raise AlphaVantageRateLimitError(f"Alpha Vantage rate limit exceeded: {info_message}")
    except json.JSONDecodeError:
        # Response is not JSON (likely CSV data), which is normal
        pass

    return response_text



def _filter_csv_by_date_range(csv_data: str, start_date: str, end_date: str) -> str:
    """
    Filter CSV data to include only rows within the specified date range.

    Args:
        csv_data: CSV string from Alpha Vantage API
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format

    Returns:
        Filtered CSV string
    """
    if not csv_data or csv_data.strip() == "":
        return csv_data

    try:
        # Parse CSV data
        df = pd.read_csv(StringIO(csv_data))

        # Assume the first column is the date column (timestamp)
        date_col = df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col])

        # Filter by date range
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)

        filtered_df = df[(df[date_col] >= start_dt) & (df[date_col] <= end_dt)]

        # Convert back to CSV string
        return filtered_df.to_csv(index=False)

    except Exception as e:
        # If filtering fails, return original data with a warning
        logger.warning(f"Failed to filter CSV data by date range: {e}")
        return csv_data


def get_active_us_symbols() -> Set[str]:
    """Fetch active US stock symbols via LISTING_STATUS (24h cached)."""
    cache = get_cache()
    cached = cache.get("fundamentals", _LISTING_STATUS_CACHE_KEY)
    if isinstance(cached, list):
        return {str(s).upper() for s in cached if str(s).strip()}
    if isinstance(cached, set):
        return {str(s).upper() for s in cached if str(s).strip()}

    try:
        csv_text = _make_api_request("LISTING_STATUS", {"state": "active"})
        if not isinstance(csv_text, str) or not csv_text.strip():
            return set()
        frame = pd.read_csv(StringIO(csv_text))
    except Exception as exc:
        logger.warning("Alpha Vantage LISTING_STATUS failed: %s", exc)
        return set()

    out: Set[str] = set()
    for _, row in frame.iterrows():
        try:
            symbol = str(row.get("symbol") or "").strip().upper()
            status = str(row.get("status") or "").strip().lower()
            asset_type = str(row.get("assetType") or "").strip().lower()
            exchange = str(row.get("exchange") or "").strip().upper()
            if not symbol:
                continue
            if status and status != "active":
                continue
            if asset_type and asset_type not in {"stock", "etf"}:
                continue
            if exchange and exchange not in {"NYSE", "NASDAQ", "NYSE ARCA", "AMEX", "BATS", "NYSE MKT"}:
                continue
            out.add(symbol.replace(".", "-"))
        except Exception:
            continue

    cache.set(
        "fundamentals",
        _LISTING_STATUS_CACHE_KEY,
        data=sorted(out),
        ttl=_LISTING_STATUS_TTL_SECONDS,
    )
    return out
