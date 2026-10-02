"""
Ticker Metadata Auto-Resolver

Fetches classification metadata from yfinance for any ticker and deterministically
maps it to the correct investment profile and screening preset. Caches results in
the ticker_metadata DB table with a 7-day staleness window.

Classification fields:
    - sector, industry      (from yfinance .info)
    - market_cap_tier       (derived: mega/large/mid/small/micro)
    - is_profitable         (trailingEPS > 0 or netIncome > 0)
    - has_dividend          (dividendYield > 0)
    - beta_tier             (derived: defensive/low/medium/high/extreme)

Resolution rules (ticker → investment_profile + screening_preset):
    These follow the same logic an institutional portfolio analyst would use
    to assign a coverage bucket.
"""

import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

logger = logging.getLogger(__name__)


def _safe_num(value: Any) -> Optional[float]:
    """Coerce arbitrary yfinance ``.info`` values to a finite float or None.

    yfinance occasionally returns numeric fields as strings (``"N/A"``,
    ``"Infinity"``, ``"0.05"``), bool (``True``/``False``), or NaN/inf floats.
    Downstream classifiers rely on strict numeric comparisons, so we normalize
    at the boundary.  Booleans are rejected because they would silently compare
    as 0/1 and corrupt tier classification.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in {"n/a", "na", "none", "null", "infinity", "inf", "-inf", "nan"}:
            return None
        try:
            f = float(s.replace(",", "").rstrip("%"))
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    return None


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def classify_market_cap(market_cap: Any) -> str:
    """Classify market cap into tier. Null-safe against strings / NaN / bools."""
    mc = _safe_num(market_cap)
    if mc is None or mc <= 0:
        return "unknown"
    if mc >= 200e9:
        return "mega"
    if mc >= 10e9:
        return "large"
    if mc >= 2e9:
        return "mid"
    if mc >= 300e6:
        return "small"
    return "micro"


# US 2-letter country codes / long names that count as "domestic equity" for
# asset-class classification purposes. yfinance returns these inconsistently
# (sometimes "United States", sometimes "US"), so we match loosely.
_US_COUNTRY_TOKENS = frozenset({
    "united states",
    "us",
    "usa",
    "u.s.",
    "u.s.a.",
})

# yfinance ``quoteType`` values that should be treated as ETFs (including
# closed-end funds and mutual funds). ETNs are rare but behave like ETFs
# for screening purposes.
_ETF_QUOTE_TYPES = frozenset({"ETF", "MUTUALFUND", "CLOSEDENDFUND"})


def classify_asset_class(info: Dict[str, Any]) -> str:
    """Classify a ticker's asset class from yfinance ``.info`` payload.

    Returns one of:
      - ``"etf"``     — quoteType is ETF / MUTUALFUND / CLOSEDENDFUND
      - ``"adr"``     — common-stock quoteType *and* non-US country of domicile
                         (or the long name explicitly contains "ADR")
      - ``"equity"``  — common-stock quoteType with US / unknown domicile
      - ``"unknown"`` — no quoteType available (typically a failed fetch)

    We bias toward classifying ambiguous non-US listings as ADRs so they get
    the "foreign equity listed on US exchange" handling; the downstream
    screening engine still treats them as full-fundamental tickers like
    equities (unlike ETFs which lack PE/EPS/analyst coverage in the
    traditional sense).
    """
    if not info:
        return "unknown"
    long_name = str(info.get("longName") or "").lower()
    short_name = str(info.get("shortName") or "").lower()
    name_blob = f"{long_name} {short_name}"
    if " etf" in f" {name_blob}" or name_blob.rstrip().endswith("etf") or "exchange traded" in name_blob:
        return "etf"
    qt = str(info.get("quoteType") or "").strip().upper()
    if not qt:
        return "unknown"
    if qt in _ETF_QUOTE_TYPES:
        return "etf"
    if " adr" in long_name or long_name.endswith(" adr") or "american depositary" in long_name:
        return "adr"
    country = str(info.get("country") or "").strip().lower()
    if country and country not in _US_COUNTRY_TOKENS:
        return "adr"
    # EQUITY / COMMON / None-of-the-above US-domiciled → plain equity
    return "equity"


def classify_beta(beta: Any) -> str:
    """Classify beta into risk tier. Null-safe against strings / NaN / bools.

    Handles negative beta (gold miners, inverse instruments) as 'defensive'
    since they move opposite to the market — low risk from a portfolio perspective.
    """
    b = _safe_num(beta)
    if b is None:
        return "unknown"
    if b < 0:
        return "defensive"  # Negative beta = inverse correlation to market
    if b < 0.8:
        return "low"
    if b < 1.2:
        return "medium"
    if b < 1.8:
        return "high"
    return "extreme"


def resolve_profile_and_preset(
    sector: str,
    market_cap_tier: str,
    is_profitable: bool,
    has_dividend: bool,
    beta_tier: str,
    asset_class: str = "equity",
    industry: str = "",
) -> Tuple[str, str]:
    """Deterministically resolve the best investment_profile and screening_preset
    for a ticker based on its classification metadata.

    Returns (investment_profile, screening_preset).

    ETFs bypass the standard equity decision tree entirely — they lack most
    fundamentals-based signals (PE, analyst targets, estimate revisions,
    earnings dates), so the ``etf_technical`` preset weights heavily toward
    Tier 1 technicals (momentum, trend, relative strength). ADRs flow
    through the normal tree because they trade and behave like US equities.

    Decision tree (priority order):
    1. Energy / basic materials → commodity_cyclical
    2. Small/micro cap OR not profitable OR extreme beta → momentum_speculative
    3. Real estate → dividend_income (REITs are income instruments)
    4. Utilities → dividend_income (defensive/income, not cyclical)
    5. Growth sectors (tech, comm, consumer cyclical) + large/mega → high_growth
       Exception: low/defensive-beta dividend payers → dividend_income
    6. Healthcare + large/mega → high_growth UNLESS low/defensive-beta dividend payer
       (broader exception than pure growth sectors to catch defensive pharma)
    7. Financial services → dividend_income if dividend + stable, else value_fisher
    8. Dividend payers with stability (any remaining sector) → dividend_income
    9. Default: large_cap_core → value_fisher
    """
    # 0. ETFs → dedicated preset (technicals-heavy, no fundamentals)
    if asset_class == "etf":
        return "etf_baseline", "etf_technical"

    sector_lower = (sector or "").lower()

    # 1. Energy, basic materials, and shipping → commodity/cyclical.
    # Marine shipping is an asset-heavy global-rate cycle; its sector is often
    # reported as Industrials, so sector alone misclassifies tickers like ZIM.
    commodity_sectors = {"energy", "basic materials"}
    industry_lower = (industry or "").lower()
    if sector_lower in commodity_sectors or "shipping" in industry_lower:
        return "commodity_cyclical", "commodity_cyclical"

    # 1b. Airlines are cyclical transports, not income cores — even when a
    # small or data-glitched dividend yield is present (LUV → dividend_income).
    if "airline" in industry_lower:
        if market_cap_tier in ("mega", "large"):
            return "large_cap_core", "value_fisher"
        return "momentum_speculative", "momentum_hunter"

    # 2. Small/micro cap, unprofitable, or extreme volatility → speculative
    if market_cap_tier in ("small", "micro", "unknown"):
        return "momentum_speculative", "momentum_hunter"
    if not is_profitable:
        return "momentum_speculative", "momentum_hunter"
    if beta_tier == "extreme":
        return "momentum_speculative", "momentum_hunter"

    # 3. Real estate (REITs) → income instruments
    if sector_lower == "real estate":
        return "dividend_income", "dividend_income"

    # 4. Utilities → defensive income (NOT cyclical commodity)
    if sector_lower == "utilities":
        return "dividend_income", "dividend_income"

    # 5. Growth sectors (excluding healthcare — handled separately below)
    growth_sectors = {"technology", "communication services", "consumer cyclical"}
    if sector_lower in growth_sectors and market_cap_tier in ("mega", "large"):
        if has_dividend and beta_tier in ("low", "defensive"):
            # Defensive dividend payer in a growth sector — treat as income
            return "dividend_income", "dividend_income"
        return "high_growth", "momentum_hunter"

    # 6. Healthcare — broader defensive exception (low OR medium beta + dividend)
    #    Catches JNJ (β 0.35), PFE (β 0.65), BMY (β 0.30) as dividend/income
    #    while keeping ISRG (β 1.1, no div) as high_growth
    if sector_lower == "healthcare" and market_cap_tier in ("mega", "large"):
        if has_dividend and beta_tier in ("low", "defensive", "medium"):
            return "dividend_income", "dividend_income"
        return "high_growth", "momentum_hunter"

    # 7. Financial services — banks, insurers, asset managers
    #    Banks are inherently cyclical (higher beta) but remain income instruments
    #    if they pay a dividend, so we accept up to "high" beta here.
    if sector_lower == "financial services":
        if has_dividend and beta_tier in ("low", "defensive", "medium", "high"):
            return "dividend_income", "dividend_income"
        return "large_cap_core", "value_fisher"

    # 8. Dividend payers with stability (consumer defensive, industrials, etc.)
    #    For mega/large-cap established dividend payers, accept up to "high" beta.
    #    Cyclical industrials (CAT, DE) and consumer names still warrant income
    #    treatment when they have a consistent dividend history.
    if has_dividend and market_cap_tier in ("mega", "large"):
        if beta_tier in ("low", "defensive", "medium", "high"):
            return "dividend_income", "dividend_income"
    elif has_dividend and beta_tier in ("low", "defensive", "medium"):
        return "dividend_income", "dividend_income"

    # 9. Default: stable large cap — value-focused screening
    return "large_cap_core", "value_fisher"


# ---------------------------------------------------------------------------
# Single ticker resolution
# ---------------------------------------------------------------------------

def resolve_ticker_metadata(ticker: str) -> Optional[Dict]:
    """Fetch yfinance info for a single ticker and return classification dict.

    Returns None if the ticker cannot be resolved (delisted, invalid, etc.).
    Uses the centralized get_ticker_info() cache (6-hour TTL).
    """
    from tradingagents.dataflows.yfinance_extended import get_ticker_info
    from tradingagents.utils.tradingview_links import resolve_exchange_from_info

    try:
        info = get_ticker_info(ticker)
    except Exception as e:
        logger.warning("Failed to fetch info for %s: %s", ticker, e)
        return None

    if not info or not info.get("quoteType"):
        return None

    sector = str(info.get("sector") or "")
    industry = str(info.get("industry") or "")
    # Coerce every numeric field defensively — yfinance occasionally returns
    # strings, NaN, or bools that would otherwise raise TypeError when compared.
    market_cap = _safe_num(info.get("marketCap"))
    trailing_eps = _safe_num(info.get("trailingEPS"))
    profit_margins = _safe_num(info.get("profitMargins"))
    net_income = _safe_num(info.get("netIncomeToCommon"))
    from tradingagents.screening.discovery import normalize_dividend_yield

    dividend_yield = normalize_dividend_yield(info.get("dividendYield"))
    beta = _safe_num(info.get("beta"))

    market_cap_tier = classify_market_cap(market_cap)
    # Profitability: trailingEPS is primary, netIncome and profitMargins as fallback.
    # trailingPE intentionally excluded — unreliable (can be positive with negative EPS
    # depending on data source; doesn't distinguish GAAP vs non-GAAP).
    is_profitable = (
        (trailing_eps is not None and trailing_eps > 0)
        or (net_income is not None and net_income > 0)
        or (profit_margins is not None and profit_margins > 0)
    )
    # Treat only a plausible recurring yield as a dividend. Anomalous prints
    # (e.g. 182% from a percent-already-multiplied Yahoo field) must not
    # route the name into dividend_income.
    has_dividend = dividend_yield is not None and 0 < dividend_yield <= 0.15
    beta_tier = classify_beta(beta)
    asset_class = classify_asset_class(info)
    country = str(info.get("country") or "").strip() or None

    profile, preset = resolve_profile_and_preset(
        sector, market_cap_tier, is_profitable, has_dividend, beta_tier,
        asset_class=asset_class,
        industry=industry,
    )

    # Exchange resolution is used for TradingView export determinism.
    exchange_res = resolve_exchange_from_info(ticker.upper(), info)

    return {
        "ticker": ticker.upper(),
        "sector": sector,
        "industry": industry,
        # market_cap stored as int|None so downstream sorts are type-safe.
        "market_cap": int(market_cap) if market_cap is not None else None,
        "market_cap_tier": market_cap_tier,
        "is_profitable": is_profitable,
        "has_dividend": has_dividend,
        "beta": beta,
        "beta_tier": beta_tier,
        "resolved_profile": profile,
        "resolved_preset": preset,
        "asset_class": asset_class,
        "country": country,
        "primary_exchange": exchange_res.get("exchange") or "",
        "exchange_source": exchange_res.get("source"),
        "exchange_confidence": exchange_res.get("confidence"),
        "raw_exchange": exchange_res.get("raw_exchange"),
        "exchange_source_timestamp": exchange_res.get("source_timestamp"),
    }


# ---------------------------------------------------------------------------
# Bulk resolution with caching
# ---------------------------------------------------------------------------

def _metadata_row_complete(row: Dict) -> bool:
    preset = str(row.get("resolved_preset") or "").strip()
    tier = str(row.get("market_cap_tier") or "").strip()
    return bool(preset and tier)


def _metadata_row_within_days(row: Dict, max_age_days: int) -> bool:
    last = row.get("last_updated")
    if not last:
        return False
    try:
        from datetime import datetime, timedelta

        cutoff = datetime.now() - timedelta(days=max(1, int(max_age_days)))
        raw = str(last).replace("Z", "")[:19]
        updated = datetime.fromisoformat(raw)
        return updated >= cutoff
    except Exception:
        return False


def resolve_and_cache(
    tickers: List[str],
    db,
    max_workers: int = 8,
    max_age_days: int = 7,
    total_budget_seconds: Optional[float] = None,
    per_ticker_timeout_seconds: float = 30.0,
    refresh_stale: bool = True,
    cached_max_age_days: Optional[int] = None,
) -> Dict[str, Dict]:
    """Resolve metadata for a list of tickers, using DB cache for fresh entries.

    Args:
        tickers: list of ticker symbols
        db: ResearchDatabase instance
        max_workers: concurrency for yfinance calls
        max_age_days: how old cached data can be before re-fetching
        total_budget_seconds: optional wall-clock budget; once exceeded any
            in-flight futures are abandoned (their threads are allowed to
            leak rather than block the caller — yfinance can't be cancelled
            cleanly). Tickers not yet resolved are simply not cached.
        per_ticker_timeout_seconds: best-effort per-future timeout (only
            applied while we're still within the total budget).

    Returns:
        {TICKER: metadata_dict} for all successfully resolved tickers.
    """
    if not tickers or not db:
        return {}

    upper = list({t.upper() for t in tickers if t and t.strip()})

    cached = db.get_ticker_metadata_bulk(upper)
    result: Dict[str, Dict] = {}

    if refresh_stale:
        stale = set(db.get_stale_tickers(upper, max_age_days=max_age_days))
        result = {t: cached[t] for t in upper if t in cached and t not in stale}
    else:
        cap = int(cached_max_age_days if cached_max_age_days is not None else 30)
        for t in upper:
            row = cached.get(t)
            if row and _metadata_row_complete(row) and _metadata_row_within_days(row, cap):
                result[t] = row

    # Resolve stale/missing
    to_fetch = [t for t in upper if t not in result]
    if not to_fetch:
        return result

    logger.info("Resolving metadata for %d tickers (%d cached, %d stale/missing)",
                len(upper), len(result), len(to_fetch))

    def _fetch_one(ticker):
        return resolve_ticker_metadata(ticker)

    t0 = time.monotonic()
    deadline = (t0 + total_budget_seconds) if total_budget_seconds else None
    timed_out_count = 0

    # NOTE: we explicitly do NOT use a context manager here so we can bail
    # out without waiting on hung worker threads (which yfinance can leave
    # blocked indefinitely on a stalled socket).
    pool = ThreadPoolExecutor(max_workers=min(max_workers, len(to_fetch)))
    futures: Dict[Any, str] = {}
    try:
        futures = {pool.submit(_fetch_one, t): t for t in to_fetch}
        for future in as_completed(futures):
            ticker = futures[future]
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "resolve_and_cache: total budget (%.0fs) exhausted with %d ticker(s) unresolved",
                    total_budget_seconds,
                    sum(1 for f in futures if not f.done()),
                )
                break
            try:
                # Apply a per-future wall clock — never wait longer than
                # what's left of the budget (if a budget is set).
                if deadline is not None:
                    timeout = max(1.0, min(per_ticker_timeout_seconds, deadline - time.monotonic()))
                else:
                    timeout = per_ticker_timeout_seconds
                meta = future.result(timeout=timeout)
                if meta:
                    # Save classification metadata (DB contract expects specific keys).
                    db.save_ticker_metadata(
                        ticker=meta.get("ticker"),
                        sector=meta.get("sector"),
                        industry=meta.get("industry"),
                        market_cap=meta.get("market_cap"),
                        market_cap_tier=meta.get("market_cap_tier"),
                        is_profitable=meta.get("is_profitable"),
                        has_dividend=meta.get("has_dividend"),
                        beta=meta.get("beta"),
                        beta_tier=meta.get("beta_tier"),
                        resolved_profile=meta.get("resolved_profile"),
                        resolved_preset=meta.get("resolved_preset"),
                        asset_class=meta.get("asset_class"),
                        country=meta.get("country"),
                    )
                    # Save primary exchange resolution for TradingView exports.
                    if meta.get("primary_exchange"):
                        db.save_ticker_exchange_resolution(
                            ticker=ticker,
                            primary_exchange=meta.get("primary_exchange"),
                            exchange_source=meta.get("exchange_source"),
                            exchange_confidence=meta.get("exchange_confidence"),
                            raw_exchange=meta.get("raw_exchange"),
                            source_timestamp=meta.get("exchange_source_timestamp"),
                        )
                    result[ticker] = meta
            except FuturesTimeoutError:
                timed_out_count += 1
                logger.debug("Metadata resolution timed out for %s; leaving worker leaked", ticker)
            except Exception as e:
                logger.warning("Metadata resolution failed for %s: %s", ticker, e)
    finally:
        # Don't wait on stuck workers — they will eventually die at the
        # socket layer. cancel_futures was added in 3.9; fall back for
        # earlier Pythons.
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=False)

    if timed_out_count:
        logger.warning("resolve_and_cache: %d ticker(s) timed out", timed_out_count)

    return result
