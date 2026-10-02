from typing import Annotated
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import logging
import yfinance as yf
import os
from .stockstats_utils import (
    StockstatsUtils,
    _assert_ohlcv_not_stale,
    _clean_dataframe,
    load_or_refresh_ohlcv_csv,
)
from .vendor_errors import format_vendor_unavailable
from .yfinance_limiter import get_yfinance_limiter

logger = logging.getLogger("tradingagents.dataflows.y_finance")

def get_YFin_data_online(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
):

    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    limiter = get_yfinance_limiter()
    if limiter.is_open():
        return format_vendor_unavailable("get_YFin_data_online", "yfinance", "breaker open")

    # Create ticker object
    ticker = yf.Ticker(symbol.upper())

    # yfinance end date is exclusive; add one day to keep the requested end date inclusive.
    inclusive_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    if not limiter.acquire(block=True):
        return format_vendor_unavailable("get_YFin_data_online", "yfinance", "rate limit")
    try:
        data = ticker.history(start=start_date, end=inclusive_end)
        limiter.record_success()
    except Exception as exc:
        if limiter.record_if_rate_limited(exc):
            return format_vendor_unavailable("get_YFin_data_online", "yfinance", "rate limit")
        raise

    # Check if data is empty
    if data.empty:
        return (
            f"No data found for symbol '{symbol}' between {start_date} and {end_date}"
        )

    # Remove timezone info from index for cleaner output
    if data.index.tz is not None:
        data.index = data.index.tz_localize(None)

    stale_note = _assert_ohlcv_not_stale(data, end_date)
    if stale_note:
        return (
            f"No data found for symbol '{symbol}' between {start_date} and {end_date} "
            f"({stale_note})"
        )

    # Round numerical values to 2 decimal places for cleaner display
    numeric_columns = ["Open", "High", "Low", "Close", "Adj Close"]
    for col in numeric_columns:
        if col in data.columns:
            data[col] = data[col].round(2)

    # Convert DataFrame to CSV string
    csv_string = data.to_csv()

    # Add header information
    header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + csv_string

