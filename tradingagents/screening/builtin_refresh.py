"""
Built-in watchlist refresh engine.

Creates proposal-only diffs for objective and curated built-in watchlists.
"""

from __future__ import annotations

import logging
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.request import Request, urlopen

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.alpha_vantage_common import get_active_us_symbols
from tradingagents.dataflows.alpha_vantage_fundamentals import get_fundamentals as av_get_fundamentals
from tradingagents.dataflows.finnhub_api import get_company_profile, get_us_symbol_set
from tradingagents.dataflows.yfinance_limiter import get_yfinance_limiter
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting.database import ResearchDatabase

logger = logging.getLogger("tradingagents.screening.builtin_refresh")

WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_SP400 = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
WIKI_SP600 = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
WIKI_NDX = "https://en.wikipedia.org/wiki/Nasdaq-100"
WIKI_ARISTOCRATS = "https://en.wikipedia.org/wiki/S%26P_500_Dividend_Aristocrats"
ISHARES_IWM_HOLDINGS_CSV = "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
# Vanguard VTWO tracks the Russell 2000 Index and exposes a public JSON holdings API
# that does not require browser bot-protection bypass (unlike iShares CSV downloads).
VANGUARD_VTWO_HOLDINGS_API = (
    "https://investor.vanguard.com/vmf/api/VTWO/portfolio-holding/stock?start=1&count=2500"
)
# ProShares NOBL (S&P 500 Dividend Aristocrats ETF) holdings — used as fallback
# when the Wikipedia predicate fails (the Wikipedia table schema has shifted
# multiple times over the past year). This is a public CSV endpoint.
PROSHARES_NOBL_HOLDINGS_CSV = "https://www.proshares.com/globalassets/proshares/fundcsvs/nobl_holdings.csv"
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# US-listed symbol shape: start with letter, up to 6 chars total, allow .-
# Rejects Wikipedia-typo artifacts like "AMAZN"/"APPL"/"CIVI2"/" TSLA" early
# without spending tier1/2/3 budget on them. NOTE: this is a FORMAT gate, not
# an existence gate — real symbols may still fail tier1/2/3 membership.
_VALID_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,4}([.\-][A-Z0-9]{1,3})?$")


def _is_valid_symbol_format(sym: str) -> bool:
    """Return True iff *sym* matches the US-listed ticker shape.

    We deliberately keep this narrow — the tier1/2 existence gates are the
    source-of-truth authority. This regex's job is only to stop Wikipedia
    typos and whitespace artifacts from burning tier3 (yfinance) budget.
    """
    if not sym or len(sym) > 6:
        return False
    return bool(_VALID_SYMBOL_RE.match(sym))


def _normalize_symbol(raw: str) -> str:
    sym = (raw or "").strip().upper()
    if not sym:
        return ""
    # Yahoo-style normalization for classes (e.g., BRK.B -> BRK-B)
    return sym.replace(".", "-")


