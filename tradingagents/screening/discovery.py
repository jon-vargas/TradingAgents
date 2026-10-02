"""Shared ticker-discovery catalog, prompt packs, and listing gates.

Discover (manual) and auto-discovery both consume this catalog so operator
chips, Perplexity prompts, and Yahoo listing checks stay aligned with the
visible screening books. This module does not call Perplexity or Yahoo.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z]{1,2})?$")
TICKER_FIND_RE = re.compile(r"\b([A-Z]{1,5}(?:[.-][A-Z]{1,2})?)\b")

US_EQUITY_EXCHANGES = {
    "NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS",
    "NYSE", "NASDAQ", "NYSE ARCA", "NYSE AMERICAN", "AMEX", "BATS", "CBOE",
}
OTC_EXCHANGES = {"PNK", "OTC", "OQB", "OCB", "OBB", "GREY", "OTCPK", "OTCBB", "PINK"}

TICKER_STOPWORDS = {
    "NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "CBOE",
    "ETF", "ETFS", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FTC",
    "USD", "JSON", "NULL", "TRUE", "FALSE", "HTTP", "HTTPS", "WWW",
    "THE", "AND", "FOR", "NOT", "ARE", "BUT", "WITH", "HAS", "WAS",
    "ALL", "ANY", "CAN", "HER", "ONE", "OUR", "OUT", "THIS", "THAT",
    "FROM", "HAVE", "BEEN", "WILL", "INTO", "OVER", "UNDER",
    "AI", "GPU", "CPU", "CHIP", "CHIPS", "STOCK", "STOCKS", "SHARE",
    "SHARES", "FUND", "FUNDS", "BOND", "BONDS", "REIT", "REITS",
    "ADR", "ADRS", "OTC", "US", "USA", "UK", "EU", "GDP", "EPS",
    "YOY", "QOQ", "TTM", "NTM", "PE", "PS", "EV", "EBITDA",
    "FDA", "PDUFA", "NDA", "BLA", "IRA", "LNG", "EV",
}

_MCAP_RE = re.compile(
    r"(under|over|above|below)?\s*\$?\s*([\d.]+)\s*([MBT])\b",
    re.IGNORECASE,
)

# Mega-cap names that pollute hoppers. Income chips do NOT use this list
# (PG/JNJ/WMT are valid growers); they use TOKEN_DIVIDEND_TICKERS + yield floor.
HOUSEHOLD_MEGA_TICKERS = frozenset({
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "TSLA",
    "AVGO", "BRK.A", "BRK.B", "LLY", "JPM", "V", "MA", "UNH",
    "XOM", "JNJ", "WMT", "ORCL", "NFLX", "COST", "HD", "PG",
    "PEP", "KO", "TSM", "BABA", "ASML", "NVO", "NTES", "SNY", "BIDU", "PDD",
    "ABBV", "GILD", "MMM", "MRK", "PFE", "AMGN", "AMD", "MU",
})
TOKEN_DIVIDEND_TICKERS = frozenset({
    "NVDA", "TSLA", "AMZN", "GOOGL", "GOOG", "META", "NFLX", "AMD", "PLTR",
})
LEVERAGED_ETF_TICKERS = frozenset({
    "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXL", "SPXS", "SOXL", "SOXS",
    "TNA", "TZA", "FNGU", "FNGD", "LABU", "LABD", "NUGT", "DUST",
    "BOIL", "KOLD", "TBT", "TMF", "QLD", "QID", "SSO", "SDS",
})
_LEVERAGED_ETF_NAME_RE = re.compile(
    r"(ultrapro|ultra pro|direxion daily|\b3x\b|\b2x\b|inverse |"
    r"leveraged |-3x|-2x|times bull|times bear)",
    re.IGNORECASE,
)
MIN_INCOME_YIELD = 0.008  # 0.80% — incidental penny dividends fail
VALID_FIT_POLICIES = frozenset({"", "household", "income", "etf", "momentum"})
VALID_PROMPT_PACKS = frozenset({
    "equity_search", "etf_search", "squeeze_flow", "income_quality", "flow_search",
    "momentum_breakout",
})
# Liquid names that pollute "early momentum" hoppers but are not MAG7.
MOMENTUM_SKIP_TICKERS = HOUSEHOLD_MEGA_TICKERS | frozenset({
    "SNAP", "AMD", "INTC", "SMCI", "DELL", "PINS", "HOOD", "SOFI", "PLTR", "RIVN", "LCID",
})
EXTENDED_YEAR_CHANGE = 1.00  # +100% over 52w and near the high → already ran
PARABOLIC_YEAR_CHANGE = 1.50  # +150% over 52w regardless of pullback
NEAR_HIGH_FRAC = 0.08
CAP_BAND_HUG_FRAC = 0.90  # ranged filters: reject names sitting on the ceiling


@dataclass(frozen=True)
class DiscoveryTemplate:
    id: str
    label: str
    group: str
    theme: str
    criteria: str
    market_cap_filter: str
    preset: str
    profile: str
    prompt_pack: str
    asset_policy: str  # equity | etf | adr | any
    fit_policy: str = ""  # "" | household | income | etf | momentum
    sector_allowlist: Tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


DISCOVERY_TEMPLATES: Tuple[DiscoveryTemplate, ...] = (
    DiscoveryTemplate(
        id="micro_momentum",
        label="Micro momentum",
        group="Momentum / growth",
        theme="US-listed micro-cap operating companies showing early momentum: rising relative strength, volume expansion, or a fresh breakout from a multi-week base",
        criteria="Prefer $50M–$250M NYSE/NASDAQ operators. Speculative listed names are OK; mix sectors. Skip OTC, SPACs, empty shells under $25M, anything already mid-cap, and names that have already doubled over the past year. Do not invent RSI or percent moves.",
        market_cap_filter="over $25M under $300M",
        preset="momentum_hunter",
        profile="high_growth",
        prompt_pack="momentum_breakout",
        asset_policy="equity",
        fit_policy="momentum",
        description="Micro-cap hopper ($25M–$300M). Add New names, then Scan mode → Early momentum → Scan All → Momentum tab.",
    ),
    DiscoveryTemplate(
        id="small_cap_momentum",
        label="Small-cap momentum",
        group="Momentum / growth",
        theme="US-listed small-cap operating companies showing early momentum: rising relative strength, volume expansion, or a fresh breakout from a multi-week base",
        criteria="Prefer $500M–$2B listed operators that are not household names. Skip OTC, SPACs, names that already re-rated through $2B, and names that have already doubled over the past year. Do not invent RSI or percent moves.",
        market_cap_filter="over $500M under $2B",
        preset="momentum_hunter",
        profile="high_growth",
        prompt_pack="momentum_breakout",
        asset_policy="equity",
        fit_policy="momentum",
        description="Small-cap hopper ($500M–$2B). Score with Early momentum (not Opportunity composite).",
    ),
    DiscoveryTemplate(
        id="mid_cap_momentum",
        label="Mid-cap momentum",
        group="Momentum / growth",
        theme="US-listed mid-cap operating companies showing early momentum: rising relative strength, volume expansion, or a fresh breakout from a multi-week base",
        criteria="Prefer $2B–$8B names that are not already household or liquid mid-caps (SNAP, AMD). Skip names that already live as large-cap or have already doubled over the past year. Do not invent RSI or percent moves.",
        market_cap_filter="over $2B under $8B",
        preset="momentum_hunter",
        profile="high_growth",
        prompt_pack="momentum_breakout",
        asset_policy="equity",
        fit_policy="momentum",
        description="Mid-cap hopper ($2B–$8B). Add to a list, then Score with Early momentum or Scan All → Momentum.",
    ),
    DiscoveryTemplate(
        id="ai_infrastructure",
        label="AI infrastructure",
        group="Momentum / growth",
        theme="US-listed AI, cloud, and data-center infrastructure companies with accelerating demand",
        criteria="Prefer names with recent enterprise wins, capacity expansion, or capex-cycle exposure in the last 6 months; real revenue over pre-revenue stories",
        market_cap_filter="under $10B",
        preset="momentum_hunter",
        profile="high_growth",
        prompt_pack="equity_search",
        asset_policy="equity",
        fit_policy="household",
        description="Small/mid AI and cloud infrastructure for the Momentum hunter book",
    ),
    DiscoveryTemplate(
        id="semiconductor_ai",
        label="Semiconductor AI capex",
        group="Momentum / growth",
        theme="Semiconductor designers, equipment, packaging, and EDA names tied to the AI capex cycle",
        criteria="Fabless, foundry equipment, advanced packaging, or EDA; named product or customer catalyst in the last 6 months",
        market_cap_filter="under $10B",
        preset="momentum_hunter",
        profile="high_growth",
        prompt_pack="equity_search",
        asset_policy="equity",
        fit_policy="household",
    ),
    DiscoveryTemplate(
        id="biotech_catalysts",
        label="Biotech catalysts",
        group="Momentum / growth",
        theme="Biotech and specialty pharma with dated FDA or clinical catalysts in the next 6 months",
        criteria="Phase 2/3 readouts, PDUFA dates, or NDA/BLA filings; prefer orphan or first-in-class; skip bankrupt shells and sub-$25M names",
        market_cap_filter="over $25M under $5B",
        preset="earnings_play",
        profile="high_growth",
        prompt_pack="equity_search",
        asset_policy="equity",
        fit_policy="household",
        sector_allowlist=("Healthcare",),
    ),
    DiscoveryTemplate(
        id="defense_space",
        label="Defense and space",
        group="Momentum / growth",
        theme="Defense, satellite, launch, and dual-use space companies with recent government awards",
        criteria="Named contract, program, or budget-line catalyst in the last 6 months",
        market_cap_filter="under $10B",
        preset="momentum_hunter",
        profile="high_growth",
        prompt_pack="equity_search",
        asset_policy="equity",
        fit_policy="household",
    ),
    DiscoveryTemplate(
        id="earnings_window",
        label="Earnings window",
        group="Earnings / flow",
        theme="US-listed companies with a dated earnings print in the next 21 days or a fresh beat-and-raise in the last 30 days",
        criteria="Named report date or PEAD setup. Prefer $1B–$8B operators with coverage, not S&P 100. Skip MAG7, WMT, COST, and other household mega-caps. Never pad with out-of-band names. Skip silent microcaps with no coverage.",
        market_cap_filter="under $10B",
        preset="earnings_play",
        profile="high_growth",
        prompt_pack="equity_search",
        asset_policy="equity",
        fit_policy="household",
        description="Event-risk names for the Earnings play book",
    ),
    DiscoveryTemplate(
        id="value_revisions",
        label="Value with revisions",
        group="Value / quality / income",
        theme="Undervalued US-listed companies with upward estimate or rating revisions",
        criteria="Cheap vs history or peers plus improving revisions; avoid value traps with collapsing sales",
        market_cap_filter="",
        preset="value_fisher",
        profile="large_cap_core",
        prompt_pack="income_quality",
        asset_policy="equity",
        fit_policy="household",
    ),
    DiscoveryTemplate(
        id="quality_compounders",
        label="Quality compounders",
        group="Value / quality / income",
        theme="High-quality compounders with stable revisions and durable returns on capital",
        criteria="Pricing power, high ROIC or margins, and light valuation support; skip serial diluters",
        market_cap_filter="over $2B",
        preset="quality_compounder",
        profile="large_cap_core",
        prompt_pack="income_quality",
        asset_policy="equity",
        fit_policy="",
    ),
    DiscoveryTemplate(
        id="dividend_growers",
        label="Dividend growers",
        group="Value / quality / income",
        theme="Dividend growers with covered payouts and multi-year dividend growth",
        criteria="Payout funded by free cash flow; 5+ years of dividend growth preferred; skip REITs with unsustainable yields and token-dividend growth names",
        market_cap_filter="over $5B",
        preset="dividend_income",
        profile="dividend_income",
        prompt_pack="income_quality",
        asset_policy="equity",
        fit_policy="income",
    ),
    DiscoveryTemplate(
        id="long_horizon",
        label="Long horizon (12–36m)",
        group="Value / quality / income",
        theme="Cheap quality names with revision support suitable for a 12–36 month hold",
        criteria="Valuation gap plus estimate stability and quality; de-emphasize next-week catalysts",
        market_cap_filter="over $2B",
        preset="long_horizon_12to36m",
        profile="large_cap_core",
        prompt_pack="income_quality",
        asset_policy="equity",
        fit_policy="",
    ),
    DiscoveryTemplate(
        id="energy_producers",
        label="Energy producers",
        group="Cyclical / squeeze",
        theme="Energy producers and oil-field services with rising production or insider buying",
        criteria="Prefer $500M–$8B Permian, natural gas, or LNG operators; skip pre-revenue explorers and $15B+ names",
        market_cap_filter="under $10B",
        preset="commodity_cyclical",
        profile="commodity_cyclical",
        prompt_pack="equity_search",
        asset_policy="equity",
        fit_policy="household",
        sector_allowlist=("Energy",),
    ),
    DiscoveryTemplate(
        id="critical_minerals",
        label="Critical minerals",
        group="Cyclical / squeeze",
        theme="Critical mineral miners and processors for power, EVs, and defense supply chains",
        criteria="Copper, uranium, lithium, rare earths, or silver with producing assets or near-term production",
        market_cap_filter="under $10B",
        preset="commodity_cyclical",
        profile="commodity_cyclical",
        prompt_pack="equity_search",
        asset_policy="equity",
        fit_policy="household",
        sector_allowlist=("Energy", "Basic Materials"),
    ),
    DiscoveryTemplate(
        id="squeeze_setups",
        label="Squeeze setups",
        group="Cyclical / squeeze",
        theme="High short-interest US-listed names with rising volume and a fresh catalyst",
        criteria="Elevated short float, volume expansion, and a dated catalyst in the last 6 months; skip OTC shells",
        market_cap_filter="under $10B",
        preset="short_squeeze",
        profile="momentum_speculative",
        prompt_pack="squeeze_flow",
        asset_policy="equity",
        fit_policy="household",
    ),
    DiscoveryTemplate(
        id="smart_money_flow",
        label="Smart money flow",
        group="Earnings / flow",
        theme="US-listed names with clustered insider buying, notable 13F adds, or unusual options activity in the last quarter",
        criteria="Prefer Form 4 clusters or a named 13F/options print; do not invent percentages; skip mega-cap household names",
        market_cap_filter="under $10B",
        preset="smart_money_tracker",
        profile="high_growth",
        prompt_pack="flow_search",
        asset_policy="equity",
        fit_policy="household",
        description="Insider/13F/options flow for the Smart money book",
    ),
    DiscoveryTemplate(
        id="etf_factors",
        label="Factor and theme ETFs",
        group="Vehicles",
        theme="US-listed ETFs for factor, sector, or thematic exposure the desk does not already cover well",
        criteria="Liquid ETFs with a clear factor or theme mandate; skip inverse and 3x products unless the theme names them",
        market_cap_filter="",
        preset="etf_technical",
        profile="etf_baseline",
        prompt_pack="etf_search",
        asset_policy="etf",
        fit_policy="etf",
    ),
    DiscoveryTemplate(
        id="adr_leaders",
        label="ADR leaders",
        group="Vehicles",
        theme="Liquid US-listed ADRs in semis, China tech, Japan industrials, European pharma, or Canada",
        criteria="Primary NYSE/NASDAQ ADR, not OTC pinks; prefer operating companies with a 6-month catalyst",
        market_cap_filter="",
        preset="momentum_hunter",
        profile="large_cap_core",
        prompt_pack="equity_search",
        asset_policy="adr",
        fit_policy="household",
    ),
)

_TEMPLATE_BY_ID = {t.id: t for t in DISCOVERY_TEMPLATES}

PROMPT_PACKS: Dict[str, Dict[str, str]] = {
    "equity_search": {
        "system": (
            "You are a US-listed equity ticker discovery tool for a research desk. "
            "Return ONLY a JSON array. No markdown fences, no prose before or after the array."
        ),
        "rules": (
            "- Only NYSE, NASDAQ, or NYSE American common stock (or the ADR class if the theme asks for ADRs)\n"
            "- Primary ticker only; no warrants, units, preferreds, OTC/pinks, or indexes\n"
            "- Prefer operating companies with revenue or a dated catalyst in the last 6 months\n"
            "- Prefer under-followed small/mid names over household mega-caps "
            "(do not return NVDA, AAPL, MSFT, AMZN, GOOGL, META, TSLA, AVGO unless the theme names that issuer)\n"
            "- Honor the market-cap band exactly; omit a name rather than stretching the band\n"
            "- Return only in-band names; never pad with mega-caps to fill the list\n"
            "- If unsure of the ticker, omit the name rather than guessing"
        ),
    },
    "momentum_breakout": {
        "system": (
            "You are a US-listed small/mid-cap momentum discovery tool. "
            "Find names showing early tape strength before a large-cap re-rate. "
            "Return ONLY a JSON array. No markdown fences, no prose."
        ),
        "rules": (
            "- Only NYSE/NASDAQ/NYSE American common stock of operating companies\n"
            "- No OTC, SPACs, warrants, funds, CEFs, ETFs, or REITs\n"
            "- Return only names inside the market-cap band. 5-12 in-band names is enough; "
            "never pad with out-of-band or mega-cap names to hit a count\n"
            "- Honor the market-cap band exactly; prefer the lower half of the band; "
            "do not pad with names sitting just under the ceiling\n"
            "- Prefer early momentum: rising relative strength, volume expansion, or a breakout "
            "from a multi-week base\n"
            "- Skip names that have already doubled over the past year or are sitting on a 52-week "
            "high after a large run — those already exploded\n"
            "- Skip household mega-caps and liquid mid-caps (NVDA, AAPL, MSFT, AMZN, META, TSLA, "
            "SNAP, AMD, SMCI, HOOD, PLTR)\n"
            "- Mix sectors across industrials, healthcare, tech, consumer, and energy; "
            "do not fill the list with one theme unless the operator theme requires it\n"
            "- Do not invent RSI, volume multiples, or percent moves; keep the rationale qualitative\n"
            "- If unsure of the ticker, omit it"
        ),
    },
    "etf_search": {
        "system": (
            "You are a US-listed ETF discovery tool for a research desk. "
            "Return ONLY a JSON array. No markdown fences, no prose."
        ),
        "rules": (
            "- Only US-listed ETFs (NYSE Arca, NASDAQ, CBOE BZX)\n"
            "- Use the fund ticker, not a holding inside the fund\n"
            "- Prefer liquid, plain-vanilla or single-theme ETFs; skip illiquid and single-stock ETFs\n"
            "- Skip inverse, 2x, and 3x products unless the theme names them\n"
            "- Prefer vehicles the desk may not already cover (not just SPY/QQQ/SMH)\n"
            "- Sort by relevance to the theme"
        ),
    },
    "squeeze_flow": {
        "system": (
            "You are a short-interest and flow ticker discovery tool. "
            "Return ONLY a JSON array. No markdown fences, no prose."
        ),
        "rules": (
            "- Only NYSE/NASDAQ listed common stock; no OTC shells\n"
            "- Prefer elevated short interest, rising volume, or options activity plus a dated catalyst\n"
            "- Do not invent short-interest percentages; keep the rationale qualitative if you lack a figure\n"
            "- Skip household mega-caps (NVDA, AAPL, MSFT, AMZN, TSLA, META)\n"
            "- Honor the market-cap band exactly\n"
            "- Return only in-band names; never pad with mega-caps to fill the list\n"
            "- Sort by relevance to a squeeze/flow setup"
        ),
    },
    "flow_search": {
        "system": (
            "You are an insider, 13F, and options-flow ticker discovery tool. "
            "Return ONLY a JSON array. No markdown fences, no prose."
        ),
        "rules": (
            "- Only NYSE/NASDAQ listed common stock; no OTC shells\n"
            "- Prefer clustered Form 4 buying, a named 13F add, or unusual options activity in the last quarter\n"
            "- Do not invent share counts or percentages; keep the rationale qualitative if you lack a figure\n"
            "- Skip household mega-caps unless the filing is about that issuer\n"
            "- Honor the market-cap band exactly\n"
            "- Return only in-band tickers (no suffixes like _SCN); never pad with mega-caps"
        ),
    },
    "income_quality": {
        "system": (
            "You are a quality/value/income ticker discovery tool for a 6–36 month research desk. "
            "Return ONLY a JSON array. No markdown fences, no prose."
        ),
        "rules": (
            "- Only NYSE/NASDAQ listed common stock\n"
            "- Prefer durable cash generation, covered payouts, or cheap-plus-revisions setups\n"
            "- Skip yield traps, serial diluters, and pre-revenue stories\n"
            "- For dividend/income themes: skip token-dividend growth names "
            "(NVDA, TSLA, AMZN, GOOGL, META, NFLX, AMD, PLTR); require a real income component\n"
            "- Prefer under-followed compounders over padding with MAG7\n"
            "- Honor any market-cap band; never pad with mega-caps to fill the list\n"
            "- Skip REITs, CEFs, and funds unless the theme asks for them\n"
            "- Sort by relevance to the theme, not next-week momentum"
        ),
    },
}


def list_discovery_templates() -> List[Dict[str, Any]]:
    return [t.to_dict() for t in DISCOVERY_TEMPLATES]


def get_discovery_template(template_id: Optional[str]) -> Optional[DiscoveryTemplate]:
    if not template_id:
        return None
    return _TEMPLATE_BY_ID.get(str(template_id).strip())


def parse_market_cap_filter(raw: Optional[str]) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """Return (min_cap, max_cap) in USD, or None when unconstrained.

    Accepts ``under $2B``, ``over $500M``, combined ranges
    (``over $50M under $500M``), and dash ranges (``$500M-$2B``).
    """
    text = str(raw or "").strip()
    if not text or text.lower() in {"any", "none", "all"}:
        return None
    cleaned = text.replace(",", "").replace("–", "-").replace("—", "-")
    scale = {"M": 1e6, "B": 1e9, "T": 1e12}

    lo: Optional[float] = None
    hi: Optional[float] = None

    range_match = re.search(
        r"(?:between\s+)?\$?\s*([\d.]+)\s*([MBT])\s*(?:-|to|and)\s*\$?\s*([\d.]+)\s*([MBT])",
        cleaned,
        flags=re.IGNORECASE,
    )
    if range_match:
        v1 = float(range_match.group(1)) * scale[range_match.group(2).upper()]
        v2 = float(range_match.group(3)) * scale[range_match.group(4).upper()]
        lo, hi = (min(v1, v2), max(v1, v2))

    matches = list(_MCAP_RE.finditer(cleaned))
    for match in matches:
        direction = (match.group(1) or "").lower()
        value = float(match.group(2)) * scale[match.group(3).upper()]
        if direction in {"over", "above"}:
            lo = value if lo is None else max(lo, value)
        elif direction in {"under", "below"}:
            hi = value if hi is None else min(hi, value)
        elif not direction and len(matches) == 1 and lo is None and hi is None:
            hi = value

    if lo is None and hi is None:
        return None
    return (lo, hi)


def hugging_top_of_band(
    market_cap: Optional[float],
    bounds: Optional[Tuple[Optional[float], Optional[float]]],
    *,
    frac: float = CAP_BAND_HUG_FRAC,
) -> bool:
    """True when both ends of a range exist and cap sits on the ceiling."""
    if bounds is None or market_cap is None:
        return False
    lo, hi = bounds
    if lo is None or hi is None or hi <= 0:
        return False
    return market_cap > frac * hi


def market_cap_in_bounds(market_cap: Optional[float], bounds: Optional[Tuple[Optional[float], Optional[float]]]) -> bool:
    if bounds is None or market_cap is None:
        return bounds is None or market_cap is not None
    lo, hi = bounds
    if lo is not None and market_cap < lo:
        return False
    if hi is not None and market_cap > hi:
        return False
    return True


WATCHLIST_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")

# Opportunity blender: hunter books keep the tape (Score), not the
# EQ-stripped fundamental composite. MA≈0 is dampened on these books.
HUNTER_PRESETS = frozenset({
    "momentum_hunter",
    "small_cap_growth",
    "short_squeeze",
    "movers_swing_1to5d",
})
PRESET_BOOK_ALIASES = {
    "small_cap_growth": "momentum_hunter",
    "long_horizon_6to12m": "long_horizon_12to36m",
}


def canonical_preset_name(name: Any) -> str:
    key = str(name or "").strip()
    return PRESET_BOOK_ALIASES.get(key, key)


def normalize_ticker_symbol(raw: Any) -> str:
    return str(raw or "").strip().upper().replace(" ", "")


def is_plausible_ticker(symbol: str) -> bool:
    return bool(symbol and TICKER_RE.match(symbol))


def parse_watchlist_tickers(raw: Any) -> Tuple[List[str], List[str]]:
    """Split a watchlist payload into (valid, rejected) symbols.

    Does not glue internal spaces: ``NB SYY`` is rejected, not ``NBSYY``.
    """
    if isinstance(raw, (list, tuple)):
        tokens = [str(t) for t in raw]
    else:
        text = str(raw or "").replace("\n", ",")
        tokens = text.split(",")
    valid: List[str] = []
    rejected: List[str] = []
    seen = set()
    for token in tokens:
        original = str(token or "").strip()
        if not original:
            continue
        if any(ch.isspace() for ch in original):
            rejected.append(original)
            continue
        sym = original.upper()
        if not WATCHLIST_TICKER_RE.match(sym):
            rejected.append(original)
            continue
        if sym in seen:
            continue
        seen.add(sym)
        valid.append(sym)
    return valid, rejected


def is_implausible_mega_identity(
    ticker: str,
    market_cap: Any = None,
    market_cap_tier: str = "",
) -> bool:
    """True when Yahoo mapped a $1T+ mega onto a non-household ticker (SPCX/SpaceX)."""
    sym = str(ticker or "").strip().upper()
    if not sym:
        return False
    # Yahoo uses BRK-B; the household list stores BRK.B.
    household_key = sym.replace("-", ".")
    if sym in HOUSEHOLD_MEGA_TICKERS or household_key in HOUSEHOLD_MEGA_TICKERS:
        return False
    try:
        cap = float(market_cap or 0)
    except (TypeError, ValueError):
        cap = 0.0
    if cap >= 1_000_000_000_000:
        return True
    return False


def extract_ticker_candidates(text: str, max_results: int = 20) -> List[str]:
    """Conservative symbol harvest when Perplexity does not return JSON."""
    found: List[str] = []
    seen = set()
    for match in TICKER_FIND_RE.findall(text or ""):
        sym = normalize_ticker_symbol(match)
        if not is_plausible_ticker(sym) or sym in TICKER_STOPWORDS or sym in seen:
            continue
        seen.add(sym)
        found.append(sym)
        if len(found) >= max_results:
            break
    return found


def _exchange_code(info: Dict[str, Any]) -> str:
    raw = (
        info.get("exchange")
        or info.get("fullExchangeName")
        or info.get("exchangeName")
        or ""
    )
    return str(raw).strip().upper()


def evaluate_listing(
    info: Optional[Dict[str, Any]],
    *,
    asset_policy: str = "equity",
    mcap_bounds: Optional[Tuple[Optional[float], Optional[float]]] = None,
) -> Dict[str, Any]:
    """Pure listing gate. ``info`` is a yfinance-like dict."""
    payload = dict(info or {})
    quote_type = str(payload.get("quoteType") or "").strip().upper()
    exchange = _exchange_code(payload)
    name = payload.get("shortName") or payload.get("longName") or ""
    market_cap = payload.get("marketCap")
    try:
        market_cap_f = float(market_cap) if market_cap is not None else None
    except (TypeError, ValueError):
        market_cap_f = None

    result = {
        "ok": False,
        "reason": "",
        "quote_type": quote_type,
        "exchange": exchange,
        "company": str(name or ""),
        "market_cap": market_cap_f,
    }
    if not payload:
        result["reason"] = "no listing data"
        return result
    if not name:
        result["reason"] = "missing name"
        return result
    if exchange in OTC_EXCHANGES:
        result["reason"] = f"OTC listing ({exchange})"
        return result

    policy = (asset_policy or "equity").strip().lower()
    if policy == "etf":
        if quote_type != "ETF":
            result["reason"] = f"not an ETF ({quote_type or 'unknown'})"
            return result
    elif policy == "adr":
        if quote_type != "EQUITY":
            result["reason"] = f"not an equity ADR ({quote_type or 'unknown'})"
            return result
        if exchange and exchange not in US_EQUITY_EXCHANGES:
            result["reason"] = f"not a US listing ({exchange})"
            return result
        country = str(payload.get("country") or "").strip().lower()
        if country in {"united states", "usa", "u.s.", "u.s.a.", "us"}:
            result["reason"] = "US domestic, not ADR"
            return result
    elif policy == "any":
        if quote_type not in {"EQUITY", "ETF"}:
            result["reason"] = f"unsupported type ({quote_type or 'unknown'})"
            return result
    else:
        if quote_type != "EQUITY":
            result["reason"] = f"not an equity ({quote_type or 'unknown'})"
            return result
        if exchange and exchange not in US_EQUITY_EXCHANGES:
            result["reason"] = f"not a US listing ({exchange})"
            return result

    if mcap_bounds is not None:
        if market_cap_f is None:
            result["reason"] = "missing market cap for filter"
            return result
        if not market_cap_in_bounds(market_cap_f, mcap_bounds):
            result["reason"] = "outside market-cap filter"
            return result
        if hugging_top_of_band(market_cap_f, mcap_bounds):
            result["reason"] = "hugging top of cap band"
            return result

    result["ok"] = True
    return result


def normalize_dividend_yield(raw: Any) -> Optional[float]:
    """Return yield as a 0–1 fraction. yfinance may send 0.012 or 1.2."""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value > 1.0:
        value = value / 100.0
    if value > 1.0:
        return None
    return value


def infer_fit_policy(
    template: Optional[DiscoveryTemplate] = None,
    *,
    prompt_pack: str = "",
    asset_policy: str = "",
    theme: str = "",
) -> str:
    """Resolve book-fit policy from a chip, else from pack/theme."""
    if template and template.fit_policy:
        return template.fit_policy
    pack = (prompt_pack or "").strip()
    policy = (asset_policy or "").strip().lower()
    if policy == "etf" or pack == "etf_search":
        return "etf"
    if pack in {"equity_search", "squeeze_flow", "flow_search"}:
        return "household"
    if pack == "momentum_breakout":
        return "momentum"
    text = f"{theme or ''}".lower()
    if pack == "income_quality" and any(k in text for k in ("dividend", "yield", "income")):
        return "income"
    return ""


def year_change_frac(info: Optional[Dict[str, Any]]) -> Optional[float]:
    """52-week change as a fraction (1.0 = +100%). Fail-open on missing data."""
    payload = dict(info or {})
    for key in ("fiftyTwoWeekChangePercent", "52WeekChange", "fiftyTwoWeekChange"):
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if abs(value) > 5.0:
            value = value / 100.0
        return value
    return None


def pct_below_high(info: Optional[Dict[str, Any]]) -> Optional[float]:
    """Distance below 52-week high as a fraction (0.05 = 5% below)."""
    payload = dict(info or {})
    price = None
    for key in ("currentPrice", "regularMarketPrice", "previousClose"):
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            price = float(raw)
            break
        except (TypeError, ValueError):
            continue
    try:
        high = float(payload["fiftyTwoWeekHigh"]) if payload.get("fiftyTwoWeekHigh") not in (None, "") else None
    except (TypeError, ValueError, KeyError):
        high = None
    if not price or not high or high <= 0:
        return None
    return (high - price) / high


_FUND_NAME_RE = re.compile(r"\b(fund|etf|closed[- ]end|cefs?)\b", re.IGNORECASE)


def vehicle_reject_reason(info: Optional[Dict[str, Any]], name: str = "") -> str:
    """REIT / CEF / fund / ETF — not an operating-company hopper name."""
    payload = dict(info or {})
    quote_type = str(payload.get("quoteType") or "").strip().upper()
    if quote_type in {"ETF", "MUTUALFUND", "CLOSEDEND"}:
        return "fund/CEF/ETF"
    industry = str(payload.get("industry") or "")
    if "REIT" in industry.upper():
        return "REIT"
    blob = f"{name} {payload.get('shortName') or ''} {payload.get('longName') or ''} {industry}"
    if _FUND_NAME_RE.search(blob):
        return "fund/CEF/ETF"
    return ""


def evaluate_book_fit(
    symbol: str,
    info: Optional[Dict[str, Any]] = None,
    *,
    fit_policy: str = "",
    sector_allowlist: Sequence[str] = (),
) -> Dict[str, Any]:
    """Cheap post-listing book fit. Does not call the network."""
    result = {"ok": True, "reason": "", "fit_policy": fit_policy or ""}
    policy = (fit_policy or "").strip().lower()
    if not policy:
        return result
    sym = normalize_ticker_symbol(symbol)
    payload = dict(info or {})
    name = str(payload.get("shortName") or payload.get("longName") or "")

    if policy in {"household", "momentum"}:
        skip = MOMENTUM_SKIP_TICKERS if policy == "momentum" else HOUSEHOLD_MEGA_TICKERS
        if sym in skip:
            result["ok"] = False
            result["reason"] = "household mega-cap" if policy == "household" else "liquid/household name"
            return result
        if policy == "household":
            vehicle = vehicle_reject_reason(payload, name)
            if vehicle:
                result["ok"] = False
                result["reason"] = vehicle
                return result
            if sector_allowlist:
                sector = str(payload.get("sector") or "")
                allowed = {s.lower() for s in sector_allowlist}
                if sector.lower() not in allowed:
                    result["ok"] = False
                    result["reason"] = f"sector {sector or 'unknown'} not in theme"
                    return result
            return result
        vehicle = vehicle_reject_reason(payload, name)
        if vehicle:
            result["ok"] = False
            result["reason"] = vehicle
            return result
        change = year_change_frac(payload)
        below = pct_below_high(payload)
        if change is not None and change >= PARABOLIC_YEAR_CHANGE:
            result["ok"] = False
            result["reason"] = "already extended"
            return result
        if (
            change is not None
            and change >= EXTENDED_YEAR_CHANGE
            and below is not None
            and below <= NEAR_HIGH_FRAC
        ):
            result["ok"] = False
            result["reason"] = "already extended"
            return result
        return result

    if policy == "income":
        if sym in TOKEN_DIVIDEND_TICKERS:
            result["ok"] = False
            result["reason"] = "token-dividend growth name"
            return result
        yield_frac = normalize_dividend_yield(
            payload.get("dividendYield")
            if payload.get("dividendYield") not in (None, "")
            else payload.get("trailingAnnualDividendYield")
        )
        if yield_frac is None:
            result["ok"] = False
            result["reason"] = "missing dividend yield"
            return result
        if yield_frac < MIN_INCOME_YIELD:
            result["ok"] = False
            result["reason"] = "yield below income floor"
            return result
        return result

    if policy == "etf":
        if sym in LEVERAGED_ETF_TICKERS:
            result["ok"] = False
            result["reason"] = "leveraged or inverse ETF"
            return result
        if name and _LEVERAGED_ETF_NAME_RE.search(name):
            result["ok"] = False
            result["reason"] = "leveraged or inverse ETF"
            return result
        return result

    return result


def is_momentum_discovery_context(
    template_id: Optional[str] = None,
    *,
    fit_policy: str = "",
    prompt_pack: str = "",
) -> bool:
    """True when Discover should attach Early Momentum hopper flags (not full score)."""
    tmpl = get_discovery_template(template_id)
    if tmpl and tmpl.fit_policy == "momentum":
        return True
    if (fit_policy or "").strip().lower() == "momentum":
        return True
    if (prompt_pack or "").strip().lower() == "momentum_breakout":
        return True
    return False


def _hopper_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def annotate_momentum_hopper_flags(
    tickers: Sequence[Dict[str, Any]],
    *,
    asof_date: Optional[str] = None,
    max_workers: int = 8,
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Post-validate hopper flags for momentum Discover runs (Yahoo-only; no PPLX dilution)."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import date

    from tradingagents.screening.early_momentum import check_gates, get_momentum_config
    from tradingagents.screening.early_momentum_enrich import enrich_ticker

    cfg = get_momentum_config(config)
    scan_date = (asof_date or str(date.today()))[:10]
    rows = [dict(t) for t in tickers]

    def _annotate_one(row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        if not item.get("validated"):
            return item
        sym = normalize_ticker_symbol(item.get("ticker"))
        if not sym:
            return item

        info: Dict[str, Any] = {}
        try:
            from tradingagents.dataflows.yfinance_extended import get_ticker_info

            info = get_ticker_info(sym) or {}
        except Exception:
            pass

        price = _hopper_float(
            info.get("regularMarketPrice") or info.get("previousClose") or info.get("currentPrice"),
        )
        share_adv = _hopper_float(info.get("averageVolume"))
        dollar_adv = (share_adv * price) if share_adv is not None and price is not None else None
        meta = {
            "market_cap": info.get("marketCap") or item.get("market_cap_actual"),
            "exchange": info.get("exchange") or item.get("exchange"),
            "fullExchangeName": info.get("fullExchangeName"),
            "float_shares": info.get("floatShares"),
            "floatShares": info.get("floatShares"),
        }
        passed, gate_flags = check_gates(
            price=price,
            meta=meta,
            share_adv_50d=share_adv,
            dollar_adv_50d=dollar_adv,
            cfg=cfg,
        )
        enrich = enrich_ticker(sym, asof_date=scan_date, allow_pplx=False, config=config)
        item["momentum_hopper"] = {
            "gate_pass": passed,
            "gate_reasons": list(gate_flags.get("gate_reasons") or []),
            "low_float": bool(gate_flags.get("low_float")),
            "form4_p_buy_count": int(enrich.get("form4_p_buy_count") or 0),
            "binary_event_within_days": enrich.get("binary_event_within_days"),
            "runway_months": enrich.get("runway_months"),
            "going_concern": bool(enrich.get("going_concern")),
        }
        return item

    indices = [i for i, row in enumerate(rows) if row.get("validated")]
    if not indices:
        return rows

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {i: pool.submit(_annotate_one, rows[i]) for i in indices}
        for i, fut in futures.items():
            rows[i] = fut.result()
    return rows


def annotate_desk_overlap(
    tickers: Sequence[Dict[str, Any]],
    watchlist_index: Dict[str, Sequence[str]],
) -> List[Dict[str, Any]]:
    """Flag names already present on local watchlists."""
    annotated: List[Dict[str, Any]] = []
    for row in tickers:
        item = dict(row)
        sym = normalize_ticker_symbol(item.get("ticker"))
        lists = [str(n) for n in (watchlist_index.get(sym) or []) if str(n).strip()]
        item["on_desk"] = bool(lists)
        item["desk_watchlists"] = lists
        item["novelty"] = "on_desk" if lists else "new"
        annotated.append(item)
    return annotated


def build_watchlist_ticker_index(watchlists: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    index: Dict[str, List[str]] = {}
    for wl in watchlists or []:
        name = str(wl.get("name") or "").strip() or "Watchlist"
        raw = wl.get("tickers") or ""
        for token in str(raw).split(","):
            sym = normalize_ticker_symbol(token)
            if not sym:
                continue
            bucket = index.setdefault(sym, [])
            if name not in bucket:
                bucket.append(name)
    return index


def build_discovery_prompt(
    *,
    theme: str,
    criteria: str = "",
    market_cap_filter: str = "",
    max_results: int = 20,
    prompt_pack: str = "equity_search",
    asset_policy: str = "equity",
) -> Tuple[str, str]:
    """Return (system_prompt, user_prompt)."""
    pack = PROMPT_PACKS.get(prompt_pack) or PROMPT_PACKS["equity_search"]
    criteria_line = f"Additional criteria: {criteria.strip()}\n" if criteria and str(criteria).strip() else ""
    mcap_line = (
        f"Market cap constraint (hard): {market_cap_filter}. Do not include names outside this band.\n"
        if market_cap_filter and str(market_cap_filter).strip()
        else ""
    )
    policy_line = {
        "etf": "Instrument class: US-listed ETFs only.\n",
        "adr": (
            "Instrument class: US-listed ADRs or foreign issuers only "
            "(NYSE/NASDAQ). Skip US domestic companies (AMD, ON, NVDA). Not OTC.\n"
        ),
        "any": "Instrument class: US-listed equities or ETFs.\n",
    }.get((asset_policy or "equity").lower(), "Instrument class: US-listed common equity only.\n")

    user = (
        "Return JSON only. Find US-listed names matching this research theme.\n\n"
        f"Theme: {theme.strip()}\n"
        f"{criteria_line}"
        f"{mcap_line}"
        f"{policy_line}\n"
        f"Return up to {max_results} companies as a JSON array:\n"
        "[\n"
        "  {\n"
        '    "ticker": "XXXX",\n'
        '    "company": "Company Name",\n'
        '    "sector": "Sector",\n'
        '    "exchange": "NASDAQ",\n'
        '    "market_cap_approx": "$XB",\n'
        '    "catalyst": "dated catalyst if any",\n'
        '    "as_of": "YYYY-MM",\n'
        '    "confidence": "high|medium|low",\n'
        '    "rationale": "1-2 sentences on why this matches the theme"\n'
        "  }\n"
        "]\n\n"
        f"Rules:\n{pack['rules']}"
    )
    return pack["system"], user