def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:

    best_ind_params = {
        # Moving Averages
        "close_50_sma": (
            "50 SMA: A medium-term trend indicator. "
            "Usage: Identify trend direction and serve as dynamic support/resistance. "
            "Tips: It lags price; combine with faster indicators for timely signals."
        ),
        "close_200_sma": (
            "200 SMA: A long-term trend benchmark. "
            "Usage: Confirm overall market trend and identify golden/death cross setups. "
            "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
        ),
        "close_10_ema": (
            "10 EMA: A responsive short-term average. "
            "Usage: Capture quick shifts in momentum and potential entry points. "
            "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
        ),
        # MACD Related
        "macd": (
            "MACD: Computes momentum via differences of EMAs. "
            "Usage: Look for crossovers and divergence as signals of trend changes. "
            "Tips: Confirm with other indicators in low-volatility or sideways markets."
        ),
        "macds": (
            "MACD Signal: An EMA smoothing of the MACD line. "
            "Usage: Use crossovers with the MACD line to trigger trades. "
            "Tips: Should be part of a broader strategy to avoid false positives."
        ),
        "macdh": (
            "MACD Histogram: Shows the gap between the MACD line and its signal. "
            "Usage: Visualize momentum strength and spot divergence early. "
            "Tips: Can be volatile; complement with additional filters in fast-moving markets."
        ),
        # Momentum Indicators
        "rsi": (
            "RSI: Measures momentum to flag overbought/oversold conditions. "
            "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
            "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
        ),
        # Volatility Indicators
        "boll": (
            "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
            "Usage: Acts as a dynamic benchmark for price movement. "
            "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
        ),
        "boll_ub": (
            "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
            "Usage: Signals potential overbought conditions and breakout zones. "
            "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
        ),
        "boll_lb": (
            "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
            "Usage: Indicates potential oversold conditions. "
            "Tips: Use additional analysis to avoid false reversal signals."
        ),
        "atr": (
            "ATR: Averages true range to measure volatility. "
            "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
            "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
        ),
        # Volume-Based Indicators
        "vwma": (
            "VWMA: A moving average weighted by volume. "
            "Usage: Confirm trends by integrating price action with volume data. "
            "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
        ),
        "mfi": (
            "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
            "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
            "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
        ),
        # === Tier 2: Enhanced Technical Indicators ===
        "kdjk": (
            "Stochastic %K: Compares closing price to the range over a period. "
            "Usage: Identify overbought (>80) and oversold (<20) conditions with crossover signals. "
            "Tips: More sensitive than RSI; use %K/%D crossovers for entry/exit timing in ranging markets."
        ),
        "kdjd": (
            "Stochastic %D: Signal line for Stochastic %K (3-period SMA of %K). "
            "Usage: When %K crosses above %D in oversold territory, it's a buy signal (and vice versa). "
            "Tips: Confirms %K signals; slower but more reliable than %K alone."
        ),
        "dx": (
            "ADX (Average Directional Index): Measures trend strength regardless of direction. "
            "Usage: ADX > 25 = strong trend, ADX < 20 = weak/no trend. Does not indicate direction. "
            "Tips: Use with +DI/-DI for direction. High ADX means trend-following strategies work; low ADX favors mean-reversion."
        ),
        "wr": (
            "Williams %R: Momentum oscillator measuring overbought/oversold levels (0 to -100). "
            "Usage: Values above -20 = overbought, below -80 = oversold. Good for reversal detection. "
            "Tips: Similar to Stochastic but inverted scale. Fast signals; filter with trend indicators."
        ),
        "close_10_roc": (
            "Rate of Change (10-period): Percentage change in price over 10 periods. "
            "Usage: Positive ROC = upward momentum, negative = downward. Extreme values signal potential reversals. "
            "Tips: Simpler than RSI/MACD but effective for gauging speed of price movement and divergence."
        ),
    }

    if indicator not in best_ind_params:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(best_ind_params.keys())}"
        )

    end_date = curr_date
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    # Optimized: Get stock data once and calculate indicators for all dates
    from .vendor_errors import VendorUnavailableError, annotate_vendor_payload

    try:
        indicator_data = _get_stock_stats_bulk(symbol, indicator, curr_date)
    except VendorUnavailableError as exc:
        return annotate_vendor_payload(exc)

    try:
        
        # Generate the date range we need
        current_dt = curr_date_dt
        date_values = []
        
        while current_dt >= before:
            date_str = current_dt.strftime('%Y-%m-%d')
            
            # Look up the indicator value for this date
            if date_str in indicator_data:
                indicator_value = indicator_data[date_str]
            else:
                indicator_value = "N/A: Not a trading day (weekend or holiday)"
            
            date_values.append((date_str, indicator_value))
            current_dt = current_dt - relativedelta(days=1)
        
        # Build the result string
        ind_string = ""
        for date_str, value in date_values:
            ind_string += f"{date_str}: {value}\n"
        
    except Exception as e:
        logger.error(f"Error getting bulk stockstats data: {e}")
        # Fallback to original implementation if bulk method fails
        ind_string = ""
        curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        while curr_date_dt >= before:
            indicator_value = get_stockstats_indicator(
                symbol, indicator, curr_date_dt.strftime("%Y-%m-%d")
            )
            ind_string += f"{curr_date_dt.strftime('%Y-%m-%d')}: {indicator_value}\n"
            curr_date_dt = curr_date_dt - relativedelta(days=1)

    result_str = (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {end_date}:\n\n"
        + ind_string
        + "\n\n"
        + best_ind_params.get(indicator, "No description available.")
    )

    return result_str