def _dedupe_keep_order(symbols: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for sym in symbols:
        norm = _normalize_symbol(sym)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _extract_symbol_col(table: pd.DataFrame) -> str:
    for col in table.columns:
        col_name = str(col).lower()
        if "symbol" in col_name or "ticker" in col_name:
            return str(col)
    return ""


def _table_cols_lower(table: pd.DataFrame) -> List[str]:
    return [str(c).strip().lower() for c in table.columns]


def _is_sp500_table(table: pd.DataFrame) -> bool:
    cols = _table_cols_lower(table)
    return any("symbol" in c for c in cols) and any("security" in c for c in cols)


def _is_sp400_table(table: pd.DataFrame) -> bool:
    cols = _table_cols_lower(table)
    return any("symbol" in c for c in cols) and any("security" in c for c in cols)


def _is_sp600_table(table: pd.DataFrame) -> bool:
    cols = _table_cols_lower(table)
    return any("symbol" in c for c in cols) and any("security" in c for c in cols)


def _is_nasdaq100_table(table: pd.DataFrame) -> bool:
    cols = _table_cols_lower(table)
    has_symbol = any("ticker" in c or "symbol" in c for c in cols)
    has_company = any("company" in c for c in cols)
    return has_symbol and has_company


def _is_aristocrats_table(table: pd.DataFrame) -> bool:
    """Heuristic for the S&P 500 Dividend Aristocrats Wikipedia member table.

    Wikipedia has renamed the streak column over time — "Years of dividend
    increases", "Years of consecutive dividend increases", "Streak", etc. —
    so we match on any of those plus the symbol column. Falls back on
    sector+company columns when streak information is formatted into a
    non-standard header name.
    """
    cols = _table_cols_lower(table)
    has_symbol = any("symbol" in c or "ticker" in c for c in cols)
    if not has_symbol:
        return False
    has_streak = any(
        "year" in c or "streak" in c or "consecutive" in c or "dividend" in c
        for c in cols
    )
    # Fallback: a table with Symbol + Company + Sector is almost certainly
    # the member roster even if the streak column was renamed again.
    has_company = any("company" in c or "security" in c for c in cols)
    has_sector = any("sector" in c or "industry" in c for c in cols)
    return has_streak or (has_company and has_sector)


def _fetch_wikipedia_symbols(
    url: str,
    max_rows: int | None = None,
    table_predicate: Callable[[pd.DataFrame], bool] | None = None,
    min_symbols: int = 1,
    max_symbols: int | None = None,
) -> Tuple[List[str], List[str]]:
    warnings: List[str] = []
    try:
        req = Request(url, headers=_HTTP_HEADERS)
        with urlopen(req, timeout=15) as response:
            html = response.read()
        tables = pd.read_html(html)
    except Exception as exc:
        # Some hosts block default Python agents; keep direct read_html as a fallback.
        try:
            tables = pd.read_html(url)
            warnings.append(f"source fetch required fallback parser: {url}")
        except Exception as fallback_exc:
            logger.warning("Wikipedia fetch failed for %s: %s", url, fallback_exc)
            return [], [f"source fetch failed: {url} ({fallback_exc})"]

    for table in tables:
        if table is None or table.empty:
            continue
        if table_predicate and not table_predicate(table):
            continue
        symbol_col = _extract_symbol_col(table)
        if not symbol_col:
            continue
        symbols = [_normalize_symbol(str(v)) for v in table[symbol_col].tolist()]
        symbols = [s for s in symbols if s]
        symbols = _dedupe_keep_order(symbols)
        if len(symbols) < max(1, int(min_symbols)):
            continue
        if max_symbols is not None and len(symbols) > int(max_symbols):
            # Skip clearly wrong tables before truncation.
            continue
        if max_rows is not None:
            symbols = symbols[:max_rows]
        if symbols:
            return symbols, warnings

    warnings.append(f"no symbol table found: {url}")
    return [], warnings


def _fetch_index_symbols(index_name: str) -> Tuple[List[str], List[str]]:
    """Fetch index members from deterministic table predicates."""
    if index_name == "S&P 500 Top 100":
        symbols, warnings = _fetch_wikipedia_symbols(
            WIKI_SP500,
            table_predicate=_is_sp500_table,
            min_symbols=450,
            max_symbols=600,
        )
    elif index_name == "NASDAQ 100":
        symbols, warnings = _fetch_wikipedia_symbols(
            WIKI_NDX,
            table_predicate=_is_nasdaq100_table,
            min_symbols=80,
            max_symbols=130,
        )
    elif index_name == "Dividend Aristocrats Top 50":
        symbols, warnings = _fetch_wikipedia_symbols(
            WIKI_ARISTOCRATS,
            table_predicate=_is_aristocrats_table,
            min_symbols=40,
            max_symbols=120,
        )
        # NOBL (ProShares S&P 500 Dividend Aristocrats ETF) is the
        # authoritative public mirror of the index. Fall through to its
        # holdings CSV whenever Wikipedia's schema drift breaks us.
        if not symbols:
            nobl_symbols, nobl_warnings = _fetch_etf_holdings_symbols(PROSHARES_NOBL_HOLDINGS_CSV)
            warnings.append(
                "aristocrats wiki predicate failed; "
                f"using NOBL holdings fallback ({len(nobl_symbols)} symbols)"
            )
            warnings.extend(nobl_warnings)
            if nobl_symbols:
                symbols = nobl_symbols[:120]
    elif index_name == "S&P 500":
        symbols, warnings = _fetch_wikipedia_symbols(
            WIKI_SP500,
            table_predicate=_is_sp500_table,
            min_symbols=450,
            max_symbols=600,
        )
    elif index_name == "S&P 400":
        symbols, warnings = _fetch_wikipedia_symbols(
            WIKI_SP400,
            table_predicate=_is_sp400_table,
            min_symbols=330,
            max_symbols=500,
        )
    elif index_name == "S&P 600":
        symbols, warnings = _fetch_wikipedia_symbols(
            WIKI_SP600,
            table_predicate=_is_sp600_table,
            min_symbols=500,
            max_symbols=700,
        )
    elif index_name == "Russell 2000":
        symbols, warnings = _fetch_etf_holdings_symbols(ISHARES_IWM_HOLDINGS_CSV)
        if not symbols:
            vanguard_symbols, vanguard_warnings = _fetch_vanguard_etf_holdings_symbols(
                VANGUARD_VTWO_HOLDINGS_API
            )
            warnings.extend(vanguard_warnings)
            if vanguard_symbols:
                detail = warnings[0] if warnings else "iShares CSV unavailable"
                warnings.append(
                    f"iShares IWM CSV unavailable ({detail}); "
                    f"using Vanguard VTWO holdings fallback ({len(vanguard_symbols)} symbols)"
                )
                symbols = vanguard_symbols
        if symbols:
            return symbols, warnings
        return [], warnings
    else:
        return [], []

    if symbols:
        return symbols, warnings

    # Deterministic predicate failed. Fall back to generic symbol-table extraction
    # to preserve operational continuity while surfacing a clear warning.
    fallback_url = {
        "S&P 500 Top 100": WIKI_SP500,
        "S&P 500": WIKI_SP500,
        "S&P 400": WIKI_SP400,
        "S&P 600": WIKI_SP600,
        "NASDAQ 100": WIKI_NDX,
        "Dividend Aristocrats Top 50": WIKI_ARISTOCRATS,
    }.get(index_name, "")
    fallback_symbols, fallback_warnings = _fetch_wikipedia_symbols(fallback_url) if fallback_url else ([], [])
    all_warnings = warnings + [f"{index_name}: predicate selection failed, used generic fallback"] + fallback_warnings
    return fallback_symbols, all_warnings


def _looks_like_html_payload(text: str) -> bool:
    """Return True when a download body is HTML rather than CSV/JSON holdings data."""
    snippet = (text or "").lstrip()[:256].lower()
    return snippet.startswith("<!doctype") or snippet.startswith("<html")


def _fetch_vanguard_etf_holdings_symbols(api_url: str) -> Tuple[List[str], List[str]]:
    """Fetch ETF equity holdings from Vanguard's public JSON portfolio API."""
    warnings: List[str] = []
    try:
        req = Request(
            api_url,
            headers={
                **_HTTP_HEADERS,
                "Accept": "application/json",
            },
        )
        with urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        entities = (payload.get("fund") or {}).get("entity") or []
        if not entities and payload.get("next", {}).get("href"):
            # Paginate when the provider caps page size below total holdings.
            symbols: List[str] = []
            next_href = payload["next"]["href"]
            seen_urls = set()
            while next_href and next_href not in seen_urls:
                seen_urls.add(next_href)
                page_req = Request(next_href.replace("http://", "https://"), headers={**_HTTP_HEADERS, "Accept": "application/json"})
                with urlopen(page_req, timeout=30) as page_resp:
                    page_payload = json.loads(page_resp.read().decode("utf-8", errors="ignore"))
                entities.extend((page_payload.get("fund") or {}).get("entity") or [])
                next_href = (page_payload.get("next") or {}).get("href") or ""
        raw_tickers = [
            str(row.get("ticker") or "").strip()
            for row in entities
            if isinstance(row, dict)
        ]
        symbols = _dedupe_keep_order([_normalize_symbol(t) for t in raw_tickers if t])
        symbols = [s for s in symbols if s and _is_valid_symbol_format(s)]
        if not symbols:
            return [], [f"Vanguard holdings parse failed: empty ticker set ({api_url})"]
        expected = int(payload.get("size") or 0)
        if expected >= 200 and len(symbols) < int(expected * 0.85):
            warnings.append(
                f"Vanguard holdings sparse ({len(symbols)}/{expected}); verify pagination"
            )
        return symbols, warnings
    except Exception as exc:
        logger.warning("Vanguard holdings fetch failed for %s: %s", api_url, exc)
        return [], [f"Vanguard holdings fetch failed: {api_url} ({exc})"]


def _fetch_etf_holdings_symbols(csv_url: str) -> Tuple[List[str], List[str]]:
    warnings: List[str] = []
    try:
        req = Request(csv_url, headers=_HTTP_HEADERS)
        with urlopen(req, timeout=15) as response:
            csv_bytes = response.read()
        text = csv_bytes.decode("utf-8", errors="ignore")
        if _looks_like_html_payload(text):
            return [], [
                f"holdings fetch returned HTML instead of CSV (likely bot protection) ({csv_url})"
            ]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # iShares CSV contains header lines and a "Ticker" column in the table section.
        header_idx = -1
        for i, line in enumerate(lines):
            # Strip surrounding quotes from each field so we handle both the
            # legacy unquoted format ("Ticker,Name,...") and the newer quoted
            # format ('"Ticker","Name",...') that iShares switched to.
            stripped = line.replace('"', '').strip()
            lowered = stripped.lower()
            if (
                lowered.startswith("ticker,")
                or lowered.startswith("ticker\t")
                or ",ticker," in lowered
                or "\tticker\t" in lowered
            ):
                header_idx = i
                break
        if header_idx < 0:
            return [], [f"holdings parse failed: missing ticker header ({csv_url})"]
        data = "\n".join(lines[header_idx:])
        frame = pd.read_csv(StringIO(data))
        symbol_col = ""
        for c in frame.columns:
            if str(c).strip().lower() == "ticker":
                symbol_col = str(c)
                break
        if not symbol_col:
            return [], [f"holdings parse failed: ticker column not found ({csv_url})"]
        symbols = _dedupe_keep_order([_normalize_symbol(str(v)) for v in frame[symbol_col].tolist()])
        symbols = [s for s in symbols if s]
        if not symbols:
            return [], [f"holdings parse failed: empty ticker set ({csv_url})"]
        return symbols, warnings
    except Exception as exc:
        logger.warning("ETF holdings fetch failed for %s: %s", csv_url, exc)
        return [], [f"holdings fetch failed: {csv_url} ({exc})"]


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _metadata_market_cap_fresh(meta: Dict[str, Any], max_age_days: int = 7) -> bool:
    if not meta:
        return False
    cap = float(meta.get("market_cap") or 0.0)
    if cap <= 0:
        return False
    updated = _parse_iso_datetime(meta.get("last_updated"))
    if not updated:
        return False
    return updated >= (datetime.now(timezone.utc) - timedelta(days=max_age_days))


def _to_float_safe(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _fetch_ticker_snapshot_limited(symbol: str, retries: int = 1) -> Dict[str, Any]:
    limiter = get_yfinance_limiter()
    for attempt in range(max(0, retries) + 1):
        if limiter.is_open():
            cooldown = limiter.cooldown_remaining()
            if cooldown > 0:
                time.sleep(min(cooldown, 5.0))
        if not limiter.acquire(block=True):
            continue
        ticker = yf.Ticker(symbol)
        try:
            fast_info = getattr(ticker, "fast_info", {}) or {}
            if fast_info:
                market_cap = _to_float_safe(fast_info.get("marketCap") or fast_info.get("market_cap"))
                if market_cap > 0:
                    limiter.record_success()
                    return {"symbol": symbol, "valid": True}
            info = ticker.info or {}
            if info.get("shortName") or info.get("longName") or _to_float_safe(info.get("marketCap")) > 0:
                limiter.record_success()
                return {"symbol": symbol, "valid": True}
            hist = ticker.history(period="5d", interval="1d", auto_adjust=False, actions=False)
            limiter.record_success()
            return {"symbol": symbol, "valid": bool(hist is not None and not hist.empty)}
        except Exception as exc:
            if limiter.record_if_rate_limited(exc):
                continue
            if attempt < retries:
                time.sleep(0.25 * (attempt + 1))
                continue
            return {"symbol": symbol, "valid": False}
    return {"symbol": symbol, "valid": False}


def _validate_symbols_tiered(
    symbols: List[str],
    max_workers: int = 4,
    chunk_size: int = 250,
) -> Tuple[List[str], List[str], Dict[str, float], Dict[str, int]]:
    valid: List[str] = []
    invalid: List[str] = []
    caps: Dict[str, float] = {}
    stats = {
        "tier1_validated": 0,
        "tier2_validated": 0,
        "tier3_validated": 0,
        "tier3_attempted": 0,
        "format_rejected": 0,
    }
    if not symbols:
        return valid, invalid, caps, stats

    raw_ordered = _dedupe_keep_order(symbols)
    # Format pre-filter: reject Wikipedia-typo artifacts (e.g. "AMAZN", "APPL")
    # before they consume tier1/2/3 budget. Rejected symbols are still surfaced
    # in ``invalid`` for diff reporting but never reach downstream gates.
    ordered: List[str] = []
    format_rejects: List[str] = []
    for sym in raw_ordered:
        if _is_valid_symbol_format(sym):
            ordered.append(sym)
        else:
            format_rejects.append(sym)
    if format_rejects:
        stats["format_rejected"] = len(format_rejects)
        logger.info(
            "Refresh: format-rejected %d symbols (sample=%s)",
            len(format_rejects),
            format_rejects[:5],
        )

    tier1: Set[str] = set()
    tier2: Set[str] = set()
    try:
        tier1 = {s.upper() for s in get_active_us_symbols()}
    except Exception as exc:
        logger.warning("Tier1 validation source unavailable (Alpha Vantage): %s", exc)
    try:
        tier2 = {s.upper() for s in get_us_symbol_set()}
    except Exception as exc:
        logger.warning("Tier2 validation source unavailable (Finnhub): %s", exc)

    valid_set: Set[str] = set()
    leftovers: List[str] = []
    for sym in ordered:
        if sym in tier1:
            valid_set.add(sym)
            stats["tier1_validated"] += 1
        elif sym in tier2:
            valid_set.add(sym)
            stats["tier2_validated"] += 1
        else:
            leftovers.append(sym)

    if leftovers:
        limiter = get_yfinance_limiter()
        size = max(1, int(chunk_size))
        workers = max(1, int(max_workers))
        for start in range(0, len(leftovers), size):
            chunk = leftovers[start : start + size]
            if limiter.is_open():
                cooldown = limiter.cooldown_remaining()
                if cooldown > 0:
                    logger.info(
                        "Tier3 validator paused for %.1fs (breaker open)",
                        cooldown,
                    )
                    time.sleep(cooldown)
            stats["tier3_attempted"] += len(chunk)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_fetch_ticker_snapshot_limited, sym): sym for sym in chunk}
                for fut in as_completed(futures):
                    sym = futures[fut]
                    try:
                        payload = fut.result()
                    except Exception:
                        payload = {"symbol": sym, "valid": False}
                    if payload.get("valid"):
                        valid_set.add(sym)
                        stats["tier3_validated"] += 1

    for sym in ordered:
        if sym in valid_set:
            valid.append(sym)
        else:
            invalid.append(sym)
    # Append format-rejected symbols at the tail of ``invalid`` so callers
    # can report them without polluting the main validation diff.
    invalid.extend(format_rejects)
    return valid, invalid, caps, stats


