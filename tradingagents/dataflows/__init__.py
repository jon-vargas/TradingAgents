"""
TradingAgents Data Flows Module

Provides unified data access with:
- Multi-vendor routing (yfinance, Finnhub, Alpha Vantage, Perplexity, etc.)
- Smart caching with TTL management
- Usage tracking and rate limit management
- Analysis modes (quick/standard/deep)
"""

from .interface import (
    route_to_vendor,
    get_category_for_method,
    get_vendor,
    get_deep_research,
    get_earnings_deep_dive,
    get_sec_deep_dive,
    get_sec_filings_snapshot,
    get_earnings_transcript_snapshot,
    get_catalyst_pipeline,
    discover_opportunities,
    TOOLS_CATEGORIES,
    VENDOR_LIST,
    VENDOR_METHODS,
)

from .cache import (
    DataCache,
    get_cache,
    cached,
    CacheConfig,
)

from .usage_tracker import (
    UsageTracker,
    get_tracker,
    UsageLimits,
)

from .perplexity_api import (
    get_deep_research as perplexity_deep_research,
    get_earnings_analysis as perplexity_earnings,
    get_sec_filings_summary as perplexity_sec,
    get_sec_filings_snapshot as perplexity_sec_snapshot,
    get_earnings_transcript_snapshot as perplexity_earnings_transcript,
    PerplexityAPIError,
    PerplexityRateLimitError,
)

from .yfinance_extended import (
    get_analyst_ratings,
    get_analyst_ratings_batch,
    get_earnings_profile,
    get_ownership_summary,
    get_valuation_metrics,
    get_macro_snapshot,
    get_options_summary,
    format_analyst_context,
    format_risk_context,
    format_macro_context,
    format_options_context,
    compute_intrinsic_value,
    compute_scenario_analysis,
    compute_peer_comps,
    get_corporate_actions,
    get_insider_net_buy,
)

from .risk_metrics import (
    compute_risk_metrics,
)

from .index_regime import (
    normalize_index_regime,
    index_regime_display_label,
    index_regime_storage_fields,
)

__all__ = [
    # Core routing
    "route_to_vendor",
    "get_category_for_method",
    "get_vendor",
    "TOOLS_CATEGORIES",
    "VENDOR_LIST",
    "VENDOR_METHODS",
    
    # Deep research functions
    "get_deep_research",
    "get_earnings_deep_dive",
    "get_sec_deep_dive",
    "get_sec_filings_snapshot",
    "get_earnings_transcript_snapshot",
    "get_catalyst_pipeline",
    "discover_opportunities",
    
    # Caching
    "DataCache",
    "get_cache",
    "cached",
    "CacheConfig",
    
    # Usage tracking
    "UsageTracker",
    "get_tracker",
    "UsageLimits",
    
    # Perplexity direct access
    "perplexity_deep_research",
    "perplexity_earnings",
    "perplexity_sec",
    "perplexity_sec_snapshot",
    "perplexity_earnings_transcript",
    "PerplexityAPIError",
    "PerplexityRateLimitError",
    
    # Tier 2: Extended yfinance data
    "get_analyst_ratings",
    "get_analyst_ratings_batch",
    "get_earnings_profile",
    "get_ownership_summary",
    "get_valuation_metrics",
    "get_macro_snapshot",
    "get_options_summary",
    "format_analyst_context",
    "format_risk_context",
    "format_macro_context",
    "format_options_context",
    "compute_intrinsic_value",
    "compute_scenario_analysis",
    "compute_peer_comps",
    "get_corporate_actions",
    "get_insider_net_buy",

    # Tier 2: Risk metrics
    "compute_risk_metrics",

    # Index regime helpers
    "normalize_index_regime",
    "index_regime_display_label",
    "index_regime_storage_fields",
]