def _get_stock_stats_bulk(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to calculate"],
    curr_date: Annotated[str, "current date for reference"]
) -> dict:
    """
    Optimized bulk calculation of stock stats indicators.
    Fetches data once and calculates indicator for all available dates.
    Returns dict mapping date strings to indicator values.
    """
    from .config import get_config
    import pandas as pd
    from stockstats import wrap
    import os
    
    config = get_config()
    online = config["data_vendors"]["technical_indicators"] != "local"
    
    if not online:
        # Local data path
        try:
            data = pd.read_csv(
                os.path.join(
                    config.get("data_cache_dir", "data"),
                    f"{symbol}-YFin-data-2015-01-01-2025-03-25.csv",
                ),
                on_bad_lines="skip",
            )
            data = _clean_dataframe(data)
            df = wrap(data)
        except FileNotFoundError:
            raise Exception("Stockstats fail: Yahoo Finance data not fetched yet!")
    else:
        # Online data fetching with caching
        today_date = pd.Timestamp.today()
        curr_date_dt = pd.to_datetime(curr_date)
        
        end_date = today_date
        start_date = today_date - pd.DateOffset(years=15)
        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = end_date.strftime("%Y-%m-%d")
        
        os.makedirs(config["data_cache_dir"], exist_ok=True)
        
        data_file = os.path.join(
            config["data_cache_dir"],
            f"{symbol}-YFin-data-{start_date_str}-{end_date_str}.csv",
        )
        try:
            data = load_or_refresh_ohlcv_csv(
                symbol,
                start_date_str,
                end_date_str,
                data_file,
                today_date=today_date,
                curr_date=curr_date_dt,
            )
        except Exception as exc:
            from .vendor_errors import VendorUnavailableError

            if isinstance(exc, VendorUnavailableError):
                raise
            raise

        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    
    # Calculate the indicator for all rows at once
    df[indicator]  # This triggers stockstats to calculate the indicator
    
    # Create a dictionary mapping date strings to indicator values
    result_dict = {}
    for _, row in df.iterrows():
        date_str = row["Date"]
        indicator_value = row[indicator]
        
        # Handle NaN/None values
        if pd.isna(indicator_value):
            result_dict[date_str] = "N/A"
        else:
            result_dict[date_str] = str(indicator_value)
    
    return result_dict


def get_stockstats_indicator(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
) -> str:

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    curr_date = curr_date_dt.strftime("%Y-%m-%d")

    try:
        indicator_value = StockstatsUtils.get_stock_stats(
            symbol,
            indicator,
            curr_date,
        )
    except Exception as e:
        from .vendor_errors import VendorUnavailableError, annotate_vendor_payload

        if isinstance(e, VendorUnavailableError):
            return annotate_vendor_payload(e)
        logger.error(
            f"Error getting stockstats indicator data for indicator {indicator} on {curr_date}: {e}"
        )
        return ""

    return str(indicator_value)