def _validate_symbols(symbols: List[str], max_workers: int = 8) -> Tuple[List[str], List[str], Dict[str, float]]:
    """Compatibility shim for older tests/callers."""
    valid, invalid, caps, _ = _validate_symbols_tiered(symbols, max_workers=max_workers)
    return valid, invalid, caps


def _extract_market_cap_from_finnhub_profile(payload: Dict[str, Any]) -> float:
    if not isinstance(payload, dict):
        return 0.0
    # Finnhub returns marketCapitalization in millions of USD.
    cap_millions = _to_float_safe(payload.get("marketCapitalization"))
    return cap_millions * 1_000_000 if cap_millions > 0 else 0.0


def _extract_market_cap_from_av_overview(payload: Any) -> float:
    data = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except Exception:
            return 0.0
    if not isinstance(data, dict):
        return 0.0
    return _to_float_safe(data.get("MarketCapitalization"))


def _upsert_market_cap(db: ResearchDatabase, symbol: str, existing_meta: Dict[str, Any], market_cap: float) -> None:
    db.save_ticker_metadata(
        ticker=symbol,
        sector=existing_meta.get("sector"),
        industry=existing_meta.get("industry"),
        market_cap=market_cap,
        avg_dollar_volume_usd=existing_meta.get("avg_dollar_volume_usd"),
        liquidity_rank=existing_meta.get("liquidity_rank"),
        market_cap_tier=existing_meta.get("market_cap_tier"),
        is_profitable=existing_meta.get("is_profitable"),
        has_dividend=existing_meta.get("has_dividend"),
        beta=existing_meta.get("beta"),
        beta_tier=existing_meta.get("beta_tier"),
        resolved_profile=existing_meta.get("resolved_profile"),
        resolved_preset=existing_meta.get("resolved_preset"),
    )


