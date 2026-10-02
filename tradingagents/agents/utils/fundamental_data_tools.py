import logging
from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.config import get_config

logger = logging.getLogger("tradingagents.agents.utils.fundamental_data_tools")

try:
    from tradingagents.utils.token_management import optimize_analyst_input, count_tokens
    TOKEN_MANAGEMENT_AVAILABLE = True
except ImportError:
    TOKEN_MANAGEMENT_AVAILABLE = False

_FUNDAMENTALS_MAX = 6000
_STATEMENT_MAX = 4000


def _fundamentals_cap() -> int:
    try:
        cfg = get_config()
        cap = cfg.get("token_budget", {}).get("tools", {}).get("get_fundamentals", _FUNDAMENTALS_MAX)
        return max(1200, int(cap))
    except Exception:
        return _FUNDAMENTALS_MAX


def _statement_cap(method_name: str) -> int:
    try:
        cfg = get_config()
        cap = cfg.get("token_budget", {}).get("tools", {}).get(method_name, _STATEMENT_MAX)
        return max(1000, int(cap))
    except Exception:
        return _STATEMENT_MAX


@tool
def get_fundamentals(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """
    Retrieve comprehensive fundamental data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing comprehensive fundamental data
    """
    result = route_to_vendor("get_fundamentals", ticker, curr_date)
    max_tokens = _fundamentals_cap()

    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > max_tokens:
            logger.info("Fundamentals for %s: %d tokens — truncating to %d", ticker, tokens, max_tokens)
            result = optimize_analyst_input(str(result), data_type="fundamentals", max_tokens=max_tokens)

    return result


@tool
def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve balance sheet data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing balance sheet data
    """
    result = route_to_vendor("get_balance_sheet", ticker, freq, curr_date)
    max_tokens = _statement_cap("get_balance_sheet")

    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > max_tokens:
            logger.info("Balance sheet for %s: %d tokens — truncating to %d", ticker, tokens, max_tokens)
            result = optimize_analyst_input(str(result), data_type="fundamentals", max_tokens=max_tokens)

    return result


@tool
def get_cashflow(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve cash flow statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing cash flow statement data
    """
    result = route_to_vendor("get_cashflow", ticker, freq, curr_date)
    max_tokens = _statement_cap("get_cashflow")

    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > max_tokens:
            logger.info("Cashflow for %s: %d tokens — truncating to %d", ticker, tokens, max_tokens)
            result = optimize_analyst_input(str(result), data_type="fundamentals", max_tokens=max_tokens)

    return result


@tool
def get_income_statement(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve income statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing income statement data
    """
    result = route_to_vendor("get_income_statement", ticker, freq, curr_date)
    max_tokens = _statement_cap("get_income_statement")

    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > max_tokens:
            logger.info("Income statement for %s: %d tokens — truncating to %d", ticker, tokens, max_tokens)
            result = optimize_analyst_input(str(result), data_type="fundamentals", max_tokens=max_tokens)

    return result