def get_yfinance_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "current date (not used for yfinance)"] = None,
):
    """Get comprehensive fundamental data from yfinance (1-day DataCache TTL).

    Aggregates key valuation, profitability, and balance sheet metrics from
    yf.Ticker.info into a formatted string suitable for LLM consumption.
    """
    from .cache import get_cache
    cache = get_cache()
    cache_key_args = (ticker.upper(),)
    cached = cache.get("fundamentals", *cache_key_args)
    if cached is not None:
        return cached

    try:
        ticker_obj = yf.Ticker(ticker.upper())
        info = ticker_obj.info

        if not info or info.get("trailingPE") is None and info.get("marketCap") is None:
            return f"No fundamental data found for symbol '{ticker}'"

        def _fmt(val, fmt="{:,.2f}", fallback="N/A"):
            if val is None:
                return fallback
            try:
                return fmt.format(val)
            except (TypeError, ValueError):
                return str(val)

        def _fmt_pct(val, fallback="N/A"):
            if val is None:
                return fallback
            try:
                return f"{val * 100:.2f}%"
            except (TypeError, ValueError):
                return str(val)

        lines = [
            f"## Fundamental Data for {ticker.upper()}",
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "### Valuation",
            f"Market Cap: {_fmt(info.get('marketCap'), '${:,.0f}')}",
            f"Enterprise Value: {_fmt(info.get('enterpriseValue'), '${:,.0f}')}",
            f"Trailing P/E: {_fmt(info.get('trailingPE'))}",
            f"Forward P/E: {_fmt(info.get('forwardPE'))}",
            f"P/B Ratio: {_fmt(info.get('priceToBook'))}",
            f"PEG Ratio: {_fmt(info.get('pegRatio'))}",
            f"EV/EBITDA: {_fmt(info.get('enterpriseToEbitda'))}",
            f"EV/Revenue: {_fmt(info.get('enterpriseToRevenue'))}",
            "",
            "### Per Share",
            f"EPS (Trailing): {_fmt(info.get('trailingEps'), '${:.2f}')}",
            f"EPS (Forward): {_fmt(info.get('forwardEps'), '${:.2f}')}",
            f"Dividend Yield: {_fmt_pct(info.get('dividendYield'))}",
            f"Book Value Per Share: {_fmt(info.get('bookValue'), '${:.2f}')}",
            "",
            "### Price & Volatility",
            f"Beta: {_fmt(info.get('beta'))}",
            f"52-Week High: {_fmt(info.get('fiftyTwoWeekHigh'), '${:.2f}')}",
            f"52-Week Low: {_fmt(info.get('fiftyTwoWeekLow'), '${:.2f}')}",
            f"50-Day MA: {_fmt(info.get('fiftyDayAverage'), '${:.2f}')}",
            f"200-Day MA: {_fmt(info.get('twoHundredDayAverage'), '${:.2f}')}",
            "",
            "### Income Statement",
            f"Total Revenue (TTM): {_fmt(info.get('totalRevenue'), '${:,.0f}')}",
            f"Gross Profit (TTM): {_fmt(info.get('grossProfits'), '${:,.0f}')}",
            f"EBITDA: {_fmt(info.get('ebitda'), '${:,.0f}')}",
            f"Net Income (TTM): {_fmt(info.get('netIncomeToCommon'), '${:,.0f}')}",
            f"Gross Margin: {_fmt_pct(info.get('grossMargins'))}",
            f"Operating Margin: {_fmt_pct(info.get('operatingMargins'))}",
            f"Profit Margin: {_fmt_pct(info.get('profitMargins'))}",
            f"Revenue Growth (YoY): {_fmt_pct(info.get('revenueGrowth'))}",
            f"Earnings Growth (YoY): {_fmt_pct(info.get('earningsGrowth'))}",
            "",
            "### Efficiency & Returns",
            f"Return on Equity: {_fmt_pct(info.get('returnOnEquity'))}",
            f"Return on Assets: {_fmt_pct(info.get('returnOnAssets'))}",
            "",
            "### Balance Sheet",
            f"Total Cash: {_fmt(info.get('totalCash'), '${:,.0f}')}",
            f"Total Debt: {_fmt(info.get('totalDebt'), '${:,.0f}')}",
            f"Debt/Equity: {_fmt(info.get('debtToEquity'))}",
            f"Current Ratio: {_fmt(info.get('currentRatio'))}",
            f"Quick Ratio: {_fmt(info.get('quickRatio'))}",
            "",
            "### Cash Flow",
            f"Free Cash Flow: {_fmt(info.get('freeCashflow'), '${:,.0f}')}",
            f"Operating Cash Flow: {_fmt(info.get('operatingCashflow'), '${:,.0f}')}",
        ]

        result = "\n".join(lines)
        cache.set("fundamentals", *cache_key_args, data=result, ttl=86400)
        return result

    except Exception as e:
        return f"Error retrieving fundamental data for {ticker}: {str(e)}"


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date (not used for yfinance)"] = None
):
    """Get balance sheet data from yfinance (7-day DataCache TTL)."""
    from .cache import get_cache
    cache = get_cache()
    cache_key_args = (ticker.upper(), freq.lower())
    cached = cache.get("balance_sheet", *cache_key_args)
    if cached is not None:
        return cached

    try:
        ticker_obj = yf.Ticker(ticker.upper())
        
        if freq.lower() == "quarterly":
            data = ticker_obj.quarterly_balance_sheet
        else:
            data = ticker_obj.balance_sheet
            
        if data.empty:
            return f"No balance sheet data found for symbol '{ticker}'"
            
        csv_string = data.to_csv()
        header = f"# Balance Sheet data for {ticker.upper()} ({freq})\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        result = header + csv_string
        cache.set("balance_sheet", *cache_key_args, data=result)
        return result
        
    except Exception as e:
        return f"Error retrieving balance sheet for {ticker}: {str(e)}"