def _hydrate_market_caps(
    db: ResearchDatabase,
    symbols: List[str],
    warnings: Optional[List[str]] = None,
    max_age_days: int = 7,
) -> Tuple[Dict[str, float], int]:
    cap_map: Dict[str, float] = {}
    null_caps = 0
    if not symbols:
        return cap_map, 0
    metadata_map = db.get_ticker_metadata_bulk(symbols) if hasattr(db, "get_ticker_metadata_bulk") else {}
    for sym in symbols:
        meta = dict(metadata_map.get(sym, {}))
        if _metadata_market_cap_fresh(meta, max_age_days=max_age_days):
            cap_map[sym] = _to_float_safe(meta.get("market_cap"))
            continue

        cap = 0.0
        try:
            cap = _extract_market_cap_from_finnhub_profile(get_company_profile(sym))
        except Exception:
            cap = 0.0
        if cap <= 0:
            try:
                cap = _extract_market_cap_from_av_overview(av_get_fundamentals(sym))
            except Exception:
                cap = 0.0

        if cap > 0:
            cap_map[sym] = cap
            try:
                _upsert_market_cap(db, sym, meta, cap)
            except Exception as exc:
                if warnings is not None:
                    warnings.append(f"{sym}: metadata update failed ({exc})")
        else:
            cap_map[sym] = 0.0
            null_caps += 1

    return cap_map, null_caps


