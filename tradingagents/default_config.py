import os
from copy import deepcopy

DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", "./results"),
    "data_dir": None,  # Set to your local data directory path if you have one (e.g., "/path/to/data")
    "data_cache_dir": os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
        "dataflows/data_cache",
    ),
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.6-terra",  # GPT-5.6 Terra: debates / final judgment (A/B vs gpt-5.4)
    # GPT-5.6 Luna for tool-calling analyst loops; watch cost + 429 rate vs gpt-4o-mini.
    "quick_think_llm": "gpt-5.6-luna",
    "backend_url": "https://api.openai.com/v1",
    # Optional per-model cost overrides used by runtime telemetry estimation.
    # Prices are USD per 1M tokens. ``cached_input`` is the discounted rate the
    # provider charges for prompt-cache hits (OpenAI: 50% of input). Omitting
    # ``cached_input`` falls back to ``input * 0.5`` in the estimator. Keep in
    # sync with your active contract/card rates for billing-grade accuracy.
    "model_pricing_usd_per_1m": {
        "gpt-5.6-terra": {"input": 2.00, "cached_input": 0.20, "output": 12.00},
        "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "output": 1.20},
        # Prior defaults retained for A/B rollback and dated usage metadata.
        "gpt-5.4": {"input": 2.50, "cached_input": 1.25, "output": 15.00},
        "gpt-5.2-2025-12-11": {
            "input": 2.50,
            "cached_input": 1.25,
            "output": 15.00,
        },
        "gpt-4o-mini-2024-07-18": {
            "input": 0.15,
            "cached_input": 0.075,
            "output": 0.60,
        },
    },
    # OpenAI prompt caching is automatic for prompts >= 1024 tokens. Setting a
    # ``prompt_cache_key`` improves cache locality across distributed
    # deployments by routing requests with the same key to the same backend
    # shard. Use a per-process/run identifier to maximize hits within a single
    # analysis without leaking cache between unrelated workloads. Set to None
    # to disable (useful for A/B comparisons).
    "openai_prompt_cache_key": "tradingagents-default",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    "reset_memory": False,

    # Token budgets and runtime safeguards (mode-aware via ANALYSIS_MODES overrides below)
    "token_budget": {
        "analyst": {
            "max_context_tokens": 16000,
            "max_messages": 18,
            "market_max_tool_messages": 6,
            "social_max_tool_messages": 4,
            "news_max_tool_messages": 5,
            "fundamentals_max_tool_messages": 5,
            "market_max_indicators": 4,
            "fundamentals_enriched_context_max_tokens": 6000,
        },
        "tools": {
            "get_stock_data": 6000,
            "get_indicators": 3000,
            "get_fundamentals": 6000,
            "get_balance_sheet": 4000,
            "get_cashflow": 4000,
            "get_income_statement": 4000,
            "get_news": 3000,
            "get_global_news": 2000,
            "get_insider_sentiment": 1200,
            "get_insider_transactions": 1500,
        },
        "llm": {
            "max_retries": 6,
            "quick_max_output_tokens": 1600,
            "deep_max_output_tokens": 2200,
        },
        "adaptive_degrade": {
            "enabled": True,
            "retry_on_tpm": True,
            "cooldown_multiplier_max": 4,
            "cooldown_retry_sleep_seconds": 45,
            "feature_flag": True,
        },
        "observability": {
            "log_token_metrics": True,
        },
    },
    
    # ==========================================================================
    # ANALYSIS MODE CONFIGURATION
    # ==========================================================================
    # Research Modes (control depth/thoroughness):
    # "quick"    - Fast scan: Basic technicals + 5 headlines, 1 debate round (~1-2 min, $0)
    # "standard" - Full analysis: All data sources, 2 debate rounds (~4-5 min, $0)
    # "deep"     - Research grade: Standard + Perplexity deep research (~5-6 min, ~$0.10)
    "analysis_mode": "standard",
    "analysis": {
        # Hard ceiling on analyst tool loops; effective cap is min(this, token_budget.analyst.*_max_tool_messages)
        "max_tool_rounds": int(os.getenv("TRADINGAGENTS_MAX_TOOL_ROUNDS", "20") or 20),
        # parallel: analysts fan out from START (wall time ≈ slowest); sequential: TPM escape hatch
        "analyst_layout": (
            os.getenv("TRADINGAGENTS_ANALYST_LAYOUT", "parallel").strip().lower() or "parallel"
        ),
    },
    "checkpoint_enabled": os.getenv("TRADINGAGENTS_CHECKPOINT_ENABLED", "").strip().lower()
    in {"1", "true", "yes", "on"},
    
    # ==========================================================================
    # RISK PROFILE CONFIGURATION
    # ==========================================================================
    # Risk Profiles (control decision weighting - applies to ALL research modes):
    # "aggressive"   - High conviction: Favors BUY, views dips as opportunities
    # "growth"       - Balanced: Weighs analyst consensus + growth potential
    # "conservative" - Cautious: Prioritizes downside protection
    "risk_profile": "growth",
    
    # Enable/disable caching (recommended: True)
    "enable_cache": True,
    
    # Enable/disable usage tracking (recommended: True)
    "enable_usage_tracking": True,

    # Enable/disable data provenance logging
    "enable_provenance": True,
    "provenance_max_events": 500,
    
    # ==========================================================================
    # DATA VENDOR CONFIGURATION
    # ==========================================================================
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: yfinance, alpha_vantage, local
        "technical_indicators": "yfinance",  # Options: yfinance, alpha_vantage, local
        "fundamental_data": "yfinance",      # Changed from alpha_vantage: matches all ANALYSIS_MODES overrides; conserves Alpha Vantage quota
        "news_data": "finnhub",              # Options: finnhub, perplexity, alpha_vantage, openai, google, local
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
        # Example: "get_news": "openai",               # Override category default
    },
    
    # ==========================================================================
    # PERPLEXITY CONFIGURATION (for deep research mode)
    # ==========================================================================
    "perplexity": {
        "model": "sonar-pro",          # "sonar-pro" (best quality) or "sonar" (faster)
        "temperature": 0.2,            # Lower = more factual
        "max_tokens": 3000,            # Max response length
        "monthly_limit": 100,          # Monthly request budget
        "discovery_reserve": 10,       # Sub-budget reserved for Discover / auto-discovery
        "warn_at_percent": 80,         # Warn when usage reaches this %
    },
    # ==========================================================================
    # SCREENING CONFIGURATION (Tier 2: Opportunity Engine)
    # ==========================================================================
    "screening": {
        "max_watchlist_size": 500,
        "hard_max_watchlist_size": 3250,
        "default_top_n": 20,
        "scan_all": {
            "mode": "union_buckets",  # per_watchlist|union_buckets
            "union_feature_flag": True,
            # Keep aligned with hard_max_watchlist_size so Scan All reports an
            # accurate scanned universe (deduped union, not raw watchlist sum).
            "union_max_tickers": 3250,
            # Max gap (minutes) between consecutive watchlist runs to treat as
            # one Scan All batch. Long batches can run 60–90 min end-to-end.
            "batch_gap_minutes": 45,
            # Prefer strategy-tagged Scan All runs while retaining gap-only
            # fallback for batches produced before this metadata existed.
            "batch_prefer_strategy_tag": True,
            # When a watchlist has a newer Run Screening than its Scan All row,
            # Cross-Watchlist overlays the fresher per-list run on that list.
            # Orphan singles (no prior Scan All for that lens) stay in Screening
            # Results only. False = Scan All provenance only (no overlays).
            "overlay_fresher_single_runs": True,
            # Include a list if its matching run is within this many days, or
            # it belongs to the latest Scan All job for that lens.
            "board_max_age_days": 30,
            # Board-only: sparse/micro names stay in the full run and cards,
            # but do not occupy the default Cross-Watchlist headline.
            "headline": {
                "min_coverage_pct": 50,
                "min_dollar_adv_usd": 5_000_000,
                "include_etf_slot": True,
                "etf_max": 2,
            },
            # Stronger than the global warn-only coverage policy so Scan All
            # opportunity sort does not treat a 41% coverage print as equal
            # to a fully enriched large-cap.
            "coverage_penalty": {
                "mode": "penalize",
                "threshold": 60,
                "multiplier": 0.90,
            },
            "weekly_mtf": {
                "enabled": True,
                "hydrate_top_n_cap": 90,
                "hard_max_hydrate_top_n": 150,
                # After final leaderboard sort, backfill weekly MTF for every
                # row we actually display (top table + stratified heads). This
                # closes the gap where a ticker moves into #1 after overlay
                # re-scoring but was outside the pre-sort hydrate pool.
                "ensure_display_hydrate": True,
                "hydrate_short_candidates": False,
                "short_hydrate_cap": 20,
                "confluence_demote_enabled": True,
                "weekly_bullish_threshold": 0.65,
                "weekly_bearish_threshold": 0.35,
            },
        },
        # Ticker health tracks per-ticker OHLCV outcomes so the engine can
        # identify likely-delisted symbols. ``track`` is cheap (one INSERT
        # per ticker per scan). ``auto_evict`` is a separate opt-in that the
        # evict-delisted-tickers script consults; the scan loop itself never
        # deletes rows — eviction is always an explicit admin action.
        "ticker_health": {
            "track": True,
            "auto_evict": False,
            "min_failures": 3,
            "min_days_since_success": 2,
            # Skip tickers already evicted from ticker_metadata up-front in
            # engine.scan() so we don't waste yfinance budget retrying them on
            # every run. Set to False to re-enable retries (e.g. when a
            # delisted ticker is re-listed on a different venue).
            "skip_evicted": True,
            # Optional window (in days) limiting which evictions count for the
            # skip filter. None / 0 means "always skip". Setting e.g. 90 means
            # evictions older than 90 days will be retried so re-listings can
            # be picked up automatically by the next refresh.
            "skip_evicted_within_days": None,
        },
        "scan_buckets": {
            "enabled": True,
            "large_cap": {"index_key": "sp500_full", "max_tickers": 500},
            "mid_cap": {"index_key": "sp400_mid", "max_tickers": 400},
            "small_cap": {"index_key": "sp600_small", "max_tickers": 600},
            "small_cap_breadth": {"index_key": "russell2000_full", "max_tickers": 900},
        },
        "funnel_cutoff_pct": 0.60,  # Top 60% proceed from Tier 1 to Tier 2
        # Income/value/quality books need .info before they can rank. Skip the
        # T1 technical funnel on lists at or below this size; above it, raise
        # the cutoff so the identity sleeve is computed for most names.
        "identity_funnel_bypass_below": 150,
        "identity_funnel_min_cutoff": 0.85,
        "sector_median_persist_min_tickers": 80,
        # Cap expensive Tier 2 enrichment to keep union scan-all runtimes bounded.
        # 0 disables the cap (enrich all funnel survivors).
        "tier2_max_tickers": 700,
        # Cap auxiliary per-ticker calls used by estimate/rating momentum.
        # These are costlier than plain .info pulls and are not required for
        # every survivor to produce useful ranked output.
        "tier2_aux_max_tickers": 250,
        # Skip estimate/rating fetch for symbols with sparse fundamentals
        # coverage (market-cap present but no valuation + no analyst fields),
        # reducing repeated quoteSummary 404 noise for micro/special situations.
        "skip_sparse_fundamentals_aux": True,
        # Window (in days) used when computing the scan-all p95 metric. Older
        # samples roll out of the calculation so the reported p95 reflects
        # current behaviour after a perf fix lands instead of dragging upward
        # from regressed historical runs. Use ``make scan-metrics-prune`` to
        # also delete the underlying rows when the rolling window isn't enough.
        "p95_window_days": 7,
        # Minimum normalized directional conviction required to label a row
        # "bullish" or "bearish". Below this threshold the label becomes
        # "neutral". Prevents bullish-polarity bias in the 16-signal space from
        # tripping weakly-scoring rows into a "bullish" label.
        "direction_conviction_threshold": 0.05,
        "enable_enhanced_signals": True,  # Compute valuation/options/smart_money for top tickers
        "enhanced_top_pct": 0.30,   # Top % for enhanced enrichment
        "enhanced_min_count": 15,   # Minimum enhanced enrichment count
        # Absolute cap on the enhanced pool, mirroring tier2_max_tickers, so
        # runtime/API-call volume doesn't scale linearly as the universe
        # grows (enhanced_top_pct alone let this climb from ~600 to ~960
        # tickers when hard_max_watchlist_size went from 2000 to 3250).
        # 0 disables the cap.
        "enhanced_max_tickers": 600,
        "chunk_sleep_seconds": 1.25,  # Inter-chunk pacing (OHLCV)
        "info_chunk_sleep_seconds": 0.5,  # Inter-chunk pacing (info/fundamental fetch)
        # During scan: use DB metadata younger than metadata_cached_max_age_days
        # without live Yahoo refresh. "refresh_stale" restores pre-change behavior.
        "metadata_resolve_during_scan": "use_cached",
        "metadata_cached_max_age_days": 30,
        # Intraday scheduler: ttl_only keeps screening caches until TTL/post-close;
        # full_invalidate wipes screening_prices + per-ticker + SPY hourly.
        "intraday_cache_policy": "ttl_only",
        # When Yahoo's breaker opens mid-scan, wait and retry instead of
        # marking the rest of a 3k universe skipped (Scan All job 2260b71d
        # finished 0 scored / 6444 skipped). Cap so a stuck breaker cannot
        # hang the process indefinitely.
        "breaker_wait_max_seconds": 900,
        "breaker_wait_max_per_trip": 360,
        "cross_sectional_zscore": {
            "enabled": True,
            "min_peers": 8,
            "clip": 3.0,
            "eligible_keys": [
                "quality_factor",
                "income_factor",
                "valuation_gap",
                "relative_strength",
                "residual_momentum_12_1",
            ],
        },
        "signal_weights": {
            # Full map sums to 1.00. rsi_overbought stays > 0 for direction;
            # composite skips that key locally.
            "volume_surge": 0.08,
            "rsi_oversold": 0.03,
            "rsi_overbought": 0.03,
            "ma_crossover": 0.06,
            "relative_strength": 0.08,
            "bollinger_squeeze": 0.03,
            "trend_strength": 0.05,
            "price_vs_target": 0.09,
            "earnings_proximity": 0.05,
            "insider_buying": 0.04,
            "estimate_momentum": 0.08,
            "rating_momentum": 0.07,
            "valuation_gap": 0.08,
            "options_sentiment": 0.04,
            "smart_money": 0.04,
            "pead_drift": 0.03,
            "weekly_trend_alignment": 0.04,
            "quality_factor": 0.05,
            "income_factor": 0.01,
            "residual_momentum_12_1": 0.02,
        },
        "presets": {
            # Every named preset carries weekly_trend_alignment > 0 and
            # rsi_overbought > 0 (direction). Composite skips overbought.
            # Hidden aliases keep identical weights to their visible books.
            "momentum_hunter": {
                "description": "Strong upward momentum with volume and signed trend confirmation",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.14, "rsi_oversold": 0.02, "rsi_overbought": 0.01, "ma_crossover": 0.11,
                    "price_vs_target": 0.03, "earnings_proximity": 0.02, "insider_buying": 0.02,
                    "relative_strength": 0.16, "bollinger_squeeze": 0.04, "trend_strength": 0.08,
                    "estimate_momentum": 0.04, "rating_momentum": 0.03,
                    "valuation_gap": 0.02, "options_sentiment": 0.05, "smart_money": 0.06,
                    "pead_drift": 0.05, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.02, "income_factor": 0.00, "residual_momentum_12_1": 0.06,
                },
            },
            "value_fisher": {
                "description": "Undervalued names with revisions and quality, not cloned flow",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.01, "rsi_oversold": 0.04, "rsi_overbought": 0.02, "ma_crossover": 0.01,
                    "price_vs_target": 0.14, "earnings_proximity": 0.03, "insider_buying": 0.03,
                    "relative_strength": 0.01, "bollinger_squeeze": 0.02, "trend_strength": 0.03,
                    "estimate_momentum": 0.10, "rating_momentum": 0.08,
                    "valuation_gap": 0.20, "options_sentiment": 0.02, "smart_money": 0.06,
                    "pead_drift": 0.02, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.10, "income_factor": 0.02, "residual_momentum_12_1": 0.02,
                },
            },
            "earnings_play": {
                "description": "Event-risk gate plus revisions, options, and PEAD — not 20% proximity alpha",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.05, "rsi_oversold": 0.02, "rsi_overbought": 0.01, "ma_crossover": 0.03,
                    "price_vs_target": 0.07, "earnings_proximity": 0.09, "insider_buying": 0.03,
                    "relative_strength": 0.05, "bollinger_squeeze": 0.03, "trend_strength": 0.04,
                    "estimate_momentum": 0.13, "rating_momentum": 0.07,
                    "valuation_gap": 0.03, "options_sentiment": 0.10, "smart_money": 0.04,
                    "pead_drift": 0.10, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.03, "income_factor": 0.00, "residual_momentum_12_1": 0.04,
                },
            },
            "smart_money_tracker": {
                "description": "Institutional flow, short-interest positioning, and options activity",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.06, "rsi_oversold": 0.02, "rsi_overbought": 0.01, "ma_crossover": 0.03,
                    "price_vs_target": 0.05, "earnings_proximity": 0.02, "insider_buying": 0.08,
                    "relative_strength": 0.06, "bollinger_squeeze": 0.02, "trend_strength": 0.03,
                    "estimate_momentum": 0.05, "rating_momentum": 0.04,
                    "valuation_gap": 0.03, "options_sentiment": 0.14, "smart_money": 0.20,
                    "pead_drift": 0.04, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.03, "income_factor": 0.00, "residual_momentum_12_1": 0.05,
                },
            },
            "dividend_income": {
                "description": "Income book: yield with payout sanity plus quality, not unsigned ADX",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.01, "rsi_oversold": 0.03, "rsi_overbought": 0.02, "ma_crossover": 0.02,
                    "price_vs_target": 0.08, "earnings_proximity": 0.02, "insider_buying": 0.04,
                    "relative_strength": 0.02, "bollinger_squeeze": 0.01, "trend_strength": 0.04,
                    "estimate_momentum": 0.06, "rating_momentum": 0.05,
                    "valuation_gap": 0.10, "options_sentiment": 0.01, "smart_money": 0.03,
                    "pead_drift": 0.02, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.12, "income_factor": 0.26, "residual_momentum_12_1": 0.02,
                },
            },
            "small_cap_growth": {
                "description": "Alias of momentum_hunter for stored watchlist defaults and CLI",
                "operator_visible": False,
                "weights": {
                    "volume_surge": 0.14, "rsi_oversold": 0.02, "rsi_overbought": 0.01, "ma_crossover": 0.11,
                    "price_vs_target": 0.03, "earnings_proximity": 0.02, "insider_buying": 0.02,
                    "relative_strength": 0.16, "bollinger_squeeze": 0.04, "trend_strength": 0.08,
                    "estimate_momentum": 0.04, "rating_momentum": 0.03,
                    "valuation_gap": 0.02, "options_sentiment": 0.05, "smart_money": 0.06,
                    "pead_drift": 0.05, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.02, "income_factor": 0.00, "residual_momentum_12_1": 0.06,
                },
            },
            "commodity_cyclical": {
                "description": "Energy, metals, and mining — trend, relative strength, and flow",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.08, "rsi_oversold": 0.02, "rsi_overbought": 0.02, "ma_crossover": 0.05,
                    "price_vs_target": 0.04, "earnings_proximity": 0.02, "insider_buying": 0.04,
                    "relative_strength": 0.14, "bollinger_squeeze": 0.03, "trend_strength": 0.14,
                    "estimate_momentum": 0.05, "rating_momentum": 0.04,
                    "valuation_gap": 0.04, "options_sentiment": 0.03, "smart_money": 0.12,
                    "pead_drift": 0.05, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.02, "income_factor": 0.00, "residual_momentum_12_1": 0.03,
                },
            },
            "short_squeeze": {
                "description": "Squeeze setup: high SI pressure, volume, options skew — covering just starting",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.12, "rsi_oversold": 0.02, "rsi_overbought": 0.02, "ma_crossover": 0.04,
                    "price_vs_target": 0.02, "earnings_proximity": 0.02, "insider_buying": 0.02,
                    "relative_strength": 0.10, "bollinger_squeeze": 0.04, "trend_strength": 0.03,
                    "estimate_momentum": 0.03, "rating_momentum": 0.02,
                    "valuation_gap": 0.02, "options_sentiment": 0.12, "smart_money": 0.04,
                    "pead_drift": 0.03, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.01, "income_factor": 0.00, "residual_momentum_12_1": 0.10,
                    "short_pressure": 0.16,
                },
            },
            "quality_compounder": {
                "description": "Quality plus revision stability and light value",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.02, "rsi_oversold": 0.02, "rsi_overbought": 0.01, "ma_crossover": 0.02,
                    "price_vs_target": 0.08, "earnings_proximity": 0.02, "insider_buying": 0.04,
                    "relative_strength": 0.04, "bollinger_squeeze": 0.02, "trend_strength": 0.05,
                    "estimate_momentum": 0.14, "rating_momentum": 0.12,
                    "valuation_gap": 0.08, "options_sentiment": 0.02, "smart_money": 0.04,
                    "pead_drift": 0.03, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.18, "income_factor": 0.01, "residual_momentum_12_1": 0.02,
                },
            },
            "movers_swing_1to5d": {
                "description": "Short-horizon movers follow-through (1-5 trading days)",
                "operator_visible": False,
                "weights": {
                    "volume_surge": 0.0893, "rsi_oversold": 0.04, "rsi_overbought": 0.02, "ma_crossover": 0.1093,
                    "price_vs_target": 0.04, "earnings_proximity": 0.08, "insider_buying": 0.03,
                    "relative_strength": 0.1176, "bollinger_squeeze": 0.0638, "trend_strength": 0.07,
                    "estimate_momentum": 0.06, "rating_momentum": 0.05,
                    "valuation_gap": 0.02, "options_sentiment": 0.03, "smart_money": 0.04,
                    "pead_drift": 0.07, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.00, "income_factor": 0.00, "residual_momentum_12_1": 0.03,
                },
            },
            "movers_swing_1to4w": {
                "description": "Medium-horizon movers continuation/reversion (1-4 weeks)",
                "operator_visible": False,
                "weights": {
                    "volume_surge": 0.0423, "rsi_oversold": 0.05, "rsi_overbought": 0.02, "ma_crossover": 0.0762,
                    "price_vs_target": 0.12, "earnings_proximity": 0.05, "insider_buying": 0.07,
                    "relative_strength": 0.0761, "bollinger_squeeze": 0.0254, "trend_strength": 0.10,
                    "estimate_momentum": 0.08, "rating_momentum": 0.07,
                    "valuation_gap": 0.07, "options_sentiment": 0.02, "smart_money": 0.05,
                    "pead_drift": 0.02, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.00, "income_factor": 0.00, "residual_momentum_12_1": 0.02,
                },
            },
            "long_horizon_6to12m": {
                "description": "Intermediate hold: 12-1 + weekly + revisions; quality still required",
                "operator_visible": False,
                "weights": {
                    "volume_surge": 0.01, "rsi_oversold": 0.02, "rsi_overbought": 0.01, "ma_crossover": 0.01,
                    "price_vs_target": 0.10, "earnings_proximity": 0.01, "insider_buying": 0.04,
                    "relative_strength": 0.03, "bollinger_squeeze": 0.01, "trend_strength": 0.06,
                    "estimate_momentum": 0.13, "rating_momentum": 0.11,
                    "valuation_gap": 0.08, "options_sentiment": 0.01, "smart_money": 0.03,
                    "pead_drift": 0.02, "weekly_trend_alignment": 0.08,
                    "quality_factor": 0.14, "income_factor": 0.02, "residual_momentum_12_1": 0.08,
                },
            },
            "long_horizon_12to36m": {
                "description": "Long-duration compounding: quality + 12-1 + revisions; value secondary",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.01, "rsi_oversold": 0.02, "rsi_overbought": 0.01, "ma_crossover": 0.01,
                    "price_vs_target": 0.10, "earnings_proximity": 0.01, "insider_buying": 0.04,
                    "relative_strength": 0.03, "bollinger_squeeze": 0.01, "trend_strength": 0.06,
                    "estimate_momentum": 0.13, "rating_momentum": 0.11,
                    "valuation_gap": 0.10, "options_sentiment": 0.01, "smart_money": 0.03,
                    "pead_drift": 0.02, "weekly_trend_alignment": 0.06,
                    "quality_factor": 0.16, "income_factor": 0.02, "residual_momentum_12_1": 0.06,
                },
            },
            "etf_technical": {
                "description": "ETFs / mutual funds / CEFs — momentum, trend, and relative strength only",
                "operator_visible": True,
                "weights": {
                    "volume_surge": 0.14, "rsi_oversold": 0.06, "rsi_overbought": 0.03, "ma_crossover": 0.14,
                    "price_vs_target": 0.00, "earnings_proximity": 0.00, "insider_buying": 0.00,
                    "relative_strength": 0.20, "bollinger_squeeze": 0.07, "trend_strength": 0.18,
                    "estimate_momentum": 0.00, "rating_momentum": 0.00,
                    "valuation_gap": 0.00, "options_sentiment": 0.05, "smart_money": 0.04,
                    "pead_drift": 0.00, "weekly_trend_alignment": 0.04,
                    "quality_factor": 0.00, "income_factor": 0.00, "residual_momentum_12_1": 0.05,
                },
            },
        },
        "movers": {
            "enabled": True,
            "manual_only": True,
            "include_losers": True,
            "post_close_guard_enabled": True,
            "market_timezone": "America/New_York",
            "min_market_cap": 500000000,
            "min_avg_volume": 500000,
            "min_price": 2.0,
            "us_listings_only": True,
            "max_universe_size": 120,
            "watchlist_name_prefix": "Movers",
            "watchlist_retention_days": 14,
            "snapshot_retention_days": 90,
            "auto_scan_postclose_enabled": False,  # Ignored when manual_only=true
            "idempotency_window_seconds": 600,
            "default_top_n": 25,
            "presets": {
                "short": "movers_swing_1to5d",
                "medium": "movers_swing_1to4w",
            },
        },
        "long_horizon": {
            "enabled": True,
            "manual_only": True,
            "idempotency_window_seconds": 900,
            "default_top_n": 30,
            "horizons": {
                "long_6to12m": {
                    "preset": "long_horizon_6to12m",
                    "eval_months": [6, 12],
                    "min_matured_samples": 40,
                },
                "long_12to36m": {
                    "preset": "long_horizon_12to36m",
                    "eval_months": [12, 24, 36],
                    "min_matured_samples": 30,
                },
            },
            # Universe policy (deterministic + auditable)
            "universe_policy": {
                "min_price": 5.0,
                "min_market_cap": 2000000000,
                "min_avg_volume": 750000,
                "allow_adr": False,
                "exclude_otc": True,
                "exclude_etf": True,
                "exclude_leveraged": True,
                "max_universe_size": 800,
                "metadata_workers": 12,
                "metadata_max_age_days": 7,
                "info_workers": 12,
            },
            # Expanded universe builder (personal research breadth mode)
            "expanded_universe": {
                "enabled": True,
                "include_sources": ["built-in", "user", "auto"],
                "exclude_watchlist_prefixes": ["Movers:"],
                "max_tickers": 1200,
            },
            # Execution profile (expanded universe speed controls)
            "execution_profile": {
                "expanded_fast_path_enabled": True,
                "two_pass_min_universe_size": 180,
                "fast_pass_top_k": 220,
                "fast_pass_funnel_cutoff_pct": 0.40,
                "fast_pass_enable_enhanced": False,
                "fast_pass_chunk_sleep_seconds": 0.0,
                "deep_pass_funnel_cutoff_pct": 0.55,
                "deep_pass_enable_enhanced": True,
                "deep_pass_enhanced_top_pct": 0.15,
                "deep_pass_enhanced_min_count": 8,
                "deep_pass_chunk_sleep_seconds": 0.25,
            },
            # Risk overlay + allocation policy
            "allocation": {
                "max_single_name_weight": 0.08,
                "max_sector_weight": 0.25,
                "beta_min": 0.70,
                "beta_max": 1.30,
                "beta_hard_max": 2.00,
                "beta_out_of_band_penalty_factor": 0.75,
                "market_cap_band_min": {
                    "large_or_above": 0.40,
                },
                "market_cap_band_max": {
                    "small_or_below": 0.20,
                },
                "allow_relaxations": True,
                "relaxation_order": ["beta_min", "sector_soft_cap"],
                "hard_constraints": ["max_single_name_weight", "liquidity_floor"],
            },
            # Underwriting controls (LLM second-stage only)
            "underwriting": {
                "enabled": True,
                "max_underwrite_jobs_per_run": 12,
                "max_underwrite_concurrency": 3,
                "underwrite_timeout_seconds": 120,
                "underwrite_retry_budget": 1,
                "underwrite_token_budget_per_run": 18000,
                "llm_augment_top_k": 4,
                "llm_call_timeout_seconds": 45,
                "degraded_mode_on_budget_exceed": True,
                "model_version": "research_v1",
                "template_version": "long_horizon_underwriting_v1",
            },
            # API/ops guardrails
            "api": {
                "response_cache_ttl_seconds": 900,
            },
        },
        # Regime-adjusted weight modifiers (Feature 15)
        # When market regime is detected as risk-off (bear), defensive signals get boosted
        # and momentum signals get dampened by the configured strength. Vice versa for risk-on.
        "regime_adjustments": {
            "strength": 0.15,
            "strength_when_macro_present": 0.08,
            "macro_fit_deviation_threshold": 5,
        },
        "liquidity_policy": {
            "scan_all_min_dollar_adv_usd": 2000000,
            "long_horizon_min_avg_volume_shares": 750000,
            "movers_min_avg_volume_shares": 500000,
            "note": "Scan All uses dollar ADV; LH/movers use share volume—do not compare numerically",
        },
        "liquidity_gate_mode": "penalize",
        "event_blackout": {
            "enabled": True,
            "days": 5,
            "opportunity_penalty": 0.12,
            "exempt_presets": ["earnings_play"],
        },
        "adaptive_blend": {
            "enabled": False,
            "ratio": 0.25,
            "min_samples": 30,
        },
        "coverage_penalty": {
            "mode": "warn",
            "threshold": 60,
            "multiplier": 0.95,
        },
        "risk_penalty": {
            "enabled": True,
            "threshold": 70,
            "multiplier": 0.9,
        },
        # Reversal-buildup single-watchlist mode (OHLCV-only). Tune here
        # without code changes. Score weights must continue to sum to 1.0.
        # Early Momentum lens (Cross-Watchlist third tab). Two-pass score +
        # bounded enrich; isolated from Opportunity/Reversal rank.
        "early_momentum": {
            "min_price": 5.0,
            "min_market_cap_usd": 300_000_000,
            "min_share_adv_50d": 500_000,
            "min_dollar_adv_50d_usd": 2_000_000,
            "float_warn_max": 50_000_000,
            "allowed_exchanges": ["NMS", "NGM", "NYQ", "NCM", "BTS", "PCX", "ASE"],
            "enrich_top_n": 80,
            "enrich_top_n_cap": 150,
            "hc_min_c": 6,
            "space_basket": ["ASTS", "LUNR", "PL", "GSAT", "FLY", "RKLB", "RDW", "BKSY"],
            "peer_basket_version": "v1",
            "iwm_ticker": "IWM",
            "ufo_ticker": "UFO",
            "late_chase_ret_20d": 0.40,
            "late_chase_penalty_min": 3,
            "late_chase_penalty_max": 8,
            "binary_event_days": 5,
            "runway_months_veto": 12,
            "going_concern_cap": 49,
            "domain_shrink_factor": 0.85,
            "bucket_watch_min": 50,
            "bucket_confirmed_min": 60,
            "bucket_hc_min": 80,
            "domain_caps": {"A": 25, "B": 20, "C": 20, "D": 15, "E": 10, "F": 20},
            "enrich": {
                "pplx_budget_per_job": 100,
                "cache_ttl_hours": 24,
            },
        },
        # Pre-break consolidation watchlist. Isolated from Opportunity and Mom rank.
        "base_coil": {
            "range_lookback": 20,
            "min_box_sessions": 8,
            "box_width_max_atr": 1.5,
            "box_lookback_cap": 40,
            "sma_window": 50,
            "sma_rise_lookback": 10,
            "excess_lookback": 20,
            "vol_surge_exclude": 1.5,
            "atr_fast": 5,
            "atr_slow": 20,
        },
        "reversal_buildup": {
            "rsi_period": 14,
            "rsi_lookback": 60,
            "rsi_oversold": 35.0,
            "rsi_overbought": 70.0,
            "rsi_late_long": 55.0,
            "rsi_late_short": 45.0,
            "range_lookback": 60,
            "range_long_max": 0.25,
            "range_short_min": 0.75,
            "range_mid": 0.50,
            "confirmed_pos60_long_max": 0.35,
            "confirmed_pos60_short_min": 0.65,
            "late_pos60_long_max": 0.40,
            "late_pos60_short_min": 0.60,
            "rsi_min_travel_pts": 5.0,
            "weekly_short_demote_slope": 0.0,
            "weekly_long_demote_slope": -5.0,
            "recency_sessions": 15,
            "min_bars": 60,
            "persist_sessions": 3,
            "volume_spike_mult": 5.0,
            "volume_share_window": 10,
            "volume_confirmed": 0.55,
            "sma_period": 20,
            "breakout_lookback": 10,
            "late_ret_atr": 1.0,
            "atr_period": 14,
            "rsi_recover_cap_pts": 15.0,
            "rsi_recover_peak_pts": 10.0,
            "rsi_recover_decay_pts": 12.0,
            "volume_bias_denom": 0.55,
            "penalty_late": 0.70,
            "penalty_failed_turn": 0.75,
            "penalty_weekly_against": 0.85,
            "penalty_watching": 0.80,
            "penalty_stale": 0.90,
            "penalty_market_dump_short": 0.85,
            "penalty_short_pressure": 0.90,
            "short_pressure_threshold": 0.6,
            "penalty_high_risk": 0.70,
            "high_risk_threshold": 70.0,
            "penalty_weak_composite": 0.75,
            "weak_composite_long": 20.0,
            "lens_conflict_macro_min": 65.0,
            "weights": {
                "dislocation_proximity": 0.25,
                "rsi_recovery": 0.25,
                "macd_hist_turn": 0.25,
                "volume_bias": 0.25,
            },
            "session_timezone": "America/New_York",
            "session_close_hour": 16,
            "session_close_minute": 0,
        },
        "valuation_gap_metrics": {
            "forward_pe": 0.50,
            "price_to_sales": 0.25,
            "ev_to_ebitda": 0.25,
            "live_median_min_peers": 8,
            "live_median_blend": 0.40,
        },
        "ohlcv_lookback_days": 420,

        # Scheduler settings (ScreeningScheduler)
        "scheduler": {
            "enabled": True,
            "poll_interval": 300,  # Check schedule every 5 minutes

            # Post-close / pre-market watchlist policy (no ticker-level dedup across lists).
            # Each eligible list still gets its own engine.scan(); this trims redundant
            # universes and runs heavy indices on a weekly cadence. Scan All union remains
            # the path for cross-list ticker dedup + cross-watchlist board.
            "postclose": {
                "exclude_watchlist_names": [
                    # Focus desk only. Run Scan on the list; do not add it to the daily rotation.
                    "AI & AI Infrastructure",
                ],
                # User-owned lists (source=user) are not scanned by default; name allowlist only.
                "include_user_watchlist_names": ["My Watchlist"],
                "weekly_watchlist_names": [
                    "S&P 500",
                    "S&P 400",
                    "S&P 600",
                    "Russell 2000",
                    "S&P 500 Top 100",
                    "Russell 2000 Top 100",
                    "ETFs - Factor & Style",
                    "ETFs - Thematic & Industry",
                    "ETFs - Bonds & Rates",
                    "ETFs - Countries & Regions",
                ],
                "weekly_run_day": "friday",
                "priority_watchlist_names": [
                    "My Watchlist",
                    "Healthcare & Life Sciences",
                    "Energy & Commodities",
                    "AI Infrastructure Metals",
                    "Sector ETFs",
                    "NASDAQ 100",
                    "Dividend Aristocrats Top 50",
                    "ADRs - Top",
                ],
            },

            # Post-close automation: what happens automatically after scheduled scans
            "auto_alerts": {
                "enabled": True,       # Create alerts for top N tickers after each post-close scan
                "top_n": 5,            # Number of top tickers to create alerts for
            },
            "auto_analyze": {
                "enabled": False,      # Run full analysis on top N tickers after post-close scan
                "top_n": 3,            # Number of top tickers to auto-analyze (keep low — each takes ~5min)
                "mode": "quick",       # Analysis mode: quick|standard|deep
                "risk_profile": "growth",
                "delay_seconds": 90,   # Delay between analyses to avoid rate limits
            },

            # Backtest automation: compute forward returns for past analyses
            "auto_backtest": {
                "enabled": True,       # Auto-compute forward returns for mature analyses
                "min_age_days": 7,     # Only backtest analyses at least this old
                "max_age_days": 90,    # Don't backtest analyses older than this
                "batch_size": 20,      # Max analyses to backtest per run
                "run_day": "sunday",   # Day of week to run (or "daily" for every day)
            },

            # Adaptive weight recomputation
            "auto_adaptive_weights": {
                "enabled": True,       # Periodically recompute adaptive screening weights
                "min_samples": 30,     # Minimum backtested screening results required
                "run_day": "monday",   # Day of week to run
            },

            # Auto-discovery: generate thematic watchlists from trending sectors
            "auto_discovery": {
                "enabled": False,
                "max_themes": 5,              # Max themes to discover per run
                "expiry_days": 21,            # Initial TTL for new auto-watchlists
                "renewal_extension_days": 7,  # Days added on renewal
                "full_refresh_age_days": 28,  # Re-query Perplexity if watchlist older than this
                "monthly_budget": 20,         # Max Perplexity calls/month for auto-discovery
                "model": "sonar",             # Cheaper model for auto-discovery
                "drift_threshold": 0.4,       # Sector rank drift above this = stale (0-1)
                "hard_timeout_seconds": 600,  # Abort run after this many seconds
            },

            # Exchange-resolution hydration: prewarm metadata for reliable TradingView exports.
            "exchange_resolution_hydration": {
                "enabled": True,
                "run_hour": 7,  # ET
                "minute_window": 10,
                "include_sources": ["built-in", "user", "auto"],
                "max_tickers": 2000,
                "max_workers": 12,
                "max_age_days": 7,
            },

            # Built-in refresh: generate proposal only (manual apply required)
            "builtin_refresh": {
                "enabled": True,
                "run_day": "sunday",
                "run_hour": 8,                # ET hour
                "max_churn_pct": 0.30,        # Global default churn threshold
                "max_churn_pct_by_list": {
                    "S&P 500 Top 100": 0.30,
                    "NASDAQ 100": 0.30,
                    "Russell 2000 Top 100": 0.35,
                    "Dividend Aristocrats Top 50": 0.35,
                    "Energy & Commodities": 0.40,
                    "AI Infrastructure Metals": 0.40,
                    "Healthcare & Life Sciences": 0.40,
                    "AI & AI Infrastructure": 0.40,
                },
                "max_replacements_by_list": {
                    "S&P 500 Top 100": 12,
                    "NASDAQ 100": 10,
                    "Russell 2000 Top 100": 12,
                    "Dividend Aristocrats Top 50": 8,
                    "Energy & Commodities": 6,
                    "AI Infrastructure Metals": 8,
                    "Healthcare & Life Sciences": 10,
                    "AI & AI Infrastructure": 12,
                },
                "validation_workers": 8,
                "enforce_churn_at_proposal": False,  # Keep proposal generation non-blocking
                "enforce_high_risk_apply": True,     # Block apply unless force_apply for high-risk proposals
                "force_apply_churn_pct": 0.40,       # High-risk threshold requiring force apply
                "require_force_reason": True,
                "allow_sparse_baseline_fallback": True,
                "sparse_validation_min_ratio": 0.90,
                # Per-list overrides for sparse_validation_min_ratio.
                # Small-cap and thematic lists have intrinsically lower
                # validation rates (more delisted, obscure, or thinly-traded
                # names) so we accept a lower pass rate rather than freezing
                # the list on every API pressure event.
                "sparse_validation_min_ratio_by_list": {
                    "Russell 2000 Top 100": 0.80,
                    "Energy & Commodities": 0.80,
                    "AI Infrastructure Metals": 0.75,
                    "Healthcare & Life Sciences": 0.80,
                    "AI & AI Infrastructure": 0.75,
                },
                "min_size_by_list": {
                    "S&P 500 Top 100": 80,
                    "NASDAQ 100": 80,
                    "Russell 2000 Top 100": 70,
                    "Dividend Aristocrats Top 50": 35,
                    "Energy & Commodities": 10,
                    "AI Infrastructure Metals": 20,
                    "Healthcare & Life Sciences": 70,
                    "AI & AI Infrastructure": 70,
                },
            },
        },
    },
    
    # Confidence calibration (optional)
    "confidence_calibration": {
        "enabled": True,
        "bins": 5,
        "min_samples": 10,
        "sample_limit": 2000,
        "quality_weight": 0.3,
    },

    # Risk scorecard: allow a small slack before a hard gate fail (e.g. VaR 3.04 vs 3.0).
    "risk_gate_tolerance_pct": 0.05,

    # Decision guardrails — programmatic threshold enforcement
    "decision_guardrails": {
        "enabled": True,
        "mode": "warn",  # "warn" = log only, "enforce" = override decision to HOLD
        "sell": {
            "enabled": True,
            "conflicted_min_composite": {
                "growth": -0.15,
                "conservative": -0.20,
                "aggressive": None,
            },
            "confidence_haircut": 8,
        },
        "book_action": {
            "enabled": True,
        },
    },

    # Profile resolution is cache-first and never blocks an analysis when the
    # metadata provider is unavailable.
    "investment_profile_resolution": {
        "metadata_max_age_days": 7,
    },

    # Security classifications may change the analytical lens, but automatic
    # metadata classification must not silently loosen the selected risk budget.
    "profile_risk_limit_overrides": {
        "allow_metadata_resolved_risk_relaxation": False,
        "growth": {
            "high_growth": {"beta": 2.2, "max_drawdown_pct": 35.0, "var_95_pct": 3.5},
            "momentum_speculative": {"beta": 2.5, "max_drawdown_pct": 40.0, "var_95_pct": 4.0},
            "commodity_cyclical": {"beta": 2.0, "max_drawdown_pct": 30.0, "var_95_pct": 3.5},
        },
    },

    # Overlay weights are active only for resolved investment profiles. Each
    # group sums to 0.60 / 0.40; absent transcript weight is redistributed.
    "signal_weight_overlays": {
        "high_growth": {
            "llm": {
                "Market": 0.08, "Fundamentals": 0.18, "News": 0.09,
                "Sentiment": 0.07, "Research": 0.09, "Trading Plan": 0.09,
            },
            "data": {
                "estimate_revisions": 0.16, "rating_changes": 0.11,
                "weekly_trend": 0.07, "transcript_kpi": 0.06,
            },
        },
        "momentum_speculative": {
            "llm": {
                "Market": 0.20, "Fundamentals": 0.04, "News": 0.08,
                "Sentiment": 0.10, "Research": 0.08, "Trading Plan": 0.10,
            },
            "data": {
                "estimate_revisions": 0.09, "rating_changes": 0.06,
                "weekly_trend": 0.18, "transcript_kpi": 0.07,
            },
        },
        "commodity_cyclical": {
            "llm": {
                "Market": 0.10, "Fundamentals": 0.18, "News": 0.12,
                "Sentiment": 0.04, "Research": 0.09, "Trading Plan": 0.07,
            },
            "data": {
                "estimate_revisions": 0.15, "rating_changes": 0.08,
                "weekly_trend": 0.11, "transcript_kpi": 0.06,
            },
        },
    },

    # =========================================================================
    # INVESTMENT PROFILES
    # =========================================================================
    # Per-agent directives that tell analysts HOW to think about a ticker based
    # on its investment type. Data fetching is unchanged — profiles only shape
    # the analytical lens. If a profile field is missing or empty, the agent
    # falls back to its generic prompt (non-breaking).
    # =========================================================================
    "investment_profiles": {

        "large_cap_core": {
            "display_name": "Large Cap Core",
            "company_context": "Diversified large-cap stock with established market position",
            "fundamentals_focus": (
                "Full balanced analysis: P/E, P/FCF, EV/EBITDA vs 5-year average and sector median. "
                "Revenue growth, margin trajectory, capital allocation (buybacks, M&A, R&D). "
                "Competitive position and moat durability."
            ),
            "technical_focus": (
                "Standard technical analysis: trend direction, support/resistance, momentum indicators, "
                "volume confirmation. Both trend-following and mean-reversion signals are relevant."
            ),
            "news_focus": (
                "Broad coverage: earnings results, management commentary, competitive developments, "
                "regulatory environment, and macro sensitivity."
            ),
            "sentiment_focus": (
                "Analyst consensus, institutional ownership changes, insider transactions. "
                "Retail sentiment is noise for mega-caps — focus on institutional flow."
            ),
            "bull_thesis_frame": (
                "Standard investment thesis: competitive moat, earnings growth, capital return, "
                "reasonable valuation relative to growth. Identify catalysts for re-rating."
            ),
            "bear_thesis_frame": (
                "Standard bear case: overvaluation vs history, competitive threats, margin pressure, "
                "regulatory headwinds, or cyclical peak earnings."
            ),
            "research_manager_focus": (
                "Apply standard investment framework. Weigh all analyst perspectives equally. "
                "BUY requires positive risk-reward with identifiable catalysts. SELL requires "
                "deteriorating fundamentals or extreme overvaluation. HOLD when the thesis is "
                "intact but the entry point is not compelling."
            ),
            "trader_focus": (
                "Standard position sizing based on conviction and portfolio risk. Consider "
                "technical entry points for timing. Large caps are more forgiving on timing — "
                "can scale in over days."
            ),
            "key_risks": (
                "Valuation risk (multiple compression), competitive disruption, regulatory change, "
                "macro/cyclical sensitivity, execution risk on capital allocation."
            ),
            "risk_benchmark": (
                "S&P 500 performance; sector ETF; company's own 5-year valuation range."
            ),
        },

        "dividend_income": {
            "display_name": "Dividend Income",
            "company_context": "Stable, income-producing blue-chip with long dividend history",
            "fundamentals_focus": (
                "Emphasize payout ratio sustainability, free cash flow coverage of dividends, "
                "dividend growth rate (CAGR over 5/10/20 years), debt-to-equity and interest "
                "coverage ratios. Flag any dividend cuts, freezes, or payout ratio above 80%. "
                "Compare current yield vs the 10-year Treasury yield."
            ),
            "technical_focus": (
                "Focus on trend stability and mean-reversion, not breakouts. Long-term moving "
                "averages (200 SMA) and low-volatility range trading matter most. A sharp price "
                "drop for a dividend stock is more likely a risk signal than a buying opportunity."
            ),
            "news_focus": (
                "Look for dividend announcements, payout changes, credit rating changes, and "
                "regulatory risks to the business model. Macro interest rate policy is critical — "
                "rising rates compress dividend stock valuations."
            ),
            "sentiment_focus": (
                "Retail sentiment is less relevant for these names. Focus on institutional "
                "ownership changes and activist investor involvement."
            ),
            "bull_thesis_frame": (
                "Sustainable and growing dividend with strong balance sheet; undervalued yield "
                "spread vs bonds; dividend growth rate exceeding inflation."
            ),
            "bear_thesis_frame": (
                "Payout ratio stress, earnings decline threatening the dividend, rising rate "
                "environment compressing valuations, or secular business model decline."
            ),
            "research_manager_focus": (
                "For dividend/income stocks, BUY requires: (a) dividend is safe — payout ratio "
                "below 75% and FCF covers the dividend by 1.5x or more, AND (b) yield is "
                "attractive vs bonds. HOLD is appropriate if the dividend is secure but not "
                "undervalued. Default to HOLD over BUY — capital preservation matters more "
                "than upside for income investors."
            ),
            "trader_focus": (
                "Frame position sizing around income generation, not capital appreciation. "
                "Consider ex-dividend timing. Yield-on-cost matters — a lower entry price "
                "improves the income stream. Sell only if the dividend is at risk of being cut."
            ),
            "key_risks": (
                "Dividend cut risk (the primary concern), interest rate sensitivity, business "
                "model disruption, secular revenue decline, regulatory changes affecting "
                "cash distribution policy."
            ),
            "risk_benchmark": (
                "10-year Treasury yield; sector dividend yield median; company's own 5-year "
                "average dividend yield."
            ),
        },

        "high_growth": {
            "display_name": "High Growth",
            "company_context": "High-volatility growth stock — may be pre-profit or early-stage",
            "fundamentals_focus": (
                "Revenue growth rate is THE primary metric. Evaluate total addressable market "
                "(TAM), gross margin expansion trajectory, and path to profitability. P/E is "
                "often irrelevant for pre-profit companies — use EV/Revenue, Rule of 40 "
                "(revenue growth % + profit margin %), and unit economics instead. Cash burn "
                "rate and runway (months of cash remaining) are critical survival metrics."
            ),
            "technical_focus": (
                "Momentum is everything for these names. Focus on relative strength vs sector, "
                "volume patterns on breakouts, and support/resistance at prior highs. "
                "Trend-following signals are more reliable than mean-reversion here."
            ),
            "news_focus": (
                "Product launches, partnership announcements, TAM expansion, competitive moat "
                "developments, regulatory approvals/risks. Executive departures or strategy "
                "pivots are high-impact events. Watch for secondary offering announcements."
            ),
            "sentiment_focus": (
                "Retail sentiment is a meaningful signal for high-growth names. Social media "
                "buzz, Reddit/X trends, and retail brokerage flow data matter. Watch for "
                "crowded trade risk — extreme bullish sentiment can precede sharp reversals."
            ),
            "bull_thesis_frame": (
                "Massive TAM with identifiable competitive moat, accelerating revenue growth, "
                "improving unit economics, and a clear path to profitability or cash flow "
                "break-even."
            ),
            "bear_thesis_frame": (
                "Cash burn is unsustainable, competition intensifying, multiple compression "
                "as interest rates rise, unproven business model, customer concentration, "
                "or insiders selling aggressively."
            ),
            "research_manager_focus": (
                "For high-growth stocks, BUY if: (a) revenue is accelerating, AND (b) "
                "competitive moat is identifiable, AND (c) cash runway exceeds 18 months. "
                "Growth can justify a premium valuation — don't penalize a high P/E if "
                "the growth rate warrants it. SELL if growth decelerates materially or cash "
                "runway drops below 12 months. Require clear catalysts for high-conviction calls."
            ),
            "trader_focus": (
                "Position sizing must account for higher volatility — these names can move "
                "5-10% on a single catalyst. Consider scaling in rather than a full position "
                "at once. Evaluate whether momentum is with you; do not try to catch falling "
                "knives in growth stocks."
            ),
            "key_risks": (
                "Cash runway exhaustion, dilution from secondary offerings, customer "
                "concentration, revenue multiple compression, execution risk, and competitive "
                "disruption from larger incumbents."
            ),
            "risk_benchmark": (
                "Sector revenue growth median; Rule of 40 threshold; comparable IPO cohort "
                "performance."
            ),
        },

        "commodity_cyclical": {
            "display_name": "Commodity / Cyclical",
            "company_context": "Commodity producer or cyclical company tied to macro supercycles",
            "fundamentals_focus": (
                "Focus on production costs vs commodity price (margin of safety), reserve life, "
                "capex cycles, and NAV-based valuation. Commodity companies have lumpy, cyclical "
                "earnings — normalize over the full cycle, not a single quarter. Debt levels "
                "are critical because commodity downturns can be existential for leveraged "
                "producers."
            ),
            "technical_focus": (
                "Supercycle positioning is key. Look at long-term trend (200 SMA over years, "
                "not weeks), correlation with the underlying commodity price, and relative "
                "strength vs commodity peers. Volume confirms institutional accumulation "
                "during upcycles."
            ),
            "news_focus": (
                "Commodity supply/demand dynamics, geopolitical risks (OPEC, mining regulations, "
                "trade policy), infrastructure buildout announcements, ESG/environmental "
                "regulatory changes. Macro data such as China PMI and US industrial production "
                "drive these names."
            ),
            "sentiment_focus": (
                "Institutional positioning matters more than retail sentiment. Watch for "
                "commodity ETF flows and sovereign wealth fund activity. COT (Commitment of "
                "Traders) data signals institutional conviction."
            ),
            "bull_thesis_frame": (
                "Structural supply deficit, rising commodity prices, low-cost producer with "
                "long reserve life, and disciplined capital allocation."
            ),
            "bear_thesis_frame": (
                "Commodity price at cyclical peak, rising capex destroying shareholder returns, "
                "geopolitical or regulatory risk, demand destruction from substitution or "
                "economic slowdown."
            ),
            "research_manager_focus": (
                "For commodity/cyclical stocks, assess where we are in the cycle. BUY at "
                "cycle trough when production costs are below the commodity price and the "
                "balance sheet can survive 2+ years of downturn. SELL when margins are at "
                "cycle peak and capex is expanding aggressively. HOLD during mid-cycle if "
                "the macro thesis is intact. Normalize earnings over 5 years — never judge "
                "a miner on a single quarter."
            ),
            "trader_focus": (
                "Position timing is critical — commodity stocks are not buy-and-hold. Track "
                "the underlying commodity price as the primary leading signal; the equity is "
                "a levered play on the commodity. Consider pair trades for risk management."
            ),
            "key_risks": (
                "Commodity price collapse, geopolitical and nationalization risk, environmental "
                "liability, capex overruns, cyclical margin compression, and currency risk "
                "for non-USD producers."
            ),
            "risk_benchmark": (
                "Underlying commodity spot price; peer NAV discount/premium; all-in sustaining "
                "cost (AISC) vs spot price."
            ),
        },

        "momentum_speculative": {
            "display_name": "Momentum / Speculative",
            "company_context": "High short interest or speculative trade — this is a TRADE, not an investment",
            "fundamentals_focus": (
                "Fundamentals are secondary to the trade thesis. Note short interest as % of "
                "float, days to cover, cost to borrow, and any catalysts that could trigger "
                "a squeeze. Check for potential dilution (shelf offerings, warrants, convertible "
                "notes). Perform a basic viability assessment: is this company a going concern "
                "or heading toward bankruptcy?"
            ),
            "technical_focus": (
                "This is a technicals-first analysis. Short squeeze mechanics: volume spikes, "
                "gamma exposure, options open interest at key strikes, and failed breakdowns. "
                "Identify key price levels where forced covering would trigger cascading buys."
            ),
            "news_focus": (
                "Catalyst identification is paramount: earnings surprises, insider buying, "
                "regulatory approvals, partnership announcements, or any positive fundamental "
                "development that could pressure short sellers. Also watch for dilution "
                "announcements — shelf registrations and ATM offerings kill squeeze setups."
            ),
            "sentiment_focus": (
                "CRITICAL for this profile type. Reddit, X, and retail brokerage data show "
                "positioning and momentum. Watch for coordinated retail buying, options flow "
                "(call sweeps), and short interest changes. Dark pool activity may signal "
                "institutional covering."
            ),
            "bull_thesis_frame": (
                "High short interest with identifiable near-term catalyst, gamma squeeze "
                "potential, shorts trapped at a poor average entry price."
            ),
            "bear_thesis_frame": (
                "Fundamentally impaired company where the shorts are correct, dilution is "
                "imminent, no catalyst in sight, or retail interest is fading."
            ),
            "research_manager_focus": (
                "For speculative/squeeze plays, this is a TRADE, not an INVESTMENT. BUY only "
                "if: (a) an identifiable catalyst exists within 30 days, AND (b) short interest "
                "exceeds 20% of float, AND (c) no imminent dilution risk. SELL immediately "
                "if the catalyst fails or dilution is announced. HOLD is rarely appropriate "
                "for these names — they are binary setups. Time horizon is days to weeks."
            ),
            "trader_focus": (
                "This is a tactical position, NOT a core holding. Strict stop-loss is mandatory "
                "— define maximum acceptable loss before entry. Size small relative to portfolio "
                "(2-5% max). Take profits aggressively on spikes. Consider options for defined-risk "
                "exposure. Never average down on a broken squeeze thesis."
            ),
            "key_risks": (
                "Dilution risk (the #1 killer of squeeze plays), fundamental insolvency, "
                "shorts may be patient with low borrow cost, retail exhaustion, and "
                "regulatory intervention (trading halts)."
            ),
            "risk_benchmark": (
                "Cost to borrow; days to cover; max pain on options chain; SEC EDGAR watch "
                "for shelf registration filings."
            ),
        },
    },

    # ==========================================================================
    # VALUATION CONFIGURATION
    # ==========================================================================
    "valuation": {
        # Probability weights for blending bull/base/bear scenario fair values.
        # Must sum to 1.0. Adjust per regime or investor profile.
        "scenario_weights": {
            "bull": 0.25,
            "base": 0.50,
            "bear": 0.25,
        },
        # Pre-profit P/S scenarios can produce meaningless upside; quarantine above this.
        "max_blended_upside_pct": 200.0,
        # DCF sensitivity grid: offsets applied to the ticker's computed WACC and
        # terminal_growth to produce a 3x3 fair-value grid around the base case.
        "dcf_sensitivity": {
            "wacc_offsets_pp": [-1.0, 0.0, 1.0],         # percentage points
            "terminal_growth_offsets_pp": [-0.5, 0.0, 0.5],
        },
        # Peer comp: number of sector peers to include in the comparison.
        "peer_comps": {
            "min_peers": 3,
            "max_peers": 10,
        },
    },
}