def get_cashflow(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date (not used for yfinance)"] = None
):
    """Get cash flow data from yfinance (7-day DataCache TTL)."""
    from .cache import get_cache
    cache = get_cache()
    cache_key_args = (ticker.upper(), freq.lower())
    cached = cache.get("cashflow", *cache_key_args)
    if cached is not None:
        return cached

    try:
        ticker_obj = yf.Ticker(ticker.upper())
        
        if freq.lower() == "quarterly":
            data = ticker_obj.quarterly_cashflow
        else:
            data = ticker_obj.cashflow
            
        if data.empty:
            return f"No cash flow data found for symbol '{ticker}'"
            
        csv_string = data.to_csv()
        header = f"# Cash Flow data for {ticker.upper()} ({freq})\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        result = header + csv_string
        cache.set("cashflow", *cache_key_args, data=result)
        return result
        
    except Exception as e:
        return f"Error retrieving cash flow for {ticker}: {str(e)}"


def get_income_statement(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date (not used for yfinance)"] = None
):
    """Get income statement data from yfinance (7-day DataCache TTL)."""
    from .cache import get_cache
    cache = get_cache()
    cache_key_args = (ticker.upper(), freq.lower())
    cached = cache.get("income_statement", *cache_key_args)
    if cached is not None:
        return cached

    try:
        ticker_obj = yf.Ticker(ticker.upper())
        
        if freq.lower() == "quarterly":
            data = ticker_obj.quarterly_income_stmt
        else:
            data = ticker_obj.income_stmt
            
        if data.empty:
            return f"No income statement data found for symbol '{ticker}'"
            
        csv_string = data.to_csv()
        header = f"# Income Statement data for {ticker.upper()} ({freq})\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        result = header + csv_string
        cache.set("income_statement", *cache_key_args, data=result)
        return result
        
    except Exception as e:
        return f"Error retrieving income statement for {ticker}: {str(e)}"


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol of the company"],
    curr_date: str = None,
):
    """Get insider transactions data from yfinance with optional as-of-date filtering."""
    from .cache import get_cache
    import pandas as pd

    cache = get_cache()
    as_of = curr_date or "latest"
    cached = cache.get("ownership", "insider_tx", ticker.upper(), as_of)
    if cached is not None:
        return cached

    try:
        ticker_obj = yf.Ticker(ticker.upper())
        data = ticker_obj.insider_transactions
        
        if data is None or data.empty:
            return f"No insider transactions data found for symbol '{ticker}'"
            
        if curr_date:
            as_of_dt = pd.to_datetime(curr_date, errors="coerce")
            if pd.notna(as_of_dt):
                date_col = None
                for candidate in ("Start Date", "Date", "Transaction Date"):
                    if candidate in data.columns:
                        date_col = candidate
                        break
                if date_col:
                    filtered = data.copy()
                    filtered["_tx_date"] = pd.to_datetime(filtered[date_col], errors="coerce")
                    filtered = filtered[filtered["_tx_date"] <= as_of_dt]
                    filtered = filtered.drop(columns=["_tx_date"])
                    if not filtered.empty:
                        data = filtered

        csv_string = data.to_csv()
        header = f"# Insider Transactions data for {ticker.upper()}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        result = header + csv_string
        cache.set("ownership", "insider_tx", ticker.upper(), as_of, data=result)
        return result
        
    except Exception as e:
        return f"Error retrieving insider transactions for {ticker}: {str(e)}"