def _sort_by_market_cap(symbols: List[str], caps: Dict[str, float]) -> List[str]:
    return sorted(symbols, key=lambda s: caps.get(s, 0.0), reverse=True)


def _parse_csv_tickers(csv_text: str) -> List[str]:
    return _dedupe_keep_order((csv_text or "").replace("\n", ",").split(","))


def _target_for(name: str, registry_map: Dict[str, Dict[str, Any]], default_value: int = 0) -> int:
    item = registry_map.get(name, {})
    try:
        return int(item.get("target_size", default_value))
    except Exception:
        return default_value


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _stabilize_objective_candidate(
    old_tickers: List[str],
    ordered_universe: List[str],
    target_size: int,
    max_replacements: int,
) -> List[str]:
    """Reduce churn by preserving valid incumbents before adding new names."""
    if target_size <= 0:
        return []
    ordered_universe = _dedupe_keep_order(ordered_universe)
    if not ordered_universe:
        return []

    if not old_tickers:
        return ordered_universe[:target_size]

    universe_set = set(ordered_universe)
    old_in_universe = [s for s in old_tickers if s in universe_set]
    old_in_universe_set = set(old_in_universe)
    new_only = [s for s in ordered_universe if s not in old_in_universe_set]

    replacement_cap = max(0, min(max_replacements, target_size))
    keep_target = max(0, target_size - replacement_cap)

    candidate: List[str] = old_in_universe[:keep_target]
    remaining = target_size - len(candidate)
    if remaining > 0:
        candidate.extend(new_only[:remaining])

    if len(candidate) < target_size:
        for sym in old_in_universe[keep_target:]:
            if sym not in candidate:
                candidate.append(sym)
            if len(candidate) >= target_size:
                break

    if len(candidate) < target_size:
        for sym in ordered_universe:
            if sym not in candidate:
                candidate.append(sym)
            if len(candidate) >= target_size:
                break

    return candidate[:target_size]


def _diff_and_churn(old_tickers: List[str], new_tickers: List[str]) -> Tuple[List[str], List[str], float]:
    old_set = set(old_tickers)
    new_set = set(new_tickers)
    adds = sorted(new_set - old_set)
    removes = sorted(old_set - new_set)
    churn_denom = max(1, len(old_tickers))
    churn_pct = (len(adds) + len(removes)) / churn_denom
    return adds, removes, churn_pct


