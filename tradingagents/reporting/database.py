"""
Research Database - SQLite storage for all trading analyses.

Provides persistent storage, historical queries, and export capabilities.
"""

import logging
import os
import json
import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, asdict, field

logger = logging.getLogger("tradingagents.reporting.database")

_REGISTRY_INDEX_ALLOWLIST = {
    "sp500_top100",
    "sp500_full",
    "sp400_mid",
    "sp600_small",
    "ndx100",
    "russell2000_top100",
    "russell2000_full",
}
_REGISTRY_SOURCE_ALLOWLIST = {
    "wikipedia_html",
    "etf_holdings_fallback",
    "etf_holdings_csv",
    "curated_fallback",
    "curated",
    "seed",
    "bootstrap_refresh",
    "manual_refresh",
}

from tradingagents.utils.snapshot_utils import (
    normalize_snapshot_text,
    extract_snapshot_from_messages,
)
from tradingagents.reporting.pdf_generator import compute_data_quality_score


@dataclass
class AgentReport:
    """Individual agent report data."""
    agent_type: str
    content: str
    tokens_used: int = 0
    cost: float = 0.0


@dataclass
class Analysis:
    """Complete analysis record."""
    id: Optional[int] = None
    ticker: str = ""
    analysis_date: str = ""
    created_at: str = ""
    decision: str = ""
    confidence: float = 0.0
    
    # Configuration used
    llm_provider: str = ""
    deep_think_model: str = ""
    quick_think_model: str = ""
    debate_rounds: int = 0
    analysis_mode: str = ""  # quick/standard/deep
    risk_profile: str = ""   # aggressive/growth/conservative
    
    # Reports
    market_report: str = ""
    fundamentals_report: str = ""
    news_report: str = ""
    sentiment_report: str = ""
    data_provenance: str = ""
    run_settings: str = ""
    section_attribution: str = ""
    report_warnings: str = ""
    data_quality_score: int = 0
    sec_filings_snapshot: str = ""
    earnings_transcript_snapshot: str = ""
    has_sec_snapshot: int = 0
    has_transcript_snapshot: int = 0
    
    # Debate summaries
    bull_summary: str = ""
    bear_summary: str = ""
    investment_decision: str = ""
    
    # Risk assessment
    risk_assessment: str = ""
    
    # Trading plan
    trading_plan: str = ""
    final_decision: str = ""
    
    # Metrics
    total_tokens: int = 0
    total_cost: float = 0.0
    duration_seconds: float = 0.0
    
    # Price data at analysis time
    price_at_analysis: float = 0.0
    
    # For backtesting - filled in later
    price_after_7d: Optional[float] = None
    price_after_14d: Optional[float] = None
    price_after_30d: Optional[float] = None
    actual_return_7d: Optional[float] = None
    actual_return_14d: Optional[float] = None
    actual_return_30d: Optional[float] = None
    alpha_7d: Optional[float] = None
    alpha_14d: Optional[float] = None
    alpha_30d: Optional[float] = None
    was_correct: Optional[bool] = None
    
    # User annotations
    notes: str = ""
    tags: str = ""
    
    # Tier 2: Analyst ratings (stored at analysis time)
    analyst_rating: str = ""
    analyst_target_mean: Optional[float] = None
    analyst_target_high: Optional[float] = None
    analyst_target_low: Optional[float] = None
    analyst_upside_pct: Optional[float] = None
    analyst_count: int = 0
    screening_run_id: Optional[int] = None
    
    # Tier 2: Options intelligence (stored at analysis time)
    options_atm_iv: Optional[float] = None
    options_iv_rank: Optional[float] = None
    options_pc_volume_ratio: Optional[float] = None
    options_pc_oi_ratio: Optional[float] = None
    options_max_pain: Optional[float] = None
    options_unusual_count: int = 0

    # Tier 2: Investment profile used for analysis
    investment_profile: str = ""

    # Decision pipeline audit columns
    signal_summary: str = ""
    decision_json: str = ""
    position_action: str = ""

    # Institutional analysis enhancements
    earnings_quality_grade: str = ""
    earnings_quality_data: str = ""
    intrinsic_value: Optional[float] = None
    intrinsic_value_data: str = ""
    scenario_analysis: str = ""
    catalyst_pipeline: str = ""
    peer_comps: str = ""
    screening_context_json: str = ""


@dataclass
class BacktestRun:
    """Backtest run metadata."""
    id: Optional[int] = None
    run_at: str = ""
    ticker: str = ""
    start_date: str = ""
    end_date: str = ""
    limit_count: int = 0
    lookahead_days: str = ""
    slippage_bps: float = 0.0
    transaction_cost_bps: float = 0.0
    updated_count: int = 0
    skipped_count: int = 0
    avg_return: float = 0.0
    win_rate: float = 0.0
    accuracy: float = 0.0
    strategy_sharpe: Optional[float] = None
    strategy_sortino: Optional[float] = None
    avg_alpha_30d: Optional[float] = None
    avg_signed_return_7d: Optional[float] = None
    return_vol_7d: Optional[float] = None


@dataclass
class SavedView:
    """Saved filter/view configuration."""
    id: Optional[int] = None
    name: str = ""
    description: str = ""
    filters: str = ""  # JSON string of filter parameters
    created_at: str = ""
    updated_at: str = ""


