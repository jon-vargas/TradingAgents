import os
import time
from datetime import datetime
from typing import Annotated, Optional

import pandas as pd
import yfinance as yf
from stockstats import wrap

from .config import get_config, DATA_DIR

# Calendar-day window for rejecting a yfinance frame whose latest row is far
# older than the requested end date (year-old partial responses). Weekend and
# holiday gaps of <= this many calendar days still pass.
MAX_OHLCV_STALE_DAYS = 10

# How long a today-keyed cache file may be reused before refetch. Short enough
# that an intraday run picks up today's close soon after it publishes; long
# enough that a day with no bar (weekend, holiday) cannot download on every call.
OHLCV_CACHE_TTL_SECONDS = 900


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame: parse dates, drop invalid rows, fill price gaps."""
    data = data.copy()
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])
    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()
    return data


def _assert_ohlcv_not_stale(data: pd.DataFrame, end_date: str) -> Optional[str]:
    """Return a stale-frame note if the latest bar is too old vs end_date.

    Compares naive calendar dates via ``.date()``. Caller must strip timezone
    first. Returns None when the frame is within ``MAX_OHLCV_STALE_DAYS``
    (including weekend/holiday gaps). Does not raise — callers format a
    ``No data found`` string so backtests stay compatible.
    """
    if data is None or data.empty:
        return None
    latest = data.index.max()
    latest_date = pd.Timestamp(latest).date()
    requested_end = datetime.strptime(end_date, "%Y-%m-%d").date()
    gap_days = (requested_end - latest_date).days
    if gap_days > MAX_OHLCV_STALE_DAYS:
        return f"stale frame: latest row {latest_date.isoformat()}"
    return None


def _needs_same_day_refresh(
    data_file,
    path_end_date,
    today_date=None,
    curr_date=None,
) -> bool:
    """Whether a cached CSV must be refetched to reflect the current day.

    Keys off the filename / path end date, not ``curr_date``. A today-named
    file with mtime older than ``OHLCV_CACHE_TTL_SECONDS`` refreshes even if
    the caller passed a historical ``curr_date`` (e.g. last Friday).
    Historical path end dates never refresh, however old the file is.
    """
    del curr_date  # ignored; refresh is a file/mtime problem
    if today_date is None:
        today_date = pd.Timestamp.today()
    end = pd.Timestamp(path_end_date)
    if end.date() != pd.Timestamp(today_date).date():
        return False
    if not os.path.exists(data_file):
        return False
    return time.time() - os.path.getmtime(data_file) > OHLCV_CACHE_TTL_SECONDS


def load_or_refresh_ohlcv_csv(
    symbol: str,
    start_date: str,
    end_date: str,
    data_file: str,
    today_date=None,
    curr_date=None,
) -> pd.DataFrame:
    """Load a cached OHLCV CSV or refetch when the today-keyed file is past TTL.

    When refresh is required this downloads, overwrites the CSV, then returns
    the new frame. A True refresh result never ``read_csv``s the old file.
    ``curr_date`` is accepted and ignored.
    """
    if today_date is None:
        today_date = pd.Timestamp.today()
    should_refresh = _needs_same_day_refresh(
        data_file, end_date, today_date=today_date, curr_date=curr_date
    )
    if os.path.exists(data_file) and not should_refresh:
        data = pd.read_csv(data_file, on_bad_lines="skip")
        return _clean_dataframe(data)

    from .vendor_errors import VendorUnavailableError, format_vendor_unavailable
    from .yfinance_limiter import get_yfinance_limiter

    limiter = get_yfinance_limiter()
    if limiter.is_open():
        raise VendorUnavailableError(
            format_vendor_unavailable("load_or_refresh_ohlcv_csv", "yfinance", "breaker open")
        )
    if not limiter.acquire(block=True):
        raise VendorUnavailableError(
            format_vendor_unavailable("load_or_refresh_ohlcv_csv", "yfinance", "rate limit")
        )
    try:
        data = yf.download(
            symbol,
            start=start_date,
            end=end_date,
            multi_level_index=False,
            progress=False,
            auto_adjust=True,
        )
        limiter.record_success()
    except VendorUnavailableError:
        raise
    except Exception as exc:
        if limiter.record_if_rate_limited(exc):
            raise VendorUnavailableError(
                format_vendor_unavailable("load_or_refresh_ohlcv_csv", "yfinance", "rate limit")
            ) from exc
        raise
    data = data.reset_index()
    data.to_csv(data_file, index=False)
    return _clean_dataframe(data)


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        # Get config and set up data directory path
        config = get_config()
        online = config["data_vendors"]["technical_indicators"] != "local"

        df = None
        data = None

        if not online:
            try:
                data = pd.read_csv(
                    os.path.join(
                        DATA_DIR,
                        f"{symbol}-YFin-data-2015-01-01-2025-03-25.csv",
                    ),
                    on_bad_lines="skip",
                )
                data = _clean_dataframe(data)
                df = wrap(data)
            except FileNotFoundError:
                raise Exception("Stockstats fail: Yahoo Finance data not fetched yet!")
        else:
            # Get today's date as YYYY-mm-dd to add to cache
            today_date = pd.Timestamp.today()
            curr_date = pd.to_datetime(curr_date)

            end_date = today_date
            start_date = today_date - pd.DateOffset(years=15)
            start_date = start_date.strftime("%Y-%m-%d")
            end_date = end_date.strftime("%Y-%m-%d")

            # Get config and ensure cache directory exists
            os.makedirs(config["data_cache_dir"], exist_ok=True)

            data_file = os.path.join(
                config["data_cache_dir"],
                f"{symbol}-YFin-data-{start_date}-{end_date}.csv",
            )
            try:
                data = load_or_refresh_ohlcv_csv(
                    symbol,
                    start_date,
                    end_date,
                    data_file,
                    today_date=today_date,
                    curr_date=curr_date,
                )
            except Exception as exc:
                from .vendor_errors import VendorUnavailableError, annotate_vendor_payload

                if isinstance(exc, VendorUnavailableError):
                    return annotate_vendor_payload(exc)
                raise

            df = wrap(data)
            df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
            curr_date = curr_date.strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
        matching_rows = df[df["Date"].str.startswith(curr_date)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"
