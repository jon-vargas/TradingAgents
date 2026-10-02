import logging
from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.config import get_config

logger = logging.getLogger("tradingagents.agents.utils.technical_indicators_tools")

try:
    from tradingagents.utils.token_management import optimize_analyst_input, count_tokens
    TOKEN_MANAGEMENT_AVAILABLE = True
except ImportError:
    TOKEN_MANAGEMENT_AVAILABLE = False

_MAX_TOKENS = 3000
_KNOWN_INDICATORS = frozenset({
    "close_50_sma",
    "close_200_sma",
    "close_10_ema",
    "macd",
    "macds",
    "macdh",
    "rsi",
    "boll",
    "boll_ub",
    "boll_lb",
    "atr",
    "vwma",
    "mfi",
    "kdjk",
    "kdjd",
    "dx",
    "wr",
    "close_10_roc",
})


def normalize_indicator_args(symbol: str, indicator: str, fallback_ticker: str = "") -> tuple[str, str]:
    """Recover ticker when the model passes an indicator name as ``symbol``.

    Observed on SPCX #221: ``get_indicators('atr', 'atr', date)``.
    """
    from tradingagents.agents.utils.tool_context import get_current_ticker

    sym = str(symbol or "").strip()
    ind = str(indicator or "").strip()
    fallback = str(fallback_ticker or get_current_ticker() or "").strip()
    sym_key = sym.lower()
    ind_key = ind.lower()
    if sym_key in _KNOWN_INDICATORS and ind_key not in _KNOWN_INDICATORS and ind:
        logger.warning(
            "get_indicators: swapped symbol/indicator (%s, %s) -> (%s, %s)",
            symbol, indicator, ind, sym,
        )
        return ind, sym
    if sym_key in _KNOWN_INDICATORS:
        recovered = fallback
        use_ind = ind if ind_key in _KNOWN_INDICATORS else sym
        if recovered and recovered.lower() not in _KNOWN_INDICATORS:
            logger.warning(
                "get_indicators: recovered ticker %s from swapped args (%s, %s)",
                recovered, symbol, indicator,
            )
            return recovered, use_ind
    return sym, ind


def _indicator_cap() -> int:
    try:
        cfg = get_config()
        cap = cfg.get("token_budget", {}).get("tools", {}).get("get_indicators", _MAX_TOKENS)
        return max(800, int(cap))
    except Exception:
        return _MAX_TOKENS


@tool
def get_indicators(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
) -> str:
    """
    Retrieve technical indicators for a given ticker symbol.
    Uses the configured technical_indicators vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        indicator (str): Technical indicator to get the analysis and report of
        curr_date (str): The current trading date you are trading on, YYYY-mm-dd
        look_back_days (int): How many days to look back, default is 30
    Returns:
        str: A formatted dataframe containing the technical indicators for the specified ticker symbol and indicator.
    """
    from tradingagents.dataflows.run_date import coerce_tool_date

    symbol, indicator = normalize_indicator_args(symbol, indicator)
    curr_date = coerce_tool_date(curr_date)
    if str(symbol or "").lower() in _KNOWN_INDICATORS:
        return (
            f"ERROR: get_indicators requires a ticker as the first argument, not '{symbol}'. "
            f"Call get_indicators('<TICKER>', '{indicator or symbol}', '{curr_date}')."
        )
    if "," in indicator:
        parts = [i.strip() for i in indicator.split(",") if i.strip()]
        result = "\n\n".join(
            str(route_to_vendor("get_indicators", symbol, ind, curr_date, look_back_days))
            for ind in parts
        )
    else:
        result = route_to_vendor("get_indicators", symbol, indicator.strip(), curr_date, look_back_days)
    max_tokens = _indicator_cap()

    if TOKEN_MANAGEMENT_AVAILABLE:
        tokens = count_tokens(str(result))
        if tokens > max_tokens:
            logger.info("Indicator %s for %s: %d tokens — truncating to %d", indicator, symbol, tokens, max_tokens)
            result = optimize_analyst_input(str(result), data_type="technical", max_tokens=max_tokens)

    return result
