import logging
from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.vendor_errors import annotate_vendor_payload

logger = logging.getLogger("tradingagents.agents.utils.core_stock_tools")

try:
    from tradingagents.utils.token_management import optimize_analyst_input, count_tokens
    TOKEN_MANAGEMENT_AVAILABLE = True
except ImportError:
    TOKEN_MANAGEMENT_AVAILABLE = False

_MAX_TOKENS = 6000


def _stock_data_cap() -> int:
    try:
        cfg = get_config()
        cap = cfg.get("token_budget", {}).get("tools", {}).get("get_stock_data", _MAX_TOKENS)
        return max(1000, int(cap))
    except Exception:
        return _MAX_TOKENS


@tool
def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve stock price data (OHLCV) for a given ticker symbol.
    Uses the configured core_stock_apis vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.
    """
    result = annotate_vendor_payload(
        route_to_vendor("get_stock_data", symbol, start_date, end_date)
    )
    max_tokens = _stock_data_cap()

    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > max_tokens:
            logger.info("Stock data for %s: %d tokens — truncating to %d", symbol, tokens, max_tokens)
            result = optimize_analyst_input(str(result), data_type="technical", max_tokens=max_tokens)

    return result