class ResearchDatabase:
    """SQLite database for storing and querying research analyses."""
    
    def __init__(self, db_path: str = None):
        """Initialize database connection.
        
        Args:
            db_path: Path to SQLite database file. Defaults to ./research.db
        """
        if db_path is None:
            db_path = os.path.join(os.getcwd(), "research.db")
        
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        """Open a connection with WAL-mode performance PRAGMAs.

        synchronous=NORMAL is safe with WAL and reduces fsync overhead.
        cache_size=-8000 gives 8 MB page cache (vs default 2 MB).
        temp_store=MEMORY keeps transient sort/index data in RAM.
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._connect() as conn:
            cursor = conn.cursor()
        
            # Main analyses table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decision TEXT,
                    confidence REAL,
                    
                    -- Configuration
                    llm_provider TEXT,
                    deep_think_model TEXT,
                    quick_think_model TEXT,
                    debate_rounds INTEGER,
                    analysis_mode TEXT,
                    risk_profile TEXT,
                    
                    -- Reports (stored as text)
                    market_report TEXT,
                    fundamentals_report TEXT,
                    news_report TEXT,
                    sentiment_report TEXT,
                    data_provenance TEXT,
                    section_attribution TEXT,
                    report_warnings TEXT,
                    data_quality_score INTEGER,
                    sec_filings_snapshot TEXT,
                    earnings_transcript_snapshot TEXT,
                    has_sec_snapshot INTEGER,
                    has_transcript_snapshot INTEGER,
                    
                    -- Debate
                    bull_summary TEXT,
                    bear_summary TEXT,
                    investment_decision TEXT,
                    
                    -- Risk & Trading
                    risk_assessment TEXT,
                    trading_plan TEXT,
                    final_decision TEXT,
                    
                    -- Metrics
                    total_tokens INTEGER,
                    total_cost REAL,
                    duration_seconds REAL,
                    
                    -- Price data
                    price_at_analysis REAL,
                    price_after_7d REAL,
                    price_after_14d REAL,
                    price_after_30d REAL,
                    actual_return_7d REAL,
                    actual_return_14d REAL,
                    actual_return_30d REAL,
                    was_correct INTEGER,
                    
                    -- Indexes
                    UNIQUE(ticker, analysis_date, created_at)
                )
            """)

            cursor.execute("PRAGMA table_info(analyses)")
            existing_cols = {row[1] for row in cursor.fetchall()}
            if "data_provenance" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN data_provenance TEXT")
            if "run_settings" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN run_settings TEXT")
            if "section_attribution" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN section_attribution TEXT")
            if "report_warnings" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN report_warnings TEXT")
            if "data_quality_score" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN data_quality_score INTEGER")
                cursor.execute("UPDATE analyses SET data_quality_score = 0 WHERE data_quality_score IS NULL")
            if "sec_filings_snapshot" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN sec_filings_snapshot TEXT")
            if "earnings_transcript_snapshot" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN earnings_transcript_snapshot TEXT")
            if "has_sec_snapshot" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN has_sec_snapshot INTEGER")
                cursor.execute(
                    "UPDATE analyses SET has_sec_snapshot = CASE WHEN sec_filings_snapshot IS NOT NULL AND sec_filings_snapshot != '' THEN 1 ELSE 0 END"
                )
            if "has_transcript_snapshot" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN has_transcript_snapshot INTEGER")
                cursor.execute(
                    "UPDATE analyses SET has_transcript_snapshot = CASE WHEN earnings_transcript_snapshot IS NOT NULL AND earnings_transcript_snapshot != '' THEN 1 ELSE 0 END"
                )
            if "notes" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN notes TEXT")
            if "tags" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN tags TEXT")
            if "analysis_mode" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN analysis_mode TEXT")
                cursor.execute("UPDATE analyses SET analysis_mode = 'standard' WHERE analysis_mode IS NULL")
            if "risk_profile" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN risk_profile TEXT")
                cursor.execute("UPDATE analyses SET risk_profile = 'conservative' WHERE risk_profile IS NULL")
            
            # Tier 2: Analyst ratings columns
            if "analyst_rating" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN analyst_rating TEXT")
            if "analyst_target_mean" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN analyst_target_mean REAL")
            if "analyst_target_high" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN analyst_target_high REAL")
            if "analyst_target_low" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN analyst_target_low REAL")
            if "analyst_upside_pct" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN analyst_upside_pct REAL")
            if "analyst_count" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN analyst_count INTEGER")
            if "screening_run_id" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN screening_run_id INTEGER")
            
            # Tier 2: Options intelligence columns
            if "options_atm_iv" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN options_atm_iv REAL")
            if "options_iv_rank" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN options_iv_rank REAL")
            if "options_pc_volume_ratio" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN options_pc_volume_ratio REAL")
            if "options_pc_oi_ratio" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN options_pc_oi_ratio REAL")
            if "options_max_pain" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN options_max_pain REAL")
            if "options_unusual_count" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN options_unusual_count INTEGER DEFAULT 0")
            
            # Create indexes for risk_profile and analysis_mode for filtering
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_risk_profile ON analyses(risk_profile)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_analysis_mode ON analyses(analysis_mode)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_screening_run ON analyses(screening_run_id)
            """)

            # Migrate screening_results table: add new columns if missing
            try:
                cursor.execute("PRAGMA table_info(screening_results)")
                sr_cols = {row[1] for row in cursor.fetchall()}
                if sr_cols:  # Table exists
                    if "signal_deltas" not in sr_cols:
                        cursor.execute("ALTER TABLE screening_results ADD COLUMN signal_deltas TEXT")
                    if "percentile" not in sr_cols:
                        cursor.execute("ALTER TABLE screening_results ADD COLUMN percentile REAL")
                    if "macro_fit" not in sr_cols:
                        cursor.execute("ALTER TABLE screening_results ADD COLUMN macro_fit REAL")
                    if "macro_breakdown" not in sr_cols:
                        cursor.execute("ALTER TABLE screening_results ADD COLUMN macro_breakdown TEXT")
                    if "entry_quality" not in sr_cols:
                        cursor.execute("ALTER TABLE screening_results ADD COLUMN entry_quality REAL")
                    if "entry_quality_signals" not in sr_cols:
                        cursor.execute("ALTER TABLE screening_results ADD COLUMN entry_quality_signals TEXT")
            except Exception as e:
                logger.debug("Screening results migration skipped (table may not exist yet): %s", e)

            # Migrate analyses table: add investment_profile column if missing
            if "investment_profile" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN investment_profile TEXT")

            # Institutional analysis enhancement columns
            if "earnings_quality_grade" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN earnings_quality_grade TEXT")
            if "earnings_quality_data" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN earnings_quality_data TEXT")
            if "intrinsic_value" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN intrinsic_value REAL")
            if "intrinsic_value_data" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN intrinsic_value_data TEXT")
            if "scenario_analysis" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN scenario_analysis TEXT")
            if "catalyst_pipeline" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN catalyst_pipeline TEXT")
            if "peer_comps" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN peer_comps TEXT")

            # Benchmark-relative alpha columns
            if "alpha_7d" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN alpha_7d REAL")
            if "alpha_14d" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN alpha_14d REAL")
            if "alpha_30d" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN alpha_30d REAL")

            # Decision pipeline audit columns
            if "signal_summary" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN signal_summary TEXT")
            if "decision_json" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN decision_json TEXT")
            if "screening_context_json" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN screening_context_json TEXT")
            if "position_action" not in existing_cols:
                cursor.execute("ALTER TABLE analyses ADD COLUMN position_action TEXT")
                try:
                    cursor.execute(
                        """
                        UPDATE analyses
                        SET position_action = upper(json_extract(decision_json, '$.position_action'))
                        WHERE (position_action IS NULL OR position_action = '')
                          AND decision_json IS NOT NULL
                          AND decision_json != ''
                        """
                    )
                except Exception:
                    logger.debug("position_action backfill from decision_json skipped", exc_info=True)

            # Migrate ticker_metadata table: add institutional flags
            try:
                cursor.execute("PRAGMA table_info(ticker_metadata)")
                tm_cols = {row[1] for row in cursor.fetchall()}
                if tm_cols:
                    if "earnings_quality_grade" not in tm_cols:
                        cursor.execute("ALTER TABLE ticker_metadata ADD COLUMN earnings_quality_grade TEXT")
                    if "has_positive_fcf" not in tm_cols:
                        cursor.execute("ALTER TABLE ticker_metadata ADD COLUMN has_positive_fcf INTEGER")
            except Exception as e:
                logger.debug("Ticker metadata migration skipped (table may not exist yet): %s", e)

            # Migrate watchlists table: add columns if missing
            try:
                cursor.execute("PRAGMA table_info(watchlists)")
                wl_cols = {row[1] for row in cursor.fetchall()}
                if wl_cols and "default_preset" not in wl_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN default_preset TEXT")
                if wl_cols and "default_investment_profile" not in wl_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN default_investment_profile TEXT")
                if wl_cols and "theme_rationale" not in wl_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN theme_rationale TEXT")
                if wl_cols and "sector_snapshot" not in wl_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN sector_snapshot TEXT")
                if wl_cols and "expires_at" not in wl_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN expires_at TEXT")
            except Exception as e:
                logger.debug("Watchlists migration skipped (table may not exist yet): %s", e)

            # Backtest runs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at TEXT NOT NULL,
                    ticker TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    limit_count INTEGER,
                    lookahead_days TEXT,
                    slippage_bps REAL,
                    transaction_cost_bps REAL,
                    updated_count INTEGER,
                    skipped_count INTEGER,
                    avg_return REAL,
                    win_rate REAL,
                    accuracy REAL
                )
            """)
            
            # Migrate backtest_runs: add strategy metrics columns
            try:
                cursor.execute("PRAGMA table_info(backtest_runs)")
                br_cols = {row[1] for row in cursor.fetchall()}
                if br_cols:
                    if "strategy_sharpe" not in br_cols:
                        cursor.execute("ALTER TABLE backtest_runs ADD COLUMN strategy_sharpe REAL")
                    if "strategy_sortino" not in br_cols:
                        cursor.execute("ALTER TABLE backtest_runs ADD COLUMN strategy_sortino REAL")
                    if "avg_alpha_30d" not in br_cols:
                        cursor.execute("ALTER TABLE backtest_runs ADD COLUMN avg_alpha_30d REAL")
                    if "avg_signed_return_7d" not in br_cols:
                        cursor.execute("ALTER TABLE backtest_runs ADD COLUMN avg_signed_return_7d REAL")
                    if "return_vol_7d" not in br_cols:
                        cursor.execute("ALTER TABLE backtest_runs ADD COLUMN return_vol_7d REAL")
            except Exception as e:
                logger.debug("Backtest runs migration skipped: %s", e)

            # Unified event calendar table (Plan B Phase 3)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_calendar (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_date TEXT,
                    days_to_event INTEGER,
                    description TEXT,
                    metadata TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_event_ticker ON event_calendar(ticker)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_event_date ON event_calendar(event_date)
            """)

            # Create indexes for common queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ticker ON analyses(ticker)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_date ON analyses(analysis_date)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_decision ON analyses(decision)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sec_snapshot ON analyses(has_sec_snapshot)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_transcript_snapshot ON analyses(has_transcript_snapshot)
            """)

            # Snapshot index tables for structured search
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_kpis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    snapshot_type TEXT NOT NULL,
                    kpi_name TEXT NOT NULL,
                    kpi_value TEXT,
                    kpi_period TEXT,
                    kpi_context TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_kpi_ticker ON snapshot_kpis(ticker)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_kpi_name ON snapshot_kpis(kpi_name)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_guidance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    guidance_range TEXT,
                    timeframe TEXT,
                    context TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_guidance_ticker ON snapshot_guidance(ticker)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_guidance_metric ON snapshot_guidance(metric)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_risks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    risk_text TEXT NOT NULL,
                    source_form TEXT,
                    filing_date TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_risk_ticker ON snapshot_risks(ticker)
            """)

            # Saved views table for persistent filter combinations
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS saved_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT,
                    filters TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # Per-analysis LLM usage breakdown (by provider/model). This
            # complements analyses.total_tokens/total_cost and lets us inspect
            # actual usage distribution when tuning model routing later.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS analysis_llm_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_id INTEGER NOT NULL,
                    provider TEXT,
                    model TEXT,
                    calls INTEGER DEFAULT 0,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    cached_input_tokens INTEGER DEFAULT 0,
                    retry_events INTEGER DEFAULT 0,
                    error_events INTEGER DEFAULT 0,
                    total_latency_ms REAL DEFAULT 0.0,
                    estimated_cost_usd REAL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                )
            """)
            cursor.execute("PRAGMA table_info(analysis_llm_usage)")
            _allu_cols = {row[1] for row in cursor.fetchall()}
            if "cached_input_tokens" not in _allu_cols:
                cursor.execute("ALTER TABLE analysis_llm_usage ADD COLUMN cached_input_tokens INTEGER DEFAULT 0")
            if "retry_events" not in _allu_cols:
                cursor.execute("ALTER TABLE analysis_llm_usage ADD COLUMN retry_events INTEGER DEFAULT 0")
            if "error_events" not in _allu_cols:
                cursor.execute("ALTER TABLE analysis_llm_usage ADD COLUMN error_events INTEGER DEFAULT 0")
            if "total_latency_ms" not in _allu_cols:
                cursor.execute("ALTER TABLE analysis_llm_usage ADD COLUMN total_latency_ms REAL DEFAULT 0.0")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS analysis_llm_telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_id INTEGER NOT NULL UNIQUE,
                    provider TEXT,
                    calls INTEGER DEFAULT 0,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    cached_input_tokens INTEGER DEFAULT 0,
                    retry_events INTEGER DEFAULT 0,
                    error_events INTEGER DEFAULT 0,
                    total_latency_ms REAL DEFAULT 0.0,
                    estimated_cost_usd REAL DEFAULT 0.0,
                    unknown_price_models TEXT,
                    pricing_version TEXT,
                    usage_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS analysis_quality_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_id INTEGER NOT NULL UNIQUE,
                    quality_label TEXT,
                    quality_score REAL,
                    quality_notes TEXT,
                    evaluated_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (analysis_id) REFERENCES analyses(id)
                )
            """)
            
            # =========================================================
            # Tier 2: Watchlists, Screening, and Alerts tables
            # =========================================================
            
            # Watchlists
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS watchlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT,
                    tickers TEXT NOT NULL,
                    source TEXT,
                    index_key TEXT,
                    registry_backed INTEGER DEFAULT 0,
                    defaults_version TEXT,
                    source_mode_status TEXT,
                    materialized_as_of TEXT,
                    materialized_source TEXT,
                    default_preset TEXT,
                    default_investment_profile TEXT,
                    theme_rationale TEXT,
                    sector_snapshot TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            try:
                cursor.execute("PRAGMA table_info(watchlists)")
                watchlist_cols = {row[1] for row in cursor.fetchall()}
                if watchlist_cols and "index_key" not in watchlist_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN index_key TEXT")
                if watchlist_cols and "registry_backed" not in watchlist_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN registry_backed INTEGER DEFAULT 0")
                if watchlist_cols and "defaults_version" not in watchlist_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN defaults_version TEXT")
                if watchlist_cols and "source_mode_status" not in watchlist_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN source_mode_status TEXT")
                if watchlist_cols and "materialized_as_of" not in watchlist_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN materialized_as_of TEXT")
                if watchlist_cols and "materialized_source" not in watchlist_cols:
                    cursor.execute("ALTER TABLE watchlists ADD COLUMN materialized_source TEXT")
            except Exception as e:
                logger.debug("Watchlists migration skipped: %s", e)
            
            # Screening runs
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS screening_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watchlist_id INTEGER,
                    run_at TEXT NOT NULL,
                    strategy TEXT,
                    criteria TEXT,
                    ticker_count INTEGER,
                    results_count INTEGER,
                    FOREIGN KEY (watchlist_id) REFERENCES watchlists(id)
                )
            """)
            try:
                cursor.execute("PRAGMA table_info(screening_runs)")
                sr_runs_cols = {row[1] for row in cursor.fetchall()}
                if sr_runs_cols and "strategy" not in sr_runs_cols:
                    cursor.execute("ALTER TABLE screening_runs ADD COLUMN strategy TEXT")
                    # Backfill strategy for legacy rows from JSON criteria.
                    cursor.execute("SELECT id, criteria FROM screening_runs WHERE strategy IS NULL")
                    for row in cursor.fetchall():
                        strategy_val = None
                        raw_criteria = row["criteria"]
                        if raw_criteria:
                            try:
                                parsed = json.loads(raw_criteria) if isinstance(raw_criteria, str) else dict(raw_criteria)
                                strategy_val = str(parsed.get("strategy") or "").strip() or None
                            except (json.JSONDecodeError, TypeError, ValueError):
                                strategy_val = None
                        if strategy_val:
                            cursor.execute(
                                "UPDATE screening_runs SET strategy = ? WHERE id = ?",
                                (strategy_val, int(row["id"])),
                            )
            except Exception as e:
                logger.debug("Screening runs migration skipped: %s", e)
            
            # Screening results
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS screening_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    composite_score REAL,
                    direction TEXT,
                    signals TEXT,
                    signal_deltas TEXT,
                    percentile REAL,
                    rank INTEGER,
                    return_7d REAL,
                    return_14d REAL,
                    return_30d REAL,
                    macro_fit REAL,
                    macro_breakdown TEXT,
                    entry_quality REAL,
                    entry_quality_signals TEXT,
                    composite_fundamental REAL,
                    risk_score REAL,
                    risk_components TEXT,
                    asset_class TEXT,
                    FOREIGN KEY (run_id) REFERENCES screening_runs(id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_screening_results_run ON screening_results(run_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_screening_results_ticker ON screening_results(ticker)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_screening_results_run_rank ON screening_results(run_id, rank)
            """)
            cursor.execute("PRAGMA table_info(screening_results)")
            _sr_existing_cols = {row[1] for row in cursor.fetchall()}
            if "composite_fundamental" not in _sr_existing_cols:
                cursor.execute(
                    "ALTER TABLE screening_results ADD COLUMN composite_fundamental REAL"
                )
            # risk_score / risk_components / asset_class introduced in the
            # universe-expansion pass. risk_score is 0-100 (higher = riskier),
            # risk_components stores the per-sub-component breakdown as JSON
            # for UI hovercards and audits, and asset_class is snapshotted at
            # screen time so later analysis doesn't need to cross-join
            # ticker_metadata (which may have rotated since the run).
            if "risk_score" not in _sr_existing_cols:
                cursor.execute(
                    "ALTER TABLE screening_results ADD COLUMN risk_score REAL"
                )
            if "risk_components" not in _sr_existing_cols:
                cursor.execute(
                    "ALTER TABLE screening_results ADD COLUMN risk_components TEXT"
                )
            if "asset_class" not in _sr_existing_cols:
                cursor.execute(
                    "ALTER TABLE screening_results ADD COLUMN asset_class TEXT"
                )

            # Movers snapshots (raw source captures)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS movers_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asof_date TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    source_list TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    price_change_pct REAL,
                    volume REAL,
                    market_cap REAL,
                    payload_json TEXT,
                    UNIQUE(asof_date, source_list, ticker)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_movers_snapshots_date_source
                ON movers_snapshots(asof_date, source_list)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_movers_snapshots_ticker_date
                ON movers_snapshots(ticker, asof_date)
                """
            )

            # Movers per-run classification/context side table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS movers_run_details (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    catalyst_type TEXT,
                    reason_codes TEXT,
                    primary_bucket TEXT,
                    source_tags TEXT,
                    score_breakdown_json TEXT,
                    UNIQUE(run_id, ticker),
                    FOREIGN KEY (run_id) REFERENCES screening_runs(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_movers_run_details_run_ticker
                ON movers_run_details(run_id, ticker)
                """
            )

            # Long-horizon run metadata + side tables
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS long_horizon_run_meta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL UNIQUE,
                    meta_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES screening_runs(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_long_horizon_run_meta_run
                ON long_horizon_run_meta(run_id)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS long_horizon_run_details (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    horizon TEXT,
                    sector TEXT,
                    market_cap_tier TEXT,
                    beta REAL,
                    composite_score REAL,
                    base_weight REAL,
                    target_weight REAL,
                    exclusion_reason_codes TEXT,
                    allocation_reason_codes TEXT,
                    constraint_hits TEXT,
                    expected_return_components_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, ticker),
                    FOREIGN KEY (run_id) REFERENCES screening_runs(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_long_horizon_details_run_ticker
                ON long_horizon_run_details(run_id, ticker)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS long_horizon_underwriting (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    packet_json TEXT NOT NULL,
                    confidence REAL,
                    review_cadence TEXT,
                    degraded INTEGER DEFAULT 0,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, ticker),
                    FOREIGN KEY (run_id) REFERENCES screening_runs(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_long_horizon_underwriting_run_ticker
                ON long_horizon_underwriting(run_id, ticker)
                """
            )

            # Perplexity ticker discovery runs (manual hopper + auto-discovery)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS discovery_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    external_run_id TEXT,
                    template_id TEXT,
                    theme TEXT,
                    criteria TEXT,
                    market_cap_filter TEXT,
                    max_results INTEGER,
                    preset TEXT,
                    profile TEXT,
                    ticker_count INTEGER DEFAULT 0,
                    validated_count INTEGER DEFAULT 0,
                    new_count INTEGER DEFAULT 0,
                    watchlist_id INTEGER,
                    status TEXT DEFAULT 'completed',
                    error TEXT,
                    summary_json TEXT,
                    tickers_json TEXT,
                    FOREIGN KEY (watchlist_id) REFERENCES watchlists(id)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_discovery_runs_run_at
                ON discovery_runs(run_at DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_discovery_runs_source
                ON discovery_runs(source, run_at DESC)
                """
            )
            
            # Alert rules
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alert_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    condition TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_rules_ticker ON alert_rules(ticker)
            """)
            
            # Alert history
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL,
                    triggered_at TEXT NOT NULL,
                    message TEXT,
                    data TEXT,
                    is_read INTEGER DEFAULT 0,
                    FOREIGN KEY (rule_id) REFERENCES alert_rules(id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_history_rule ON alert_history(rule_id)
            """)

            # Ticker metadata (auto-resolved classifications)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ticker_metadata (
                    ticker TEXT PRIMARY KEY,
                    sector TEXT,
                    industry TEXT,
                    market_cap REAL,
                    avg_dollar_volume_usd REAL,
                    liquidity_rank INTEGER,
                    market_cap_tier TEXT,
                    is_profitable INTEGER,
                    has_dividend INTEGER,
                    beta REAL,
                    beta_tier TEXT,
                    resolved_profile TEXT,
                    resolved_preset TEXT,
                    asset_class TEXT,
                    country TEXT,
                    last_updated TEXT NOT NULL
                )
            """)
            try:
                cursor.execute("PRAGMA table_info(ticker_metadata)")
                tm_cols = {row[1] for row in cursor.fetchall()}
                if tm_cols and "avg_dollar_volume_usd" not in tm_cols:
                    cursor.execute("ALTER TABLE ticker_metadata ADD COLUMN avg_dollar_volume_usd REAL")
                if tm_cols and "liquidity_rank" not in tm_cols:
                    cursor.execute("ALTER TABLE ticker_metadata ADD COLUMN liquidity_rank INTEGER")
                # asset_class / country introduced in universe-expansion pass.
                # asset_class ∈ {equity, etf, adr, unknown}; country is the
                # `.info["country"]` value or empty — we keep it for auditability
                # of the ADR classification (symbol on US exchange but domiciled
                # elsewhere).
                if tm_cols and "asset_class" not in tm_cols:
                    cursor.execute("ALTER TABLE ticker_metadata ADD COLUMN asset_class TEXT")
                if tm_cols and "country" not in tm_cols:
                    cursor.execute("ALTER TABLE ticker_metadata ADD COLUMN country TEXT")
            except Exception as e:
                logger.debug("Ticker metadata migration skipped: %s", e)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticker_meta_profile ON ticker_metadata(resolved_profile)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticker_meta_preset ON ticker_metadata(resolved_preset)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticker_meta_updated ON ticker_metadata(last_updated)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticker_meta_liquidity ON ticker_metadata(liquidity_rank)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticker_meta_asset_class ON ticker_metadata(asset_class)")

            # Ticker health — tracks per-ticker OHLCV fetch outcomes so we can
            # auto-evict likely-delisted / permanently-unfetchable tickers from
            # ticker_metadata (and downstream watchlists) without human triage.
            # Populated by the screening engine; consumed by evict_stale_tickers.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ticker_health (
                    ticker TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    last_failure_at TEXT,
                    last_success_at TEXT,
                    last_failure_reason TEXT,
                    evicted_at TEXT,
                    eviction_reason TEXT
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticker_health_last_failure ON ticker_health(last_failure_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticker_health_evicted ON ticker_health(evicted_at)"
            )

            # Primary exchange resolution side table (used for TradingView-ready exports)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ticker_exchange_resolution (
                    ticker TEXT PRIMARY KEY,
                    primary_exchange TEXT NOT NULL,
                    exchange_source TEXT,
                    exchange_confidence REAL,
                    raw_exchange TEXT,
                    source_timestamp TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ticker_exchange_resolution_updated ON ticker_exchange_resolution(updated_at DESC)"
            )

            # Signal performance tracking (Feature 17)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signal_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_name TEXT NOT NULL,
                    period TEXT NOT NULL,
                    hit_rate REAL,
                    avg_return REAL,
                    correlation REAL,
                    sample_count INTEGER,
                    last_updated TEXT NOT NULL,
                    UNIQUE(signal_name, period)
                )
            """)

            # ---- Additional performance indexes (infrastructure audit) ----
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analyses_created_at ON analyses(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analyses_was_correct ON analyses(was_correct)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analyses_price_7d ON analyses(price_after_7d)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analysis_llm_usage_analysis ON analysis_llm_usage(analysis_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analysis_llm_usage_model ON analysis_llm_usage(model)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analysis_llm_telemetry_analysis ON analysis_llm_telemetry(analysis_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_analysis_quality_labels_analysis ON analysis_quality_labels(analysis_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_screening_runs_run_at ON screening_runs(run_at DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_screening_runs_watchlist ON screening_runs(watchlist_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_screening_runs_strategy_run_at ON screening_runs(strategy, run_at DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_run_at ON backtest_runs(run_at DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_backtest_runs_ticker ON backtest_runs(ticker)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_alert_history_read ON alert_history(is_read)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_alert_history_triggered ON alert_history(triggered_at DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_kpi_analysis_id ON snapshot_kpis(analysis_id)")

            # Watchlist indexes for lifecycle queries
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_watchlists_source ON watchlists(source)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_watchlists_name ON watchlists(name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_watchlists_registry_backed ON watchlists(registry_backed)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_watchlists_index_key ON watchlists(index_key)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_watchlists_materialized_as_of ON watchlists(materialized_as_of)")

            # Index constituent registry (v1 SQL source of truth for index membership)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS index_constituents (
                    index_key TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    source TEXT NOT NULL,
                    weight REAL,
                    avg_dollar_volume_usd REAL,
                    market_cap REAL,
                    market_cap_tier TEXT,
                    liquidity_rank INTEGER,
                    in_scope INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(index_key, ticker, as_of)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_index_constituents_ticker_asof
                ON index_constituents(ticker, as_of DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_index_constituents_index_asof
                ON index_constituents(index_key, as_of DESC, in_scope)
                """
            )

            # Built-in refresh proposal workflow
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS builtin_refresh_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    source TEXT,
                    warning_count INTEGER DEFAULT 0,
                    metadata_json TEXT,
                    summary_json TEXT,
                    created_at TEXT NOT NULL,
                    applied_at TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS builtin_refresh_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id INTEGER NOT NULL,
                    watchlist_id INTEGER,
                    watchlist_name TEXT NOT NULL,
                    old_count INTEGER DEFAULT 0,
                    new_count INTEGER DEFAULT 0,
                    old_hash TEXT,
                    new_hash TEXT,
                    adds_json TEXT,
                    removes_json TEXT,
                    warnings_json TEXT,
                    is_noop INTEGER DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    applied_at TEXT,
                    FOREIGN KEY (proposal_id) REFERENCES builtin_refresh_proposals(id)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_builtin_refresh_proposals_status ON builtin_refresh_proposals(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_builtin_refresh_proposals_created_at ON builtin_refresh_proposals(created_at DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_builtin_refresh_items_proposal ON builtin_refresh_items(proposal_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_builtin_refresh_items_watchlist ON builtin_refresh_items(watchlist_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_guidance_analysis_id ON snapshot_guidance(analysis_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_risks_analysis_id ON snapshot_risks(analysis_id)")

            # Runtime performance metrics for QA p95 gates
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_key TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    context_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_metrics_key_created ON runtime_metrics(metric_key, created_at DESC)"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sector_median_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    universe_size INTEGER NOT NULL,
                    medians_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sector_median_snapshots_created ON sector_median_snapshots(created_at DESC)"
            )

            conn.commit()
        
        # Seed default watchlists on first run
        self._seed_watchlists()
    
    # =========================================================================
    # WATCHLISTS
    # =========================================================================

    @staticmethod
    def _builtin_watchlist_registry() -> List[Dict[str, Any]]:
        """Return built-in watchlist registry with metadata.

        This is the canonical definition used for seeding and refresh planning.
        """

        # Sector ETFs (11)
        sector_etfs = "XLK,XLV,XLF,XLE,XLI,XLY,XLP,XLU,XLB,XLRE,XLC"

        # NASDAQ 100 representative subset (top ~100 by market cap)
        nasdaq_100 = (
            "AAPL,MSFT,AMZN,NVDA,META,GOOGL,GOOG,TSLA,AVGO,COST,"
            "NFLX,ADBE,AMD,PEP,CSCO,TMUS,LIN,INTU,TXN,QCOM,"
            "ISRG,AMGN,CMCSA,BKNG,HON,AMAT,LRCX,VRTX,ADP,SBUX,"
            "MU,REGN,PANW,ADI,KLAC,MDLZ,GILD,MELI,SNPS,CDNS,"
            "PYPL,CRWD,CTAS,MAR,MNST,NXPI,ORLY,MRVL,FTNT,WDAY,"
            "ABNB,CSX,PCAR,ROST,DXCM,CPRT,KDP,ODFL,FANG,MCHP,"
            "AEP,PAYX,KHC,EXC,FAST,GEHC,LULU,IDXX,VRSK,CTSH,"
            "BKR,ON,DDOG,EA,ANSS,XEL,TEAM,DLTR,WBD,ZS,"
            "ILMN,BIIB,SIRI,ENPH,CEG,TTWO,GFS,DASH,SMCI,ARM,"
            "COIN,PLTR,MSTR,TTD,RIVN,LCID,HOOD,IONQ,RKLB,JOBY"
        )

        # S&P 500 — top ~100 large caps (manageable for screening)
        sp500_top = (
            "AAPL,MSFT,AMZN,NVDA,GOOGL,META,BRK-B,TSLA,UNH,JNJ,"
            "XOM,V,JPM,PG,MA,HD,AVGO,CVX,MRK,LLY,"
            "ABBV,PEP,COST,KO,WMT,MCD,CSCO,TMO,ABT,CRM,"
            "ACN,DHR,NFLX,ADBE,LIN,TXN,CMCSA,NEE,PM,RTX,"
            "ORCL,BMY,HON,UNP,UPS,AMGN,LOW,DE,IBM,BA,"
            "GS,INTC,QCOM,ISRG,ELV,MS,SBUX,CAT,GE,ADP,"
            "MDLZ,BKNG,ADI,BLK,PLD,GILD,CI,TGT,SYK,CB,"
            "MMC,REGN,VRTX,SCHW,PGR,DUK,SO,MO,CL,ZTS,"
            "CME,AON,BDX,TJX,FIS,FISV,NSC,MCO,WM,ITW,"
            "SHW,APD,ECL,SRE,EMR,AIG,PSA,PNC,USB,WELL"
        )

        # Russell 2000 Top 100 (small-cap leaders, expanded baseline)
        russell_2000_top_100 = (
            "CVNA,B,MSTR,INSM,FIX,SATS,ASTS,BE,FTAI,SMCI,"
            "FN,CRS,ATI,APG,RVMD,BBIO,ITCI,MDGL,KTOS,GH,"
            "MTSI,SMMT,MLI,AVAV,HL,CDE,RNA,RMBS,AMKR,DY,"
            "CADE,HLI,IBP,LNTH,GKOS,PCVX,OSCR,CALX,RHP,NOVT,"
            "MOD,STEP,CSWI,FSS,DOCS,CORT,TGTX,SFM,PIPR,ACLX,"
            "ALKS,BPOP,ASB,AVPT,BANC,BCPC,BGC,BIOC,BPMC,BRZE,"
            "CARG,CBRL,CCOI,CEIX,CHDN,CIVI,CLBK,CNMD,CNS,CNXC,"
            "CRUS,CYTK,DGX,DKS,DNLI,DORM,EAT,EEFT,ELF,ENOV,"
            "ESGR,ESNT,EXLS,FHI,FIVE,FORM,FSLY,GBCI,GDDY,GLPI"
        )

        # Dividend Aristocrats Top 50 (25+ years consecutive dividend growth)
        dividend_aristocrats_top_50 = (
            "DOV,EMR,CINF,CL,ABBV,ABT,BDX,ADM,ADP,AFL,"
            "APD,AOS,ATO,BEN,CAH,CAT,CB,CHD,CHRW,CTAS,"
            "CVX,ECL,ED,ESS,EXPD,FDS,GPC,GWW,HRL,ITW,"
            "JNJ,KMB,KO,LIN,LOW,MCD,MKC,NUE,NDSN,PEP,"
            "PG,PPG,ROP,SHW,SJM,SPGI,SWK,SYY,TGT,TROW"
        )

        # Energy & Commodity Leaders (top producers + miners)
        energy_commodities = (
            "XOM,CVX,COP,EOG,SLB,MPC,PSX,VLO,OXY,DVN,"
            "FANG,HAL,BKR,HES,CTRA,NEM,FCX,GOLD,SCCO,VALE,"
            "CLF,AA,X,RIG,WFRD"
        )

        # =====================================================================
        # Healthcare & Life Sciences (comprehensive equity universe)
        # =====================================================================
        # Single curated list spanning payers, pharma, biotech, medtech, life-
        # science tools/CROs, diagnostics, distribution, and healthcare REITs.
        # Preset/profile resolve per ticker (mixed cap and style).
        #
        # Categories:
        #   MANAGED CARE & SERVICES (14)
        #   LARGE PHARMA & ANIMAL HEALTH (14)
        #   BIOTECH & SPECIALTY PHARMA (33)
        #   MEDTECH & DEVICES (20)
        #   TOOLS / CRO / DIAGNOSTICS (19)
        #   HEALTHCARE REIT & FACILITIES (4)
        # =====================================================================
        healthcare_life_sciences = (
            # --- Managed care, payers, providers, distribution ---
            "UNH,ELV,CI,HUM,CNC,MOH,CVS,HCA,UHS,DVA,TDOC,MCK,CAH,HSIC,"
            # --- Large pharma & animal health ---
            "JNJ,LLY,PFE,MRK,ABBV,BMY,AMGN,GILD,VRTX,REGN,BIIB,ZTS,JAZZ,NBIX,"
            # --- Biotech & specialty pharma ---
            "ALNY,INCY,MRNA,BNTX,BMRN,UTHR,EXAS,SRPT,ARWR,PCVX,BBIO,MDGL,"
            "RVMD,INSM,ITCI,TGTX,CYTK,ALKS,DNLI,RNA,SMMT,VKTX,KYMR,IMVT,ACLX,"
            "RARE,IONS,FOLD,BCRX,NVAX,HALO,ACAD,AXSM,APLS,"
            # --- Medtech & devices ---
            "ISRG,SYK,MDT,BSX,EW,ABT,DXCM,PODD,PEN,HOLX,GEHC,GMED,NVST,INSP,"
            "STE,ALGN,XRAY,COO,ESTA,IRTC,"
            # --- Life science tools, CRO, diagnostics ---
            "TMO,DHR,IQV,CRL,MTD,WAT,A,ILMN,TECH,BIO,RVTY,DGX,LH,NTRA,TWST,"
            "GH,FLGT,PACB,IDXX,"
            # --- Healthcare REIT & facilities ---
            "WELL,VTR,DOC,EHC"
        )

        # =====================================================================
        # AI Infrastructure Metals & Mining
        # =====================================================================
        # Comprehensive watchlist spanning all critical metals/minerals for the
        # AI data-center & energy buildout: copper, uranium/nuclear, lithium,
        # rare earths, silver/precious, aluminum/steel, and specialty minerals.
        #
        # Categories:
        #   COPPER (12)  — Data centers need ~50k tons each; structural deficit
        #   URANIUM & NUCLEAR (12) — 24/7 carbon-free power for hyperscalers
        #   LITHIUM & BATTERY (10) — Energy storage, grid stabilization, EVs
        #   RARE EARTHS & CRITICAL (8) — GPU semiconductors, magnets, defense
        #   SILVER & PRECIOUS (8) — Conductivity in every server + store of value
        #   ALUMINUM & STEEL (8) — Chassis, heat sinks, data center construction
        #   SPECIALTY & DIVERSIFIED (7) — Multi-metal majors + ETFs
        # =====================================================================
        ai_infrastructure_metals = (
            # --- Copper (9) — data centers need ~50k tons each; structural deficit ---
            "FCX,SCCO,TECK,HBM,COPX,ERO,TGB,CPER,IVPAF,"
            # --- Uranium & Nuclear (12) — 24/7 carbon-free power for hyperscalers ---
            "CCJ,UEC,NXE,UUUU,LEU,DNN,SMR,OKLO,BWXT,CEG,VST,NNE,"
            # --- Lithium & Battery Metals (7) — energy storage, grid, EVs ---
            "ALB,SQM,LAC,SGML,IONR,QS,MVST,"
            # --- Rare Earths & Critical Minerals (6) — GPU semis, magnets ---
            "MP,TMC,USAR,TMRC,REMX,SETM,"
            # --- Silver & Precious Metals (7) — conductivity in every server ---
            "AG,PAAS,HL,CDE,WPM,EXK,SVM,"
            # --- Aluminum & Steel Infrastructure (7) — chassis, heat sinks, construction ---
            "AA,CENX,CSTM,KALU,NUE,STLD,CLF,"
            # --- Specialty & Diversified Miners (6) — multi-metal majors ---
            "RIO,BHP,VALE,GOLD,NEM,GLNCY"
        )

        # =====================================================================
        # AI & AI Infrastructure (equity stack, not the metals basket)
        # =====================================================================
        # Focus desk. Post-close excludes this name so it is not a daily auto scan.
        # Dual-class Alphabet is GOOGL only. Miners stay on AI Infrastructure Metals.
        # Shared with that metals list on purpose: CEG, VST, OKLO, SMR, BWXT (power).
        #
        #   PLATFORMS (6)
        #   AI SOFTWARE (10) — product companies, not every SaaS name
        #   ACCELERATORS AND MEMORY INTERCONNECT (11)
        #   FOUNDRY, EQUIPMENT, EDA, PACKAGING, POWER SEMIS (25)
        #   MEMORY AND STORAGE (4)
        #   SERVERS AND CONTRACT MANUFACTURING (7)
        #   NETWORKING, OPTICS, AND FIBER (11)
        #   CONNECTORS (2)
        #   POWER, COOLING, ELECTRICAL, AND GRID (22)
        #   SITE, FIBER, AND ELECTRICAL CONSTRUCTION (8)
        #   DATA-CENTER REAL ESTATE (5)
        #   NEOCLOUD AND POWERED SHELLS (10)
        #   125 names. Miners and broad regulated utilities stay off.
        # =====================================================================
        ai_and_infrastructure = (
            # --- Platforms ---
            "MSFT,GOOGL,AMZN,META,ORCL,IBM,"
            # --- AI software ---
            "PLTR,CRM,NOW,SNOW,DDOG,NET,MDB,ESTC,PATH,APP,"
            # --- Accelerators and custom silicon ---
            "NVDA,AMD,AVGO,ARM,MRVL,INTC,QCOM,AMBA,ALAB,LSCC,RMBS,"
            # --- Foundry, equipment, EDA, power semis ---
            "TSM,ASML,AMAT,LRCX,KLAC,GFS,SNPS,CDNS,TER,KEYS,ENTG,ONTO,AMKR,ASX,"
            "CAMT,MKSI,ACLS,AEHR,FORM,COHU,UCTT,ON,MPWR,TXN,VICR,"
            # --- Memory and storage ---
            "MU,WDC,STX,PSTG,"
            # --- Servers and contract manufacturing ---
            "SMCI,DELL,HPE,CLS,JBL,SANM,FLEX,"
            # --- Networking, optics, and fiber ---
            "ANET,CSCO,CIEN,COHR,LITE,GLW,FN,CRDO,AAOI,MTSI,SMTC,LUMN,"
            # --- Connectors and data-center cabling ---
            "APH,TEL,BDC,"
            # --- Power, cooling, electrical, and grid ---
            "VRT,ETN,NVT,GEV,HUBB,BE,GNRC,CMI,ENS,ATKR,POWL,PSIX,WCC,"
            "MOD,TT,CARR,JCI,CEG,VST,TLN,NRG,OKLO,SMR,BWXT,"
            # --- Site, fiber, and electrical construction ---
            "PWR,EME,FIX,STRL,DY,MTZ,MYRG,AGX,"
            # --- Data-center real estate and digital-infrastructure capital ---
            "EQIX,DLR,IRM,DBRG,GDS,"
            # --- Neocloud and powered-shell operators ---
            "CRWV,NBIS,IREN,APLD,CORZ,WULF,CIFR,HUT,GLXY,CLSK"
        )

        # Registry-backed broad index watchlists are intentionally seeded empty and
        # marked pending until first bootstrap/refresh materialization.
        sp500_full_seed = ""
        sp400_mid_seed = ""
        sp600_small_seed = ""
        russell_2000_seed = ""

        # ---------------------------------------------------------------------
        # ETF UNIVERSES (curated — high-liquidity, broadly-held instruments)
        # ---------------------------------------------------------------------
        # These are *hand-curated* rather than registry-backed because:
        #   (a) no single clean public source provides a stable "top N ETFs"
        #       list the way Wikipedia does for the S&P 500,
        #   (b) ETFs rarely churn (new launches take months to accrue AUM, and
        #       closures are pre-announced) so a quarterly manual refresh is
        #       sufficient,
        #   (c) keeping them static means users get deterministic screening
        #       without waiting on builtin_refresh scrape cycles.
        # Coverage targets the ~150 highest-volume US-listed ETFs across
        # five major research buckets. The existing "Sector ETFs" watchlist
        # (11 SPDR sector ETFs) is left untouched for backward compatibility.

        # Factor / style / broad-market ETFs — momentum, value, quality, size,
        # low-vol, growth/value tilts. These are the "how are factors behaving
        # right now?" research tool.
        etfs_factor_style = (
            # Core broad-market (6) — the essentials
            "SPY,VOO,IVV,QQQ,DIA,IWM,"
            # Growth vs value splits (8) — S&P / Russell style indices
            "IVW,IVE,IWF,IWD,VUG,VTV,SPYG,SPYV,"
            # Size tilts (6) — mid & small cap variants
            "IJH,IJR,VB,VO,MDY,SLY,"
            # Factor ETFs (9) — academically-defined factors
            "MTUM,VLUE,QUAL,USMV,SIZE,SPLV,SPHB,EFAV,DGRO,"
            # Dividend / income (6) — dividend growth & yield tilts
            "SCHD,VYM,NOBL,VIG,DVY,HDV,"
            # Broad / total market (4)
            "VTI,ITOT,SCHB,SCHX"
        )

        # Thematic & industry ETFs — semis, biotech, clean energy, housing,
        # banks, oil services, cybersecurity, robotics, AI. This is the
        # "which theme is leading?" research tool.
        etfs_thematic = (
            # Semis / AI / tech (8)
            "SMH,SOXX,XSD,PSI,QTUM,BOTZ,AIQ,IGV,"
            # Biotech / healthcare innovation (6)
            "IBB,XBI,IHI,GNOM,ARKG,PPH,"
            # Clean energy / EV / ICE replacement (6)
            "ICLN,TAN,LIT,KRBN,DRIV,QCLN,"
            # Housing / construction (4)
            "ITB,XHB,IYR,REM,"
            # Cybersecurity / cloud / fintech (5)
            "HACK,CIBR,SKYY,WCLD,FINX,"
            # Banks / financials / regionals (5)
            "KBE,KRE,XLF,IAI,IYF,"
            # Oil services / exploration & production (4)
            "OIH,XOP,XES,PXE,"
            # Retail / leisure / travel (4)
            "XRT,JETS,PEJ,AWAY,"
            # Disruptive / ARK family (5)
            "ARKK,ARKW,ARKF,ARKQ,PRNT,"
            # Commodities / metals / agri (7)
            "GDX,GDXJ,SIL,SLV,COPX,DBA,MOO,"
            # Dividend / cash-flow thematic (2)
            "COWZ,PFF"
        )

        # Bond & rates ETFs — duration, credit, inflation, floating-rate. The
        # research tool for reading the rates tape.
        etfs_bonds_rates = (
            # Treasury duration ladder (8)
            "SHV,BIL,SHY,IEF,TLT,EDV,VGSH,VGIT,"
            # Broad aggregate / total bond (4)
            "AGG,BND,SCHZ,IUSB,"
            # Investment-grade corporate (4)
            "LQD,VCIT,VCSH,IGSB,"
            # High yield / credit (5)
            "HYG,JNK,SHYG,SJNK,SRLN,"
            # TIPS / inflation (3)
            "TIP,VTIP,SCHP,"
            # International / EM bonds (4)
            "EMB,VWOB,BNDX,IGOV,"
            # Munis (3)
            "MUB,VTEB,TFI,"
            # Preferred / convertibles (2)
            "PFFD,CWB"
        )

        # Country & regional ETFs — developed and emerging markets, single-
        # country exposure. The research tool for global rotation.
        etfs_countries_regional = (
            # Developed markets broad (5)
            "VEA,IEFA,EFA,IXUS,SCHF,"
            # Emerging markets broad (5)
            "VWO,IEMG,EEM,SCHE,SPEM,"
            # Europe / UK / Germany / France (6)
            "VGK,IEUR,EZU,EWU,EWG,EWQ,"
            # Asia-Pacific / Japan (6)
            "EWJ,EWA,EWT,EWY,EWH,AAXJ,"
            # China (4)
            "MCHI,FXI,KWEB,ASHR,"
            # India / Southeast Asia (3)
            "INDA,INDY,EPI,"
            # Latin America (4)
            "EWZ,ILF,EWW,EPU,"
            # Canada / Australia (2)
            "EWC,EWA"
        )

        # ---------------------------------------------------------------------
        # ADR UNIVERSE (curated — top foreign listings on US exchanges)
        # ---------------------------------------------------------------------
        # Covers the ~60 most-traded ADRs globally by ADV. Like the ETF lists,
        # this is curated because there's no single public source that cleanly
        # enumerates "active ADRs listed on NYSE/NASDAQ" with sufficient
        # liquidity filters. Updated quarterly; see USER_MANUAL for refresh
        # process. These behave like regular equities for screening (full
        # fundamental coverage), so they use the normal equity preset tree —
        # the "adr" asset_class tag is an audit annotation, not a signal
        # suppressor.
        adrs_top = (
            # Semiconductors / AI (4)
            "TSM,ASML,NVO,SAP,"
            # Chinese tech & e-commerce (6)
            "BABA,JD,PDD,BIDU,NTES,TCEHY,"
            # Chinese EV / consumer (4)
            "NIO,LI,XPEV,YUMC,"
            # Japanese industrials / consumer (6)
            "TM,SONY,HMC,MFG,MUFG,SMFG,"
            # Korea / Taiwan (2)
            "KB,TSM,"
            # European pharma / consumer (8)
            "AZN,GSK,NVS,UL,DEO,SNY,RYAAY,BUD,"
            # European industrials / tech (6)
            "SE,STLA,ERIC,NOK,SHEL,BP,"
            # European financials (5)
            "HSBC,BCS,LYG,ING,UBS,"
            # Latin American (9)
            "VALE,PBR,ITUB,BBD,ABEV,MELI,NU,SID,GGB,"
            # Canadian banks / energy (8)
            "RY,TD,BNS,BMO,CM,ENB,TRP,SU,"
            # Israeli tech (5)
            "CYBR,CHKP,MNDY,WIX,NICE,"
            # Mining / metals global (4)
            "RIO,BHP,GOLD,AEM,"
            # UK / European large-cap consumer & energy (3)
            "BTI,TTE,EQNR"
        )
        # Dedup defensively — curated strings may contain accidental dupes
        # (TSM appears in both semis and Korea/Taiwan chunks above). Also
        # strips tokens that wouldn't match our symbol format gate.
        _seen_adr: set = set()
        _adr_out: list = []
        for sym in adrs_top.split(","):
            s = sym.strip().upper()
            if s and s not in _seen_adr:
                _seen_adr.add(s)
                _adr_out.append(s)
        adrs_top = ",".join(_adr_out)

        registry = [
            {
                "name": "Sector ETFs",
                "description": "11 SPDR Sector ETFs for broad market screening",
                "tickers": sector_etfs,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": "large_cap_core",
                "category": "benchmark",
                "source_mode": "static",
                "index_key": None,
                "registry_backed": False,
                "target_size": 11,
                "cadence": "none",
                "enabled": True,
            },
            {
                "name": "NASDAQ 100",
                "description": "Top NASDAQ 100 constituents",
                "tickers": nasdaq_100,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": "large_cap_core",
                "category": "objective_index",
                "source_mode": "wikipedia_html",
                "index_key": "ndx100",
                "registry_backed": True,
                "source_mode_status": "materialized",
                "target_size": 100,
                "cadence": "monthly",
                "enabled": True,
                "defaults_version": "v3",
            },
            {
                "name": "S&P 500 Top 100",
                "description": "Largest S&P 500 companies by market cap",
                "tickers": sp500_top,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": "large_cap_core",
                "category": "objective_index",
                "source_mode": "wikipedia_html",
                "index_key": "sp500_top100",
                "registry_backed": True,
                "source_mode_status": "materialized",
                "target_size": 100,
                "cadence": "monthly",
                "enabled": True,
            },
            {
                "name": "S&P 500",
                "description": "Full S&P 500 constituent universe",
                "tickers": sp500_full_seed,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": "large_cap_core",
                "category": "objective_index",
                "source_mode": "wikipedia_html",
                "index_key": "sp500_full",
                "registry_backed": True,
                "source_mode_status": "pending_first_refresh",
                "target_size": 500,
                "cadence": "monthly",
                "enabled": True,
            },
            {
                "name": "S&P 400",
                "description": "S&P MidCap 400 constituent universe",
                "tickers": sp400_mid_seed,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": "high_growth",
                "category": "objective_index",
                "source_mode": "wikipedia_html",
                "index_key": "sp400_mid",
                "registry_backed": True,
                "source_mode_status": "pending_first_refresh",
                "target_size": 400,
                "cadence": "monthly",
                "enabled": True,
                "defaults_version": "v3",
            },
            {
                "name": "Russell 2000 Top 100",
                "description": "Top 100 Russell 2000 small-cap leaders by market cap",
                "tickers": russell_2000_top_100,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": "high_growth",
                "category": "objective_index",
                "source_mode": "curated_fallback",
                "index_key": "russell2000_top100",
                "registry_backed": True,
                "source_mode_status": "materialized",
                "target_size": 100,
                "cadence": "monthly",
                "enabled": True,
                "defaults_version": "v3",
            },
            {
                "name": "S&P 600",
                "description": "S&P SmallCap 600 constituent universe",
                "tickers": sp600_small_seed,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": "high_growth",
                "category": "objective_index",
                "source_mode": "wikipedia_html",
                "index_key": "sp600_small",
                "registry_backed": True,
                "source_mode_status": "pending_first_refresh",
                "target_size": 600,
                "cadence": "monthly",
                "enabled": True,
                "defaults_version": "v3",
            },
            {
                "name": "Russell 2000",
                "description": "Russell 2000 broad small-cap universe (liquidity-gated)",
                "tickers": russell_2000_seed,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": "high_growth",
                "category": "objective_index",
                "source_mode": "etf_holdings_fallback",
                "index_key": "russell2000_full",
                "registry_backed": True,
                "source_mode_status": "pending_first_refresh",
                "target_size": 2000,
                "cadence": "monthly",
                "enabled": True,
                "defaults_version": "v3",
            },
            {
                "name": "Dividend Aristocrats Top 50",
                "description": "Dividend growth leaders with long track records",
                "tickers": dividend_aristocrats_top_50,
                "source": "built-in",
                "default_preset": "dividend_income",
                "default_investment_profile": "dividend_income",
                "category": "objective_index",
                "source_mode": "wikipedia_html",
                "index_key": "dividend_aristocrats_top50",
                "registry_backed": False,
                "target_size": 50,
                "cadence": "monthly",
                "enabled": True,
            },
            {
                "name": "Energy & Commodities",
                "description": "Top energy producers, oil services, and mining companies",
                "tickers": energy_commodities,
                "source": "built-in",
                "default_preset": "commodity_cyclical",
                "default_investment_profile": "commodity_cyclical",
                "category": "curated_thematic",
                "source_mode": "curated",
                "index_key": None,
                "registry_backed": False,
                "target_size": 25,
                "cadence": "monthly_validation",
                "enabled": True,
            },
            {
                "name": "Healthcare & Life Sciences",
                "description": "Comprehensive US healthcare equity universe: managed care, pharma, biotech, medtech, tools/CROs, diagnostics, and healthcare REITs",
                "tickers": healthcare_life_sciences,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": None,
                "category": "curated_thematic",
                "source_mode": "curated",
                "index_key": None,
                "registry_backed": False,
                "target_size": 105,
                "cadence": "monthly_validation",
                "enabled": True,
            },
            {
                "name": "AI Infrastructure Metals",
                "description": "Critical metals & mining for the AI buildout: copper, uranium, lithium, rare earths, silver, aluminum, steel, and specialty minerals",
                "tickers": ai_infrastructure_metals,
                "source": "built-in",
                "default_preset": "commodity_cyclical",
                "default_investment_profile": "commodity_cyclical",
                "category": "curated_thematic",
                "source_mode": "curated",
                "index_key": None,
                "registry_backed": False,
                "target_size": 54,
                "cadence": "monthly_validation",
                "enabled": True,
            },
            {
                "name": "AI & AI Infrastructure",
                "description": (
                    "US-listed AI stack for focused scans: hyperscalers, AI software, accelerators, "
                    "foundry and chip equipment, memory, servers, optics and networking, power and cooling, "
                    "data-center REITs, and neocloud or powered-shell operators. Not miners, theme ETFs, "
                    "or a general tech list. Excluded from post-close auto scans."
                ),
                "tickers": ai_and_infrastructure,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": None,
                "category": "curated_thematic",
                "source_mode": "curated",
                "index_key": None,
                "registry_backed": False,
                "target_size": 125,
                "cadence": "monthly_validation",
                "enabled": True,
            },
            # -----------------------------------------------------------------
            # ETF & ADR universe expansion (asset class: etf / adr).
            # These use the "etf_technical" preset (for ETFs) and the normal
            # equity-resolver tree (for ADRs). Curated monthly; see the
            # _builtin_watchlist_registry comments for the sourcing rationale.
            # -----------------------------------------------------------------
            {
                "name": "ETFs - Factor & Style",
                "description": "Broad-market, size, style, and academic-factor ETFs (momentum, quality, value, low-vol, dividend growth)",
                "tickers": etfs_factor_style,
                "source": "built-in",
                "default_preset": "etf_technical",
                "default_investment_profile": "etf_baseline",
                "category": "etf_research",
                "source_mode": "curated",
                "index_key": None,
                "registry_backed": False,
                "target_size": 39,
                "cadence": "quarterly_validation",
                "enabled": True,
            },
            {
                "name": "ETFs - Thematic & Industry",
                "description": "Thematic and industry ETFs: semis, biotech, clean energy, cybersecurity, housing, banks, oil services, commodities",
                "tickers": etfs_thematic,
                "source": "built-in",
                "default_preset": "etf_technical",
                "default_investment_profile": "etf_baseline",
                "category": "etf_research",
                "source_mode": "curated",
                "index_key": None,
                "registry_backed": False,
                "target_size": 56,
                "cadence": "quarterly_validation",
                "enabled": True,
            },
            {
                "name": "ETFs - Bonds & Rates",
                "description": "Treasury duration ladder, credit, TIPS, munis, and international bond ETFs — the research tool for reading the rates tape",
                "tickers": etfs_bonds_rates,
                "source": "built-in",
                "default_preset": "etf_technical",
                "default_investment_profile": "etf_baseline",
                "category": "etf_research",
                "source_mode": "curated",
                "index_key": None,
                "registry_backed": False,
                "target_size": 33,
                "cadence": "quarterly_validation",
                "enabled": True,
            },
            {
                "name": "ETFs - Countries & Regions",
                "description": "Developed, emerging, and single-country ETFs for global rotation research",
                "tickers": etfs_countries_regional,
                "source": "built-in",
                "default_preset": "etf_technical",
                "default_investment_profile": "etf_baseline",
                "category": "etf_research",
                "source_mode": "curated",
                "index_key": None,
                "registry_backed": False,
                "target_size": 35,
                "cadence": "quarterly_validation",
                "enabled": True,
            },
            {
                "name": "ADRs - Top",
                "description": "Top foreign listings as US-traded ADRs (semis, China tech/EV, Japan industrials, European pharma, LatAm majors, Canadian banks, Israeli tech)",
                "tickers": adrs_top,
                "source": "built-in",
                "default_preset": None,
                "default_investment_profile": None,
                "category": "curated_thematic",
                "source_mode": "curated",
                "index_key": None,
                "registry_backed": False,
                "target_size": 69,
                "cadence": "quarterly_validation",
                "enabled": True,
            },
            {
                "name": "My Watchlist",
                "description": "Your personal watchlist — add tickers here",
                "tickers": "",
                "source": "user",
                "default_preset": None,
                "default_investment_profile": None,
                "category": "user_default",
                "source_mode": "manual",
                "index_key": None,
                "registry_backed": False,
                "target_size": 0,
                "cadence": "none",
                "enabled": True,
            },
        ]
        for item in registry:
            item.setdefault("defaults_version", "v2")
            item.setdefault("registry_backed", False)
            item.setdefault("index_key", None)
            item.setdefault("source_mode_status", "materialized")
        return registry

    @staticmethod
    def _builtin_registry_by_name() -> Dict[str, Dict[str, Any]]:
        """Name → registry row for built-in watchlist metadata lookups."""
        return {str(item.get("name") or ""): item for item in ResearchDatabase._builtin_watchlist_registry()}

    @staticmethod
    def enrich_watchlist_registry_fields(watchlist: Dict[str, Any]) -> Dict[str, Any]:
        """Attach canonical registry metadata for built-in lists (category, cadence, …)."""
        if str(watchlist.get("source") or "").strip().lower() != "built-in":
            return watchlist
        name = str(watchlist.get("name") or "").strip()
        if not name:
            return watchlist
        meta = ResearchDatabase._builtin_registry_by_name().get(name)
        if not meta:
            return watchlist
        enriched = dict(watchlist)
        for key in (
            "category",
            "source_mode",
            "cadence",
            "target_size",
            "registry_backed",
            "index_key",
            "enabled",
        ):
            if key in meta and meta[key] is not None:
                enriched[key] = meta[key]
        return enriched

    @staticmethod
    def _builtin_watchlists():
        """Compatibility wrapper returning watchlist tuples for seed logic."""
        tuples: List[Tuple[str, str, str, str, Optional[str], Optional[str], Optional[str], int, Optional[str], Optional[str]]] = []
        for item in ResearchDatabase._builtin_watchlist_registry():
            tuples.append(
                (
                    item["name"],
                    item["description"],
                    item["tickers"],
                    item["source"],
                    item["default_preset"],
                    item["default_investment_profile"],
                    item.get("index_key"),
                    int(1 if item.get("registry_backed") else 0),
                    item.get("defaults_version"),
                    item.get("source_mode_status"),
                )
            )
        return tuples

    def _seed_watchlists(self):
        """Seed default watchlists. On first run inserts all; on subsequent runs adds only missing built-ins
        and backfills default_preset on existing built-ins that are missing it."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM watchlists")
            existing_count = cursor.fetchone()[0]

            now = datetime.now().isoformat()
            all_defaults = self._builtin_watchlists()

            if existing_count == 0:
                # Fresh database — insert everything
                for name, desc, tickers, source, default_preset, default_profile, index_key, registry_backed, defaults_version, source_mode_status in all_defaults:
                    cursor.execute(
                        """INSERT INTO watchlists (name, description, tickers, source, index_key, registry_backed, defaults_version, source_mode_status, default_preset, default_investment_profile, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (name, desc, tickers, source, index_key, registry_backed, defaults_version, source_mode_status, default_preset, default_profile, now, now),
                    )
            else:
                # Existing database — add only missing built-in watchlists
                cursor.execute("SELECT name FROM watchlists WHERE source = 'built-in'")
                existing_names = {row[0] for row in cursor.fetchall()}

                for name, desc, tickers, source, default_preset, default_profile, index_key, registry_backed, defaults_version, source_mode_status in all_defaults:
                    if source == "built-in" and name not in existing_names:
                        cursor.execute(
                            """INSERT INTO watchlists (name, description, tickers, source, index_key, registry_backed, defaults_version, source_mode_status, default_preset, default_investment_profile, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (name, desc, tickers, source, index_key, registry_backed, defaults_version, source_mode_status, default_preset, default_profile, now, now),
                        )

                # Backfill default_preset on existing built-ins that don't have one set yet
                preset_map = {name: default_preset for name, _, _, source, default_preset, _, _, _, _, _ in all_defaults
                              if source == "built-in" and default_preset is not None}
                for wl_name, preset in preset_map.items():
                    if wl_name in existing_names:
                        cursor.execute(
                            "UPDATE watchlists SET default_preset = ? WHERE name = ? AND (default_preset IS NULL OR default_preset = '')",
                            (preset, wl_name),
                        )

                # Backfill default_investment_profile on existing built-ins
                profile_map = {name: default_profile for name, _, _, source, _, default_profile, _, _, _, _ in all_defaults
                               if source == "built-in" and default_profile is not None}
                for wl_name, profile in profile_map.items():
                    if wl_name in existing_names:
                        cursor.execute(
                            "UPDATE watchlists SET default_investment_profile = ? WHERE name = ? AND (default_investment_profile IS NULL OR default_investment_profile = '')",
                            (profile, wl_name),
                        )

                # Backfill registry metadata on existing built-ins
                registry_map = {
                    name: (index_key, registry_backed, defaults_version, source_mode_status)
                    for name, _, _, source, _, _, index_key, registry_backed, defaults_version, source_mode_status in all_defaults
                    if source == "built-in"
                }
                for wl_name, payload in registry_map.items():
                    if wl_name in existing_names:
                        idx_key, reg_backed, def_ver, mode_status = payload
                        cursor.execute(
                            """
                            UPDATE watchlists
                            SET index_key = COALESCE(index_key, ?),
                                registry_backed = CASE
                                    WHEN registry_backed IS NULL THEN ?
                                    WHEN registry_backed = 0 AND ? = 1 THEN 1
                                    ELSE registry_backed
                                END,
                                defaults_version = COALESCE(defaults_version, ?),
                                source_mode_status = COALESCE(source_mode_status, ?)
                            WHERE name = ? AND source = 'built-in'
                            """,
                            (idx_key, reg_backed, reg_backed, def_ver, mode_status, wl_name),
                        )

                # v3: broad objective indices → adaptive per-ticker presets (clear pinned books)
                v3_preset_map = {
                    item["name"]: item.get("default_preset")
                    for item in ResearchDatabase._builtin_watchlist_registry()
                    if item.get("defaults_version") == "v3"
                }
                for wl_name, target_preset in v3_preset_map.items():
                    if wl_name in existing_names:
                        cursor.execute(
                            """
                            UPDATE watchlists
                            SET default_preset = ?,
                                defaults_version = 'v3',
                                updated_at = ?
                            WHERE name = ? AND source = 'built-in'
                              AND COALESCE(defaults_version, '') <> 'v3'
                            """,
                            (target_preset, now, wl_name),
                        )

            conn.commit()

        # Targeted one-time migration: remove deprecated built-ins.
        # This is intentionally NOT a generic built-in pruning mechanism.
        deprecated_thematics = (
            "Space & Defense Tech",
            "Photonics & Semiconductors",
            "Biotech Pre-Catalyst",
            "Small Cap Deep Tech",
            "Dow Jones 30",
            "ARK Innovation",
            "Recent IPOs",
            "High Short Interest",
            "Russell 2000 Top 50",
            "Dividend Aristocrats",
        )
        placeholders = ",".join("?" for _ in deprecated_thematics)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT id, name FROM watchlists WHERE source = 'built-in' AND name IN ({placeholders})",
                deprecated_thematics,
            )
            to_remove = [(row[0], row[1]) for row in cursor.fetchall()]

        for watchlist_id, watchlist_name in to_remove:
            logger.info(
                "Removing deprecated built-in thematic watchlist '%s' (id=%d)",
                watchlist_name,
                watchlist_id,
            )
            self.delete_watchlist(watchlist_id)

        # Ensure broad registry-backed watchlists surface as pending until first true materialization.
        broad_keys = ("sp500_full", "sp400_mid", "sp600_small", "russell2000_full")
        with self._connect() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" for _ in broad_keys)
            cursor.execute(
                f"""
                UPDATE watchlists
                SET source_mode_status = 'pending_first_refresh',
                    tickers = CASE
                        WHEN materialized_as_of IS NULL OR materialized_as_of = '' THEN ''
                        ELSE tickers
                    END
                WHERE source = 'built-in'
                  AND registry_backed = 1
                  AND index_key IN ({placeholders})
                  AND (materialized_as_of IS NULL OR materialized_as_of = '')
                """,
                broad_keys,
            )
            conn.commit()

    def get_watchlists(self) -> List[Dict[str, Any]]:
        """Get all watchlists."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM watchlists ORDER BY id")
            rows = cursor.fetchall()
        return [self.enrich_watchlist_registry_fields(dict(row)) for row in rows]

    def get_watchlist(self, watchlist_id: int) -> Optional[Dict[str, Any]]:
        """Get a single watchlist by ID."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM watchlists WHERE id = ?", (watchlist_id,))
            row = cursor.fetchone()
        return self.enrich_watchlist_registry_fields(dict(row)) if row else None

    def create_watchlist(self, name: str, description: str = "", tickers: str = "",
                         default_preset: str = None, default_investment_profile: str = None) -> int:
        """Create a new watchlist. Returns the ID."""
        from tradingagents.screening.discovery import parse_watchlist_tickers

        valid, _rejected = parse_watchlist_tickers(tickers)
        tickers = ",".join(valid)
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO watchlists (name, description, tickers, source, default_preset, default_investment_profile, created_at, updated_at)
                   VALUES (?, ?, ?, 'user', ?, ?, ?, ?)""",
                (name, description, tickers, default_preset, default_investment_profile, now, now),
            )
            wid = cursor.lastrowid
            conn.commit()
            return wid

    def update_watchlist(self, watchlist_id: int, name: str = None, description: str = None,
                         tickers: str = None, default_preset: str = "__unset__",
                         default_investment_profile: str = "__unset__") -> bool:
        """Update a watchlist. Pass default_preset/default_investment_profile=None to clear, or a value to set."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            updates = ["updated_at = ?"]
            params: list = [now]
            if name is not None:
                updates.append("name = ?")
                params.append(name)
            if description is not None:
                updates.append("description = ?")
                params.append(description)
            if tickers is not None:
                from tradingagents.screening.discovery import parse_watchlist_tickers

                valid, _rejected = parse_watchlist_tickers(tickers)
                updates.append("tickers = ?")
                params.append(",".join(valid))
            if default_preset != "__unset__":
                updates.append("default_preset = ?")
                params.append(default_preset)
            if default_investment_profile != "__unset__":
                updates.append("default_investment_profile = ?")
                params.append(default_investment_profile)
            params.append(watchlist_id)
            cursor.execute(f"UPDATE watchlists SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
            return cursor.rowcount > 0

    def delete_watchlist(self, watchlist_id: int) -> bool:
        """Delete a watchlist and cascade-delete its screening runs and results."""
        with self._connect() as conn:
            cursor = conn.cursor()
            # Cascade: delete movers side-table rows for runs belonging to this watchlist
            cursor.execute(
                """
                DELETE FROM movers_run_details WHERE run_id IN
                (SELECT id FROM screening_runs WHERE watchlist_id = ?)
                """,
                (watchlist_id,),
            )
            # Cascade: delete screening results for runs belonging to this watchlist
            cursor.execute("""
                DELETE FROM screening_results WHERE run_id IN
                (SELECT id FROM screening_runs WHERE watchlist_id = ?)
            """, (watchlist_id,))
            # Cascade: delete screening runs
            cursor.execute("DELETE FROM screening_runs WHERE watchlist_id = ?", (watchlist_id,))
            # Delete the watchlist itself
            cursor.execute("DELETE FROM watchlists WHERE id = ?", (watchlist_id,))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # AUTO-WATCHLIST LIFECYCLE METHODS
    # =========================================================================

    def create_auto_watchlist(
        self,
        name: str,
        description: str,
        tickers: str,
        default_preset: str,
        default_investment_profile: str,
        theme_rationale: str,
        sector_snapshot: str,
        expires_at: str,
    ) -> int:
        """Create an auto-generated watchlist. Returns the new ID."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO watchlists
                   (name, description, tickers, source, default_preset,
                    default_investment_profile, theme_rationale, sector_snapshot,
                    expires_at, created_at, updated_at)
                   VALUES (?, ?, ?, 'auto', ?, ?, ?, ?, ?, ?, ?)""",
                (name, description, tickers, default_preset,
                 default_investment_profile, theme_rationale, sector_snapshot,
                 expires_at, now, now),
            )
            wid = cursor.lastrowid
            conn.commit()
            return wid

    def get_auto_watchlists(self) -> List[Dict[str, Any]]:
        """Return all watchlists with ``source='auto'``."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM watchlists WHERE source = 'auto' ORDER BY created_at DESC")
            return [dict(r) for r in cursor.fetchall()]

    def renew_auto_watchlist(self, watchlist_id: int, new_expires_at: str,
                             sector_snapshot: str = None,
                             tickers: str = None) -> bool:
        """Extend the expiry of an auto-watchlist, optionally refreshing its
        sector snapshot and tickers."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            updates = ["expires_at = ?", "updated_at = ?"]
            params: list = [new_expires_at, now]
            if sector_snapshot is not None:
                updates.append("sector_snapshot = ?")
                params.append(sector_snapshot)
            if tickers is not None:
                updates.append("tickers = ?")
                params.append(tickers)
            params.append(watchlist_id)
            cursor.execute(
                f"UPDATE watchlists SET {', '.join(updates)} WHERE id = ? AND source = 'auto'",
                params,
            )
            conn.commit()
            return cursor.rowcount > 0

    def pin_auto_watchlist(self, watchlist_id: int) -> bool:
        """Pin an auto-watchlist: convert to user-owned, deactivate lifecycle."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE watchlists
                   SET source = 'user', expires_at = NULL, sector_snapshot = NULL,
                       updated_at = ?
                   WHERE id = ? AND source = 'auto'""",
                (now, watchlist_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def expire_auto_watchlists(self) -> int:
        """Delete all auto-watchlists whose ``expires_at`` has passed.

        Uses :meth:`delete_watchlist` for each expired entry to ensure
        cascading cleanup of screening runs and results.  Returns the
        number of watchlists deleted.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name FROM watchlists WHERE source = 'auto' AND expires_at < ?",
                (now,),
            )
            expired = cursor.fetchall()

        deleted = 0
        for row in expired:
            wl_id, wl_name = row["id"], row["name"]
            logger.info("Expiring auto-watchlist '%s' (id=%d)", wl_name, wl_id)
            if self.delete_watchlist(wl_id):
                deleted += 1
        return deleted

    # =========================================================================
    # BUILT-IN REFRESH PROPOSALS
    # =========================================================================

    @staticmethod
    def _normalize_ticker_csv(tickers: str) -> List[str]:
        values = []
        for raw in (tickers or "").replace("\n", ",").split(","):
            sym = (raw or "").strip().upper()
            if sym:
                values.append(sym)
        # Preserve order while de-duplicating
        return list(dict.fromkeys(values))

    @staticmethod
    def _ticker_set_hash(tickers: List[str]) -> str:
        normalized = sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()})
        payload = ",".join(normalized)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get_builtin_watchlists(self) -> List[Dict[str, Any]]:
        """Return all built-in watchlists."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watchlists WHERE source = 'built-in' ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_builtin_watchlist_by_name(self, watchlist_name: str) -> Optional[Dict[str, Any]]:
        """Return a single built-in watchlist by name."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM watchlists WHERE source = 'built-in' AND name = ?",
                (watchlist_name,),
            ).fetchone()
        return dict(row) if row else None

    def apply_builtin_deprecations(self, names: List[str]) -> Dict[str, Any]:
        """Hard-delete deprecated built-ins by exact name. Idempotent."""
        if not names:
            return {"removed": 0, "removed_names": []}
        placeholders = ",".join("?" for _ in names)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, name FROM watchlists WHERE source = 'built-in' AND name IN ({placeholders})",
                tuple(names),
            ).fetchall()
        to_remove = [(int(r["id"]), str(r["name"])) for r in rows]
        removed_names: List[str] = []
        for wid, name in to_remove:
            if self.delete_watchlist(wid):
                removed_names.append(name)
        return {"removed": len(removed_names), "removed_names": removed_names}

    def create_builtin_refresh_proposal(
        self,
        items: List[Dict[str, Any]],
        source: str = "manual",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Persist a built-in refresh proposal with diff items.

        Each item supports:
          - watchlist_id, watchlist_name
          - old_tickers, new_tickers (lists of symbols)
          - adds, removes (optional precomputed lists)
          - warnings (optional list of warning strings)
        """
        now = datetime.now().isoformat()
        metadata = metadata or {}
        warning_count = 0

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO builtin_refresh_proposals
                   (status, source, warning_count, metadata_json, summary_json, created_at, applied_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                ("proposed", source, 0, json.dumps(metadata), json.dumps({}), now),
            )
            proposal_id = int(cursor.lastrowid)

            for item in items:
                old_tickers = [str(t).upper() for t in item.get("old_tickers", []) if str(t).strip()]
                new_tickers = [str(t).upper() for t in item.get("new_tickers", []) if str(t).strip()]

                old_hash = item.get("old_hash") or self._ticker_set_hash(old_tickers)
                new_hash = item.get("new_hash") or self._ticker_set_hash(new_tickers)
                adds = item.get("adds")
                removes = item.get("removes")
                if adds is None:
                    adds = sorted(set(new_tickers) - set(old_tickers))
                if removes is None:
                    removes = sorted(set(old_tickers) - set(new_tickers))

                warnings = [str(w) for w in item.get("warnings", []) if str(w).strip()]
                warning_count += len(warnings)
                is_noop = 1 if old_hash == new_hash else 0
                row_status = "noop" if is_noop else "pending"

                cursor.execute(
                    """INSERT INTO builtin_refresh_items
                       (proposal_id, watchlist_id, watchlist_name, old_count, new_count,
                        old_hash, new_hash, adds_json, removes_json, warnings_json,
                        is_noop, status, created_at, applied_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        proposal_id,
                        item.get("watchlist_id"),
                        item.get("watchlist_name"),
                        len(old_tickers),
                        len(new_tickers),
                        old_hash,
                        new_hash,
                        json.dumps(adds),
                        json.dumps(removes),
                        json.dumps(warnings),
                        is_noop,
                        row_status,
                        now,
                    ),
                )

            summary = {
                "total_items": len(items),
                "noop_items": sum(1 for i in items if self._ticker_set_hash(i.get("old_tickers", [])) == self._ticker_set_hash(i.get("new_tickers", []))),
            }
            cursor.execute(
                """UPDATE builtin_refresh_proposals
                   SET warning_count = ?, summary_json = ?
                   WHERE id = ?""",
                (warning_count, json.dumps(summary), proposal_id),
            )
            conn.commit()

        return proposal_id

    def list_builtin_refresh_proposals(self, limit: int = 20) -> List[Dict[str, Any]]:
        """List recent built-in refresh proposals."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, status, source, warning_count, metadata_json, summary_json,
                          created_at, applied_at
                   FROM builtin_refresh_proposals
                   ORDER BY id DESC
                   LIMIT ?""",
                (int(limit),),
            ).fetchall()

        out: List[Dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            for key in ("metadata_json", "summary_json"):
                try:
                    row[key.replace("_json", "")] = json.loads(row.pop(key) or "{}")
                except Exception:
                    row[key.replace("_json", "")] = {}
                    row.pop(key, None)
            out.append(row)
        return out

    def get_builtin_refresh_proposal(self, proposal_id: int) -> Optional[Dict[str, Any]]:
        """Return a proposal and all its diff items."""
        with self._connect() as conn:
            proposal = conn.execute(
                """SELECT id, status, source, warning_count, metadata_json, summary_json,
                          created_at, applied_at
                   FROM builtin_refresh_proposals
                   WHERE id = ?""",
                (proposal_id,),
            ).fetchone()
            if not proposal:
                return None
            items = conn.execute(
                """SELECT id, proposal_id, watchlist_id, watchlist_name, old_count, new_count,
                          old_hash, new_hash, adds_json, removes_json, warnings_json,
                          is_noop, status, created_at, applied_at
                   FROM builtin_refresh_items
                   WHERE proposal_id = ?
                   ORDER BY watchlist_name""",
                (proposal_id,),
            ).fetchall()

        payload = dict(proposal)
        payload["metadata"] = json.loads(payload.pop("metadata_json") or "{}")
        payload["summary"] = json.loads(payload.pop("summary_json") or "{}")
        parsed_items: List[Dict[str, Any]] = []
        for row in items:
            item = dict(row)
            item["adds"] = json.loads(item.pop("adds_json") or "[]")
            item["removes"] = json.loads(item.pop("removes_json") or "[]")
            item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
            item["is_noop"] = bool(item.get("is_noop"))
            parsed_items.append(item)
        payload["items"] = parsed_items
        return payload

    def apply_builtin_refresh_proposal(
        self,
        proposal_id: int,
        force_apply: bool = False,
        force_reason: str = "",
        guard_config: Optional[Dict[str, Any]] = None,
        bootstrap: bool = False,
    ) -> Dict[str, Any]:
        """Atomically apply a proposal to built-in watchlists.

        Only items with ``is_noop = 0`` are applied.
        """
        now = datetime.now().isoformat()
        cfg = guard_config or {}
        enforce_high_risk_apply = bool(cfg.get("enforce_high_risk_apply", True))
        require_force_reason = bool(cfg.get("require_force_reason", True))
        force_apply_churn_pct = float(cfg.get("force_apply_churn_pct", cfg.get("max_churn_pct", 0.30)))
        with self._connect() as conn:
            cursor = conn.cursor()
            proposal = cursor.execute(
                "SELECT * FROM builtin_refresh_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if not proposal:
                return {"applied": False, "message": "Proposal not found", "proposal_id": proposal_id}
            if proposal["status"] == "applied":
                return {"applied": False, "message": "Proposal already applied", "proposal_id": proposal_id}

            items = cursor.execute(
                "SELECT * FROM builtin_refresh_items WHERE proposal_id = ? ORDER BY id",
                (proposal_id,),
            ).fetchall()

            # Phrases that indicate the *primary* index/ETF-holdings source
            # degraded or was substituted with a fallback provider (as opposed
            # to routine per-symbol validation noise like "N invalid symbols
            # filtered"). Any match tags the item as a source_failure risk so
            # a swapped-provider list (e.g. iShares -> Vanguard fallback) is
            # never silently applied without the operator noticing.
            _SOURCE_DEGRADED_PATTERNS = (
                "source fetch failed",
                "source empty",
                "no symbol table found",
                "holdings fetch returned html",
                "csv unavailable",
                "holdings parse failed",
                "predicate selection failed",
                "predicate failed",
                "vanguard holdings fetch failed",
                "vanguard holdings parse failed",
            )
            risk_items: List[Dict[str, Any]] = []
            for item in items:
                warnings = json.loads(item["warnings_json"] or "[]")
                if not warnings:
                    continue
                risk_types: List[str] = []
                for warning in warnings:
                    warning_text = str(warning).lower()
                    if "validation sparse" in warning_text:
                        risk_types.append("sparse_validation")
                    if any(pattern in warning_text for pattern in _SOURCE_DEGRADED_PATTERNS):
                        risk_types.append("source_failure")
                    if "high churn warning" in warning_text:
                        # Example: "high churn warning (44.0% > 30.0%)"
                        try:
                            start = warning_text.find("(")
                            end = warning_text.find("%", start + 1)
                            pct = float(warning_text[start + 1:end].strip()) / 100.0
                            if pct >= force_apply_churn_pct:
                                risk_types.append("high_churn")
                        except Exception:
                            risk_types.append("high_churn")
                if risk_types:
                    risk_items.append(
                        {
                            "watchlist_name": item["watchlist_name"],
                            "risk_types": sorted(set(risk_types)),
                            "warnings": warnings,
                        }
                    )

            if (not bootstrap) and enforce_high_risk_apply and risk_items and not force_apply:
                return {
                    "applied": False,
                    "message": "Proposal contains high-risk items; force_apply required",
                    "proposal_id": proposal_id,
                    "requires_force_apply": True,
                    "risk_items": risk_items,
                }

            if (not bootstrap) and force_apply and require_force_reason and not str(force_reason or "").strip():
                return {
                    "applied": False,
                    "message": "Force apply reason required",
                    "proposal_id": proposal_id,
                }

            updated_count = 0
            noop_count = 0
            missing_count = 0

            for item in items:
                if int(item["is_noop"]) == 1:
                    noop_count += 1
                    cursor.execute(
                        """UPDATE builtin_refresh_items
                           SET status = 'noop', applied_at = ?
                           WHERE id = ?""",
                        (now, item["id"]),
                    )
                    continue

                wl = cursor.execute(
                    "SELECT id, index_key, registry_backed FROM watchlists WHERE source = 'built-in' AND name = ?",
                    (item["watchlist_name"],),
                ).fetchone()
                if not wl:
                    missing_count += 1
                    cursor.execute(
                        """UPDATE builtin_refresh_items
                           SET status = 'missing', applied_at = ?
                           WHERE id = ?""",
                        (now, item["id"]),
                    )
                    continue

                next_set: List[str] = []
                wl_registry_backed = int(wl["registry_backed"] or 0) if "registry_backed" in wl.keys() else 0
                wl_index_key = str(wl["index_key"] or "").strip() if "index_key" in wl.keys() else ""
                if wl_registry_backed == 1 and wl_index_key:
                    registry_rows = self.get_latest_index_constituents(wl_index_key, in_scope_only=True)
                    next_set = [
                        str(r.get("ticker") or "").upper()
                        for r in registry_rows
                        if str(r.get("ticker") or "").strip()
                    ]
                    item_new_count = int(item["new_count"] or 0)
                    if item_new_count > 0:
                        next_set = next_set[:item_new_count]
                    next_set = list(dict.fromkeys(next_set))
                if not next_set:
                    adds = json.loads(item["adds_json"] or "[]")
                    removes = json.loads(item["removes_json"] or "[]")
                    current = cursor.execute(
                        "SELECT tickers FROM watchlists WHERE id = ?",
                        (wl["id"],),
                    ).fetchone()
                    current_tickers = self._normalize_ticker_csv(current["tickers"] if current else "")
                    next_set = sorted((set(current_tickers) | set(adds)) - set(removes))
                next_csv = ",".join(next_set)

                cursor.execute(
                    """
                    UPDATE watchlists
                    SET tickers = ?, updated_at = ?, source_mode_status = ?, materialized_as_of = ?, materialized_source = ?
                    WHERE id = ?
                    """,
                    (
                        next_csv,
                        now,
                        ("materialized" if next_set else "pending_first_refresh"),
                        now[:10],
                        "builtin_refresh_apply",
                        wl["id"],
                    ),
                )
                cursor.execute(
                    """UPDATE builtin_refresh_items
                       SET status = 'applied', applied_at = ?, new_count = ?, new_hash = ?
                       WHERE id = ?""",
                    (now, len(next_set), self._ticker_set_hash(next_set), item["id"]),
                )
                updated_count += 1

            summary = {
                "proposal_id": proposal_id,
                "total_items": len(items),
                "updated_items": updated_count,
                "noop_items": noop_count,
                "missing_items": missing_count,
                "forced_apply": bool(force_apply),
                "force_reason": str(force_reason or "").strip(),
                "risk_items": risk_items,
            }
            cursor.execute(
                """UPDATE builtin_refresh_proposals
                   SET status = 'applied', applied_at = ?, summary_json = ?
                   WHERE id = ?""",
                (now, json.dumps(summary), proposal_id),
            )
            conn.commit()
            return {"applied": True, **summary}

    # =========================================================================
    # INDEX CONSTITUENT REGISTRY (watchlist universe restructure)
    # =========================================================================

    def save_index_constituents(
        self,
        index_key: str,
        rows: List[Dict[str, Any]],
        as_of: Optional[str] = None,
        source: str = "manual",
        replace_existing_for_as_of: bool = True,
    ) -> int:
        """Upsert index constituents for a given index key and as-of date."""
        idx = str(index_key or "").strip().lower()
        if not idx:
            return 0
        if idx not in _REGISTRY_INDEX_ALLOWLIST:
            raise ValueError(f"index_key not allowed for registry writes: {idx}")
        as_of_value = str(as_of or datetime.now().strftime("%Y-%m-%d"))
        now = datetime.now().isoformat()
        normalized_rows: List[Tuple[Any, ...]] = []
        for row in rows:
            ticker = str((row or {}).get("ticker") or "").strip().upper()
            if not ticker:
                continue
            row_source = str((row or {}).get("source") or source).strip().lower()
            if row_source not in _REGISTRY_SOURCE_ALLOWLIST:
                raise ValueError(f"registry source not allowed: {row_source}")
            normalized_rows.append(
                (
                    idx,
                    ticker,
                    as_of_value,
                    row_source,
                    row.get("weight"),
                    row.get("avg_dollar_volume_usd"),
                    row.get("market_cap"),
                    row.get("market_cap_tier"),
                    row.get("liquidity_rank"),
                    int(1 if row.get("in_scope", True) else 0),
                    now,
                )
            )
        if not normalized_rows:
            return 0
        with self._connect() as conn:
            cursor = conn.cursor()
            if replace_existing_for_as_of:
                cursor.execute(
                    "DELETE FROM index_constituents WHERE index_key = ? AND as_of = ?",
                    (idx, as_of_value),
                )
            cursor.executemany(
                """
                INSERT INTO index_constituents (
                    index_key, ticker, as_of, source, weight, avg_dollar_volume_usd,
                    market_cap, market_cap_tier, liquidity_rank, in_scope, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(index_key, ticker, as_of) DO UPDATE SET
                    source = excluded.source,
                    weight = excluded.weight,
                    avg_dollar_volume_usd = excluded.avg_dollar_volume_usd,
                    market_cap = excluded.market_cap,
                    market_cap_tier = excluded.market_cap_tier,
                    liquidity_rank = excluded.liquidity_rank,
                    in_scope = excluded.in_scope,
                    updated_at = excluded.updated_at
                """,
                normalized_rows,
            )
            conn.commit()
        return len(normalized_rows)

    def get_latest_index_constituents(
        self,
        index_key: str,
        in_scope_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return latest available constituent rows for an index key."""
        idx = str(index_key or "").strip().lower()
        if not idx:
            return []
        with self._connect() as conn:
            cursor = conn.cursor()
            latest = cursor.execute(
                "SELECT MAX(as_of) AS as_of FROM index_constituents WHERE index_key = ?",
                (idx,),
            ).fetchone()
            as_of_value = latest["as_of"] if latest else None
            if not as_of_value:
                return []
            if in_scope_only:
                rows = cursor.execute(
                    """
                    SELECT * FROM index_constituents
                    WHERE index_key = ? AND as_of = ? AND in_scope = 1
                    ORDER BY
                        CASE WHEN liquidity_rank IS NULL THEN 1 ELSE 0 END ASC,
                        liquidity_rank ASC,
                        COALESCE(avg_dollar_volume_usd, 0) DESC,
                        COALESCE(market_cap, 0) DESC,
                        ticker ASC
                    """,
                    (idx, as_of_value),
                ).fetchall()
            else:
                rows = cursor.execute(
                    """
                    SELECT * FROM index_constituents
                    WHERE index_key = ? AND as_of = ?
                    ORDER BY ticker ASC
                    """,
                    (idx, as_of_value),
                ).fetchall()
        return [dict(r) for r in rows]

    def materialize_registry_watchlist(self, watchlist_name: str, index_key: str, target_size: int = 0) -> Dict[str, Any]:
        """Materialize watchlist tickers from latest index constituent registry rows."""
        rows = self.get_latest_index_constituents(index_key=index_key, in_scope_only=True)
        ordered = [str(r.get("ticker") or "").upper() for r in rows if str(r.get("ticker") or "").strip()]
        if int(target_size or 0) > 0:
            ordered = ordered[: int(target_size)]
        next_csv = ",".join(ordered)
        now = datetime.now().isoformat()
        materialized_as_of = str(rows[0].get("as_of") or now[:10]) if rows else now[:10]
        materialized_sources = sorted({str(r.get("source") or "").strip() for r in rows if str(r.get("source") or "").strip()})
        materialized_source = ",".join(materialized_sources[:3]) if materialized_sources else "unknown"
        with self._connect() as conn:
            cursor = conn.cursor()
            current = cursor.execute(
                "SELECT id, tickers FROM watchlists WHERE source = 'built-in' AND name = ?",
                (watchlist_name,),
            ).fetchone()
            if not current:
                return {"updated": False, "reason": "watchlist_missing", "watchlist_name": watchlist_name}
            old_tickers = self._normalize_ticker_csv(current["tickers"] if current else "")
            cursor.execute(
                """
                UPDATE watchlists
                SET tickers = ?, updated_at = ?, index_key = ?, registry_backed = 1,
                    source_mode_status = ?, materialized_as_of = ?, materialized_source = ?
                WHERE id = ?
                """,
                (
                    next_csv,
                    now,
                    str(index_key or "").strip().lower(),
                    ("materialized" if ordered else "pending_first_refresh"),
                    materialized_as_of,
                    materialized_source,
                    int(current["id"]),
                ),
            )
            conn.commit()
        new_tickers = self._normalize_ticker_csv(next_csv)
        return {
            "updated": True,
            "watchlist_name": watchlist_name,
            "old_count": len(old_tickers),
            "new_count": len(new_tickers),
            "adds": sorted(set(new_tickers) - set(old_tickers)),
            "removes": sorted(set(old_tickers) - set(new_tickers)),
        }

    def compute_watchlist_universe_metrics(self) -> Dict[str, Any]:
        """Compute overlap/truncation/hydration baseline metrics for QA gates."""
        watchlists = self.get_watchlists()
        builtins = [w for w in watchlists if str(w.get("source", "")).lower() == "built-in"]
        parsed: Dict[str, List[str]] = {
            str(w.get("name") or ""): self._normalize_ticker_csv(str(w.get("tickers") or ""))
            for w in builtins
        }
        unique_all = sorted({t for vals in parsed.values() for t in vals})
        overlap_pairs: List[Dict[str, Any]] = []
        names = sorted(parsed.keys())
        for i, left in enumerate(names):
            left_set = set(parsed[left])
            for right in names[i + 1 :]:
                right_set = set(parsed[right])
                inter = left_set & right_set
                union = left_set | right_set
                if not union:
                    continue
                jaccard = round(len(inter) / len(union), 4)
                if jaccard <= 0:
                    continue
                overlap_pairs.append(
                    {
                        "left": left,
                        "right": right,
                        "overlap_count": len(inter),
                        "jaccard": jaccard,
                    }
                )
        overlap_pairs.sort(key=lambda r: (r.get("jaccard", 0), r.get("overlap_count", 0)), reverse=True)

        hydrated = self.get_ticker_metadata_bulk(unique_all) if unique_all else {}
        hydration_required_capacity = len(unique_all)
        hydration_cap_utilization = round((len(hydrated) / max(1, hydration_required_capacity)), 4)
        return {
            "built_in_watchlist_count": len(builtins),
            "unique_tickers_baseline": len(unique_all),
            "hydration_required_capacity": hydration_required_capacity,
            "hydration_cached_count": len(hydrated),
            "hydration_cap_utilization": hydration_cap_utilization,
            "top_overlap_pairs": overlap_pairs[:20],
            "watchlist_sizes": {
                name: len(tickers)
                for name, tickers in sorted(parsed.items(), key=lambda item: item[0])
            },
        }

    def watchlist_parity_diff(self, left_name: str, right_name: str) -> Dict[str, Any]:
        """Compute deterministic parity/churn diff between two watchlists."""
        left = self.get_builtin_watchlist_by_name(left_name) or {}
        right = self.get_builtin_watchlist_by_name(right_name) or {}
        left_tickers = self._normalize_ticker_csv(str(left.get("tickers") or ""))
        right_tickers = self._normalize_ticker_csv(str(right.get("tickers") or ""))
        left_set = set(left_tickers)
        right_set = set(right_tickers)
        adds = sorted(right_set - left_set)
        removes = sorted(left_set - right_set)
        churn = (len(adds) + len(removes)) / max(1, len(left_set))
        overlap = sorted(left_set & right_set)
        return {
            "left": left_name,
            "right": right_name,
            "left_count": len(left_tickers),
            "right_count": len(right_tickers),
            "overlap_count": len(overlap),
            "adds": adds,
            "removes": removes,
            "churn_pct": round(churn, 4),
            "within_gate": churn <= 0.10,
        }

    def get_registry_bootstrap_candidates(self) -> List[Dict[str, Any]]:
        """Return registry-backed built-ins that still need first materialization."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM watchlists
                WHERE source = 'built-in'
                  AND registry_backed = 1
                  AND (
                    source_mode_status IS NULL
                    OR source_mode_status = ''
                    OR source_mode_status = 'pending_first_refresh'
                    OR tickers = ''
                  )
                ORDER BY name
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def save_sector_medians(
        self,
        medians: Dict[str, Dict[str, float]],
        universe_size: int,
    ) -> int:
        """Persist broad-universe sector valuation medians for later scans."""
        if not isinstance(medians, dict) or not medians:
            return 0
        payload: Dict[str, Dict[str, float]] = {}
        for metric, sectors in medians.items():
            if not isinstance(sectors, dict):
                continue
            cleaned: Dict[str, float] = {}
            for sector, value in sectors.items():
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    continue
                if num > 0:
                    cleaned[str(sector)] = num
            if cleaned:
                payload[str(metric)] = cleaned
        if not payload:
            return 0
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sector_median_snapshots (universe_size, medians_json, created_at)
                VALUES (?, ?, ?)
                """,
                (int(universe_size), json.dumps(payload), now),
            )
            rid = int(cursor.lastrowid)
            conn.commit()
            return rid

    def get_latest_sector_medians(self) -> Dict[str, Dict[str, float]]:
        """Return the most recent persisted sector medians, or {}."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT medians_json FROM sector_median_snapshots
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return {}
        raw = row["medians_json"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, float]] = {}
        for metric, sectors in data.items():
            if not isinstance(sectors, dict):
                continue
            cleaned: Dict[str, float] = {}
            for sector, value in sectors.items():
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    continue
                if num > 0:
                    cleaned[str(sector)] = num
            if cleaned:
                out[str(metric)] = cleaned
        return out

    def record_runtime_metric(self, metric_key: str, duration_seconds: float, context: Optional[Dict[str, Any]] = None) -> int:
        """Persist runtime duration samples for p95 QA gate reporting."""
        key = str(metric_key or "").strip()
        if not key:
            return 0
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO runtime_metrics (metric_key, duration_seconds, context_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (key, float(duration_seconds), json.dumps(context or {}), now),
            )
            rid = int(cursor.lastrowid)
            conn.commit()
            return rid

    def get_runtime_metric_p95(
        self,
        metric_key: str,
        limit: int = 200,
        since_days: Optional[int] = None,
    ) -> Optional[float]:
        """Compute p95 from recent runtime metric samples.

        Args:
            metric_key: The runtime metric key (e.g. ``scan_all_duration_seconds``).
            limit: Hard cap on the number of most-recent samples considered.
            since_days: When provided, only samples created within the last
                ``since_days`` are used. This keeps the p95 representative of
                current behaviour after fixes/optimizations land — older runs
                from regressed builds quickly age out instead of dragging the
                metric upward indefinitely.
        """
        key = str(metric_key or "").strip()
        if not key:
            return None
        params: List[Any] = [key]
        where = "metric_key = ?"
        if since_days is not None and since_days > 0:
            where += " AND created_at >= datetime('now', ?)"
            params.append(f"-{int(since_days)} day")
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT duration_seconds
                FROM runtime_metrics
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        vals = sorted(float(r["duration_seconds"]) for r in rows if r["duration_seconds"] is not None)
        if not vals:
            return None
        if len(vals) == 1:
            return round(vals[0], 3)
        idx = max(0, min(len(vals) - 1, int(round(0.95 * (len(vals) - 1)))))
        return round(vals[idx], 3)

    def prune_runtime_metrics(
        self,
        metric_key: Optional[str] = None,
        older_than_days: int = 30,
    ) -> int:
        """Remove runtime_metrics samples older than ``older_than_days``.

        Operators can call this periodically (or after a major perf fix lands)
        so that the p95 reporting reflects current behaviour. When
        ``metric_key`` is omitted, prunes across all keys.

        Returns the number of rows deleted.
        """
        days = max(1, int(older_than_days))
        with self._connect() as conn:
            cursor = conn.cursor()
            if metric_key:
                cursor.execute(
                    """
                    DELETE FROM runtime_metrics
                    WHERE metric_key = ? AND created_at < datetime('now', ?)
                    """,
                    (str(metric_key).strip(), f"-{days} day"),
                )
            else:
                cursor.execute(
                    "DELETE FROM runtime_metrics WHERE created_at < datetime('now', ?)",
                    (f"-{days} day",),
                )
            deleted = int(cursor.rowcount or 0)
            conn.commit()
            return deleted

    # =========================================================================
    # TICKER METADATA (auto-resolved classifications)
    # =========================================================================

    def get_ticker_metadata(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get cached metadata for a single ticker."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ticker_metadata WHERE ticker = ?", (ticker.upper(),))
            row = cursor.fetchone()
        return dict(row) if row else None

    def get_ticker_metadata_bulk(self, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get cached metadata for multiple tickers. Returns {TICKER: row_dict}."""
        if not tickers:
            return {}
        upper = [t.upper() for t in tickers]
        placeholders = ",".join("?" * len(upper))
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT * FROM ticker_metadata WHERE ticker IN ({placeholders})", upper)
            rows = cursor.fetchall()
        return {row["ticker"]: dict(row) for row in rows}

    def save_ticker_metadata(self, ticker: str, sector: str = None, industry: str = None,
                             market_cap: float = None, avg_dollar_volume_usd: float = None,
                             liquidity_rank: int = None, market_cap_tier: str = None,
                             is_profitable: bool = None, has_dividend: bool = None,
                             beta: float = None, beta_tier: str = None,
                             resolved_profile: str = None, resolved_preset: str = None,
                             asset_class: str = None, country: str = None):
        """Upsert ticker metadata.

        Partial writes (ADV liquidity, sector-only backfills) must not clobber
        classification fields resolved by ``ticker_resolver``. ``asset_class``,
        ``country``, ``resolved_profile``, and ``resolved_preset`` are coalesced
        on update when the caller omits them.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ticker_metadata
                    (ticker, sector, industry, market_cap, avg_dollar_volume_usd, liquidity_rank, market_cap_tier,
                     is_profitable, has_dividend, beta, beta_tier,
                     resolved_profile, resolved_preset, asset_class, country, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    sector=COALESCE(excluded.sector, ticker_metadata.sector),
                    industry=COALESCE(excluded.industry, ticker_metadata.industry),
                    market_cap=COALESCE(excluded.market_cap, ticker_metadata.market_cap),
                    avg_dollar_volume_usd=COALESCE(excluded.avg_dollar_volume_usd, ticker_metadata.avg_dollar_volume_usd),
                    liquidity_rank=COALESCE(excluded.liquidity_rank, ticker_metadata.liquidity_rank),
                    market_cap_tier=COALESCE(excluded.market_cap_tier, ticker_metadata.market_cap_tier),
                    is_profitable=COALESCE(excluded.is_profitable, ticker_metadata.is_profitable),
                    has_dividend=COALESCE(excluded.has_dividend, ticker_metadata.has_dividend),
                    beta=COALESCE(excluded.beta, ticker_metadata.beta),
                    beta_tier=COALESCE(excluded.beta_tier, ticker_metadata.beta_tier),
                    resolved_profile=COALESCE(excluded.resolved_profile, ticker_metadata.resolved_profile),
                    resolved_preset=COALESCE(excluded.resolved_preset, ticker_metadata.resolved_preset),
                    asset_class=COALESCE(excluded.asset_class, ticker_metadata.asset_class),
                    country=COALESCE(excluded.country, ticker_metadata.country),
                    last_updated=excluded.last_updated
            """, (
                ticker.upper(), sector, industry, market_cap, avg_dollar_volume_usd, liquidity_rank, market_cap_tier,
                int(is_profitable) if is_profitable is not None else None,
                int(has_dividend) if has_dividend is not None else None,
                beta, beta_tier, resolved_profile, resolved_preset,
                asset_class, country, now,
            ))
            conn.commit()

    # =========================================================================
    # EVENT CALENDAR (Plan B Phase 3)
    # =========================================================================

    def upsert_event_calendar(
        self,
        ticker: str,
        event_type: str,
        event_date: Optional[str],
        days_to_event: Optional[int] = None,
        description: str = "",
        metadata: Optional[dict] = None,
    ) -> int:
        """Insert or replace an event_calendar row. Returns the row id."""
        import json as _json
        now = datetime.now().isoformat()
        meta_str = _json.dumps(metadata) if metadata else ""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO event_calendar
                   (ticker, event_type, event_date, days_to_event, description, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ticker.upper(), event_type, event_date, days_to_event, description, meta_str, now),
            )
            conn.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    def get_upcoming_events(
        self,
        ticker: Optional[str] = None,
        event_types: Optional[List[str]] = None,
        max_days: int = 90,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return upcoming events within max_days, optionally filtered by ticker/type.

        Results are sorted by days_to_event ascending (nearest first).
        """
        import json as _json
        clauses = ["days_to_event <= ? AND days_to_event >= 0"]
        params: list = [max_days]
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if event_types:
            placeholders = ",".join("?" * len(event_types))
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(event_types)
        where = " AND ".join(clauses)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM event_calendar WHERE {where} ORDER BY days_to_event ASC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("metadata"):
                try:
                    d["metadata"] = _json.loads(d["metadata"])
                except Exception:
                    pass
            result.append(d)
        return result

    # =========================================================================
    # TICKER HEALTH (self-healing universe — auto-evict delisted tickers)
    # =========================================================================

    def record_ticker_failure(self, ticker: str, reason: str = "ohlcv_missing") -> None:
        """Increment the failure counter for *ticker* and refresh last_failure_at.

        Called from the screening engine when a bulk OHLCV download returns no
        rows for a ticker (likely-delisted, timezone-missing, etc). Success on
        any subsequent run does not reset the counter — we rely on
        :meth:`evict_stale_tickers` + admin action to close the loop.
        """
        if not ticker:
            return
        sym = ticker.upper().strip()
        if not sym:
            return
        now = datetime.now(timezone.utc).isoformat()
        reason = (reason or "")[:200]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ticker_health (ticker, failure_count, last_failure_at, last_failure_reason)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    failure_count = failure_count + 1,
                    last_failure_at = excluded.last_failure_at,
                    last_failure_reason = excluded.last_failure_reason
                """,
                (sym, now, reason),
            )
            conn.commit()

    def record_ticker_success(self, ticker: str) -> None:
        """Increment the success counter and refresh last_success_at."""
        if not ticker:
            return
        sym = ticker.upper().strip()
        if not sym:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ticker_health (ticker, success_count, last_success_at)
                VALUES (?, 1, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    success_count = success_count + 1,
                    last_success_at = excluded.last_success_at
                """,
                (sym, now),
            )
            conn.commit()

    def get_ticker_health(self, ticker: str) -> Optional[Dict[str, Any]]:
        sym = (ticker or "").upper().strip()
        if not sym:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ticker_health WHERE ticker = ?", (sym,)
            ).fetchone()
        return dict(row) if row else None

    def get_evicted_tickers(self, since_days: Optional[int] = None) -> set:
        """Return the set of tickers currently flagged as evicted.

        Optionally restricted to tickers evicted within the last ``since_days``.
        Used by the screening engine to skip known-dead symbols up-front,
        avoiding wasted yfinance calls (and limiter wear) on every scan.
        """
        sql = "SELECT ticker FROM ticker_health WHERE evicted_at IS NOT NULL"
        params: List[Any] = []
        if since_days is not None and since_days > 0:
            sql += " AND evicted_at >= datetime('now', ?)"
            params.append(f"-{int(since_days)} day")
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {str(r["ticker"]).upper().strip() for r in rows if r["ticker"]}

    def evict_tickers(self, tickers: List[str], reason: str = "manual") -> List[str]:
        """Mark specific tickers evicted so later scans skip them.

        Same side effects as ``evict_stale_tickers`` (drop metadata / exchange
        cache, stamp ``ticker_health``), but only for the given symbols.
        """
        now = datetime.now(timezone.utc).isoformat()
        evicted: List[str] = []
        with self._connect() as conn:
            for raw in tickers:
                sym = str(raw or "").upper().strip()
                if not sym:
                    continue
                conn.execute("DELETE FROM ticker_metadata WHERE ticker = ?", (sym,))
                conn.execute("DELETE FROM ticker_exchange_resolution WHERE ticker = ?", (sym,))
                conn.execute(
                    """
                    INSERT INTO ticker_health (
                        ticker, failure_count, success_count,
                        last_failure_at, last_success_at, last_failure_reason,
                        evicted_at, eviction_reason
                    ) VALUES (?, 0, 0, NULL, NULL, NULL, ?, ?)
                    ON CONFLICT(ticker) DO UPDATE SET
                        evicted_at = excluded.evicted_at,
                        eviction_reason = excluded.eviction_reason
                    """,
                    (sym, now, reason),
                )
                evicted.append(sym)
            conn.commit()
        return evicted

    def get_stale_ticker_candidates(
        self,
        min_failures: int = 3,
        min_days_since_success: int = 2,
    ) -> List[Dict[str, Any]]:
        """Return tickers eligible for eviction.

        A ticker qualifies when:
          - ``failure_count >= min_failures``
          - AND (``last_success_at`` is NULL OR older than
            ``min_days_since_success`` days ago)
          - AND ``evicted_at`` is NULL (not already evicted)
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, min_days_since_success))).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM ticker_health
                WHERE failure_count >= ?
                  AND (last_success_at IS NULL OR last_success_at < ?)
                  AND evicted_at IS NULL
                ORDER BY failure_count DESC, last_failure_at DESC
                """,
                (int(min_failures), cutoff),
            ).fetchall()
        return [dict(r) for r in rows]

    def evict_stale_tickers(
        self,
        min_failures: int = 3,
        min_days_since_success: int = 2,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Evict likely-delisted tickers from ``ticker_metadata``.

        Returns a summary dict. When ``dry_run`` is True (default), no writes
        are performed — the caller receives the candidate list for review.
        """
        candidates = self.get_stale_ticker_candidates(
            min_failures=min_failures,
            min_days_since_success=min_days_since_success,
        )
        evicted_tickers: List[str] = []
        if not dry_run and candidates:
            now = datetime.now(timezone.utc).isoformat()
            reason = f"stale_ticker min_failures={min_failures} min_days_since_success={min_days_since_success}"
            with self._connect() as conn:
                for row in candidates:
                    sym = row["ticker"]
                    conn.execute("DELETE FROM ticker_metadata WHERE ticker = ?", (sym,))
                    conn.execute("DELETE FROM ticker_exchange_resolution WHERE ticker = ?", (sym,))
                    conn.execute(
                        """
                        UPDATE ticker_health
                        SET evicted_at = ?, eviction_reason = ?
                        WHERE ticker = ?
                        """,
                        (now, reason, sym),
                    )
                    evicted_tickers.append(sym)
                conn.commit()
        return {
            "dry_run": dry_run,
            "candidate_count": len(candidates),
            "evicted_count": len(evicted_tickers),
            "evicted_tickers": evicted_tickers,
            "candidates": [c["ticker"] for c in candidates],
        }

    # =========================================================================
    # PRIMARY EXCHANGE RESOLUTION (TradingView export support)
    # =========================================================================

    def get_ticker_exchange_resolution(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get cached primary exchange resolution for a ticker."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM ticker_exchange_resolution WHERE ticker = ?",
                (ticker.upper(),),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def get_ticker_exchange_resolution_bulk(self, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get primary exchange resolution rows for many tickers."""
        if not tickers:
            return {}
        upper = [t.upper() for t in tickers if t]
        placeholders = ",".join("?" * len(upper))
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT * FROM ticker_exchange_resolution WHERE ticker IN ({placeholders})",
                upper,
            )
            rows = cursor.fetchall()
        return {row["ticker"]: dict(row) for row in rows}

    def save_ticker_exchange_resolution(
        self,
        ticker: str,
        primary_exchange: str,
        exchange_source: Optional[str] = None,
        exchange_confidence: Optional[float] = None,
        raw_exchange: Optional[str] = None,
        source_timestamp: Optional[str] = None,
    ) -> None:
        """Upsert a primary exchange resolution record for deterministic exports."""
        now = datetime.now().isoformat()
        src_ts = source_timestamp or now
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO ticker_exchange_resolution (
                    ticker, primary_exchange, exchange_source, exchange_confidence,
                    raw_exchange, source_timestamp, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    primary_exchange = excluded.primary_exchange,
                    exchange_source = excluded.exchange_source,
                    exchange_confidence = excluded.exchange_confidence,
                    raw_exchange = excluded.raw_exchange,
                    source_timestamp = excluded.source_timestamp,
                    updated_at = excluded.updated_at
                """,
                (
                    ticker.upper(),
                    primary_exchange,
                    exchange_source,
                    exchange_confidence,
                    raw_exchange,
                    src_ts,
                    now,
                ),
            )
            conn.commit()

    def get_stale_tickers(self, tickers: List[str], max_age_days: int = 7) -> List[str]:
        """Return tickers that need metadata resolution before adaptive scoring.

        A row counts as fresh only when ``last_updated`` is within ``max_age_days``
        *and* classification fields required for per-ticker preset routing are
        present. Partial rows (sector-only writes from older callers) must not
        block ``resolve_and_cache`` from refetching.
        """
        if not tickers:
            return []
        upper = [t.upper() for t in tickers]
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        placeholders = ",".join("?" * len(upper))
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT ticker FROM ticker_metadata
                WHERE ticker IN ({placeholders})
                  AND last_updated >= ?
                  AND resolved_preset IS NOT NULL
                  AND TRIM(resolved_preset) <> ''
                  AND market_cap_tier IS NOT NULL
                  AND TRIM(market_cap_tier) <> ''
                """,
                upper + [cutoff],
            )
            fresh = {row["ticker"] for row in cursor.fetchall()}
        return [t for t in upper if t not in fresh]

    # =========================================================================
    # SIGNAL PERFORMANCE (Feature 17)
    # =========================================================================

    def save_signal_performance(self, signal_name: str, period: str, hit_rate: float,
                                avg_return: float, correlation: float, sample_count: int):
        """Upsert signal performance metrics."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO signal_performance (signal_name, period, hit_rate, avg_return, correlation, sample_count, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_name, period) DO UPDATE SET
                    hit_rate=excluded.hit_rate, avg_return=excluded.avg_return,
                    correlation=excluded.correlation, sample_count=excluded.sample_count,
                    last_updated=excluded.last_updated
            """, (signal_name, period, hit_rate, avg_return, correlation, sample_count, now))
            conn.commit()

    def get_signal_performance(self) -> List[Dict[str, Any]]:
        """Get all signal performance metrics."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signal_performance ORDER BY signal_name, period")
            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # SCREENING RUNS & RESULTS
    # =========================================================================

    def save_screening_run(
        self,
        watchlist_id: int,
        criteria: str,
        ticker_count: int,
        results_count: int,
        strategy: Optional[str] = None,
    ) -> int:
        """Save a screening run summary. Returns run ID."""
        now = datetime.now().isoformat()
        strategy_value = str(strategy).strip() if strategy else None
        if not strategy_value:
            try:
                parsed = json.loads(criteria) if isinstance(criteria, str) and criteria else {}
                strategy_value = str((parsed or {}).get("strategy") or "").strip() or None
            except (json.JSONDecodeError, TypeError, ValueError):
                strategy_value = None
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO screening_runs (watchlist_id, run_at, strategy, criteria, ticker_count, results_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (watchlist_id, now, strategy_value, criteria, ticker_count, results_count),
            )
            run_id = cursor.lastrowid
            conn.commit()
            return run_id

    def save_screening_results(self, run_id: int, results: List[Dict[str, Any]]) -> int:
        """Bulk-save screening results for a run. Returns count saved."""
        count = 0
        with self._connect() as conn:
            cursor = conn.cursor()
            for r in results:
                macro_bd = r.get("macro_breakdown")
                macro_bd_json = json.dumps(macro_bd) if macro_bd is not None else None
                eq_sigs = r.get("entry_quality_signals")
                eq_sigs_json = json.dumps(eq_sigs) if eq_sigs is not None else None
                risk_components = r.get("risk_components")
                risk_components_json = (
                    json.dumps(risk_components) if risk_components is not None else None
                )
                # Embed factor_scorecard into signals dict under a private key
                # so it is persisted without a schema migration and is available
                # to compute_calibration_by_factor via signals["_factor_scorecard"].
                signals_dict = dict(r.get("signals") or {})
                fs = r.get("factor_scorecard")
                if fs and isinstance(fs, dict):
                    signals_dict["_factor_scorecard"] = json.dumps(fs)
                cursor.execute(
                    """INSERT INTO screening_results
                       (run_id, ticker, composite_score, direction, signals, signal_deltas,
                        percentile, rank, macro_fit, macro_breakdown,
                        entry_quality, entry_quality_signals, composite_fundamental,
                        risk_score, risk_components, asset_class)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, r["ticker"], r["composite_score"], r.get("direction", ""),
                     json.dumps(signals_dict),
                     json.dumps(r.get("signal_deltas", {})),
                     r.get("percentile"),
                     r.get("rank", 0),
                     r.get("macro_fit"),
                     macro_bd_json,
                     r.get("entry_quality"),
                     eq_sigs_json,
                     r.get("composite_fundamental"),
                     r.get("risk_score"),
                     risk_components_json,
                     r.get("asset_class")),
                )
                count += 1
            conn.commit()
        return count

    def save_movers_snapshots(self, asof_date: str, fetched_at: str, rows: List[Dict[str, Any]]) -> int:
        """Upsert raw movers rows for an ingestion snapshot."""
        count = 0
        with self._connect() as conn:
            cursor = conn.cursor()
            for row in rows:
                cursor.execute(
                    """
                    INSERT INTO movers_snapshots
                        (asof_date, fetched_at, source_list, ticker, price_change_pct, volume, market_cap, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(asof_date, source_list, ticker) DO UPDATE SET
                        fetched_at = excluded.fetched_at,
                        price_change_pct = excluded.price_change_pct,
                        volume = excluded.volume,
                        market_cap = excluded.market_cap,
                        payload_json = excluded.payload_json
                    """,
                    (
                        asof_date,
                        fetched_at,
                        row.get("source_list", ""),
                        row.get("ticker", ""),
                        row.get("price_change_pct"),
                        row.get("volume"),
                        row.get("market_cap"),
                        row.get("payload_json"),
                    ),
                )
                count += 1
            conn.commit()
        return count

    def get_movers_snapshots(self, asof_date: str) -> List[Dict[str, Any]]:
        """Return raw movers snapshot rows for a tape date."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM movers_snapshots WHERE asof_date = ? ORDER BY ticker, source_list",
                (asof_date,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def save_movers_run_details(self, run_id: int, details: List[Dict[str, Any]]) -> int:
        """Upsert movers per-run details keyed by (run_id, ticker)."""
        count = 0
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM movers_run_details WHERE run_id = ?", (run_id,))
            for row in details:
                cursor.execute(
                    """
                    INSERT INTO movers_run_details
                        (run_id, ticker, catalyst_type, reason_codes, primary_bucket, source_tags, score_breakdown_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, ticker) DO UPDATE SET
                        catalyst_type = excluded.catalyst_type,
                        reason_codes = excluded.reason_codes,
                        primary_bucket = excluded.primary_bucket,
                        source_tags = excluded.source_tags,
                        score_breakdown_json = excluded.score_breakdown_json
                    """,
                    (
                        run_id,
                        row.get("ticker", ""),
                        row.get("catalyst_type"),
                        json.dumps(row.get("reason_codes", [])),
                        row.get("primary_bucket"),
                        json.dumps(row.get("source_tags", [])),
                        json.dumps(row.get("score_breakdown_json", {})),
                    ),
                )
                count += 1
            conn.commit()
        return count

    def get_movers_run_details(self, run_id: int) -> Dict[str, Dict[str, Any]]:
        """Return movers side-table rows mapped by ticker."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM movers_run_details WHERE run_id = ?",
                (run_id,),
            )
            details: Dict[str, Dict[str, Any]] = {}
            for row in cursor.fetchall():
                payload = dict(row)
                for field_name in ("reason_codes", "source_tags", "score_breakdown_json"):
                    raw = payload.get(field_name)
                    if isinstance(raw, str):
                        try:
                            payload[field_name] = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            payload[field_name] = [] if field_name != "score_breakdown_json" else {}
                details[str(payload.get("ticker", "")).upper()] = payload
            return details

    def save_long_horizon_run_meta(self, run_id: int, meta: Dict[str, Any]) -> int:
        """Upsert long-horizon run-level reproducibility metadata."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO long_horizon_run_meta (run_id, meta_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    meta_json = excluded.meta_json,
                    updated_at = excluded.updated_at
                """,
                (run_id, json.dumps(meta), now, now),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def get_long_horizon_run_meta(self, run_id: int) -> Dict[str, Any]:
        """Get run-level long-horizon metadata by run id."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM long_horizon_run_meta WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            if not row:
                return {}
            payload = dict(row)
            raw = payload.get("meta_json")
            if isinstance(raw, str):
                try:
                    payload["meta_json"] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    payload["meta_json"] = {}
            return payload

    def save_long_horizon_run_details(self, run_id: int, details: List[Dict[str, Any]]) -> int:
        """Upsert long-horizon per-ticker run details keyed by (run_id, ticker)."""
        count = 0
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            for row in details:
                cursor.execute(
                    """
                    INSERT INTO long_horizon_run_details
                        (run_id, ticker, horizon, sector, market_cap_tier, beta, composite_score,
                         base_weight, target_weight, exclusion_reason_codes, allocation_reason_codes,
                         constraint_hits, expected_return_components_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, ticker) DO UPDATE SET
                        horizon = excluded.horizon,
                        sector = excluded.sector,
                        market_cap_tier = excluded.market_cap_tier,
                        beta = excluded.beta,
                        composite_score = excluded.composite_score,
                        base_weight = excluded.base_weight,
                        target_weight = excluded.target_weight,
                        exclusion_reason_codes = excluded.exclusion_reason_codes,
                        allocation_reason_codes = excluded.allocation_reason_codes,
                        constraint_hits = excluded.constraint_hits,
                        expected_return_components_json = excluded.expected_return_components_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        row.get("ticker", ""),
                        row.get("horizon"),
                        row.get("sector"),
                        row.get("market_cap_tier"),
                        row.get("beta"),
                        row.get("composite_score"),
                        row.get("base_weight"),
                        row.get("target_weight"),
                        json.dumps(row.get("exclusion_reason_codes", [])),
                        json.dumps(row.get("allocation_reason_codes", [])),
                        json.dumps(row.get("constraint_hits", [])),
                        json.dumps(row.get("expected_return_components_json", {})),
                        now,
                        now,
                    ),
                )
                count += 1
            conn.commit()
        return count

    def get_long_horizon_run_details(self, run_id: int) -> List[Dict[str, Any]]:
        """Return long-horizon side-table rows for a run."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM long_horizon_run_details WHERE run_id = ? ORDER BY composite_score DESC, ticker ASC",
                (run_id,),
            )
            out: List[Dict[str, Any]] = []
            for row in cursor.fetchall():
                payload = dict(row)
                for field_name, fallback in (
                    ("exclusion_reason_codes", []),
                    ("allocation_reason_codes", []),
                    ("constraint_hits", []),
                    ("expected_return_components_json", {}),
                ):
                    raw = payload.get(field_name)
                    if isinstance(raw, str):
                        try:
                            payload[field_name] = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            payload[field_name] = fallback
                out.append(payload)
            return out

    def save_long_horizon_underwriting(self, run_id: int, rows: List[Dict[str, Any]]) -> int:
        """Upsert long-horizon underwriting packets keyed by (run_id, ticker)."""
        count = 0
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            for row in rows:
                cursor.execute(
                    """
                    INSERT INTO long_horizon_underwriting
                        (run_id, ticker, packet_json, confidence, review_cadence, degraded, error_code, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, ticker) DO UPDATE SET
                        packet_json = excluded.packet_json,
                        confidence = excluded.confidence,
                        review_cadence = excluded.review_cadence,
                        degraded = excluded.degraded,
                        error_code = excluded.error_code,
                        updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        row.get("ticker", ""),
                        json.dumps(row),
                        row.get("confidence"),
                        row.get("review_cadence"),
                        1 if row.get("degraded") else 0,
                        row.get("error_code"),
                        now,
                        now,
                    ),
                )
                count += 1
            conn.commit()
        return count

    def get_long_horizon_underwriting(self, run_id: int) -> List[Dict[str, Any]]:
        """Return underwriting packets for a long-horizon run."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM long_horizon_underwriting WHERE run_id = ? ORDER BY ticker ASC",
                (run_id,),
            )
            out: List[Dict[str, Any]] = []
            for row in cursor.fetchall():
                payload = dict(row)
                raw = payload.get("packet_json")
                if isinstance(raw, str):
                    try:
                        payload["packet_json"] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        payload["packet_json"] = {}
                out.append(payload)
            return out

    def get_long_horizon_runs(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        """List screening runs where criteria.strategy == 'long_horizon'."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT sr.*, w.name as watchlist_name, lhm.meta_json as run_meta_json
                   FROM screening_runs sr
                   LEFT JOIN watchlists w ON sr.watchlist_id = w.id
                   LEFT JOIN long_horizon_run_meta lhm ON lhm.run_id = sr.id
                   WHERE (
                        sr.strategy = ?
                        OR (sr.strategy IS NULL AND (sr.criteria LIKE ? OR sr.criteria LIKE ?))
                   )
                   ORDER BY sr.run_at DESC
                   LIMIT ? OFFSET ?""",
                (
                    "long_horizon",
                    '%"strategy":"long_horizon"%',
                    '%"strategy": "long_horizon"%',
                    int(limit),
                    int(offset),
                ),
            )
            raw_rows = [dict(row) for row in cursor.fetchall()]
        runs: List[Dict[str, Any]] = []
        for run in raw_rows:
            criteria_raw = run.get("criteria")
            if not criteria_raw:
                continue
            try:
                criteria = json.loads(criteria_raw) if isinstance(criteria_raw, str) else dict(criteria_raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if criteria.get("strategy") != "long_horizon":
                continue
            run["criteria"] = criteria
            run_meta_raw = run.pop("run_meta_json", None)
            if run_meta_raw:
                try:
                    run["run_meta"] = json.loads(run_meta_raw) if isinstance(run_meta_raw, str) else dict(run_meta_raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    run["run_meta"] = {}
            runs.append(run)
        return runs

    def get_movers_runs(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        """List screening runs where criteria.strategy == 'movers'.

        LIMIT/OFFSET are applied after strategy filtering so the caller always
        receives up to ``limit`` movers rows even on a mixed ``screening_runs``
        table.  Python validation is kept as a correctness backstop against
        LIKE false-positives due to formatting variation.
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            # Filter first, then paginate via subquery so LIMIT targets only
            # movers rows rather than the raw unfiltered table.
            cursor.execute(
                """SELECT sr.*, w.name as watchlist_name
                   FROM screening_runs sr
                   LEFT JOIN watchlists w ON sr.watchlist_id = w.id
                   WHERE (
                        sr.strategy = ?
                        OR (sr.strategy IS NULL AND (sr.criteria LIKE ? OR sr.criteria LIKE ?))
                   )
                   ORDER BY sr.run_at DESC
                   LIMIT ? OFFSET ?""",
                (
                    "movers",
                    '%"strategy":"movers"%',
                    '%"strategy": "movers"%',
                    int(limit),
                    int(offset),
                ),
            )
            raw_rows = [dict(row) for row in cursor.fetchall()]
        movers_runs: List[Dict[str, Any]] = []
        for run in raw_rows:
            criteria_raw = run.get("criteria")
            if not criteria_raw:
                continue
            try:
                criteria = json.loads(criteria_raw) if isinstance(criteria_raw, str) else dict(criteria_raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            # Python backstop: LIKE can over-match due to JSON formatting variations
            if criteria.get("strategy") != "movers":
                continue
            run["criteria"] = criteria
            movers_runs.append(run)
        return movers_runs

    def save_discovery_run(
        self,
        *,
        source: str = "manual",
        external_run_id: Optional[str] = None,
        template_id: str = "",
        theme: str = "",
        criteria: str = "",
        market_cap_filter: str = "",
        max_results: Optional[int] = None,
        preset: Optional[str] = None,
        profile: Optional[str] = None,
        ticker_count: int = 0,
        validated_count: int = 0,
        new_count: int = 0,
        watchlist_id: Optional[int] = None,
        status: str = "completed",
        error: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
        tickers: Optional[List[Dict[str, Any]]] = None,
        run_at: Optional[str] = None,
    ) -> int:
        """Persist a manual Discover hopper run or auto-discovery summary."""
        run_at_val = run_at or datetime.now(timezone.utc).isoformat()
        summary_json = json.dumps(summary) if summary else None
        tickers_json = json.dumps(tickers) if tickers is not None else None
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO discovery_runs (
                    run_at, source, external_run_id, template_id, theme, criteria,
                    market_cap_filter, max_results, preset, profile,
                    ticker_count, validated_count, new_count, watchlist_id,
                    status, error, summary_json, tickers_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_at_val,
                    source,
                    external_run_id,
                    template_id or None,
                    theme or None,
                    criteria or None,
                    market_cap_filter or None,
                    max_results,
                    preset,
                    profile,
                    int(ticker_count),
                    int(validated_count),
                    int(new_count),
                    watchlist_id,
                    status,
                    error,
                    summary_json,
                    tickers_json,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def _decode_discovery_run_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        run = dict(row)
        for field in ("summary_json", "tickers_json"):
            raw = run.pop(field, None)
            key = field.replace("_json", "")
            if raw:
                try:
                    run[key] = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    run[key] = {}
            else:
                run[key] = {} if key == "summary" else []
        return run

    def get_discovery_runs(
        self,
        limit: int = 20,
        offset: int = 0,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List recent discovery runs (summary fields only; tickers omitted)."""
        clauses = []
        params: List[Any] = []
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([int(limit), int(offset)])
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT id, run_at, source, external_run_id, template_id, theme, criteria,
                       market_cap_filter, max_results, preset, profile,
                       ticker_count, validated_count, new_count, watchlist_id,
                       status, error, summary_json
                FROM discovery_runs
                {where}
                ORDER BY run_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            )
            rows = [dict(row) for row in cursor.fetchall()]
        runs: List[Dict[str, Any]] = []
        for row in rows:
            run = self._decode_discovery_run_row(row)
            run.pop("tickers", None)
            runs.append(run)
        return runs

    def get_discovery_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Return one discovery run including stored tickers when present."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM discovery_runs
                WHERE id = ?
                """,
                (int(run_id),),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return self._decode_discovery_run_row(dict(row))

    def get_screening_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent screening runs."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT sr.*, w.name as watchlist_name
                   FROM screening_runs sr
                   LEFT JOIN watchlists w ON sr.watchlist_id = w.id
                   ORDER BY sr.run_at DESC LIMIT ?""",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_screening_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        """Get a single screening run with watchlist name."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT sr.*, w.name as watchlist_name
                   FROM screening_runs sr
                   LEFT JOIN watchlists w ON sr.watchlist_id = w.id
                   WHERE sr.id = ?""",
                (run_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_latest_watchlist_run_after(
        self,
        watchlist_id: int,
        after_run_at: str,
        *,
        min_results: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """Return the newest screening run for a watchlist strictly after ``after_run_at``.

        Used by Cross-Watchlist consolidation to overlay fresher per-list scans
        on top of an older Scan All batch. Strategy filtering is applied by the
        caller — this method only enforces watchlist, timestamp, and result count.
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT sr.*, w.name as watchlist_name
                   FROM screening_runs sr
                   LEFT JOIN watchlists w ON sr.watchlist_id = w.id
                   WHERE sr.watchlist_id = ?
                     AND sr.run_at > ?
                     AND sr.results_count >= ?
                   ORDER BY sr.run_at DESC
                   LIMIT 1""",
                (int(watchlist_id), str(after_run_at), int(min_results)),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_latest_screening_runs_per_watchlist(
        self,
        min_results: int = 1,
    ) -> List[Dict[str, Any]]:
        """Return qualifying screening runs for Cross-Watchlist board picking.

        Returns **all** watchlist-backed runs with ``results_count >= min_results``,
        ordered by ``run_at DESC, id DESC``. This is a candidate set, not one
        global-latest row per list — the board picker needs older matching-lens
        runs when a newer run belongs to a different lens.
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT sr.*, w.name as watchlist_name
                   FROM screening_runs sr
                   LEFT JOIN watchlists w ON sr.watchlist_id = w.id
                   WHERE sr.watchlist_id IS NOT NULL
                     AND sr.results_count >= ?
                   ORDER BY sr.run_at DESC, sr.id DESC""",
                (int(min_results),),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_backtested_screening_results(self, limit: int = 2000) -> List[Dict[str, Any]]:
        """Return screening rows that already have at least one forward return.

        Newest result ids first. Used by signal-performance / adaptive-weights
        so last-N *runs* (often today's Scan All) do not hide older backtests.
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT * FROM screening_results
                   WHERE return_7d IS NOT NULL
                      OR return_14d IS NOT NULL
                      OR return_30d IS NOT NULL
                   ORDER BY id DESC
                   LIMIT ?""",
                (int(limit),),
            )
            rows = []
            for row in cursor.fetchall():
                d = dict(row)
                raw = d.get("signals")
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                        d["signals"] = parsed if isinstance(parsed, dict) else {}
                    except (json.JSONDecodeError, TypeError):
                        d["signals"] = {}
                elif not isinstance(d.get("signals"), dict):
                    d["signals"] = {}
                rows.append(d)
            return rows

    def get_screening_results(self, run_id: int) -> List[Dict[str, Any]]:
        """Get results for a screening run, ordered by rank."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM screening_results WHERE run_id = ? ORDER BY rank ASC",
                (run_id,),
            )
            rows = []
            for row in cursor.fetchall():
                d = dict(row)
                for field_name, fallback in (("signals", {}), ("signal_deltas", {})):
                    raw = d.get(field_name)
                    if isinstance(raw, str):
                        try:
                            parsed = json.loads(raw)
                            d[field_name] = parsed if isinstance(parsed, dict) else fallback
                        except (json.JSONDecodeError, TypeError):
                            d[field_name] = fallback
                # Deserialize macro_breakdown JSON
                mb = d.get("macro_breakdown")
                if isinstance(mb, str):
                    try:
                        d["macro_breakdown"] = json.loads(mb)
                    except (json.JSONDecodeError, TypeError):
                        d["macro_breakdown"] = None
                # Deserialize entry_quality_signals JSON
                eqs = d.get("entry_quality_signals")
                if isinstance(eqs, str):
                    try:
                        d["entry_quality_signals"] = json.loads(eqs)
                    except (json.JSONDecodeError, TypeError):
                        d["entry_quality_signals"] = None
                # Deserialize risk_components JSON (added in universe-expansion
                # pass). Older rows will have NULL here which deserializes to
                # None and the frontend treats as "no breakdown available".
                rc = d.get("risk_components")
                if isinstance(rc, str):
                    try:
                        d["risk_components"] = json.loads(rc)
                    except (json.JSONDecodeError, TypeError):
                        d["risk_components"] = None
                rows.append(d)
            return rows

    def update_screening_result_returns(self, result_id: int, return_7d: float = None, return_14d: float = None, return_30d: float = None):
        """Update forward returns on a screening result (for backtesting)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE screening_results SET return_7d = ?, return_14d = ?, return_30d = ? WHERE id = ?""",
                (return_7d, return_14d, return_30d, result_id),
            )
            conn.commit()

    def save_analysis(self, analysis: Analysis) -> int:
        """Save an analysis to the database.
        
        Args:
            analysis: Analysis object to save
            
        Returns:
            ID of the saved analysis
        """
        # Set created_at if not set
        if not analysis.created_at:
            analysis.created_at = datetime.now().isoformat()

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO analyses (
                    ticker, analysis_date, created_at, decision, position_action, confidence,
                    llm_provider, deep_think_model, quick_think_model, debate_rounds,
                    analysis_mode, risk_profile,
                    market_report, fundamentals_report, news_report, sentiment_report, data_provenance, run_settings, section_attribution,
                    report_warnings, data_quality_score, sec_filings_snapshot, earnings_transcript_snapshot, has_sec_snapshot, has_transcript_snapshot,
                    bull_summary, bear_summary, investment_decision,
                    risk_assessment, trading_plan, final_decision,
                    total_tokens, total_cost, duration_seconds,
                    price_at_analysis, price_after_7d, price_after_14d, price_after_30d,
                    actual_return_7d, actual_return_14d, actual_return_30d,
                    alpha_7d, alpha_14d, alpha_30d, was_correct,
                    analyst_rating, analyst_target_mean, analyst_target_high, analyst_target_low,
                    analyst_upside_pct, analyst_count, screening_run_id,
                    options_atm_iv, options_iv_rank, options_pc_volume_ratio,
                    options_pc_oi_ratio, options_max_pain, options_unusual_count,
                    investment_profile,
                    earnings_quality_grade, earnings_quality_data,
                    intrinsic_value, intrinsic_value_data,
                    scenario_analysis, catalyst_pipeline, peer_comps,
                    signal_summary, decision_json, screening_context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                analysis.ticker, analysis.analysis_date, analysis.created_at,
                analysis.decision, analysis.position_action, analysis.confidence,
                analysis.llm_provider, analysis.deep_think_model, analysis.quick_think_model,
                analysis.debate_rounds, analysis.analysis_mode, analysis.risk_profile,
                analysis.market_report, analysis.fundamentals_report,
                analysis.news_report, analysis.sentiment_report, analysis.data_provenance, analysis.run_settings, analysis.section_attribution,
                analysis.report_warnings, analysis.data_quality_score, analysis.sec_filings_snapshot, analysis.earnings_transcript_snapshot,
                analysis.has_sec_snapshot, analysis.has_transcript_snapshot,
                analysis.bull_summary, analysis.bear_summary, analysis.investment_decision,
                analysis.risk_assessment, analysis.trading_plan, analysis.final_decision,
                analysis.total_tokens, analysis.total_cost, analysis.duration_seconds,
                analysis.price_at_analysis, analysis.price_after_7d, analysis.price_after_14d,
                analysis.price_after_30d, analysis.actual_return_7d, analysis.actual_return_14d,
                analysis.actual_return_30d,
                analysis.alpha_7d, analysis.alpha_14d, analysis.alpha_30d,
                1 if analysis.was_correct else 0 if analysis.was_correct is not None else None,
                analysis.analyst_rating, analysis.analyst_target_mean, analysis.analyst_target_high,
                analysis.analyst_target_low, analysis.analyst_upside_pct, analysis.analyst_count,
                analysis.screening_run_id,
                analysis.options_atm_iv, analysis.options_iv_rank, analysis.options_pc_volume_ratio,
                analysis.options_pc_oi_ratio, analysis.options_max_pain, analysis.options_unusual_count,
                analysis.investment_profile,
                analysis.earnings_quality_grade, analysis.earnings_quality_data,
                analysis.intrinsic_value, analysis.intrinsic_value_data,
                analysis.scenario_analysis, analysis.catalyst_pipeline,
                analysis.peer_comps,
                analysis.signal_summary, analysis.decision_json,
                analysis.screening_context_json,
            ))
            analysis_id = cursor.lastrowid
            conn.commit()

        # Index snapshot fields for search
        if analysis.has_sec_snapshot or analysis.has_transcript_snapshot:
            try:
                self.index_snapshot_fields(analysis_id)
            except Exception as e:
                logger.debug("Snapshot indexing skipped (non-critical; can be re-run later): %s", e)

        return analysis_id

    def save_analysis_llm_usage(self, analysis_id: int, usage_summary: Dict[str, Any]) -> int:
        """Save per-model LLM usage rows for an analysis.

        Args:
            analysis_id: analyses.id foreign key
            usage_summary: state["llm_usage_summary"] payload from graph runtime

        Returns:
            Number of usage rows inserted.
        """
        if not analysis_id or not isinstance(usage_summary, dict):
            return 0
        by_model = usage_summary.get("by_model")
        if not isinstance(by_model, dict) or not by_model:
            return 0

        provider = str(usage_summary.get("provider") or "")
        now = datetime.now().isoformat()
        inserted = 0
        with self._connect() as conn:
            cursor = conn.cursor()
            for model_name, stats in by_model.items():
                if not isinstance(stats, dict):
                    continue
                cursor.execute(
                    """INSERT INTO analysis_llm_usage
                       (analysis_id, provider, model, calls, input_tokens, output_tokens, total_tokens,
                        cached_input_tokens, retry_events, error_events, total_latency_ms,
                        estimated_cost_usd, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(analysis_id),
                        provider,
                        str(model_name or ""),
                        int(stats.get("calls") or 0),
                        int(stats.get("input_tokens") or 0),
                        int(stats.get("output_tokens") or 0),
                        int(stats.get("total_tokens") or 0),
                        int(stats.get("cached_input_tokens") or 0),
                        int(stats.get("retry_events") or 0),
                        int(stats.get("error_events") or 0),
                        float(stats.get("total_latency_ms") or 0.0),
                        float(stats.get("estimated_cost_usd") or 0.0),
                        now,
                    ),
                )
                inserted += 1
            conn.commit()
        return inserted

    def save_analysis_llm_telemetry(self, analysis_id: int, usage_summary: Dict[str, Any]) -> bool:
        """Upsert aggregated LLM telemetry for an analysis."""
        if not analysis_id or not isinstance(usage_summary, dict):
            return False
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO analysis_llm_telemetry
                (analysis_id, provider, calls, input_tokens, output_tokens, total_tokens,
                 cached_input_tokens, retry_events, error_events, total_latency_ms,
                 estimated_cost_usd, unknown_price_models, pricing_version, usage_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(analysis_id) DO UPDATE SET
                    provider=excluded.provider,
                    calls=excluded.calls,
                    input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens,
                    total_tokens=excluded.total_tokens,
                    cached_input_tokens=excluded.cached_input_tokens,
                    retry_events=excluded.retry_events,
                    error_events=excluded.error_events,
                    total_latency_ms=excluded.total_latency_ms,
                    estimated_cost_usd=excluded.estimated_cost_usd,
                    unknown_price_models=excluded.unknown_price_models,
                    pricing_version=excluded.pricing_version,
                    usage_json=excluded.usage_json,
                    updated_at=excluded.updated_at
                """,
                (
                    int(analysis_id),
                    str(usage_summary.get("provider") or ""),
                    int(usage_summary.get("calls") or 0),
                    int(usage_summary.get("input_tokens") or 0),
                    int(usage_summary.get("output_tokens") or 0),
                    int(usage_summary.get("total_tokens") or 0),
                    int(usage_summary.get("cached_input_tokens") or 0),
                    int(usage_summary.get("retry_events") or 0),
                    int(usage_summary.get("error_events") or 0),
                    float(usage_summary.get("total_latency_ms") or 0.0),
                    float(usage_summary.get("total_cost_usd") or 0.0),
                    json.dumps(usage_summary.get("unknown_price_models") or []),
                    str(usage_summary.get("pricing_version") or ""),
                    json.dumps(usage_summary),
                    now,
                    now,
                ),
            )
            conn.commit()
        return True

    def upsert_analysis_quality(
        self,
        analysis_id: int,
        quality_label: str = "",
        quality_score: Optional[float] = None,
        quality_notes: str = "",
        evaluated_by: str = "",
    ) -> bool:
        """Upsert quality annotation for an analysis."""
        if not analysis_id:
            return False
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO analysis_quality_labels
                (analysis_id, quality_label, quality_score, quality_notes, evaluated_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(analysis_id) DO UPDATE SET
                    quality_label=excluded.quality_label,
                    quality_score=excluded.quality_score,
                    quality_notes=excluded.quality_notes,
                    evaluated_by=excluded.evaluated_by,
                    updated_at=excluded.updated_at
                """,
                (
                    int(analysis_id),
                    (quality_label or "").strip(),
                    None if quality_score is None else float(quality_score),
                    quality_notes or "",
                    evaluated_by or "",
                    now,
                    now,
                ),
            )
            conn.commit()
        return True

    def get_analysis_quality(self, analysis_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT analysis_id, quality_label, quality_score, quality_notes, evaluated_by, created_at, updated_at
                   FROM analysis_quality_labels WHERE analysis_id = ?""",
                (analysis_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_backtest_outcomes(
        self,
        analysis_id: int,
        price_at_analysis: Optional[float] = None,
        price_after_7d: Optional[float] = None,
        price_after_14d: Optional[float] = None,
        price_after_30d: Optional[float] = None,
        actual_return_7d: Optional[float] = None,
        actual_return_14d: Optional[float] = None,
        actual_return_30d: Optional[float] = None,
        was_correct: Optional[bool] = None,
        alpha_7d: Optional[float] = None,
        alpha_14d: Optional[float] = None,
        alpha_30d: Optional[float] = None,
    ) -> None:
        """Update backtesting fields for an analysis record."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE analyses
                SET price_at_analysis = ?,
                    price_after_7d = ?,
                    price_after_14d = ?,
                    price_after_30d = ?,
                    actual_return_7d = ?,
                    actual_return_14d = ?,
                    actual_return_30d = ?,
                    was_correct = ?,
                    alpha_7d = ?,
                    alpha_14d = ?,
                    alpha_30d = ?
                WHERE id = ?
                """,
                (
                    price_at_analysis,
                    price_after_7d,
                    price_after_14d,
                    price_after_30d,
                    actual_return_7d,
                    actual_return_14d,
                    actual_return_30d,
                    1 if was_correct else 0 if was_correct is not None else None,
                    alpha_7d,
                    alpha_14d,
                    alpha_30d,
                    analysis_id,
                ),
            )
            conn.commit()
    
    def get_analyses_needing_backtest(
        self,
        min_age_days: int = 7,
        max_age_days: int = 90,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Get analyses that have no forward return data and are old enough to backtest.

        Args:
            min_age_days: Minimum age in days (need price data to be available)
            max_age_days: Maximum age in days (skip very old analyses)
            limit: Max number to return per call

        Returns:
            List of dicts with id, ticker, analysis_date, decision
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, ticker, analysis_date, decision
                FROM analyses
                WHERE price_after_7d IS NULL
                  AND analysis_date <= date('now', ?)
                  AND analysis_date >= date('now', ?)
                ORDER BY analysis_date DESC
                LIMIT ?
            """, (f"-{min_age_days} days", f"-{max_age_days} days", limit))
            return [dict(row) for row in cursor.fetchall()]

    def save_backtest_run(self, run: BacktestRun) -> int:
        """Persist a backtest run summary."""
        if not run.run_at:
            run.run_at = datetime.now().isoformat()

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO backtest_runs (
                    run_at, ticker, start_date, end_date, limit_count, lookahead_days,
                    slippage_bps, transaction_cost_bps, updated_count, skipped_count,
                    avg_return, win_rate, accuracy,
                    strategy_sharpe, strategy_sortino, avg_alpha_30d,
                    avg_signed_return_7d, return_vol_7d
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_at,
                    run.ticker,
                    run.start_date,
                    run.end_date,
                    run.limit_count,
                    run.lookahead_days,
                    run.slippage_bps,
                    run.transaction_cost_bps,
                    run.updated_count,
                    run.skipped_count,
                    run.avg_return,
                    run.win_rate,
                    run.accuracy,
                    run.strategy_sharpe,
                    run.strategy_sortino,
                    run.avg_alpha_30d,
                    run.avg_signed_return_7d,
                    run.return_vol_7d,
                ),
            )
            run_id = cursor.lastrowid
            conn.commit()
            return run_id
    
    def get_analysis(self, analysis_id: int) -> Optional[Analysis]:
        """Retrieve an analysis by ID."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,))
            row = cursor.fetchone()
        
        if row:
            return self._row_to_analysis(row)
        return None
    
    def get_analyses_by_ticker(self, ticker: str, limit: int = 50) -> List[Analysis]:
        """Get all analyses for a ticker."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM analyses 
                WHERE ticker = ? 
                ORDER BY created_at DESC
                LIMIT ?
            """, (ticker.upper(), limit))
            rows = cursor.fetchall()
        
        return [self._row_to_analysis(row) for row in rows]
    
    def get_recent_analyses(self, limit: int = 20) -> List[Analysis]:
        """Get most recent analyses."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM analyses 
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
        
        return [self._row_to_analysis(row) for row in rows]

    def get_analyses_by_date_range(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        ticker: Optional[str] = None,
        risk_profile: Optional[str] = None,
        analysis_mode: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Analysis]:
        """Get analyses within a date range with optional filters."""
        start = start_date or "0001-01-01"
        end = end_date or "9999-12-31"

        with self._connect() as conn:
            cursor = conn.cursor()
            params: List[Any] = [start, end]
            query = """
                SELECT * FROM analyses
                WHERE analysis_date >= ? AND analysis_date <= ?
            """
            if ticker:
                query += " AND ticker = ?"
                params.append(ticker.upper())
            if risk_profile:
                query += " AND risk_profile = ?"
                params.append(risk_profile)
            if analysis_mode:
                query += " AND analysis_mode = ?"
                params.append(analysis_mode)
            query += " ORDER BY analysis_date DESC, created_at DESC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)

            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

        return [self._row_to_analysis(row) for row in rows]

    def get_backtested_analyses(self, limit: Optional[int] = None) -> List[Analysis]:
        """Get analyses that have backtesting outcomes."""
        with self._connect() as conn:
            cursor = conn.cursor()
            if limit is not None:
                cursor.execute(
                    """
                    SELECT * FROM analyses
                    WHERE was_correct IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM analyses
                    WHERE was_correct IS NOT NULL
                    ORDER BY created_at DESC
                    """
                )
            rows = cursor.fetchall()

        return [self._row_to_analysis(row) for row in rows]
    
    def get_analyses_by_decision(self, decision: str, limit: int = 50) -> List[Analysis]:
        """Get analyses by decision type (BUY/SELL/HOLD)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM analyses 
                WHERE decision LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (f"%{decision.upper()}%", limit))
            rows = cursor.fetchall()
        
        return [self._row_to_analysis(row) for row in rows]
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get overall database statistics."""
        with self._connect() as conn:
            cursor = conn.cursor()
            stats = {}
            
            # Total analyses
            cursor.execute("SELECT COUNT(*) FROM analyses")
            stats["total_analyses"] = cursor.fetchone()[0]
            
            # By decision
            cursor.execute("""
                SELECT decision, COUNT(*) as count 
                FROM analyses 
                GROUP BY decision
            """)
            stats["by_decision"] = {row[0]: row[1] for row in cursor.fetchall()}
            
            # Unique tickers
            cursor.execute("SELECT COUNT(DISTINCT ticker) FROM analyses")
            stats["unique_tickers"] = cursor.fetchone()[0]
            
            # Total cost
            cursor.execute("SELECT SUM(total_cost) FROM analyses")
            result = cursor.fetchone()[0]
            stats["total_cost"] = result if result else 0.0
            
            # Win rate (if backtesting data available)
            cursor.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN was_correct = 1 THEN 1 ELSE 0 END) as wins
                FROM analyses
                WHERE was_correct IS NOT NULL
            """)
            row = cursor.fetchone()
            if row[0] > 0:
                stats["backtested_count"] = row[0]
                stats["win_rate"] = row[1] / row[0] if row[0] > 0 else 0
        return stats

    def get_backtest_runs(
        self,
        limit: int = 20,
        ticker: Optional[str] = None,
        run_start_date: Optional[str] = None,
        run_end_date: Optional[str] = None,
    ) -> List[BacktestRun]:
        """Get recent backtest runs with optional filters."""
        with self._connect() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM backtest_runs WHERE 1=1"
            params: List[Any] = []
            if ticker:
                query += " AND ticker = ?"
                params.append(ticker.upper())
            if run_start_date:
                start = run_start_date if "T" in run_start_date else f"{run_start_date}T00:00:00"
                query += " AND run_at >= ?"
                params.append(start)
            if run_end_date:
                end = run_end_date if "T" in run_end_date else f"{run_end_date}T23:59:59"
                query += " AND run_at <= ?"
                params.append(end)

            query += " ORDER BY run_at DESC"
            query += " LIMIT ?"
            params.append(limit)

            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            return [self._row_to_backtest_run(row) for row in rows]

    def export_backtest_runs(
        self,
        filepath: str,
        limit: int = 100,
        ticker: Optional[str] = None,
        run_start_date: Optional[str] = None,
        run_end_date: Optional[str] = None,
    ) -> str:
        """Export backtest runs to CSV with optional filters."""
        import csv

        with self._connect() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM backtest_runs WHERE 1=1"
            params: List[Any] = []
            if ticker:
                query += " AND ticker = ?"
                params.append(ticker.upper())
            if run_start_date:
                start = run_start_date if "T" in run_start_date else f"{run_start_date}T00:00:00"
                query += " AND run_at >= ?"
                params.append(start)
            if run_end_date:
                end = run_end_date if "T" in run_end_date else f"{run_end_date}T23:59:59"
                query += " AND run_at <= ?"
                params.append(end)

            query += " ORDER BY run_at DESC"
            query += " LIMIT ?"
            params.append(limit)

            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

        def compute_attribution(analyses: List[Analysis]) -> Dict[str, float]:
            from tradingagents.backtesting.metrics import compute_researcher_attribution

            payload = compute_researcher_attribution(analyses)
            bull = payload.get("bull") or {}
            bear = payload.get("bear") or {}
            return {
                "bull_wins": bull.get("wins", 0),
                "bull_losses": bull.get("losses", 0),
                "bear_wins": bear.get("wins", 0),
                "bear_losses": bear.get("losses", 0),
                "bull_win_rate": bull.get("win_rate", 0.0),
                "bear_win_rate": bear.get("win_rate", 0.0),
            }

        def select_analyses(row: sqlite3.Row) -> List[Analysis]:
            limit_count = row["limit_count"] or 0
            limit_value = limit_count if limit_count > 0 else None
            if row["start_date"] or row["end_date"]:
                return self.get_analyses_by_date_range(
                    start_date=row["start_date"] or None,
                    end_date=row["end_date"] or None,
                    ticker=row["ticker"] or None,
                    limit=limit_value,
                )
            if row["ticker"]:
                return self.get_analyses_by_ticker(row["ticker"], limit_value or 50)
            return self.get_recent_analyses(limit_value or 50)

        if rows:
            enriched_rows = []
            for row in rows:
                row_dict = dict(row)
                attribution = compute_attribution(select_analyses(row))
                row_dict.update(attribution)
                enriched_rows.append(row_dict)

            total = len(rows)
            avg_return = sum(row["avg_return"] or 0.0 for row in enriched_rows) / total
            avg_win = sum(row["win_rate"] or 0.0 for row in enriched_rows) / total
            avg_acc = sum(row["accuracy"] or 0.0 for row in enriched_rows) / total
            avg_slippage = sum(row["slippage_bps"] or 0.0 for row in enriched_rows) / total
            avg_cost = sum(row["transaction_cost_bps"] or 0.0 for row in enriched_rows) / total
            updated_total = sum(row["updated_count"] or 0 for row in enriched_rows)
            skipped_total = sum(row["skipped_count"] or 0 for row in enriched_rows)
            bull_wins_total = sum(row.get("bull_wins", 0) for row in enriched_rows)
            bull_losses_total = sum(row.get("bull_losses", 0) for row in enriched_rows)
            bear_wins_total = sum(row.get("bear_wins", 0) for row in enriched_rows)
            bear_losses_total = sum(row.get("bear_losses", 0) for row in enriched_rows)

            aggregate = {key: "" for key in enriched_rows[0].keys()}
            aggregate["run_at"] = f"AGGREGATE (n={total})"
            aggregate["slippage_bps"] = round(avg_slippage, 4)
            aggregate["transaction_cost_bps"] = round(avg_cost, 4)
            aggregate["updated_count"] = updated_total
            aggregate["skipped_count"] = skipped_total
            aggregate["avg_return"] = round(avg_return, 6)
            aggregate["win_rate"] = round(avg_win, 6)
            aggregate["accuracy"] = round(avg_acc, 6)
            aggregate["bull_wins"] = bull_wins_total
            aggregate["bull_losses"] = bull_losses_total
            aggregate["bear_wins"] = bear_wins_total
            aggregate["bear_losses"] = bear_losses_total
            aggregate["bull_win_rate"] = round(
                bull_wins_total / (bull_wins_total + bull_losses_total), 6
            ) if (bull_wins_total + bull_losses_total) else 0.0
            aggregate["bear_win_rate"] = round(
                bear_wins_total / (bear_wins_total + bear_losses_total), 6
            ) if (bear_wins_total + bear_losses_total) else 0.0

            with open(filepath, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(enriched_rows[0].keys())
                for row in enriched_rows:
                    writer.writerow([row.get(key, "") for key in enriched_rows[0].keys()])
                writer.writerow(list(aggregate.values()))

        return filepath
    
    def export_to_csv(self, filepath: str, ticker: str = None) -> str:
        """Export analyses to CSV file.
        
        Args:
            filepath: Output CSV path
            ticker: Optional ticker filter
            
        Returns:
            Path to exported file
        """
        import csv
        
        with self._connect() as conn:
            cursor = conn.cursor()
            if ticker:
                cursor.execute(
                    "SELECT * FROM analyses WHERE ticker = ? ORDER BY created_at",
                    (ticker.upper(),),
                )
            else:
                cursor.execute("SELECT * FROM analyses ORDER BY created_at")
            rows = cursor.fetchall()
        
        if rows:
            with open(filepath, 'w', newline='') as f:
                writer = csv.writer(f)
                # Write header
                writer.writerow(rows[0].keys())
                # Write data
                for row in rows:
                    writer.writerow(list(row))
        
        return filepath

    def export_provenance(
        self,
        filepath: str,
        export_format: str = "json",
        limit: int = 100,
        ticker: Optional[str] = None,
    ) -> str:
        """Export provenance events for analyses to JSON or CSV."""
        import csv

        analyses = (
            self.get_analyses_by_ticker(ticker, limit)
            if ticker
            else self.get_recent_analyses(limit)
        )
        events: List[Dict[str, Any]] = []
        for analysis in analyses:
            raw = analysis.data_provenance or "[]"
            try:
                entries = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                entries = []
            for idx, event in enumerate(entries):
                row = {
                    "analysis_id": analysis.id,
                    "ticker": analysis.ticker,
                    "analysis_date": analysis.analysis_date,
                    "created_at": analysis.created_at,
                    "event_index": idx,
                }
                if isinstance(event, dict):
                    for key, value in event.items():
                        if isinstance(value, (dict, list)):
                            row[key] = json.dumps(value)
                        else:
                            row[key] = value
                events.append(row)

        if export_format.lower() == "json":
            with open(filepath, "w", encoding="utf-8") as handle:
                json.dump(events, handle, indent=2)
            return filepath

        keys = []
        for event in events:
            for key in event.keys():
                if key not in keys:
                    keys.append(key)
        with open(filepath, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            for row in events:
                writer.writerow(row)
        return filepath

    def get_snapshots(
        self,
        snapshot_type: Optional[str] = None,
        ticker: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return indexed snapshots for SEC filings and transcripts."""
        with self._connect() as conn:
            cursor = conn.cursor()
            query = """
                SELECT id, ticker, analysis_date, created_at,
                       sec_filings_snapshot, earnings_transcript_snapshot,
                       has_sec_snapshot, has_transcript_snapshot
                FROM analyses
                WHERE 1=1
            """
            params: List[Any] = []
            if ticker:
                query += " AND ticker = ?"
                params.append(ticker.upper())
            if snapshot_type == "sec":
                query += " AND has_sec_snapshot = 1"
            elif snapshot_type == "transcript":
                query += " AND has_transcript_snapshot = 1"
            else:
                query += " AND (has_sec_snapshot = 1 OR has_transcript_snapshot = 1)"
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

        snapshots: List[Dict[str, Any]] = []
        for row in rows:
            if snapshot_type in (None, "sec") and row["sec_filings_snapshot"]:
                snapshots.append(
                    {
                        "analysis_id": row["id"],
                        "ticker": row["ticker"],
                        "analysis_date": row["analysis_date"],
                        "created_at": row["created_at"],
                        "snapshot_type": "sec",
                        "snapshot_payload": row["sec_filings_snapshot"],
                    }
                )
            if snapshot_type in (None, "transcript") and row["earnings_transcript_snapshot"]:
                snapshots.append(
                    {
                        "analysis_id": row["id"],
                        "ticker": row["ticker"],
                        "analysis_date": row["analysis_date"],
                        "created_at": row["created_at"],
                        "snapshot_type": "transcript",
                        "snapshot_payload": row["earnings_transcript_snapshot"],
                    }
                )
        return snapshots

    def index_snapshot_fields(self, analysis_id: int) -> Dict[str, int]:
        """Parse and index KPIs, guidance, and risks from an analysis snapshot."""
        import json
        from datetime import datetime as dt, timezone as _tz

        counts = {"kpis": 0, "guidance": 0, "risks": 0}
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT ticker, analysis_date, created_at,
                       sec_filings_snapshot, earnings_transcript_snapshot
                FROM analyses WHERE id = ?
                """,
                (analysis_id,),
            )
            row = cursor.fetchone()
            if not row:
                return counts

            ticker = row["ticker"]
            analysis_date = row["analysis_date"]
            created_at = row["created_at"] or dt.now(_tz.utc).isoformat()

            # Delete existing index rows for this analysis
            cursor.execute("DELETE FROM snapshot_kpis WHERE analysis_id = ?", (analysis_id,))
            cursor.execute("DELETE FROM snapshot_guidance WHERE analysis_id = ?", (analysis_id,))
            cursor.execute("DELETE FROM snapshot_risks WHERE analysis_id = ?", (analysis_id,))

            # Parse transcript snapshot for KPIs and guidance
            transcript_raw = row["earnings_transcript_snapshot"] or ""
            transcript_payload = self._parse_snapshot_json(transcript_raw)
            if transcript_payload:
                for item in transcript_payload.get("kpis") or []:
                    cursor.execute(
                        """
                        INSERT INTO snapshot_kpis
                            (analysis_id, ticker, analysis_date, snapshot_type,
                             kpi_name, kpi_value, kpi_period, kpi_context, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            analysis_id,
                            ticker,
                            analysis_date,
                            "transcript",
                            item.get("name") or "",
                            item.get("value") or "",
                            item.get("period") or "",
                            item.get("context") or "",
                            created_at,
                        ),
                    )
                    counts["kpis"] += 1
                for item in transcript_payload.get("guidance") or []:
                    cursor.execute(
                        """
                        INSERT INTO snapshot_guidance
                            (analysis_id, ticker, analysis_date, metric,
                             guidance_range, timeframe, context, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            analysis_id,
                            ticker,
                            analysis_date,
                            item.get("metric") or "",
                            item.get("range") or item.get("value") or "",
                            item.get("timeframe") or "",
                            item.get("context") or "",
                            created_at,
                        ),
                    )
                    counts["guidance"] += 1

            # Parse SEC snapshot for risks
            sec_raw = row["sec_filings_snapshot"] or ""
            sec_payload = self._parse_snapshot_json(sec_raw)
            if sec_payload:
                for filing in sec_payload.get("filings") or []:
                    form = filing.get("form") or ""
                    filing_date = filing.get("filing_date") or ""
                    for risk in filing.get("risk_factors") or []:
                        cursor.execute(
                            """
                            INSERT INTO snapshot_risks
                                (analysis_id, ticker, analysis_date, risk_text,
                                 source_form, filing_date, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                analysis_id,
                                ticker,
                                analysis_date,
                                risk,
                                form,
                                filing_date,
                                created_at,
                            ),
                        )
                        counts["risks"] += 1

            conn.commit()
        return counts

    @staticmethod
    def _parse_snapshot_json(raw: str) -> Optional[Dict[str, Any]]:
        if not raw:
            return None
        text = raw.strip()
        for prefix in ("SEC_FILINGS_SNAPSHOT_JSON:", "EARNINGS_TRANSCRIPT_SNAPSHOT_JSON:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else None
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    def search_kpis(
        self,
        ticker: Optional[str] = None,
        kpi_name: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search indexed KPIs by ticker and/or name."""
        with self._connect() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM snapshot_kpis WHERE 1=1"
            params: List[Any] = []
            if ticker:
                query += " AND ticker = ?"
                params.append(ticker.upper())
            if kpi_name:
                query += " AND kpi_name LIKE ?"
                params.append(f"%{kpi_name}%")
            query += " ORDER BY analysis_date DESC, id DESC LIMIT ?"
            params.append(limit)
            cursor.execute(query, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    def search_guidance(
        self,
        ticker: Optional[str] = None,
        metric: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search indexed guidance by ticker and/or metric."""
        with self._connect() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM snapshot_guidance WHERE 1=1"
            params: List[Any] = []
            if ticker:
                query += " AND ticker = ?"
                params.append(ticker.upper())
            if metric:
                query += " AND metric LIKE ?"
                params.append(f"%{metric}%")
            query += " ORDER BY analysis_date DESC, id DESC LIMIT ?"
            params.append(limit)
            cursor.execute(query, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    def search_risks(
        self,
        ticker: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search indexed risks by ticker and/or keyword."""
        with self._connect() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM snapshot_risks WHERE 1=1"
            params: List[Any] = []
            if ticker:
                query += " AND ticker = ?"
                params.append(ticker.upper())
            if keyword:
                query += " AND risk_text LIKE ?"
                params.append(f"%{keyword}%")
            query += " ORDER BY analysis_date DESC, id DESC LIMIT ?"
            params.append(limit)
            cursor.execute(query, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    def get_kpi_history(
        self,
        ticker: str,
        kpi_name: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Get history of a specific KPI for a ticker (for delta comparison)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM snapshot_kpis
                WHERE ticker = ? AND kpi_name LIKE ?
                ORDER BY analysis_date DESC, id DESC
                LIMIT ?
                """,
                (ticker.upper(), f"%{kpi_name}%", limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_guidance_history(
        self,
        ticker: str,
        metric: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Get history of a specific guidance metric for a ticker (for shift detection)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM snapshot_guidance
                WHERE ticker = ? AND metric LIKE ?
                ORDER BY analysis_date DESC, id DESC
                LIMIT ?
                """,
                (ticker.upper(), f"%{metric}%", limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def compute_kpi_deltas(
        self,
        ticker: str,
        current_analysis_id: int,
    ) -> List[Dict[str, Any]]:
        """Compare current KPIs to previous period and return deltas."""
        current = self.search_kpis(ticker=ticker, limit=50)
        current_kpis = [k for k in current if k["analysis_id"] == current_analysis_id]
        previous_kpis = [k for k in current if k["analysis_id"] != current_analysis_id]

        deltas: List[Dict[str, Any]] = []
        for curr in current_kpis:
            name = curr["kpi_name"]
            prev = next((p for p in previous_kpis if p["kpi_name"] == name), None)
            deltas.append(
                {
                    "kpi_name": name,
                    "current_value": curr["kpi_value"],
                    "current_period": curr["kpi_period"],
                    "previous_value": prev["kpi_value"] if prev else None,
                    "previous_period": prev["kpi_period"] if prev else None,
                    "has_previous": prev is not None,
                }
            )
        return deltas

    def compute_guidance_shifts(
        self,
        ticker: str,
        current_analysis_id: int,
    ) -> List[Dict[str, Any]]:
        """Compare current guidance to previous period and return shifts."""
        current = self.search_guidance(ticker=ticker, limit=50)
        current_guidance = [g for g in current if g["analysis_id"] == current_analysis_id]
        previous_guidance = [g for g in current if g["analysis_id"] != current_analysis_id]

        shifts: List[Dict[str, Any]] = []
        for curr in current_guidance:
            metric = curr["metric"]
            prev = next((p for p in previous_guidance if p["metric"] == metric), None)
            shifts.append(
                {
                    "metric": metric,
                    "current_range": curr["guidance_range"],
                    "current_timeframe": curr["timeframe"],
                    "previous_range": prev["guidance_range"] if prev else None,
                    "previous_timeframe": prev["timeframe"] if prev else None,
                    "has_previous": prev is not None,
                }
            )
        return shifts

    # =========================================================================
    # NOTES & TAGS
    # =========================================================================

    def update_notes(self, analysis_id: int, notes: str) -> bool:
        """Update notes for an analysis."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE analyses SET notes = ? WHERE id = ?",
                (notes, analysis_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_tags(self, analysis_id: int, tags: str) -> bool:
        """Update tags for an analysis (comma-separated string)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE analyses SET tags = ? WHERE id = ?",
                (tags, analysis_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_analyses_by_tag(self, tag: str, limit: int = 50) -> List[Analysis]:
        """Get analyses that have a specific tag."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM analyses 
                WHERE tags LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%{tag}%", limit),
            )
            rows = cursor.fetchall()
        return [self._row_to_analysis(row) for row in rows]

    def get_all_tags(self) -> List[str]:
        """Get all unique tags across analyses."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT tags FROM analyses WHERE tags IS NOT NULL AND tags != ''")
            rows = cursor.fetchall()
        
        all_tags = set()
        for row in rows:
            if row["tags"]:
                for tag in row["tags"].split(","):
                    tag = tag.strip()
                    if tag:
                        all_tags.add(tag)
        return sorted(all_tags)

    # =========================================================================
    # SAVED VIEWS
    # =========================================================================

    def save_view(self, view: SavedView) -> int:
        """Create or update a saved view."""
        now = datetime.now().isoformat()
        if not view.created_at:
            view.created_at = now
        view.updated_at = now

        with self._connect() as conn:
            cursor = conn.cursor()
            # Try to update existing
            cursor.execute(
                """
                UPDATE saved_views 
                SET description = ?, filters = ?, updated_at = ?
                WHERE name = ?
                """,
                (view.description, view.filters, view.updated_at, view.name),
            )
            if cursor.rowcount == 0:
                # Insert new
                cursor.execute(
                    """
                    INSERT INTO saved_views (name, description, filters, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (view.name, view.description, view.filters, view.created_at, view.updated_at),
                )
            view_id = cursor.lastrowid or cursor.execute(
                "SELECT id FROM saved_views WHERE name = ?", (view.name,)
            ).fetchone()["id"]
            conn.commit()
            return view_id

    def get_saved_view(self, view_id: int) -> Optional[SavedView]:
        """Get a saved view by ID."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM saved_views WHERE id = ?", (view_id,))
            row = cursor.fetchone()
        if row:
            return self._row_to_saved_view(row)
        return None

    def get_saved_view_by_name(self, name: str) -> Optional[SavedView]:
        """Get a saved view by name."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM saved_views WHERE name = ?", (name,))
            row = cursor.fetchone()
        if row:
            return self._row_to_saved_view(row)
        return None

    def list_saved_views(self) -> List[SavedView]:
        """List all saved views."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM saved_views ORDER BY name")
            rows = cursor.fetchall()
        return [self._row_to_saved_view(row) for row in rows]

    def delete_analysis(self, analysis_id: int) -> Optional[Analysis]:
        """Delete an analysis and all related data.
        
        Returns the deleted Analysis object (for file cleanup) or None if not found.
        """
        # First get the analysis to return it (for file cleanup)
        analysis = self.get_analysis(analysis_id)
        if not analysis:
            return None
        
        with self._connect() as conn:
            cursor = conn.cursor()
            
            # Delete from related tables first (foreign key references)
            cursor.execute("DELETE FROM snapshot_kpis WHERE analysis_id = ?", (analysis_id,))
            cursor.execute("DELETE FROM snapshot_guidance WHERE analysis_id = ?", (analysis_id,))
            cursor.execute("DELETE FROM snapshot_risks WHERE analysis_id = ?", (analysis_id,))
            
            # Delete the main analysis record
            cursor.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
            conn.commit()
        
        return analysis

    def delete_saved_view(self, view_id: int) -> bool:
        """Delete a saved view."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Tier 2 Phase 3: Alert System CRUD
    # =========================================================================

    def create_alert_rule(
        self,
        ticker: str,
        alert_type: str,
        condition: dict,
    ) -> int:
        """
        Create an alert rule.
        
        Args:
            ticker: Stock ticker to monitor
            alert_type: One of: price_cross, volume_spike, rsi_extreme,
                        ma_crossover, earnings_approaching, rating_change, 
                        short_interest_spike
            condition: Dict with type-specific condition parameters
            
        Returns:
            Rule ID
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO alert_rules (ticker, alert_type, condition, is_active, created_at)
                   VALUES (?, ?, ?, 1, ?)""",
                (ticker.upper(), alert_type, json.dumps(condition), now),
            )
            conn.commit()
            return cursor.lastrowid

    def get_active_rules(self) -> List[Dict[str, Any]]:
        """Get all active alert rules."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM alert_rules WHERE is_active = 1 ORDER BY ticker, alert_type"
            )
            rows = cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "ticker": r["ticker"],
                    "alert_type": r["alert_type"],
                    "condition": json.loads(r["condition"]) if r["condition"] else {},
                    "is_active": bool(r["is_active"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def get_all_rules(self) -> List[Dict[str, Any]]:
        """Get all alert rules (active and inactive)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM alert_rules ORDER BY created_at DESC")
            rows = cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "ticker": r["ticker"],
                    "alert_type": r["alert_type"],
                    "condition": json.loads(r["condition"]) if r["condition"] else {},
                    "is_active": bool(r["is_active"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def deactivate_rule(self, rule_id: int) -> bool:
        """Deactivate an alert rule (soft-delete)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE alert_rules SET is_active = 0 WHERE id = ?", (rule_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def activate_rule(self, rule_id: int) -> bool:
        """Re-activate an alert rule."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE alert_rules SET is_active = 1 WHERE id = ?", (rule_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_rule(self, rule_id: int) -> bool:
        """Permanently delete an alert rule and its history."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM alert_history WHERE rule_id = ?", (rule_id,))
            cursor.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
            conn.commit()
            return cursor.rowcount > 0

    def fire_alert(self, rule_id: int, message: str, data: dict = None) -> int:
        """
        Record a fired alert.
        
        Args:
            rule_id: ID of the rule that triggered
            message: Human-readable alert message
            data: Optional dict with trigger data (prices, values, etc.)
            
        Returns:
            Alert ID
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO alert_history (rule_id, triggered_at, message, data, is_read)
                   VALUES (?, ?, ?, ?, 0)""",
                (rule_id, now, message, json.dumps(data) if data else None),
            )
            conn.commit()
            return cursor.lastrowid

    def get_unread_alerts(self) -> List[Dict[str, Any]]:
        """Get all unread alerts with rule info."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ah.*, ar.ticker, ar.alert_type
                FROM alert_history ah
                JOIN alert_rules ar ON ah.rule_id = ar.id
                WHERE ah.is_read = 0
                ORDER BY ah.triggered_at DESC
            """)
            rows = cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "rule_id": r["rule_id"],
                    "ticker": r["ticker"],
                    "alert_type": r["alert_type"],
                    "triggered_at": r["triggered_at"],
                    "message": r["message"],
                    "data": json.loads(r["data"]) if r["data"] else {},
                    "is_read": False,
                }
                for r in rows
            ]

    def get_alert_history(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Get paginated alert history."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ah.*, ar.ticker, ar.alert_type
                FROM alert_history ah
                JOIN alert_rules ar ON ah.rule_id = ar.id
                ORDER BY ah.triggered_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))
            rows = cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "rule_id": r["rule_id"],
                    "ticker": r["ticker"],
                    "alert_type": r["alert_type"],
                    "triggered_at": r["triggered_at"],
                    "message": r["message"],
                    "data": json.loads(r["data"]) if r["data"] else {},
                    "is_read": bool(r["is_read"]),
                }
                for r in rows
            ]

    def mark_alert_read(self, alert_id: int) -> bool:
        """Mark a single alert as read."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE alert_history SET is_read = 1 WHERE id = ?", (alert_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def mark_all_alerts_read(self) -> int:
        """Mark all alerts as read. Returns count of updated rows."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE alert_history SET is_read = 1 WHERE is_read = 0")
            conn.commit()
            return cursor.rowcount

    def get_unread_count(self) -> int:
        """Get count of unread alerts (for badge)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM alert_history WHERE is_read = 0")
            return cursor.fetchone()[0]

    def _row_to_saved_view(self, row: sqlite3.Row) -> SavedView:
        """Convert database row to SavedView object."""
        return SavedView(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            filters=row["filters"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_analysis(self, row: sqlite3.Row) -> Analysis:
        """Convert database row to Analysis object."""
        return Analysis(
            id=row["id"],
            ticker=row["ticker"],
            analysis_date=row["analysis_date"],
            created_at=row["created_at"],
            decision=row["decision"] or "",
            confidence=row["confidence"] or 0.0,
            llm_provider=row["llm_provider"] or "",
            deep_think_model=row["deep_think_model"] or "",
            quick_think_model=row["quick_think_model"] or "",
            debate_rounds=row["debate_rounds"] or 0,
            analysis_mode=row["analysis_mode"] if "analysis_mode" in row.keys() else "",
            risk_profile=row["risk_profile"] if "risk_profile" in row.keys() else "",
            market_report=row["market_report"] or "",
            fundamentals_report=row["fundamentals_report"] or "",
            news_report=row["news_report"] or "",
            sentiment_report=row["sentiment_report"] or "",
            data_provenance=row["data_provenance"] or "",
            run_settings=(row["run_settings"] or "") if "run_settings" in row.keys() else "",
            section_attribution=row["section_attribution"] or "",
            report_warnings=row["report_warnings"] or "",
            data_quality_score=row["data_quality_score"] or 0,
            sec_filings_snapshot=row["sec_filings_snapshot"] or "",
            earnings_transcript_snapshot=row["earnings_transcript_snapshot"] or "",
            has_sec_snapshot=row["has_sec_snapshot"] or 0,
            has_transcript_snapshot=row["has_transcript_snapshot"] or 0,
            bull_summary=row["bull_summary"] or "",
            bear_summary=row["bear_summary"] or "",
            investment_decision=row["investment_decision"] or "",
            risk_assessment=row["risk_assessment"] or "",
            trading_plan=row["trading_plan"] or "",
            final_decision=row["final_decision"] or "",
            total_tokens=row["total_tokens"] or 0,
            total_cost=row["total_cost"] or 0.0,
            duration_seconds=row["duration_seconds"] or 0.0,
            price_at_analysis=row["price_at_analysis"] or 0.0,
            price_after_7d=row["price_after_7d"],
            price_after_14d=row["price_after_14d"],
            price_after_30d=row["price_after_30d"],
            actual_return_7d=row["actual_return_7d"],
            actual_return_14d=row["actual_return_14d"],
            actual_return_30d=row["actual_return_30d"],
            alpha_7d=row["alpha_7d"] if "alpha_7d" in row.keys() else None,
            alpha_14d=row["alpha_14d"] if "alpha_14d" in row.keys() else None,
            alpha_30d=row["alpha_30d"] if "alpha_30d" in row.keys() else None,
            was_correct=bool(row["was_correct"]) if row["was_correct"] is not None else None,
            notes=row["notes"] if "notes" in row.keys() else "",
            tags=row["tags"] if "tags" in row.keys() else "",
            # Tier 2: Analyst ratings
            analyst_rating=row["analyst_rating"] if "analyst_rating" in row.keys() else "",
            analyst_target_mean=row["analyst_target_mean"] if "analyst_target_mean" in row.keys() else None,
            analyst_target_high=row["analyst_target_high"] if "analyst_target_high" in row.keys() else None,
            analyst_target_low=row["analyst_target_low"] if "analyst_target_low" in row.keys() else None,
            analyst_upside_pct=row["analyst_upside_pct"] if "analyst_upside_pct" in row.keys() else None,
            analyst_count=row["analyst_count"] if "analyst_count" in row.keys() else 0,
            screening_run_id=row["screening_run_id"] if "screening_run_id" in row.keys() else None,
            # Tier 2: Options intelligence
            options_atm_iv=row["options_atm_iv"] if "options_atm_iv" in row.keys() else None,
            options_iv_rank=row["options_iv_rank"] if "options_iv_rank" in row.keys() else None,
            options_pc_volume_ratio=row["options_pc_volume_ratio"] if "options_pc_volume_ratio" in row.keys() else None,
            options_pc_oi_ratio=row["options_pc_oi_ratio"] if "options_pc_oi_ratio" in row.keys() else None,
            options_max_pain=row["options_max_pain"] if "options_max_pain" in row.keys() else None,
            options_unusual_count=row["options_unusual_count"] if "options_unusual_count" in row.keys() else 0,
            # Tier 2: Investment profile
            investment_profile=row["investment_profile"] if "investment_profile" in row.keys() else "",
            # Decision pipeline audit
            signal_summary=row["signal_summary"] if "signal_summary" in row.keys() else "",
            decision_json=row["decision_json"] if "decision_json" in row.keys() else "",
            position_action=row["position_action"] if "position_action" in row.keys() else "",
            earnings_quality_grade=row["earnings_quality_grade"] if "earnings_quality_grade" in row.keys() else "",
            earnings_quality_data=row["earnings_quality_data"] if "earnings_quality_data" in row.keys() else "",
            intrinsic_value=row["intrinsic_value"] if "intrinsic_value" in row.keys() else None,
            intrinsic_value_data=row["intrinsic_value_data"] if "intrinsic_value_data" in row.keys() else "",
            scenario_analysis=row["scenario_analysis"] if "scenario_analysis" in row.keys() else "",
            catalyst_pipeline=row["catalyst_pipeline"] if "catalyst_pipeline" in row.keys() else "",
            peer_comps=row["peer_comps"] if "peer_comps" in row.keys() else "",
            screening_context_json=row["screening_context_json"] if "screening_context_json" in row.keys() else "",
        )

    def _row_to_backtest_run(self, row: sqlite3.Row) -> BacktestRun:
        return BacktestRun(
            id=row["id"],
            run_at=row["run_at"],
            ticker=row["ticker"] or "",
            start_date=row["start_date"] or "",
            end_date=row["end_date"] or "",
            limit_count=row["limit_count"] or 0,
            lookahead_days=row["lookahead_days"] or "",
            slippage_bps=row["slippage_bps"] or 0.0,
            transaction_cost_bps=row["transaction_cost_bps"] or 0.0,
            updated_count=row["updated_count"] or 0,
            skipped_count=row["skipped_count"] or 0,
            avg_return=row["avg_return"] or 0.0,
            win_rate=row["win_rate"] or 0.0,
            accuracy=row["accuracy"] or 0.0,
            strategy_sharpe=row["strategy_sharpe"] if "strategy_sharpe" in row.keys() else None,
            strategy_sortino=row["strategy_sortino"] if "strategy_sortino" in row.keys() else None,
            avg_alpha_30d=row["avg_alpha_30d"] if "avg_alpha_30d" in row.keys() else None,
            avg_signed_return_7d=(
                row["avg_signed_return_7d"] if "avg_signed_return_7d" in row.keys() else None
            ),
            return_vol_7d=row["return_vol_7d"] if "return_vol_7d" in row.keys() else None,
        )

    # =========================================================================
    # Database Maintenance
    # =========================================================================

    def vacuum(self) -> Dict[str, Any]:
        """Run VACUUM + ANALYZE to reclaim space and update query planner stats.

        Returns dict with size_before, size_after, bytes_saved.
        """
        db_file = Path(self.db_path)
        size_before = db_file.stat().st_size if db_file.is_file() else 0
        with self._connect() as conn:
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
        size_after = db_file.stat().st_size if db_file.is_file() else 0
        saved = size_before - size_after
        logger.info(
            "VACUUM complete: %d -> %d bytes (saved %d)",
            size_before, size_after, saved,
        )
        return {
            "size_before": size_before,
            "size_after": size_after,
            "bytes_saved": saved,
        }

    def purge_old_analyses(self, max_age_days: int, dry_run: bool = True) -> Dict[str, Any]:
        """Delete analyses older than *max_age_days* with cascade to related tables.

        Defaults to dry_run=True for safety.
        """
        cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, ticker, analysis_date FROM analyses WHERE analysis_date < ?",
                (cutoff,),
            )
            rows = cursor.fetchall()

        ids = [r["id"] for r in rows]
        summaries = [
            {"id": r["id"], "ticker": r["ticker"], "date": r["analysis_date"]}
            for r in rows
        ]

        if not dry_run and ids:
            with self._connect() as conn:
                cursor = conn.cursor()
                placeholders = ",".join("?" * len(ids))
                cursor.execute(f"DELETE FROM snapshot_kpis WHERE analysis_id IN ({placeholders})", ids)
                cursor.execute(f"DELETE FROM snapshot_guidance WHERE analysis_id IN ({placeholders})", ids)
                cursor.execute(f"DELETE FROM snapshot_risks WHERE analysis_id IN ({placeholders})", ids)
                cursor.execute(f"DELETE FROM analyses WHERE id IN ({placeholders})", ids)
                conn.commit()
            logger.info("Purged %d analyses older than %s", len(ids), cutoff)

        return {
            "count": len(ids),
            "cutoff_date": cutoff,
            "dry_run": dry_run,
            "purged": summaries,
        }

    def purge_old_movers_data(self, max_age_days: int, dry_run: bool = True) -> Dict[str, Any]:
        """Purge old movers snapshot rows and movers strategy run details."""
        cutoff_iso = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        cutoff_date = cutoff_iso[:10]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) AS c FROM movers_snapshots WHERE asof_date < ?",
                (cutoff_date,),
            )
            snapshots_count = int(cursor.fetchone()["c"])

            cursor.execute(
                """
                SELECT sr.id FROM screening_runs sr
                WHERE sr.run_at < ?
                  AND (
                        sr.strategy = 'movers'
                        OR (sr.strategy IS NULL AND sr.criteria LIKE '%"strategy": "movers"%')
                  )
                """,
                (cutoff_iso,),
            )
            run_ids = [int(r["id"]) for r in cursor.fetchall()]

        if not dry_run:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM movers_snapshots WHERE asof_date < ?",
                    (cutoff_date,),
                )
                if run_ids:
                    placeholders = ",".join("?" * len(run_ids))
                    cursor.execute(
                        f"DELETE FROM movers_run_details WHERE run_id IN ({placeholders})",
                        run_ids,
                    )
                conn.commit()

        return {
            "dry_run": dry_run,
            "cutoff_date": cutoff_date,
            "movers_snapshots": snapshots_count,
            "movers_run_details": len(run_ids),
        }


def _normalize_snapshot_payload(raw: str, marker_prefix: str) -> str:
    return normalize_snapshot_text(raw, marker_prefix)


def _extract_snapshot_from_state(state: Dict[str, Any], field: str, marker_prefix: str) -> str:
    raw = state.get(field, "")
    if raw:
        return _normalize_snapshot_payload(raw, marker_prefix)
    return extract_snapshot_from_messages(state.get("messages", []) or [], marker_prefix)


def _compute_state_warnings(state: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    seen: set = set()

    def _add(warning: str) -> None:
        if warning not in seen:
            seen.add(warning)
            warnings.append(warning)

    existing = state.get("report_warnings", [])
    from tradingagents.reporting.context_qc import RECOMPUTABLE_CONTEXT_WARNINGS

    structured_warnings = set(RECOMPUTABLE_CONTEXT_WARNINGS)
    if isinstance(existing, list):
        for warning in existing:
            if str(warning) in structured_warnings:
                continue
            _add(str(warning))

    required_fields = (
        "market_report",
        "fundamentals_report",
        "news_report",
        "sentiment_report",
        "investment_plan",
        "trader_investment_plan",
        "final_trade_decision",
    )
    for field in required_fields:
        if not (state.get(field) or "").strip():
            _add(f"{field}_missing")
    if not state.get("data_provenance"):
        _add("provenance_missing")

    try:
        from tradingagents.reporting.attribution import _extract_signal_json
    except Exception:
        _extract_signal_json = None  # type: ignore[assignment,misc]

    analyst_signal_fields = (
        "market_report",
        "fundamentals_report",
        "news_report",
        "sentiment_report",
    )
    if _extract_signal_json is not None:
        missing_signal = any(
            (state.get(field) or "").strip()
            and not _extract_signal_json(state.get(field) or "")
            for field in analyst_signal_fields
        )
    else:
        missing_signal = any(
            (state.get(field) or "").strip() and "SIGNAL_JSON:" not in (state.get(field) or "")
            for field in analyst_signal_fields
        )
    if missing_signal:
        _add("signal_json_missing")

    final_decision = state.get("final_trade_decision") or ""
    if str(final_decision).strip() and "DECISION_JSON:" not in str(final_decision):
        _add("decision_json_missing")

    try:
        from tradingagents.graph.signal_processing import extract_explicit_decision
        from tradingagents.reporting.context_qc import compute_context_warnings

        decision_word = extract_explicit_decision(final_decision) or ""
        for warning in compute_context_warnings(
            state,
            decision_word,
            ticker=state.get("company_of_interest"),
        ):
            _add(warning)
    except Exception:
        pass

    return warnings


def _extract_price_from_stock_payload(stock_data: str, analysis_date: str) -> Optional[float]:
    """Extract close price on (or before) analysis_date from stock CSV payload."""
    if not stock_data:
        return None
    try:
        target_date = datetime.strptime(str(analysis_date)[:10], "%Y-%m-%d")
    except Exception:
        target_date = None

    in_csv = False
    best_date = None
    best_close = None
    for raw_line in stock_data.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not in_csv:
            if line.startswith("Date,") and "Close" in line:
                in_csv = True
            continue

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            row_date = datetime.strptime(parts[0], "%Y-%m-%d")
            row_close = float(parts[4])
        except Exception:
            continue

        if target_date is not None:
            if row_date <= target_date and (best_date is None or row_date > best_date):
                best_date = row_date
                best_close = row_close
        else:
            if best_date is None or row_date > best_date:
                best_date = row_date
                best_close = row_close

    return best_close


def _extract_price_at_analysis_from_state(
    state: Dict[str, Any],
    ticker: str,
    analysis_date: str,
) -> float:
    """Resolve deterministic as-of close price from stock-data provenance."""
    provenance = state.get("data_provenance") or []
    args = None

    for event in reversed(provenance if isinstance(provenance, list) else []):
        if (
            isinstance(event, dict)
            and event.get("method") == "get_stock_data"
            and event.get("status") == "success"
        ):
            ev_args = event.get("args") or []
            if len(ev_args) >= 3 and str(ev_args[0]).upper() == ticker.upper():
                args = [str(ev_args[0]), str(ev_args[1]), str(ev_args[2])]
                break

    # Always resolve price through the true analysis_date (not whichever end-date the model requested).
    try:
        analysis_dt = datetime.strptime(str(analysis_date)[:10], "%Y-%m-%d")
    except Exception:
        return 0.0

    if args is None:
        start_dt = analysis_dt.replace(year=max(1970, analysis_dt.year - 2))
        args = [ticker.upper(), start_dt.strftime("%Y-%m-%d"), analysis_dt.strftime("%Y-%m-%d")]
    else:
        args[2] = analysis_dt.strftime("%Y-%m-%d")

    try:
        from tradingagents.dataflows.interface import route_to_vendor

        stock_data = route_to_vendor("get_stock_data", args[0], args[1], args[2])
        price = _extract_price_from_stock_payload(str(stock_data), analysis_date)
        return float(price) if price is not None else 0.0
    except Exception as exc:
        logger.debug("Could not resolve price_at_analysis for %s: %s", ticker, exc)
        return 0.0


def create_analysis_from_state(
    state: Dict[str, Any],
    ticker: str,
    analysis_date: str,
    decision: str,
    config: Dict[str, Any],
    duration_seconds: float = 0.0,
    screening_run_id: Optional[int] = None,
    screening_context: Optional[Dict[str, Any]] = None,
) -> Analysis:
    """Create an Analysis object from TradingAgents state.
    
    Args:
        state: Final state from ta.propagate()
        ticker: Stock ticker
        analysis_date: Date of analysis
        decision: Final decision string
        config: Configuration used
        duration_seconds: Time taken for analysis
        
    Returns:
        Analysis object ready to save
    """
    # Extract debate info
    investment_debate = state.get("investment_debate_state", {})
    risk_debate = state.get("risk_debate_state", {})
    
    # Get bull/bear summaries (last entry in history)
    # History values are strings (accumulated debate text), not lists.
    # Use the full string as the summary — it contains the final debate position.
    def _last_segment(history) -> str:
        """Extract last meaningful segment from a history value (string or list)."""
        if not history:
            return ""
        if isinstance(history, list):
            return str(history[-1]) if history else ""
        # It's a string — return the full content (it IS the summary)
        return str(history)

    bull_history = investment_debate.get("bull_history", "")
    bear_history = investment_debate.get("bear_history", "")
    
    bull_summary = _last_segment(bull_history)
    bear_summary = _last_segment(bear_history)
    
    # Risk assessment - combine all perspectives
    risk_parts = []
    if risk_debate.get("risky_history"):
        risk_parts.append(f"AGGRESSIVE: {_last_segment(risk_debate['risky_history'])}")
    if risk_debate.get("safe_history"):
        risk_parts.append(f"CONSERVATIVE: {_last_segment(risk_debate['safe_history'])}")
    if risk_debate.get("neutral_history"):
        risk_parts.append(f"NEUTRAL: {_last_segment(risk_debate['neutral_history'])}")
    
    from tradingagents.reporting.attribution import compute_section_attribution, attribution_to_json

    # Fetch analyst ratings for storage (Tier 2)
    analyst_rating_str = ""
    analyst_target_mean = None
    analyst_target_high = None
    analyst_target_low = None
    analyst_upside_pct = None
    analyst_count = 0
    ratings: Dict[str, Any] = {}
    try:
        from tradingagents.dataflows.yfinance_extended import get_analyst_ratings
        ratings = get_analyst_ratings(ticker)
        analyst_rating_str = ratings.get("recommendation_key") or ""
        analyst_target_mean = ratings.get("target_mean_price")
        analyst_target_high = ratings.get("target_high_price")
        analyst_target_low = ratings.get("target_low_price")
        analyst_upside_pct = ratings.get("upside_pct")
        analyst_count = ratings.get("number_of_analysts", 0) or 0
        state["analyst_ratings"] = ratings
    except Exception as e:
        logger.debug("Analyst ratings fetch for storage failed for %s: %s", ticker, e)

    # Fetch options intelligence for storage (Tier 2)
    options_atm_iv = None
    options_iv_rank = None
    options_pc_volume_ratio = None
    options_pc_oi_ratio = None
    options_max_pain = None
    options_unusual_count = 0
    price_at_analysis = _extract_price_at_analysis_from_state(state, ticker, analysis_date)
    try:
        from tradingagents.dataflows.yfinance_extended import get_options_summary, sanitize_options_snapshot
        opts = sanitize_options_snapshot(get_options_summary(ticker), spot=price_at_analysis or None)
        options_atm_iv = opts.get("atm_iv")
        options_iv_rank = opts.get("iv_rank")
        options_pc_volume_ratio = opts.get("put_call_volume_ratio")
        options_pc_oi_ratio = opts.get("put_call_oi_ratio")
        options_max_pain = opts.get("max_pain")
        options_unusual_count = opts.get("unusual_activity_count", 0) or 0
    except Exception as e:
        logger.debug("Options data fetch for storage failed for %s: %s", ticker, e)

    section_attribution = compute_section_attribution(state, decision)
    sec_filings_snapshot = _extract_snapshot_from_state(
        state,
        "sec_filings_snapshot",
        "SEC_FILINGS_SNAPSHOT_",
    )
    earnings_transcript_snapshot = _extract_snapshot_from_state(
        state,
        "earnings_transcript_snapshot",
        "EARNINGS_TRANSCRIPT_SNAPSHOT_",
    )

    try:
        from tradingagents.reporting.position_action import (
            enforce_position_action,
            parse_position_action_from_state,
        )

        enforce_position_action(state)
        position_action = parse_position_action_from_state(state) or ""
    except Exception as e:
        logger.debug("Position action enforcement skipped during save: %s", e)
        position_action = ""

    try:
        from tradingagents.reporting.book_action import (
            decision_from_state,
            enforce_book_action,
        )
        from tradingagents.reporting.position_action import (
            parse_position_action_from_state as _parse_position_action,
        )

        enforce_book_action(state, config)
        decision = decision_from_state(state, decision)
        position_action = _parse_position_action(state) or position_action
    except Exception as e:
        logger.debug("Book action enforcement skipped during save: %s", e)

    report_warnings = _compute_state_warnings(state)
    has_sec_snapshot = 1 if sec_filings_snapshot else 0
    has_transcript_snapshot = 1 if earnings_transcript_snapshot else 0
    try:
        from tradingagents.dataflows.instrument_identity import is_commodity_etf

        identity = state.get("instrument_identity") if isinstance(state.get("instrument_identity"), dict) else None
        commodity_etf = is_commodity_etf(ticker, identity)
    except Exception:
        commodity_etf = False
    resolved_analysis_mode = (
        config.get("analysis_mode") or state.get("analysis_mode") or "standard"
    )
    data_quality_score = compute_data_quality_score(
        state.get("data_provenance", []),
        analysis_mode=resolved_analysis_mode,
        has_transcript_snapshot=bool(has_transcript_snapshot),
        is_commodity_etf=commodity_etf,
    )
    # LLM token/cost usage is attached by TradingAgentsGraph.propagate()
    # through the callback-based runtime tracker. Keep robust fallback to 0.
    try:
        total_tokens = int(float(state.get("total_tokens", 0) or 0))
    except (TypeError, ValueError):
        total_tokens = 0
    try:
        total_cost = float(state.get("total_cost", 0.0) or 0.0)
    except (TypeError, ValueError):
        total_cost = 0.0

    # Compute confidence score (same logic used for HTML report)
    confidence_score = 0.0
    try:
        from tradingagents.reporting.pdf_generator import (
            extract_confidence_score,
            calibrate_confidence_score,
        )
        raw_confidence = extract_confidence_score(state, decision)
        config_calibration = config.get("confidence_calibration", {})
        if config_calibration.get("enabled", True):
            quality_weight = config_calibration.get("quality_weight", 0.3)
            confidence_score = float(calibrate_confidence_score(
                raw_confidence,
                data_quality=data_quality_score,
                quality_weight=quality_weight,
            ))
        else:
            confidence_score = float(raw_confidence)
    except Exception as e:
        logger.warning("Confidence score computation failed: %s", e)

    return Analysis(
        ticker=ticker.upper(),
        analysis_date=analysis_date,
        decision=decision,
        confidence=confidence_score,
        llm_provider=config.get("llm_provider", ""),
        deep_think_model=config.get("deep_think_llm", ""),
        quick_think_model=config.get("quick_think_llm", ""),
        debate_rounds=config.get("max_debate_rounds", 0),
        analysis_mode=resolved_analysis_mode,
        risk_profile=config.get("risk_profile", "growth"),
        market_report=state.get("market_report", ""),
        fundamentals_report=state.get("fundamentals_report", ""),
        news_report=state.get("news_report", ""),
        sentiment_report=state.get("sentiment_report", ""),
        data_provenance=json.dumps(state.get("data_provenance", [])),
        run_settings=json.dumps(state.get("run_settings") or {}),
        section_attribution=attribution_to_json(section_attribution),
        report_warnings=json.dumps(report_warnings),
        data_quality_score=data_quality_score,
        sec_filings_snapshot=sec_filings_snapshot,
        earnings_transcript_snapshot=earnings_transcript_snapshot,
        has_sec_snapshot=has_sec_snapshot,
        has_transcript_snapshot=has_transcript_snapshot,
        bull_summary=bull_summary,
        bear_summary=bear_summary,
        investment_decision=investment_debate.get("judge_decision", ""),
        risk_assessment="\n\n".join(risk_parts),
        trading_plan=state.get("trader_investment_plan", ""),
        final_decision=state.get("final_trade_decision", ""),
        total_tokens=total_tokens,
        total_cost=total_cost,
        duration_seconds=duration_seconds,
        price_at_analysis=price_at_analysis,
        # Tier 2: Analyst ratings stored at analysis time
        analyst_rating=analyst_rating_str,
        analyst_target_mean=analyst_target_mean,
        analyst_target_high=analyst_target_high,
        analyst_target_low=analyst_target_low,
        analyst_upside_pct=analyst_upside_pct,
        analyst_count=analyst_count,
        screening_run_id=screening_run_id,
        # Tier 2: Options intelligence stored at analysis time
        options_atm_iv=options_atm_iv,
        options_iv_rank=options_iv_rank,
        options_pc_volume_ratio=options_pc_volume_ratio,
        options_pc_oi_ratio=options_pc_oi_ratio,
        options_max_pain=options_max_pain,
        options_unusual_count=options_unusual_count,
        # Tier 2: Investment profile used for this analysis
        investment_profile=json.dumps(state.get("investment_profile")) if state.get("investment_profile") else None,
        # Institutional analysis enhancements
        earnings_quality_grade=(state.get("earnings_quality") or {}).get("grade", ""),
        earnings_quality_data=json.dumps(state.get("earnings_quality")) if state.get("earnings_quality") else "",
        intrinsic_value=(state.get("intrinsic_value") or {}).get("fair_value"),
        intrinsic_value_data=json.dumps(state.get("intrinsic_value")) if state.get("intrinsic_value") else "",
        scenario_analysis=json.dumps(state.get("scenario_analysis")) if state.get("scenario_analysis") else "",
        catalyst_pipeline=state.get("catalyst_pipeline") or "",
        peer_comps=json.dumps(state.get("peer_comps")) if state.get("peer_comps") else "",
        # Decision pipeline audit data
        signal_summary=_compute_signal_summary_for_db(state, ticker),
        decision_json=_extract_decision_json_for_db(state),
        position_action=position_action,
        screening_context_json=json.dumps(
            screening_context or state.get("screening_context") or {}
        ),
    )


def _compute_signal_summary_for_db(state: Dict[str, Any], ticker: str) -> str:
    """Compute and serialize the signal summary for database storage."""
    try:
        from tradingagents.graph.signal_aggregator import compute_signal_summary
        result = compute_signal_summary(state, ticker)
        return json.dumps({
            "composite": result.get("composite"),
            "composite_label": result.get("composite_label"),
            "bullish_count": result.get("bullish_count"),
            "bearish_count": result.get("bearish_count"),
            "neutral_count": result.get("neutral_count"),
            "total_dimensions": result.get("total_dimensions"),
            "avg_confidence": result.get("avg_confidence"),
            "data_completeness": result.get("data_completeness"),
            "disagreement": result.get("disagreement"),
            "disagreement_label": result.get("disagreement_label"),
            "missing_signals": result.get("missing_signals"),
            "market_regime": result.get("market_regime"),
            "index_trend": result.get("index_trend"),
            "index_stress": result.get("index_stress"),
            "index_regime": result.get("index_regime"),
            "index_regime_label": result.get("index_regime_label"),
            "weight_overlay_key": result.get("weight_overlay_key"),
            "effective_weights": result.get("effective_weights"),
        })
    except Exception:
        return ""


def _extract_decision_json_for_db(state: Dict[str, Any]) -> str:
    """Extract DECISION_JSON from final_trade_decision for database storage."""
    text = state.get("final_trade_decision", "")
    if not text:
        return ""
    marker = "DECISION_JSON:"
    idx = text.find(marker)
    if idx == -1:
        return ""
    after = text[idx + len(marker):]
    brace_start = after.find("{")
    if brace_start == -1:
        return ""
    depth = 0
    for i in range(brace_start, len(after)):
        ch = after[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return after[brace_start:i + 1]
    return ""


# =========================================================================
# Singleton accessor — avoids repeated __init__ / _init_db overhead
# =========================================================================
_db_instances: Dict[str, "ResearchDatabase"] = {}
_db_lock = threading.Lock()


def get_db(db_path: str = "research.db") -> ResearchDatabase:
    """Return (or create) a singleton ResearchDatabase for the given path.

    Thread-safe.  The instance is reused across all callers that share the
    same *db_path* string, eliminating repeated ``_init_db()`` schema checks
    and connection overhead.
    """
    if db_path not in _db_instances:
        with _db_lock:
            if db_path not in _db_instances:
                _db_instances[db_path] = ResearchDatabase(db_path)
    return _db_instances[db_path]
