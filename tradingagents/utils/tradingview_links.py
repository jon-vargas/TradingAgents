"""
TradingView symbol/link helpers.

Centralizes exchange normalization and file/export formatting for TradingView
watchlist imports and symbol deep links.
"""

from __future__ import annotations

from datetime import datetime
import re
import socket
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import quote
from urllib.request import Request, urlopen


# Common exchange variants observed in yfinance / vendor metadata.
_EXCHANGE_NORMALIZATION: Dict[str, str] = {
    # NASDAQ variants
    "NASDAQ": "NASDAQ",
    "NMS": "NASDAQ",
    "NAS": "NASDAQ",
    "NASD": "NASDAQ",
    "NASDAQGS": "NASDAQ",
    "NASDAQGM": "NASDAQ",
    "NASDAQCM": "NASDAQ",
    "NASDAQ GS": "NASDAQ",
    "NASDAQ GM": "NASDAQ",
    "NASDAQ CM": "NASDAQ",
    # NYSE variants
    "NYSE": "NYSE",
    "NYQ": "NYSE",
    "NYS": "NYSE",
    # AMEX / NYSE Arca variants
    "AMEX": "AMEX",
    "ASE": "AMEX",
    "ARCA": "AMEX",
    "NYSEARCA": "AMEX",
    "NYSE ARCA": "AMEX",
    # BATS / Cboe variants
    "BATS": "BATS",
    "BZX": "BATS",
    "CBOE": "BATS",
    "CBOE BZX": "BATS",
    # OTC variants
    "OTC": "OTC",
    "OTCMKTS": "OTC",
    "OTCQB": "OTC",
    "OTCQX": "OTC",
    "PINK": "OTC",
    "PNK": "OTC",
    # Non-US examples
    "TSX": "TSX",
    "TOR": "TSX",
    "LSE": "LSE",
    "LON": "LSE",
    "ASX": "ASX",
}

_TV_EXCHANGE_ALLOWLIST: frozenset[str] = frozenset(
    {
        # US venues
        "NASDAQ",
        "NYSE",
        "AMEX",
        "BATS",
        # OTC
        "OTC",
        # Non-US
        "TSX",
        "TSE",
        "LSE",
        "ASX",
    }
)

_TV_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")

_VENUE_RANK_PREFERRED: Dict[str, int] = {
    # Higher preference => smaller number
    "NASDAQ": 1,
    "NYSE": 2,
    "AMEX": 3,
    "BATS": 4,
    "TSX": 5,
    "TSE": 6,
    "LSE": 7,
    "ASX": 8,
    "OTC": 9,
}


def normalize_exchange_loose(raw_exchange: Optional[str], default_exchange: str = "NASDAQ") -> str:
    """
    Loose normalization:
    - maps known vendor variants (e.g. NYQ -> NYSE)
    - for unknown non-empty values, returns the normalized string (backward-compatible)
    - for empty/missing values, returns default_exchange
    """
    if not raw_exchange:
        return default_exchange
    normalized_key = raw_exchange.strip().upper().replace("-", " ").replace("_", " ")
    normalized_key = " ".join(normalized_key.split())
    return _EXCHANGE_NORMALIZATION.get(normalized_key, normalized_key or default_exchange)


def normalize_exchange_strict(raw_exchange: Optional[str], default_exchange: str = "NASDAQ") -> str:
    """
    Strict normalization:
    - returns "" if exchange cannot be mapped to a supported TradingView exchange id
    - never applies a default exchange for missing/unresolved values
    """
    if not raw_exchange:
        return ""
    normalized = normalize_exchange_loose(raw_exchange, default_exchange=default_exchange)
    return normalized if normalized in _TV_EXCHANGE_ALLOWLIST else ""


def normalize_exchange(raw_exchange: Optional[str], default_exchange: str = "NASDAQ") -> str:
    """
    Backward compatible alias for existing code.
    Prefer `normalize_exchange_strict(...)` in strict export paths.
    """
    return normalize_exchange_loose(raw_exchange, default_exchange=default_exchange)