# ==========================================================================
# ANALYSIS MODE PRESETS
# ==========================================================================
ANALYSIS_MODES = {
    "quick": {
        "description": "Fast scan: Basic technicals + headlines with debate",
        "estimated_time": "2-3 minutes",
        "estimated_cost": "~$0.01",
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "data_completeness": 0.75,
        "news_limit": 5,
        "use_perplexity": False,
        "data_vendors": {
            "core_stock_apis": "yfinance",
            "technical_indicators": "yfinance",
            "fundamental_data": "yfinance",  # Use yfinance for speed
            "news_data": "finnhub",
        },
        "token_budget_overrides": {
            "analyst": {
                "max_context_tokens": 12000,
                "max_messages": 14,
                "market_max_tool_messages": 4,
                "social_max_tool_messages": 3,
                "news_max_tool_messages": 4,
                "fundamentals_max_tool_messages": 4,
                "market_max_indicators": 3,
                "fundamentals_enriched_context_max_tokens": 4500,
            },
            "tools": {
                "get_stock_data": 4500,
                "get_indicators": 2200,
                "get_fundamentals": 4500,
                "get_balance_sheet": 3200,
                "get_cashflow": 3200,
                "get_income_statement": 3200,
                "get_news": 2200,
                "get_global_news": 1500,
                "get_insider_sentiment": 900,
                "get_insider_transactions": 1200,
            },
            "llm": {
                "quick_max_output_tokens": 1200,
                "deep_max_output_tokens": 1800,
            },
        },
    },
    "standard": {
        "description": "Full analysis: All data sources, thorough debates",
        "estimated_time": "4-5 minutes",
        "estimated_cost": "$0",
        "max_debate_rounds": 2,
        "max_risk_discuss_rounds": 1,
        "data_completeness": 0.90,
        "news_limit": 25,  # Increased for better coverage
        "use_perplexity": False,
        "data_vendors": {
            "core_stock_apis": "yfinance",
            "technical_indicators": "yfinance",
            "fundamental_data": "yfinance",  # yfinance for batch safety (no daily limits)
            "news_data": "finnhub",
        },
        "token_budget_overrides": {
            "analyst": {
                "max_context_tokens": 15000,
                "max_messages": 16,
                "market_max_tool_messages": 5,
                "social_max_tool_messages": 4,
                "news_max_tool_messages": 5,
                "fundamentals_max_tool_messages": 5,
                "market_max_indicators": 4,
                "fundamentals_enriched_context_max_tokens": 5500,
            },
            "tools": {
                "get_stock_data": 5500,
                "get_indicators": 2800,
                "get_fundamentals": 5500,
                "get_balance_sheet": 3800,
                "get_cashflow": 3800,
                "get_income_statement": 3800,
                "get_news": 2800,
                "get_global_news": 1900,
                "get_insider_sentiment": 1100,
                "get_insider_transactions": 1400,
            },
            "llm": {
                "quick_max_output_tokens": 1600,
                "deep_max_output_tokens": 2200,
            },
        },
    },
    "deep": {
        "description": "Research grade: Standard + Perplexity deep research",
        "estimated_time": "5-6 minutes",
        "estimated_cost": "~$0.10",
        "max_debate_rounds": 2,
        "max_risk_discuss_rounds": 1,
        "data_completeness": 1.0,
        "news_limit": 30,
        "use_perplexity": True,
        "data_vendors": {
            "core_stock_apis": "yfinance",
            "technical_indicators": "yfinance",
            "fundamental_data": "yfinance",  # yfinance for batch safety (Alpha Vantage: 25 req/day limit)
            "news_data": "finnhub",  # Finnhub + Perplexity supplement
        },
        "token_budget_overrides": {
            "analyst": {
                "max_context_tokens": 18000,
                "max_messages": 20,
                "market_max_tool_messages": 6,
                "social_max_tool_messages": 4,
                "news_max_tool_messages": 7,
                "fundamentals_max_tool_messages": 5,
                "market_max_indicators": 4,
                "fundamentals_enriched_context_max_tokens": 6500,
            },
            "tools": {
                "get_stock_data": 6500,
                "get_indicators": 3200,
                "get_fundamentals": 6500,
                "get_balance_sheet": 4500,
                "get_cashflow": 4500,
                "get_income_statement": 4500,
                "get_news": 3200,
                "get_global_news": 2200,
                "get_insider_sentiment": 1300,
                "get_insider_transactions": 1700,
            },
            "llm": {
                "quick_max_output_tokens": 2000,
                "deep_max_output_tokens": 3600,
            },
        },
    },
}

