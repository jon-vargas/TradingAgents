"""
TradingAgents Screening Module — Tier 2 Opportunity Engine.

Provides algorithmic watchlist scanning with 16 scoring signals,
macro regime awareness, peer comparison, alert evaluation, and
automatic ticker metadata resolution for per-ticker weight mapping.
"""

from .engine import ScreeningEngine, ScreeningResult
from .alert_evaluator import AlertEvaluator
from .ticker_resolver import resolve_and_cache, resolve_ticker_metadata
from .builtin_refresh import build_refresh_proposal
from .movers import MoversIntelligenceService
from .long_horizon import LongHorizonService
from .reversal_buildup import score_ticker as score_reversal_buildup

__all__ = [
    "ScreeningEngine",
    "ScreeningResult",
    "AlertEvaluator",
    "MoversIntelligenceService",
    "LongHorizonService",
    "resolve_and_cache",
    "resolve_ticker_metadata",
    "build_refresh_proposal",
    "score_reversal_buildup",
]