def exchange_from_info(
    info: Optional[Mapping[str, Any]],
    default_exchange: str = "NASDAQ",
) -> str:
    """
    Extract and normalize exchange from yfinance-style ``info`` dict.
    """
    if not info:
        return default_exchange
    raw = (
        info.get("exchangeShortName")
        or info.get("exchange")
        or info.get("fullExchangeName")
    )
    return normalize_exchange_loose(raw, default_exchange=default_exchange)


def _disambiguate_tse_vs_tsx(info: Mapping[str, Any]) -> str:
    """Best-effort disambiguation for ambiguous 'TSE' values in vendor metadata."""
    country = str(info.get("country") or "").lower()
    currency = str(info.get("currency") or "").upper()
    tz = str(info.get("exchangeTimezoneName") or "").lower()

    is_japan = "japan" in country or currency == "JPY" or "tokyo" in tz
    is_canada = "canada" in country or currency == "CAD"
    if is_japan and not is_canada:
        return "TSE"
    # Default to TSX for CAD/Canada-like metadata; otherwise prefer TSE with lower confidence.
    return "TSX" if is_canada else "TSE"


def is_valid_tradingview_symbol_token(token: str) -> bool:
    """Offline validator for TradingView import tokens (EXCHANGE:TICKER)."""
    if not token or ":" not in token:
        return False
    ex, sym = token.split(":", 1)
    ex = (ex or "").strip().upper()
    sym = (sym or "").strip().upper()
    if ex not in _TV_EXCHANGE_ALLOWLIST:
        return False
    if not _TV_TICKER_RE.match(sym):
        return False
    return True


