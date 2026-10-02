"""Deterministic ticker identity so agents do not invent a different company."""

from __future__ import annotations

from typing import Any, Mapping

import logging

logger = logging.getLogger("tradingagents.dataflows.instrument_identity")

# Physical-commodity ETFs where equity beta / earnings transcripts are not applicable.
COMMODITY_ETF_TICKERS = frozenset(
    {
        "GLD",
        "SLV",
        "IAU",
        "SIVR",
        "PPLT",
        "GLDM",
        "SGOL",
        "PSLV",
        "BAR",
        "OUNZ",
    }
)

_COMMODITY_NAME_HINTS = (
    "gold",
    "silver",
    "platinum",
    "palladium",
    "precious metal",
    "commodity",
)


def _clean_identity_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"none", "n/a", "nan", "null"}:
        return None
    return cleaned


def resolve_instrument_identity(ticker: str) -> dict:
    """Resolve company name / sector / exchange for a ticker.

    Fail-open: returns ``{}`` if Yahoo is unavailable or the ticker is unknown.
    Uses the shared yfinance ticker-info cache so analysis does not add a
    second ``.info`` call per symbol.
    """
    symbol = (ticker or "").upper().strip()
    if not symbol:
        return {}
    try:
        from tradingagents.dataflows.yfinance_extended import get_ticker_info

        info = get_ticker_info(symbol) or {}
    except Exception as exc:
        logger.debug("Could not resolve instrument identity for %s: %s", symbol, exc)
        return {}

    identity: dict[str, str] = {}
    company_name = _clean_identity_value(info.get("longName")) or _clean_identity_value(
        info.get("shortName")
    )
    if company_name:
        identity["company_name"] = company_name
    for source_key, target_key in (
        ("sector", "sector"),
        ("industry", "industry"),
        ("exchange", "exchange"),
        ("quoteType", "quote_type"),
    ):
        value = _clean_identity_value(info.get(source_key))
        if value:
            identity[target_key] = value
    return identity


def is_commodity_etf(ticker: str, identity: Mapping[str, str] | None = None) -> bool:
    """True for physical-commodity ETFs (GLD/SLV-style), not generic sector ETFs."""
    symbol = (ticker or "").upper().strip()
    if not symbol:
        return False
    if symbol in COMMODITY_ETF_TICKERS:
        return True
    if not identity:
        return False
    quote_type = str(identity.get("quote_type") or "").upper()
    if quote_type != "ETF":
        return False
    name_blob = " ".join(
        str(identity.get(key) or "")
        for key in ("company_name", "sector", "industry")
    ).lower()
    return any(hint in name_blob for hint in _COMMODITY_NAME_HINTS)


def build_instrument_context(ticker: str, identity: Mapping[str, str] | None = None) -> str:
    """Describe the exact instrument so agents preserve ticker and company."""
    symbol = (ticker or "").strip()
    context = (
        f"The instrument to analyze is `{symbol}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`, `-USD`)."
    )
    details = []
    if identity:
        name = identity.get("company_name") or identity.get("name")
        if name:
            details.append(f"Company: {name}")
        sector, industry = identity.get("sector"), identity.get("industry")
        if sector and industry:
            details.append(f"Business classification: {sector} / {industry}")
        elif sector:
            details.append(f"Sector: {sector}")
        elif industry:
            details.append(f"Industry: {industry}")
        if identity.get("exchange"):
            details.append(f"Exchange: {identity['exchange']}")
    if details:
        context += (
            f" Resolved identity: {'; '.join(details)}. "
            "Do not substitute a different company or ticker unless a tool "
            "result explicitly disproves this resolved identity."
        )
    return context


def resolve_instrument_context(ticker: str) -> str:
    """Resolve identity once and render the prompt context string."""
    return build_instrument_context(ticker, resolve_instrument_identity(ticker))


def identity_from_metadata_as_of(row: Mapping[str, Any] | None, trade_date: str) -> dict:
    """Use stored ticker metadata only when it was written on or before the trade date.

    ``ticker_metadata`` has sector and industry, not a company name. A row updated
    after the trade date is omitted so a historical run does not borrow later facts.
    """
    if not isinstance(row, Mapping):
        return {}
    stamp = str(row.get("last_updated") or "")[:10]
    as_of = str(trade_date or "")[:10]
    if not stamp or not as_of or stamp > as_of:
        return {}
    identity: dict[str, str] = {}
    for source_key, target_key in (("sector", "sector"), ("industry", "industry")):
        value = _clean_identity_value(row.get(source_key))
        if value:
            identity[target_key] = value
    return identity


def _load_ticker_metadata_row(ticker: str) -> dict | None:
    try:
        from tradingagents.reporting.database import get_db

        row = get_db().get_ticker_metadata(ticker)
    except Exception as exc:
        logger.debug("Stored ticker metadata skipped for %s: %s", ticker, exc)
        return None
    return row if isinstance(row, dict) else None


def resolve_instrument_context_for_run(
    ticker: str, trade_date: str
) -> tuple[dict, str]:
    """Resolve identity + prompt context, skipping live lookup on historical runs."""
    from tradingagents.dataflows.run_date import is_historical_run

    if is_historical_run(trade_date):
        identity = identity_from_metadata_as_of(_load_ticker_metadata_row(ticker), trade_date)
        context = build_instrument_context(ticker, identity or None)
        context += (
            " Historical run: the company name was not resolved live for this past "
            "trade date; treat the ticker as authoritative unless tool output proves otherwise."
        )
        if identity:
            context += (
                " Sector and industry, when present, come from stored ticker metadata "
                f"dated on or before {str(trade_date)[:10]}."
            )
        return identity, context
    identity = resolve_instrument_identity(ticker)
    return identity, build_instrument_context(ticker, identity)


def get_instrument_context_from_state(state: Mapping[str, Any]) -> str:
    """Prefer the run-start context stored on state; never look up mid-graph."""
    context = state.get("instrument_context")
    if isinstance(context, str) and context.strip():
        return context
    ticker = str(state.get("company_of_interest") or "")
    return build_instrument_context(ticker)