def get_insider_sentiment(
    ticker: Annotated[str, "ticker symbol of the company"],
    curr_date: str = None,
):
    """Derive insider sentiment from yfinance insider transactions (24-hour DataCache TTL).

    Aggregates recent insider buys vs sells over the last 90 days to produce a
    net-sentiment summary analogous to Finnhub's insider sentiment endpoint.
    """
    from .cache import get_cache
    cache = get_cache()
    as_of = curr_date or "latest"
    cached = cache.get("ownership", "insider_sentiment", ticker.upper(), as_of)
    if cached is not None:
        return cached

    try:
        import pandas as pd
        ticker_obj = yf.Ticker(ticker.upper())
        txns = ticker_obj.insider_transactions

        if txns is None or txns.empty:
            return f"No insider sentiment data available for {ticker.upper()} (no recent insider transactions)"

        df = txns.copy()

        if "Start Date" in df.columns:
            df["_date"] = pd.to_datetime(df["Start Date"], errors="coerce")
        elif "Date" in df.columns:
            df["_date"] = pd.to_datetime(df["Date"], errors="coerce")
        else:
            return f"No insider sentiment data available for {ticker.upper()} (date column missing)"

        df = df.dropna(subset=["_date"])
        as_of_ts = pd.to_datetime(curr_date, errors="coerce") if curr_date else pd.Timestamp.now()
        if pd.isna(as_of_ts):
            as_of_ts = pd.Timestamp.now()
        cutoff = as_of_ts - pd.Timedelta(days=90)
        df = df[df["_date"] <= as_of_ts]
        df = df[df["_date"] >= cutoff]

        if df.empty:
            return f"No insider transactions in the last 90 days for {ticker.upper()}"

        text_col = next((c for c in df.columns if c.lower() in ("text", "transaction")), None)
        shares_col = next((c for c in df.columns if "shares" in c.lower()), None)
        value_col = next((c for c in df.columns if "value" in c.lower()), None)

        buy_count = sell_count = 0
        buy_shares = sell_shares = 0
        buy_value = sell_value = 0.0

        for _, row in df.iterrows():
            label = str(row.get(text_col, "")).lower() if text_col else ""
            is_buy = any(kw in label for kw in ("purchase", "buy", "acquisition"))
            is_sell = any(kw in label for kw in ("sale", "sell", "disposition"))

            shares = abs(float(row[shares_col])) if shares_col and pd.notna(row.get(shares_col)) else 0
            value = abs(float(row[value_col])) if value_col and pd.notna(row.get(value_col)) else 0.0

            if is_buy:
                buy_count += 1
                buy_shares += shares
                buy_value += value
            elif is_sell:
                sell_count += 1
                sell_shares += shares
                sell_value += value

        total_txns = buy_count + sell_count
        if total_txns == 0:
            mspr = 0.0
        else:
            mspr = (buy_count - sell_count) / total_txns

        net_change = buy_shares - sell_shares

        if mspr > 0.3:
            sentiment_label = "Bullish"
        elif mspr < -0.3:
            sentiment_label = "Bearish"
        else:
            sentiment_label = "Neutral"

        lines = [
            f"## {ticker.upper()} Insider Sentiment (last 90 days)",
            f"Overall sentiment: **{sentiment_label}** (MSPR: {mspr:+.2f})",
            "",
            f"- Buy transactions: {buy_count} ({buy_shares:,.0f} shares, ${buy_value:,.0f})",
            f"- Sell transactions: {sell_count} ({sell_shares:,.0f} shares, ${sell_value:,.0f})",
            f"- Net share change: {net_change:+,.0f}",
            "",
            "MSPR (Monthly Share Purchase Ratio) ranges from -1 (all sells) to +1 (all buys).",
        ]
        result = "\n".join(lines)
        cache.set("ownership", "insider_sentiment", ticker.upper(), as_of, data=result)
        return result

    except Exception as e:
        return f"Error deriving insider sentiment for {ticker}: {str(e)}"