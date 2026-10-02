from langchain_core.tools import tool
from typing import Annotated

from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot
from tradingagents.dataflows.vendor_errors import annotate_vendor_payload


@tool
def get_verified_market_snapshot(
    symbol: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "The current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many recent closes to include"] = 30,
) -> str:
    """Return a deterministic OHLCV + indicator snapshot for exact numeric claims.

    A vendor-unavailable result means retry later. It is not evidence the symbol is delisted.
    """
    return annotate_vendor_payload(
        build_verified_market_snapshot(symbol, curr_date, look_back_days=look_back_days)
    )