def resolve_exchange_from_info(
    ticker: str,
    info: Optional[Mapping[str, Any]],
    *,
    default_exchange: str = "NASDAQ",
) -> Dict[str, Any]:
    """
    Resolve a primary TradingView exchange from yfinance-style `.info`.

    Returns:
      - exchange: resolved TradingView exchange id (or "" if unresolved)
      - source: which field/heuristic was used
      - confidence: numeric confidence in [0,1]
      - raw_exchange: raw vendor exchange string
      - candidate_exchanges: ordered list of exchange candidates
    """
    ticker = (ticker or "").upper().strip()
    if not ticker or not info:
        return {
            "exchange": "",
            "source": "missing_info",
            "confidence": 0.0,
            "raw_exchange": None,
            "candidate_exchanges": [],
        }

    # Prefer short/exchange fields first, then fullExchangeName.
    raw_candidates: List[Tuple[str, str]] = [
        ("exchangeShortName", str(info.get("exchangeShortName") or "").strip()),
        ("exchange", str(info.get("exchange") or "").strip()),
        ("fullExchangeName", str(info.get("fullExchangeName") or "").strip()),
    ]

    country = str(info.get("country") or "").lower()
    currency = str(info.get("currency") or "").upper()

    info_ts = _extract_info_timestamp(info)

    # Heuristic candidate exchanges (offline).
    suffix_candidates: List[str] = []
    if ticker.endswith(".TO"):
        suffix_candidates.append("TSX")
    if ticker.endswith(".L"):
        suffix_candidates.append("LSE")
    if ticker.endswith(".AX"):
        suffix_candidates.append("ASX")

    currency_candidates: List[str] = []
    if currency == "JPY" or "japan" in country:
        currency_candidates.append("TSE")
    if currency == "CAD" or "canada" in country:
        currency_candidates.append("TSX")
    if "united kingdom" in country or currency == "GBP":
        currency_candidates.append("LSE")
    if "australia" in country or currency == "AUD":
        currency_candidates.append("ASX")

    candidate_set: List[str] = []
    for ex in suffix_candidates + currency_candidates:
        if ex in _TV_EXCHANGE_ALLOWLIST and ex not in candidate_set:
            candidate_set.append(ex)

    best_exchange = ""
    best_conf = 0.0
    best_source = "unresolved"
    best_ts = info_ts or 0.0
    best_raw: Optional[str] = None

    # Resolve candidates deterministically.
    for field, raw in raw_candidates:
        if not raw:
            continue
        if field == "fullExchangeName":
            confidence = 0.40
        elif field == "exchange":
            confidence = 0.70
        else:
            confidence = 0.90

        if raw.strip().upper().replace(" ", "") == "TSE":
            resolved = _disambiguate_tse_vs_tsx(info)
        else:
            resolved = normalize_exchange_strict(raw, default_exchange=default_exchange)

        if resolved:
            if resolved not in candidate_set:
                candidate_set.append(resolved)
            # tie-breakers prefer confidence first; if equal, prefer freshest info timestamp then venue rank then lexical
            if confidence > best_conf:
                best_conf = confidence
                best_exchange = resolved
                best_source = field
                best_raw = raw
                best_ts = info_ts or 0.0
            elif confidence == best_conf and resolved:
                # Freshest info timestamp wins first.
                candidate_ts = info_ts or 0.0
                if candidate_ts > best_ts:
                    best_exchange = resolved
                    best_source = field
                    best_raw = raw
                    best_ts = candidate_ts
                # lower rank wins next
                elif _VENUE_RANK_PREFERRED.get(resolved, 999) < _VENUE_RANK_PREFERRED.get(best_exchange, 999):
                    best_exchange = resolved
                    best_source = field
                    best_raw = raw

    # Deterministic tie-break list: sort by best selection first, then freshest timestamp, then venue rank, then lexical.
    def _candidate_sort_key(ex: str) -> Tuple[int, float, int, str]:
        venue_rank = _VENUE_RANK_PREFERRED.get(ex, 999)
        is_best = 0 if ex == best_exchange else 1
        # Higher timestamps should appear first, so include in descending order via negative.
        ts = info_ts or 0.0
        return (is_best, -ts, venue_rank, ex)

    candidate_exchanges = sorted(set(candidate_set + ([best_exchange] if best_exchange else [])), key=_candidate_sort_key)

    return {
        "exchange": best_exchange,
        "source": best_source if best_exchange else ("unresolved" if candidate_exchanges else "no_candidates"),
        "confidence": float(best_conf) if best_exchange else 0.0,
        "raw_exchange": best_raw,
        "source_timestamp": datetime.fromtimestamp(info_ts).isoformat() if info_ts else None,
        "candidate_exchanges": candidate_exchanges,
    }


def tradingview_symbol_token(
    ticker: str,
    exchange: Optional[str] = None,
    default_exchange: str = "NASDAQ",
    strict: bool = False,
) -> str:
    """
    Build TradingView import token (``EXCHANGE:TICKER``).
    """
    sym = (ticker or "").upper().strip()
    if not sym:
        return ""

    # Already prefixed (e.g. NASDAQ:AAPL)
    if ":" in sym:
        if not strict:
            return sym
        ex, sym2 = sym.split(":", 1)
        ex_strict = normalize_exchange_strict(ex, default_exchange=default_exchange)
        token = f"{ex_strict}:{sym2.upper().strip()}" if ex_strict else ""
        return token if token and is_valid_tradingview_symbol_token(token) else ""

    if strict:
        ex = normalize_exchange_strict(exchange, default_exchange=default_exchange)
        token = f"{ex}:{sym}" if ex else ""
        return token if token and is_valid_tradingview_symbol_token(token) else ""

    ex = normalize_exchange_loose(exchange, default_exchange=default_exchange)
    return f"{ex}:{sym}"


def tradingview_symbol_url(
    ticker: str,
    exchange: Optional[str] = None,
    default_exchange: str = "NASDAQ",
) -> str:
    """
    Build canonical TradingView symbol page URL.

    Example: ``https://www.tradingview.com/symbols/NYSE-CRM/``
    """
    token = tradingview_symbol_token(
        ticker=ticker,
        exchange=exchange,
        default_exchange=default_exchange,
    )
    if not token:
        return ""
    ex, sym = token.split(":", 1)
    safe_ex = quote(ex, safe="")
    safe_sym = quote(sym, safe=".-")
    return f"https://www.tradingview.com/symbols/{safe_ex}-{safe_sym}/"