# ==========================================================================
# BATCH ANALYSIS RECOMMENDATIONS
# ==========================================================================
# Rate limits to consider when running batch analyses:
#
# | API            | Limit               | Recommendation                       |
# |----------------|---------------------|--------------------------------------|
# | Finnhub        | 60 req/min          | Use 60s+ delay between analyses      |
# | Alpha Vantage  | 25 req/day (free)   | Use yfinance for fundamentals        |
# | Perplexity     | ~100 req/month      | Reserve for Deep mode, single ticker |
# | yfinance       | No hard limit       | Safe for batch operations            |
# | OpenAI         | Tier-dependent      | Usually not the bottleneck           |
#
# Recommended batch settings:
# - delay_seconds: 60 (minimum), 90 (recommended for large batches)
# - fundamental_data: "yfinance" (avoids Alpha Vantage daily limit)
# - use_perplexity: False (unless you need Deep research)


def get_config_for_mode(mode: str, base_config: dict = None) -> dict:
    """
    Get configuration merged with analysis mode settings.
    
    Args:
        mode: Analysis mode ("quick", "standard", "deep")
        base_config: Optional base configuration to merge with
        
    Returns:
        Merged configuration dictionary
    """
    if mode not in ANALYSIS_MODES:
        raise ValueError(f"Unknown analysis mode: {mode}. Choose from: {list(ANALYSIS_MODES.keys())}")
    
    config = deepcopy(base_config or DEFAULT_CONFIG)
    mode_config = ANALYSIS_MODES[mode]
    
    # Merge mode settings into config (preserves risk_profile from base config)
    config["analysis_mode"] = mode
    config["max_debate_rounds"] = mode_config["max_debate_rounds"]
    config["max_risk_discuss_rounds"] = mode_config["max_risk_discuss_rounds"]
    config["news_limit"] = mode_config["news_limit"]
    config["use_perplexity"] = mode_config["use_perplexity"]
    config["data_vendors"] = mode_config["data_vendors"].copy()
    config["data_completeness"] = mode_config.get("data_completeness", 1.0)
    if mode_config.get("token_budget_overrides"):
        config["token_budget"] = _deep_merge_dict(
            config.get("token_budget", {}),
            mode_config["token_budget_overrides"],
        )
    # risk_profile is preserved from base_config, not overridden by mode
    
    return config


def _deep_merge_dict(base: dict, override: dict) -> dict:
    """Recursively merge dict-like config structures."""
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged
