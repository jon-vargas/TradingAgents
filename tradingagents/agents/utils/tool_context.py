"""Per-run context for tool calls (ticker recovery when the LLM swaps args)."""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

_current_ticker: ContextVar[str] = ContextVar("analysis_ticker", default="")
_current_trade_date: ContextVar[str] = ContextVar("analysis_trade_date", default="")


def set_current_ticker(ticker: str) -> Token:
    return _current_ticker.set(str(ticker or "").upper().strip())


def get_current_ticker() -> str:
    return str(_current_ticker.get() or "").upper().strip()


def set_current_trade_date(trade_date: str) -> Token:
    return _current_trade_date.set(str(trade_date or "")[:10])


def get_current_trade_date() -> str:
    return str(_current_trade_date.get() or "")[:10]


def reset_current_trade_date(token: Optional[Token] = None) -> None:
    if token is not None:
        _current_trade_date.reset(token)
    else:
        _current_trade_date.set("")


def reset_current_ticker(token: Optional[Token] = None) -> None:
    if token is not None:
        _current_ticker.reset(token)
    else:
        _current_ticker.set("")