def tradingview_chart_url(
    ticker: str,
    exchange: Optional[str] = None,
    chart_id: Optional[str] = None,
    default_exchange: str = "NASDAQ",
) -> str:
    """
    Build TradingView chart URL with symbol query parameter.

    If ``chart_id`` is provided, uses:
      ``https://www.tradingview.com/chart/{chart_id}/?symbol=EXCHANGE%3ATICKER``

    Otherwise uses:
      ``https://www.tradingview.com/chart/?symbol=EXCHANGE%3ATICKER``
    """
    token = tradingview_symbol_token(
        ticker=ticker,
        exchange=exchange,
        default_exchange=default_exchange,
    )
    if not token:
        return ""
    safe_symbol = quote(token, safe="")
    if chart_id:
        safe_chart = quote(chart_id.strip(), safe="")
        return f"https://www.tradingview.com/chart/{safe_chart}/?symbol={safe_symbol}"
    return f"https://www.tradingview.com/chart/?symbol={safe_symbol}"


def tradingview_txt_content(tokens: Iterable[str]) -> str:
    """
    Build TradingView import text content (comma-separated tokens).
    """
    cleaned = [t.strip() for t in tokens if t and t.strip()]
    return ",".join(cleaned)


def _extract_info_timestamp(info: Mapping[str, Any]) -> Optional[float]:
    """
    Extract a best-effort "freshness" timestamp from yfinance-style info.

    Returns epoch seconds (float) or None.
    """
    # Common candidates from yfinance .info (epoch seconds or ms).
    for key in ("regularMarketTime", "preMarketTime", "postMarketTime", "lastUpdated", "lastTradeDate"):
        raw = info.get(key)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue

        # Heuristic: ms vs seconds
        if val > 1e12:
            return val / 1000.0
        if val > 1e9:
            return val

    return None


_ONLINE_VALIDATION_CONSEC_FAILS = 0
_ONLINE_VALIDATION_DISABLED_UNTIL = 0.0


def validate_tradingview_token_online(
    token: str,
    *,
    timeout_s: float = 1.0,
    user_agent: str = "Mozilla/5.0 (compatible; TradingAgents/1.0)",
    max_consecutive_failures: int = 5,
    disable_seconds: int = 60,
) -> bool:
    """
    Optional online verification for an `EXCHANGE:TICKER` token.

    Circuit-breaker behavior:
    - On timeouts/network errors, returns True (fallback to offline-only result set)
    - If too many consecutive failures happen, temporarily disables online checks
    """
    global _ONLINE_VALIDATION_CONSEC_FAILS, _ONLINE_VALIDATION_DISABLED_UNTIL
    if not token or ":" not in token:
        return False

    now = time.time()
    if now < _ONLINE_VALIDATION_DISABLED_UNTIL:
        return True

    ex, sym = token.split(":", 1)
    safe_ex = quote(ex.strip(), safe="")
    safe_sym = quote(sym.strip().upper(), safe=".-")
    url = f"https://www.tradingview.com/symbols/{safe_ex}-{safe_sym}/"

    req = Request(url, headers={"User-Agent": user_agent})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            code = getattr(resp, "status", None) or getattr(resp, "getcode", lambda: None)()
            _ONLINE_VALIDATION_CONSEC_FAILS = 0
            if code is not None and 200 <= int(code) < 400:
                return True
            return False
    except (socket.timeout, TimeoutError):
        _ONLINE_VALIDATION_CONSEC_FAILS += 1
    except Exception:
        _ONLINE_VALIDATION_CONSEC_FAILS += 1

    if _ONLINE_VALIDATION_CONSEC_FAILS >= max_consecutive_failures:
        _ONLINE_VALIDATION_DISABLED_UNTIL = now + float(disable_seconds)
    return True