def build_refresh_proposal(
    db: ResearchDatabase,
    config: Dict[str, Any] | None = None,
    source: str = "manual",
) -> Dict[str, Any]:
    """Build and persist a built-in refresh proposal (proposal-only)."""
    cfg = config or DEFAULT_CONFIG
    refresh_cfg = (
        cfg.get("screening", {})
        .get("scheduler", {})
        .get("builtin_refresh", {})
    )
    max_churn_pct = float(refresh_cfg.get("max_churn_pct", 0.30))
    min_size_by_list = refresh_cfg.get("min_size_by_list", {})
    max_churn_pct_by_list = refresh_cfg.get("max_churn_pct_by_list", {})
    max_replacements_by_list = refresh_cfg.get("max_replacements_by_list", {})
    enforce_churn_at_proposal = bool(refresh_cfg.get("enforce_churn_at_proposal", False))
    allow_sparse_baseline_fallback = bool(refresh_cfg.get("allow_sparse_baseline_fallback", False))
    sparse_validation_min_ratio = _to_float(refresh_cfg.get("sparse_validation_min_ratio", 0.90), 0.90)
    sparse_validation_min_ratio_by_list: Dict[str, float] = {
        k: _to_float(v, sparse_validation_min_ratio)
        for k, v in refresh_cfg.get("sparse_validation_min_ratio_by_list", {}).items()
    }
    validation_workers = max(2, _to_int(refresh_cfg.get("validation_workers", 8), 8))

    registry = [r for r in db._builtin_watchlist_registry() if r.get("source") == "built-in" and r.get("enabled", True)]
    registry_map = {r["name"]: r for r in registry}
    existing = {w["name"]: w for w in db.get_builtin_watchlists()}

    items: List[Dict[str, Any]] = []
    global_warnings: List[str] = []
    started_at = time.monotonic()
    total_validated = 0
    total_invalid = 0
    total_null_market_caps = 0
    tier_counts = {"tier1_validated": 0, "tier2_validated": 0, "tier3_validated": 0, "format_rejected": 0}

    def _run_tiered(symbol_pool: List[str]) -> Tuple[List[str], List[str], Dict[str, int]]:
        valid, invalid, _, tier_stats = _validate_symbols_tiered(
            symbol_pool,
            max_workers=validation_workers,
            chunk_size=250,
        )
        return valid, invalid, tier_stats

    for entry in registry:
        name = entry["name"]
        old_row = existing.get(name, {})
        old_tickers = _parse_csv_tickers(old_row.get("tickers") or entry.get("tickers", ""))
        warnings: List[str] = []
        source_mode = entry.get("source_mode", "curated")
        target_size = _target_for(name, registry_map, len(old_tickers))

        candidate: List[str] = []
        cap_map: Dict[str, float] = {}

        list_churn_limit = _to_float(max_churn_pct_by_list.get(name, max_churn_pct), max_churn_pct)
        list_max_replacements = _to_int(max_replacements_by_list.get(name, target_size), target_size)

        if name == "S&P 500 Top 100":
            wiki_symbols, source_warnings = _fetch_index_symbols(name)
            warnings.extend(source_warnings)
            if not wiki_symbols:
                warnings.append("source empty; retained existing list")
                candidate = old_tickers
            symbol_pool = _dedupe_keep_order(wiki_symbols + old_tickers)
            valid, invalid, tier_stats = _run_tiered(symbol_pool)
            for key in tier_counts:
                tier_counts[key] += int(tier_stats.get(key, 0))
            total_validated += len(valid)
            total_invalid += len(invalid)
            if invalid:
                warnings.append(f"{len(invalid)} invalid symbols filtered")
            cap_map, null_caps = _hydrate_market_caps(db=db, symbols=valid, warnings=warnings)
            total_null_market_caps += null_caps
            ordered_universe = _sort_by_market_cap(valid, cap_map)
            candidate = _stabilize_objective_candidate(
                old_tickers=old_tickers,
                ordered_universe=ordered_universe,
                target_size=target_size,
                max_replacements=list_max_replacements,
            )
            if len(valid) < target_size:
                warnings.append(f"candidate under target ({len(valid)}/{target_size})")
        elif name == "S&P 500":
            wiki_symbols, source_warnings = _fetch_index_symbols(name)
            warnings.extend(source_warnings)
            if not wiki_symbols:
                warnings.append("source empty; retained existing list")
                candidate = old_tickers
            symbol_pool = _dedupe_keep_order(wiki_symbols + old_tickers)
            valid, invalid, tier_stats = _run_tiered(symbol_pool)
            for key in tier_counts:
                tier_counts[key] += int(tier_stats.get(key, 0))
            total_validated += len(valid)
            total_invalid += len(invalid)
            if invalid:
                warnings.append(f"{len(invalid)} invalid symbols filtered")
            candidate = valid[:target_size] if target_size > 0 else valid
            if len(candidate) < min(450, target_size or 450):
                warnings.append(f"candidate below expected S&P 500 floor ({len(candidate)})")
        elif name == "S&P 400":
            wiki_symbols, source_warnings = _fetch_index_symbols(name)
            warnings.extend(source_warnings)
            if not wiki_symbols:
                warnings.append("source empty; retained existing list")
                candidate = old_tickers
            symbol_pool = _dedupe_keep_order(wiki_symbols + old_tickers)
            valid, invalid, tier_stats = _run_tiered(symbol_pool)
            for key in tier_counts:
                tier_counts[key] += int(tier_stats.get(key, 0))
            total_validated += len(valid)
            total_invalid += len(invalid)
            if invalid:
                warnings.append(f"{len(invalid)} invalid symbols filtered")
            candidate = valid[:target_size] if target_size > 0 else valid
            if len(candidate) < min(320, target_size or 320):
                warnings.append(f"candidate below expected S&P 400 floor ({len(candidate)})")
        elif name == "S&P 600":
            wiki_symbols, source_warnings = _fetch_index_symbols(name)
            warnings.extend(source_warnings)
            if not wiki_symbols:
                warnings.append("source empty; retained existing list")
                candidate = old_tickers
            symbol_pool = _dedupe_keep_order(wiki_symbols + old_tickers)
            valid, invalid, tier_stats = _run_tiered(symbol_pool)
            for key in tier_counts:
                tier_counts[key] += int(tier_stats.get(key, 0))
            total_validated += len(valid)
            total_invalid += len(invalid)
            if invalid:
                warnings.append(f"{len(invalid)} invalid symbols filtered")
            candidate = valid[:target_size] if target_size > 0 else valid
            if len(candidate) < min(450, target_size or 450):
                warnings.append(f"candidate below expected S&P 600 floor ({len(candidate)})")
        elif name == "NASDAQ 100":
            wiki_symbols, source_warnings = _fetch_index_symbols(name)
            warnings.extend(source_warnings)
            if not wiki_symbols:
                warnings.append("source empty; retained existing list")
                candidate = old_tickers
            symbol_pool = _dedupe_keep_order(wiki_symbols + old_tickers)
            valid, invalid, tier_stats = _run_tiered(symbol_pool)
            for key in tier_counts:
                tier_counts[key] += int(tier_stats.get(key, 0))
            total_validated += len(valid)
            total_invalid += len(invalid)
            if invalid:
                warnings.append(f"{len(invalid)} invalid symbols filtered")
            cap_map, null_caps = _hydrate_market_caps(db=db, symbols=valid, warnings=warnings)
            total_null_market_caps += null_caps
            ordered_universe = _sort_by_market_cap(valid, cap_map)
            candidate = _stabilize_objective_candidate(
                old_tickers=old_tickers,
                ordered_universe=ordered_universe,
                target_size=target_size,
                max_replacements=list_max_replacements,
            )
        elif name == "Dividend Aristocrats Top 50":
            wiki_symbols, source_warnings = _fetch_index_symbols(name)
            warnings.extend(source_warnings)
            if not wiki_symbols:
                warnings.append("source empty; retained existing list")
                candidate = old_tickers
            valid, invalid, tier_stats = _run_tiered(_dedupe_keep_order(wiki_symbols + old_tickers))
            for key in tier_counts:
                tier_counts[key] += int(tier_stats.get(key, 0))
            total_validated += len(valid)
            total_invalid += len(invalid)
            if invalid:
                warnings.append(f"{len(invalid)} invalid symbols filtered")
            cap_map, null_caps = _hydrate_market_caps(db=db, symbols=valid, warnings=warnings)
            total_null_market_caps += null_caps
            ordered_universe = _sort_by_market_cap(valid, cap_map)
            candidate = _stabilize_objective_candidate(
                old_tickers=old_tickers,
                ordered_universe=ordered_universe,
                target_size=target_size,
                max_replacements=list_max_replacements,
            )
        elif name == "Russell 2000 Top 100":
            # Use per-list sparse threshold (default 0.80 for small-cap list)
            # since small-cap symbols fail tier1/2 gates more often than large-caps.
            list_sparse_min_ratio = sparse_validation_min_ratio_by_list.get(name, sparse_validation_min_ratio)
            baseline = _parse_csv_tickers(entry.get("tickers", ""))
            valid, invalid, tier_stats = _run_tiered(
                _dedupe_keep_order(old_tickers + baseline),
            )
            for key in tier_counts:
                tier_counts[key] += int(tier_stats.get(key, 0))
            total_validated += len(valid)
            total_invalid += len(invalid)
            if invalid:
                warnings.append(f"{len(invalid)} invalid symbols filtered")
            cap_map, null_caps = _hydrate_market_caps(db=db, symbols=valid, warnings=warnings)
            total_null_market_caps += null_caps
            ordered_universe = _sort_by_market_cap(valid, cap_map)
            # Use stabilization to reduce churn: preserve valid incumbents first,
            # only rotate in new names up to max_replacements.
            candidate = _stabilize_objective_candidate(
                old_tickers=old_tickers,
                ordered_universe=ordered_universe,
                target_size=target_size,
                max_replacements=list_max_replacements,
            )
            valid_ratio = (len(candidate) / max(1, target_size)) if target_size else 1.0
            if valid_ratio < list_sparse_min_ratio:
                warnings.append(
                    f"validation sparse ({len(candidate)}/{target_size}); "
                    f"below required ratio ({valid_ratio:.0%} < {list_sparse_min_ratio:.0%})"
                )
                if allow_sparse_baseline_fallback:
                    fallback_valid, fallback_invalid, fallback_stats = _run_tiered(baseline)
                    for key in tier_counts:
                        tier_counts[key] += int(fallback_stats.get(key, 0))
                    total_validated += len(fallback_valid)
                    total_invalid += len(fallback_invalid)
                    if fallback_invalid:
                        warnings.append(f"{len(fallback_invalid)} baseline symbols invalid")
                    fallback_caps, fallback_null_caps = _hydrate_market_caps(
                        db=db,
                        symbols=fallback_valid,
                        warnings=warnings,
                    )
                    total_null_market_caps += fallback_null_caps
                    fallback_ordered = _sort_by_market_cap(fallback_valid, fallback_caps)
                    candidate = _stabilize_objective_candidate(
                        old_tickers=old_tickers,
                        ordered_universe=fallback_ordered,
                        target_size=target_size,
                        max_replacements=list_max_replacements,
                    )
                    warnings.append(
                        f"sparse validation guard: below {list_sparse_min_ratio:.0%} threshold; "
                        "applied validated baseline fallback (no net change to existing list until threshold met)"
                    )
                else:
                    candidate = old_tickers
                    warnings.append(
                        f"sparse validation guard: below {list_sparse_min_ratio:.0%} threshold "
                        f"({valid_ratio:.0%} valid); retained existing list without applying new candidates"
                    )
        elif name == "Russell 2000":
            holdings_symbols, source_warnings = _fetch_index_symbols(name)
            warnings.extend(source_warnings)
            baseline = _parse_csv_tickers(entry.get("tickers", ""))
            symbol_pool = _dedupe_keep_order(holdings_symbols + baseline + old_tickers)
            valid, invalid, tier_stats = _run_tiered(symbol_pool)
            for key in tier_counts:
                tier_counts[key] += int(tier_stats.get(key, 0))
            total_validated += len(valid)
            total_invalid += len(invalid)
            if invalid:
                warnings.append(f"{len(invalid)} invalid symbols filtered")
            candidate = valid[:target_size] if target_size > 0 else valid
            if len(candidate) < min(1200, target_size or 1200):
                warnings.append(
                    f"candidate below expected Russell breadth floor ({len(candidate)}); "
                    "using liquidity-gated fallback set"
                )
        else:
            # Curated validation mode: validate, remove stale symbols, keep intent.
            curated = _parse_csv_tickers(entry.get("tickers", ""))
            valid, invalid, tier_stats = _run_tiered(curated)
            for key in tier_counts:
                tier_counts[key] += int(tier_stats.get(key, 0))
            total_validated += len(valid)
            total_invalid += len(invalid)
            if invalid:
                warnings.append(f"{len(invalid)} invalid symbols filtered")
            candidate = valid if valid else old_tickers
            if not valid:
                warnings.append("validation returned empty; retained existing set")

        candidate = _dedupe_keep_order(candidate)
        min_floor = int(min_size_by_list.get(name, 5))
        if len(candidate) < min_floor:
            warnings.append(
                f"below min floor ({len(candidate)}<{min_floor}); keeping existing list"
            )
            candidate = old_tickers

        adds, removes, churn_pct = _diff_and_churn(old_tickers, candidate)
        # Suppress the high-churn warning on genuine first-time population.
        # The DB row's source_mode_status is the authoritative marker: while
        # it's still 'pending_first_refresh' the watchlist has never been
        # materialized, so churn relative to the registry bootstrap seed
        # set is meaningless. Emit a dedicated initial_population note
        # instead so apply gates distinguish "legitimate first populate"
        # from "real churn on an established list".
        watchlist_row_tickers = str(old_row.get("tickers") or "").strip()
        row_status = str(old_row.get("source_mode_status") or "").lower()
        is_initial_population = (
            row_status == "pending_first_refresh"
            and not watchlist_row_tickers
        )
        if is_initial_population:
            warnings.append(
                f"initial_population: first-time populate with {len(candidate)} symbols"
            )
        elif churn_pct > list_churn_limit:
            warnings.append(
                f"high churn warning ({churn_pct:.1%} > {list_churn_limit:.1%})"
            )
            if enforce_churn_at_proposal:
                warnings.append("churn cap enforced; retained existing list")
                candidate = old_tickers
                adds, removes, churn_pct = _diff_and_churn(old_tickers, candidate)

        items.append(
            {
                "watchlist_id": old_row.get("id"),
                "watchlist_name": name,
                "old_tickers": old_tickers,
                "new_tickers": candidate,
                "adds": adds,
                "removes": removes,
                "warnings": warnings,
                "meta": {
                    "source_mode": source_mode,
                    "target_size": target_size,
                    "min_floor": min_floor,
                    "churn_pct": round(churn_pct, 4),
                },
            }
        )
        global_warnings.extend([f"{name}: {w}" for w in warnings])

        # Persist latest index constituent rows (registry-backed built-ins only).
        index_key = str(entry.get("index_key") or "").strip().lower()
        if (
            entry.get("source") == "built-in"
            and bool(entry.get("registry_backed"))
            and index_key
            and candidate
            and hasattr(db, "save_index_constituents")
        ):
            try:
                ranked = []
                if any(float(cap_map.get(sym, 0.0)) > 0 for sym in candidate):
                    sorted_for_rank = sorted(
                        candidate,
                        key=lambda sym: (
                            -float(cap_map.get(sym, 0.0)),
                            sym,
                        ),
                    )
                else:
                    sorted_for_rank = list(candidate)
                metadata_map = db.get_ticker_metadata_bulk(sorted_for_rank) if hasattr(db, "get_ticker_metadata_bulk") else {}
                for rank, sym in enumerate(sorted_for_rank, start=1):
                    meta = metadata_map.get(sym, {}) if isinstance(metadata_map, dict) else {}
                    ranked.append(
                        {
                            "ticker": sym,
                            "source": str(entry.get("source_mode") or "refresh"),
                            "weight": None,
                            "avg_dollar_volume_usd": float(meta.get("avg_dollar_volume_usd") or 0.0) or None,
                            "market_cap": float(cap_map.get(sym, 0.0) or meta.get("market_cap") or 0.0) or None,
                            "market_cap_tier": str(meta.get("market_cap_tier") or "") or None,
                            "liquidity_rank": rank,
                            "in_scope": True,
                        }
                    )
                db.save_index_constituents(
                    index_key=index_key,
                    rows=ranked,
                    source=str(entry.get("source_mode") or "refresh"),
                )
            except Exception as exc:
                warnings.append(f"registry persist failed ({index_key}): {exc}")

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "max_churn_pct": max_churn_pct,
        "warnings": global_warnings,
        "validation_counts": {
            **tier_counts,
            "total_validated": total_validated,
            "total_invalid": total_invalid,
            "total_null_market_cap_count": total_null_market_caps,
        },
    }
    proposal_id = db.create_builtin_refresh_proposal(items, source=source, metadata=metadata)
    duration_seconds = float(max(0.0, time.monotonic() - started_at))
    invalid_ratio = float(total_invalid / max(1, total_validated + total_invalid))
    runtime_context = {
        "source": source,
        "total_validated": total_validated,
        "total_invalid": total_invalid,
        "tier1_validated": tier_counts["tier1_validated"],
        "tier2_validated": tier_counts["tier2_validated"],
        "tier3_validated": tier_counts["tier3_validated"],
        "format_rejected": tier_counts.get("format_rejected", 0),
    }
    try:
        db.record_runtime_metric("builtin_refresh_duration_seconds", duration_seconds, context=runtime_context)
        db.record_runtime_metric("builtin_refresh_invalid_ratio", invalid_ratio, context=runtime_context)
        db.record_runtime_metric(
            "builtin_refresh_null_market_cap_count",
            float(total_null_market_caps),
            context=runtime_context,
        )
        db.record_runtime_metric(
            "builtin_refresh_format_rejected",
            float(tier_counts.get("format_rejected", 0)),
            context=runtime_context,
        )
    except Exception as exc:
        logger.debug("Builtin refresh runtime metric write failed: %s", exc)
    proposal = db.get_builtin_refresh_proposal(proposal_id)
    if not proposal:
        raise RuntimeError("Failed to load built-in refresh proposal after creation")
    return proposal
