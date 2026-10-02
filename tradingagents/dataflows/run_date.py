"""Trade-date helpers for historical vs live analysis runs."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DAY_COUNT = re.compile(r"^\d{1,3}$")
logger = logging.getLogger("tradingagents.dataflows.run_date")


def parse_trade_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def is_historical_run(trade_date: str | date, today: str | date | None = None) -> bool:
    """True when trade_date is strictly before today in America/New_York."""
    try:
        td = parse_trade_date(trade_date)
    except (TypeError, ValueError):
        return False
    if today is None:
        ref = datetime.now(_ET).date()
    else:
        ref = parse_trade_date(today)
    return td < ref


def run_as_of_date(trade_date: str | date) -> str:
    return parse_trade_date(trade_date).isoformat()


def coerce_tool_date(value, *, anchor: str | date | None = None, as_lookback_from: str | None = None) -> str:
    """Normalize a tool date argument to YYYY-MM-DD.

    Models sometimes pass a lookback window (``\"30\"``) where a calendar date
    is required. A bare day-count is applied backward from ``as_lookback_from``
    when that anchor is a real date; otherwise the analysis/anchor date is used.
    """
    raw = str(value or "").strip()
    if _ISO_DATE.match(raw[:10]):
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass

    anchor_iso = ""
    if anchor is not None:
        try:
            anchor_iso = parse_trade_date(anchor).isoformat()
        except (TypeError, ValueError):
            anchor_iso = ""
    if not anchor_iso:
        try:
            from tradingagents.agents.utils.tool_context import get_current_trade_date

            anchor_iso = get_current_trade_date()
        except Exception:
            anchor_iso = ""
        if anchor_iso:
            try:
                anchor_iso = parse_trade_date(anchor_iso).isoformat()
            except (TypeError, ValueError):
                anchor_iso = ""

    if _DAY_COUNT.match(raw) and as_lookback_from:
        try:
            end = parse_trade_date(as_lookback_from)
            days = min(int(raw), 400)
            logger.warning("Coerced lookback %s days before %s into a start date", raw, end.isoformat())
            return (end - timedelta(days=days)).isoformat()
        except (TypeError, ValueError):
            pass

    if anchor_iso:
        if raw and raw != anchor_iso:
            logger.warning("Invalid tool date %r; using %s", value, anchor_iso)
        return anchor_iso
    return datetime.now(_ET).date().isoformat()
