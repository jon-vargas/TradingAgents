"""Deterministic market-data snapshot to ground numeric claims."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import datetime, timedelta

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.y_finance import get_YFin_data_online
from tradingagents.dataflows.vendor_errors import (
    annotate_vendor_payload,
    is_vendor_unavailable_payload,
)

DEFAULT_SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema",
    "close_50_sma",
    "close_200_sma",
    "rsi",
    "boll",
    "boll_ub",
    "boll_lb",
    "macd",
    "macds",
    "macdh",
    "atr",
)


def _parse_ohlcv_csv(csv_text: str) -> pd.DataFrame:
    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return pd.DataFrame()
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows = []
    for row in reader:
        date_val = row.get("Date") or row.get("") or ""
        if not str(date_val).strip():
            continue
        if "Date" not in row and "" in row:
            row["Date"] = row.pop("")
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Close"])


def _verified_rows(symbol: str, curr_date: str) -> pd.DataFrame:
    start = (datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=400)).strftime(
        "%Y-%m-%d"
    )
    payload = get_YFin_data_online(symbol, start, curr_date)
    if is_vendor_unavailable_payload(payload):
        raise ValueError(annotate_vendor_payload(payload))
    if isinstance(payload, str) and payload.startswith("No data found"):
        raise ValueError(payload)
    df = _parse_ohlcv_csv(payload)
    if df.empty:
        raise ValueError(f"No OHLCV data available for {symbol}.")
    df = df[df["Date"] <= pd.to_datetime(curr_date)].sort_values("Date")
    if df.empty:
        raise ValueError(f"No OHLCV rows on or before {curr_date} for {symbol}.")
    return df


def _fmt(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def build_verified_market_snapshot(
    symbol: str,
    curr_date: str,
    look_back_days: int = 30,
    indicators: Iterable[str] | None = None,
) -> str:
    """Render a ground-truth snapshot: latest OHLCV row, indicators, recent closes."""
    try:
        df = _verified_rows(symbol, curr_date)
    except Exception as exc:
        return (
            f"## Verified market data snapshot for {symbol.upper()}\n\n"
            f"Unavailable: {exc}"
        )

    stock_df = wrap(df.copy())
    selected = tuple(indicators or DEFAULT_SNAPSHOT_INDICATORS)
    indicator_values: dict[str, str] = {}
    for name in selected:
        try:
            stock_df[name]
            indicator_values[name] = _fmt(stock_df.iloc[-1][name])
        except Exception as exc:
            indicator_values[name] = f"N/A ({type(exc).__name__})"

    latest = df.iloc[-1]
    latest_date = _fmt(latest["Date"])
    window = max(1, min(int(look_back_days), 30))
    recent = df.tail(window)

    lines = [
        f"## Verified market data snapshot for {symbol.upper()}",
        "",
        f"- Requested analysis date: {curr_date}",
        f"- Latest trading row used: {latest_date}",
        "- Rows after the requested analysis date are excluded before verification.",
        "",
        "### Latest verified OHLCV row",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in ("Open", "High", "Low", "Close", "Volume"):
        lines.append(f"| {field} | {_fmt(latest.get(field))} |")

    lines += [
        "",
        "### Verified technical indicators (latest row)",
        "",
        "| Indicator | Value |",
        "|---|---:|",
    ]
    for name, value in indicator_values.items():
        lines.append(f"| {name} | {value} |")

    lines += [
        "",
        f"### Recent verified closes (last {len(recent)} rows)",
        "",
        "| Date | Close |",
        "|---|---:|",
    ]
    for _, row in recent.iterrows():
        lines.append(f"| {_fmt(row['Date'])} | {_fmt(row.get('Close'))} |")

    lines += [
        "",
        "Use this snapshot as the source of truth for exact OHLCV, price-level, "
        "and indicator-value claims. If another tool output conflicts with it, "
        "flag the discrepancy rather than inventing a reconciled number. Do not "
        "claim historical validation, support/resistance bounces, or exact "
        "percentage moves unless directly supported by tool output with concrete "
        "dates and prices.",
    ]
    return "\n".join(lines)
