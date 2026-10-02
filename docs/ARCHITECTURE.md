# TradingAgents Architecture

High-level architecture for the local-first research system.

---

## 1. Runtime Model

- Single-user local deployment
- FastAPI backend (`webapp/app.py`)
- SQLite database (`research.db`, WAL mode)
- Browser UI served by FastAPI templates
- Background components:
  - alert monitor
  - screening scheduler
  - analysis queue worker

---

## 2. Main Modules

### Analysis pipeline

- `tradingagents/graph/*`: multi-agent orchestration and state propagation
- `tradingagents/research.py`: high-level research entrypoints
- `tradingagents/utils/investment_profile_resolution.py`: source-aware profile selection (explicit → watchlist → fresh ticker metadata → refresh) shared by analysis entry points
- `tradingagents/graph/signal_aggregator.py` and `reporting/decision_scorecard.py`: profile overlays, effective mandate limits, and decision-quality guardrails

### Screening pipeline

- `tradingagents/screening/engine.py`: core screening engine (signals + Quality/Income/12−1 sleeves)
- `tradingagents/screening/reversal_buildup.py`: OHLCV-only reversal-buildup classifier (phase + 0–100 score)
- `tradingagents/screening/movers.py`: movers ingestion + dual-horizon scoring
- `tradingagents/screening/long_horizon.py`: deterministic long-horizon workflow
- `tradingagents/screening/ticker_resolver.py`: metadata resolution

### Dataflows

- `tradingagents/dataflows/*`: market/news/fundamental providers + cache layer
- `tradingagents/dataflows/perplexity_api.py`: Perplexity Agent API (`responses.create`). Legacy Sonar model ids map to presets (`sonar` → `fast`, `sonar-pro` → `low`). SEC filing calls use explicit `web_search` domain/date filters. `PERPLEXITY_API_MODE=sonar` is a pre-sunset fallback only through 2026-09-27.
- `tradingagents/dataflows/cache.py`: disk/memory cache patterns
- `tradingagents/dataflows/yfinance_limiter.py`: process-wide yfinance circuit breaker (429/rate-limit detection, exponential backoff 5m→30m, 100ms inter-call spacing, thread-safe singleton)
- `tradingagents/dataflows/provenance.py`: dataflow provenance event stream (timezone-aware)

### Persistence and reporting

- `tradingagents/reporting/database.py`: schema + persistence API
- `tradingagents/reporting/*`: report generation and attribution helpers
- `tradingagents/backtesting/*`: return attribution, calibration, risk metrics
- `tradingagents/backtesting/metrics.py`: **analysis-decision SSOT** (signed P&L, per-horizon directional accuracy, 7d return/vol). Persisted `actual_return_*` / `alpha_*` stay unsigned tape; strategy numbers are derived on read. `backtest_runs` additive columns: `avg_signed_return_7d`, `return_vol_7d` (`avg_return` remains tape; `strategy_sharpe` / `strategy_sortino` are no longer written on new runs).
- Screening-run backtest (`backtest_screening_run`) is a separate unsigned score-vs-forward-return surface and does not use the analysis SSOT.

### Web surface

- `webapp/app.py`: API contracts + orchestration endpoints
- `webapp/templates/*`: UI pages and browser-side controls

---

## 3. Data Model (core)

Primary tables:

- `analyses`
- `watchlists` (extended with `index_key`, `registry_backed`, `defaults_version`, `source_mode_status`, `materialized_as_of`, `materialized_source`)
- `screening_runs`
- `screening_results`
- `alert_rules`
- `alert_history`
- `backtest_runs`

Strategy side tables:

- Movers: `movers_snapshots`, `movers_run_details`
- Long-horizon: `long_horizon_run_meta`, `long_horizon_run_details`, `long_horizon_underwriting`
- Registry / universe: `index_constituents` (allowlisted `index_key`, `ticker`, `as_of`, `liquidity_rank`, `avg_dollar_volume_usd`, `market_cap`)
- Ticker metadata: `ticker_metadata` (extended with `avg_dollar_volume_usd`, `liquidity_rank`, `asset_class`, `country`), `ticker_exchange_resolution`
- Built-in refresh workflow: `builtin_refresh_proposals`, `builtin_refresh_items`
- Operational telemetry: `runtime_metrics` (p95 aggregations for e.g. `scan_all_duration_seconds`)
- Ticker health ledger: `ticker_health` (per-ticker `failure_count`, `success_count`, last success/failure timestamps, `eviction_reason`) — drives stale/delisted detection and opportunistic eviction
- Event calendar: `event_calendar` — unified upcoming-event store; keyed by `(ticker, event_date, event_type)`; fed by `research.py` catalyst parser and `engine.scan()` post-scan; queried via `db.get_upcoming_events(ticker, max_days, event_types)`

`analyses` extended fields: `intrinsic_value_data TEXT`, `scenario_analysis TEXT`, `peer_comps TEXT` — JSON-serialized institutional valuation outputs persisted per analysis. The existing investment-profile JSON also records `profile_key` and `resolved_from`, so reports and API consumers can reproduce whether a lens came from an explicit selection, watchlist, cached metadata, or metadata refresh.

Design rule: additive strategy tables keyed by `run_id`/`run_id+ticker` to avoid breaking core run schemas.

Watchlist restructure model:

- `ConstituentRegistry` — authoritative source per index in `index_constituents`
- `ScanBuckets` — config-driven universe projections (`screening.scan_buckets`)
- `OperatorViews` — UI-facing watchlists materialized from the registry (`materialize_registry_watchlist`) using a deterministic 4-field sort: `liquidity_rank ASC NULLS LAST`, `avg_dollar_volume_usd DESC`, `market_cap DESC`, `ticker ASC`

---

## 4. Long-Horizon Flow

1. Universe input (`watchlist_id` or explicit tickers)
2. Universe policy pass (metadata-first filtering + parallel info fallback), including leveraged/inverse exclusion
3. Deterministic screening using horizon preset (single-pass or two-pass expanded profile)
4. Risk overlay and deterministic constrained allocation (hard constraints + ordered relaxations)
5. Optional top-N underwriting packets (bounded runtime/cost, bounded concurrency, shared token/time budgets)
6. Persist reproducibility metadata hashes, regime snapshot, and per-ticker details

Step 5 now also triggers `compute_portfolio_risk_summary` (see §13) and stores the result in run metadata under `meta.portfolio_risk`.

Key principles:

- deterministic-first (LLM is second-stage explanation, not primary signal source)
- manual-first rollout
- explicit idempotency + structured error contracts
- prewarm support for expanded-universe metadata/info caches
- metadata-aware allocator implementation (batch metadata reads, no per-ticker DB N+1 loop)

---

## 5. Operational Safety

- Manual-only defaults for movers and long-horizon specialty flows
- Stabilization gate tests in `make stabilization-gate` (refresh + movers + long-horizon + watchlist-universe + TradingView export + yfinance-limiter + signal pipeline + institutional parity)
- Database backup + cleanup runbooks in `USER_MANUAL.md`
- Readiness checks via `GET /health/ready`, including `yfinance_limiter` status (`ok` / `rate_limited:Ns`) and session trip count
- Project-wide timezone-aware timestamps (`datetime.now(timezone.utc)`); no naive `utcnow()` callers remain
- yfinance rate-limit breaker applied across the alert monitor and screening data paths; cycles skip cleanly while the breaker is open
- Runtime metrics p95 (scan-all duration) surfaced via `GET /api/watchlists/universe/metrics`

---

## 6. Watchlist Universe Diagnostics

- `GET /api/watchlists/universe/metrics` — universe size, overlap, hydration baselines, p95 scan-all latency from `runtime_metrics`
- `GET /api/watchlists/migration/precheck` — alias/scheduler safety before legacy cleanup
- `GET /api/watchlists/parity-diff?left=...&right=...` — deterministic diff of two watchlists
- Bootstrap: a background thread materializes registry-backed built-in watchlists on startup if they are empty (`pending_first_refresh`)

---

## 7. Built-in Refresh Validation Flow

Built-in refresh proposals use a tiered validator designed for large index universes:

1. Tier 1 — Alpha Vantage `LISTING_STATUS` (`get_active_us_symbols`, 24h cache)
2. Tier 2 — Finnhub `/stock/symbol?exchange=US` (`get_us_symbol_set`, 24h cache)
3. Tier 3 — yfinance fallback through `YFinanceLimiter` with chunked validation

Design intent:

- broad index watchlists (`S&P 500/400/600`, `Russell 2000`) materialize full validated membership and do not require per-symbol market-cap hydration for ordering
- **curated thematic equity** built-ins (`Healthcare & Life Sciences`, `Energy & Commodities`, `AI Infrastructure Metals`, `AI & AI Infrastructure`) store a static comma-separated pool in `_builtin_watchlist_registry()` (`source_mode=curated`, `cadence=monthly_validation`). Healthcare and AI & AI Infrastructure have no list-level default preset so `investment_profile_resolution` can adapt per ticker; Energy and AI Metals default to `commodity_cyclical`. `AI & AI Infrastructure` is listed in `screening.scheduler.postclose.exclude_watchlist_names` so post-close scans skip it. Registry fields (`category`, `cadence`, `target_size`, …) are merged at read time via `ResearchDatabase.enrich_watchlist_registry_fields()` for API/UI consumers. Operator detail: `USER_MANUAL.md` § Curated thematic equity watchlists.
- curated Top-N watchlists use metadata-first market-cap hydration:
  - fresh `ticker_metadata.market_cap` first
  - Finnhub `/stock/profile2` fallback
  - Alpha Vantage `OVERVIEW` fallback
  - null cap degrades to rank-last (proposal still builds)
- refresh telemetry is written to `runtime_metrics`:
  - `builtin_refresh_duration_seconds`
  - `builtin_refresh_invalid_ratio`
  - `builtin_refresh_null_market_cap_count`
  - context fields include `tier1_validated`, `tier2_validated`, `tier3_validated`

`scan-all` is breaker-aware at both levels:

- fetch layer (`engine.py`) guards all `yf.download` calls with `YFinanceLimiter`, and re-checks `is_open()` before every chunk so remaining tickers in an in-flight batch are deferred (`deferred_breaker_open`) instead of being pushed into a cooldown
- job layer (`webapp/app.py`) defers startup when breaker is open and uses cooldown-aware pacing between watchlists in `per_watchlist` mode

Additional hardening:

- **Symbol format gate**: `_VALID_SYMBOL_RE` in `builtin_refresh.py` rejects malformed/typo symbols before tier validation; rejections are reported as `format_rejected` in refresh telemetry.
- **Aristocrats fallback**: `Dividend Aristocrats Top 50` falls through to the ProShares NOBL holdings CSV when the Wikipedia table predicate fails due to schema drift.
- **`initial_population` suppression**: first-time populate of a `pending_first_refresh` built-in replaces the high-churn warning with an informational note.

---

## 8. Ticker Health + Eviction

`tradingagents/reporting/database.py` exposes a `ticker_health` ledger that the screening engine updates on every OHLCV fetch:

- `record_ticker_success(symbol)` / `record_ticker_failure(symbol, reason)` — per-batch bookkeeping from `engine._fetch_batch_ohlcv`
- `get_stale_ticker_candidates(min_failures, min_days_since_success)` — candidates for eviction
- `get_evicted_tickers(since_days=None)` — bulk lookup used by `engine.scan()` to skip already-evicted symbols up-front (avoids re-issuing yfinance calls for known-dead tickers like `ANSS` post-Synopsys merger). Behaviour is opt-in via `screening.ticker_health.skip_evicted` (default True). The optional `skip_evicted_within_days` window lets old evictions age out so re-listings can be retried automatically.
- `evict_stale_tickers(..., dry_run=True)` — removes matching rows from `ticker_metadata`, timestamps the ledger with `evicted_at`, `eviction_reason`

Operator entrypoints:

- `scripts/evict_delisted_tickers.py` (+ `make evict-delisted-tickers-dry` / `make evict-delisted-tickers EVICT_CONFIRM=1`)
- Metrics surfaced on `GET /api/watchlists/universe/metrics` as `ticker_health.stale_candidates` and `ticker_health.evicted_last_24h`
- `GET /health/ready` surfaces `ticker_health_stale_candidates` as a soft-degraded signal

Behaviour is toggleable via `screening.ticker_health` in `default_config.py` (`track`, `auto_evict`, `min_failures`, `min_days_since_success`, `skip_evicted`, `skip_evicted_within_days`).

---

## 9. Scan-All Operability

- **Async submission**: `make screen-all-async` POSTs `/api/screen-all` with `block=false`, returns a `job_id`, and `make screen-all-status` / `make screen-all-watch` poll the queue without blocking the CLI.
- **Prewarm**: `make screen-all-prewarm` (`scripts/prewarm_screen_all.py`) bulk-downloads OHLCV for the union universe in batch sizes tuned to the limiter cooldown window, populating `ticker_health` ahead of a real scan.
- **Smoke**: `make qa-screen-all-smoke` (`scripts/qa/smoke_screen_all.py`) scans the seeded `Sector ETFs` + `NASDAQ 100` watchlists (override via `SMOKE_WATCHLISTS=...`) against a wall-clock budget (`SMOKE_BUDGET_SEC`, default 180s); wired into `stabilization-gate`. Missing watchlists are skipped gracefully so a bare DB still passes.
- **Frontend**: the screener page surfaces a live elapsed-time badge, watchlist progress counter, yfinance cooldown notes, and total `duration_seconds` in human-readable form on completion (`webapp/templates/screener.html::pollScreenAll`).
- **p95 baseline tooling**: `scan_all_p95_sec` in `/api/watchlists/universe/metrics` is computed over a rolling window (default 7 days, configurable via `screening.p95_window_days`) so post-fix runs converge faster. After a major perf fix, `make scan-p95-reset PRUNE_CONFIRM=1 KEEP_NEWEST=N` (or `make scan-metrics-prune PRUNE_DAYS=N`) trims historical samples in `runtime_metrics` so the metric reflects current behaviour. Backed by `ResearchDatabase.prune_runtime_metrics()` and `scripts/prune_runtime_metrics.py`.
- **Performance posture (2026-04-22)**: end-to-end async union scan over 13 watchlists / 2,946 union tickers / 4,152 raw tickers completes in ~190s (vs. ~979s before hardening) with zero watchlist failures — well under the 300s `scan_all_p95_sec_budget`. Tier 2 enrichment is capped via `screening.tier2_max_tickers` (700) and `screening.tier2_aux_max_tickers` (250).

---

## 10. Ranking Model

Three score dimensions feed the final `opportunity_score` surfaced to operators:

- **Composite Score** (`screening_results.composite_score`, 0-100): weighted sum of screening signals in `ScreeningEngine._compute_composite_score` using the active preset weights (`momentum_hunter`, `value_fisher`, `dividend_income`, etc.). MA/ADX are bullish-only (signed zeros still count as coverage-present). `rsi_overbought` is kept in shared weights for direction but skipped in the long composite. Missing PEAD is `None` (no 0.5 fill). Direction is a *label* computed separately — see below — rather than a signed score. Every result stamps `signals._screening_meta.resolved_preset`. `GET /api/screen-all/latest` lifts `factor_scorecard` + `resolved_preset` and supports `?book=` / Quality-Value-Income sort.
- **Composite Fundamental** (`screening_results.composite_fundamental`, 0-100): Composite recomputed in `ScreeningEngine._compute_composite_fundamental_score` with the six EQ-overlap signals (`rsi_oversold/overbought`, `bollinger_squeeze`, `ma_crossover`, `trend_strength`, `volume_surge`) excluded and the remaining weights renormalized. Used by the Opportunity blend so the same technical setup isn't counted once in Composite and again in Entry Quality. Falls back to full composite if all remaining weights are zero.
- **Entry Quality** (`screening_results.entry_quality`, 0-100): technical setup score from `ScreeningEngine._compute_entry_quality` — mean reversion, RSI sweet spot, Bollinger position, volatility contraction, MACD phase, volume dryup.
- **Macro Fit** (`screening_results.macro_fit`, 0-100): 3-layer overlay from `tradingagents/screening/macro_overlay.py` — market environment × 0.30, sector momentum × 0.40, profile-specific regime fit × 0.30.

### Direction labeling (neutral band)

`ScreeningEngine._determine_direction` computes a weighted polarity sum over `SIGNAL_POLARITY` (bullish `+1` / bearish `-1` / neutral `0`) and **normalizes by total directional weight** so values across different presets live on the same `[-1, +1]` scale. Rows are labeled:

- `bullish`  if normalized score > `screening.direction_conviction_threshold` (default 0.05)
- `bearish`  if normalized score < `-threshold`
- `neutral` otherwise

This replaces the previous "bullish by default on any positive residual" behaviour, which produced false bullish labels on weakly-scoring defensives because the 16-signal polarity map is 12:1 bullish vs. bearish.

### Opportunity Score blend

Canonical implementation: `tradingagents/screening/opportunity_score.py::compute_opportunity_score` (webapp, CLI, and Scan All overlays import this module).

`webapp.app._compute_opportunity_score(...)` is a thin delegate.

1. **Base weights**: 0.55 × (composite_fundamental || composite) + 0.30 × entry_quality + 0.15 × macro_fit.
2. **Regime-weighted macro**: if `macro_fit` is at the tails (|mf − 50| high), the macro weight interpolates up to 0.30 and the excess is subtracted proportionally from composite + EQ. Neutral macro (50) leaves weights unchanged.
3. **Graduated penalties** (no cliffs; weekly dampener is alignment-only):
   - EQ penalty linearly `0.70 → 1.00` across `entry_quality ∈ [0, 40]`.
   - Macro penalty linearly `0.80 → 1.00` across `macro_fit ∈ [0, 45]`.
4. **Graduated confluence bonus**: when `min(dimensions) ∈ [50, 65]` the base is multiplied by `1 + 0.10 × (min - 50) / 15`. No bonus below 50, full +10% at ≥ 65.
5. **Macro double-count guard**: when macro overlay ran and `|macro_fit − 50| ≥ screening.regime_adjustments.macro_fit_deviation_threshold`, regime weight shifts in `engine.scan()` use `strength_when_macro_present` instead of full `strength`.

**Scan All overlays** (`tradingagents/screening/scan_all_overlays.py`): liquidity gate/penalty, earnings blackout, optional coverage penalty; re-sort by opportunity or sector-relative score.

**Reversal buildup** (`tradingagents/screening/reversal_buildup.py`): dedicated scan mode (`ScreenRequest.mode=reversal_buildup` → `criteria.strategy=reversal_buildup`). Payload is embedded in `signals["_reversal_buildup"]` (no schema migration). Rank stored on `screening_results.rank` follows reversal score, not composite. v1.1 phase gates: confirmed must stay near the 60d extreme (`confirmed_pos60_long_max` 0.35 / `confirmed_pos60_short_min` 0.65); early/confirmed need ≥ `rsi_min_travel_pts` (5); shorts with `weekly_rsi_slope` above `weekly_short_demote_slope` (0) demote to watching. Watching scores take `penalty_watching` (0.80); market-wide short extensions take `penalty_market_dump_short` (0.85). Longs may stay confirmed while weekly RSI is still falling (washouts often start that way). GET `/api/screen/runs/{id}` returns `strategy` and lifted `reversal_score` / `reversal_phase` / `reversal_side`. Scheduler stays standard-only. Scan All follows the Run Screening **Scan mode** control: standard batches stamp `screen_all` / `screen_all_union`; reversal batches stamp `reversal_buildup` / `reversal_all_union` plus a shared `reversal_all_job_id`. Default `GET /api/screen-all/latest` still excludes reversal/movers/long_horizon from the opportunity cluster. `GET /api/screen-all/latest?strategy=reversal_buildup` is the reversal lens (dedup by buildup score, `long_setups` / `short_setups`).

The JS fallback in `webapp/templates/screener.html::computeOpportunityScore` mirrors this formula so client-side recompute stays consistent when `opportunity_score` isn't pre-computed by the server.

### Cross-watchlist consolidation (`/api/screen-all/latest`)

When a ticker appears in multiple watchlists in the same batch:

- Each row is given an `opportunity_z` (z-score of its opportunity_score *within* its watchlist) so dedup picks the watchlist where the ticker is relatively strong rather than the preset that systematically scores higher on absolute terms.
- Dedup preserves `watchlist_breadth` (distinct list count), `mean_opportunity_across_lists`, and `batch_percentile` (rank within today's cohort) as diagnostics.
- `market_cap_tier` and `sector` are hydrated from `ticker_metadata`; `sector_relative_score = opportunity - sector_median` is included so operators can see relative-to-sector strength.
- Response surfaces `stratified_top_opportunities` (best mega/large, mid, small/micro) to prevent market-cap-band dominance, plus `short_candidates` (bearish-only laggards) alongside `laggards` (bottom-N regardless of direction). The legacy `weakest_signals` key aliases `laggards` for backward compatibility.
- **Weekly MTF hydrate:** after deduplication and overlay enrichment, the endpoint hydrates daily+weekly confluence for a bounded pre-display candidate pool (`max(requested_top, weekly_mtf.hydrate_top_n_cap)`, default 60, hard cap 150). It reapplies the canonical overlay scorer and re-sorts before slicing the displayed Top N, so a counter-trend daily setup can be displaced by an otherwise strong candidate. Weekly data with insufficient history remains `null`/unknown — it is never converted to a bearish `0.0`.
- **Run provenance:** consolidated batches prefer runs stamped with `strategy=screen_all|screen_all_union`, preventing a nearby single-watchlist scan from joining the batch. Old untagged runs retain a gap-window fallback for compatibility. Union runs stamp their watchlist preset and report requested/scanned/truncated universe size. Runs tagged `reversal_buildup`, `reversal_all_union`, `movers`, or `long_horizon` are excluded **before** opportunity-batch clustering (column `screening_runs.strategy` and `criteria.strategy` JSON). The Reversal tab clusters only `reversal_buildup` / `reversal_all_union` via `?strategy=reversal_buildup`.
- **Analysis handoff:** Scan All Analyze actions send their persisted `screening_run_id`; queued research rebuilds the canonical screening context (liquidity, blackout, weekly, sector-relative, coverage) before invoking the research graph.

## 11. Asset-Class Universe (equities + ETFs + ADRs)

The screening universe is intentionally mixed across three asset classes. Each is resolved at metadata-refresh time and carried through the pipeline so the engine + UI can make asset-appropriate decisions.

- **Classification** — `tradingagents/screening/ticker_resolver.py::classify_asset_class(info)` reads yfinance `.info.quoteType` (primary), `info.country` (ADR fallback), and `info.longName` ("ADR" token) to assign one of `"etf"` / `"adr"` / `"equity"` / `"unknown"`. Persisted in `ticker_metadata.asset_class` (with `idx_ticker_meta_asset_class`) and snapshotted into every `screening_results` row so downstream analysis doesn't need to cross-join against mutable metadata.
- **ETFs** — Flow through the `etf_technical` preset (heavy Tier 1 technical weights, zero weight on fundamental signals like analyst targets / estimate revisions / valuation gap which don't exist for pooled vehicles). `_has_sparse_fundamentals` returns True for ETF/MUTUALFUND/CLOSEDENDFUND quote types, so the engine skips all Tier 2 auxiliary fetches (analyst ratings, options chain, earnings calendar, ownership, intrinsic value, estimate revisions) — each would 404 for an ETF, waste a yfinance call, and push the circuit breaker toward trip.
- **ADRs** — Flow through the standard equity decision tree (they have PE, analyst coverage, profit margins — they behave like US equities from a signal-availability standpoint). The `"adr"` label is preserved for UI badging and for the risk-score `asset_class_baseline` component (ADRs carry an FX + geopolitical premium → baseline 40 vs. equity 50).
- **Built-in watchlists** — five new registry-backed lists (`ETFs - Factor & Style`, `ETFs - Thematic & Industry`, `ETFs - Bonds & Rates`, `ETFs - Countries & Regions`, `ADRs - Top`) are materialized by `ConstituentRegistry` alongside the existing index lists. They use curated ticker pools (no Wikipedia scrape needed) and participate in `scan-all` via the normal union-buckets aggregation.
- **Frontend** — The ticker cell in all three tables (single-watchlist, scan-all top opportunities, scan-all laggards) renders an **ETF** or **ADR** badge next to the symbol. Equities get no badge to avoid visual noise on the majority case.

## 12. Risk Model

The `risk_score` dimension (`ScreeningEngine._compute_risk_score`) is an independent 0-100 output (higher = riskier). It is NOT an input to `composite_score`, `opportunity_score`, or any ranking — it is exposed as a separate sortable column so operators can filter the leaderboard by risk appetite without distorting the opportunity signal itself.

**Components** (weights renormalize when any are missing so partial data still produces a usable score):

| Component | Weight | Transform | 0 → 100 mapping |
|---|---|---|---|
| `volatility` | 0.20 | 60d annualized daily-return stdev | 10% → 0, 50%+ → 100 |
| `drawdown` | 0.15 | current drawdown from trailing 252d high | 0% → 0, 50%+ → 100 |
| `liquidity` | 0.15 | `log10(avg dollar volume)`, inverted | $100M/day → 0, $1M/day → 100 |
| `beta` | 0.10 | `|beta - 1.0|` | 0 → 0, 1.5+ → 100 |
| `leverage` *(equity-only)* | 0.15 | yfinance `debtToEquity` (returned as ×100) | 0 → 0, 300 → 100 |
| `profitability` *(equity-only)* | 0.10 | inverted `profitMargins` | +20% → 0, -20% → 100 |
| `short_pressure` *(equity-only)* | 0.10 | `shortPercentOfFloat` (fallback `shortRatio`/20) | 0 → 0, 30%+ → 100 |
| `asset_class_baseline` | 0.05 | fixed floor | ETF=20, ADR=40, equity=50, unknown=55 |

**ETF short-circuit.** For ETFs the three equity-only components (`leverage`, `profitability`, `short_pressure`) are *intentionally skipped* — the values yfinance returns for them on pooled vehicles are misleading (the SPY `debtToEquity` field is the market-cap-weighted average of the basket, not SPY's leverage; `profitMargins` is a fund-level attribution number, not a profitability signal). Skipping them + renormalizing yields a cleaner risk reading. The `asset_class_baseline` floor (20 for ETFs) reflects that ETFs are structurally lower-risk than single names due to diversification.

**Persistence.** Every `screening_results` row carries `risk_score REAL`, `risk_components TEXT` (JSON-serialized dict of the present components), and `asset_class TEXT`. The three are additive migrations and `get_screening_results` deserializes `risk_components` back into a dict automatically.

**Frontend.** The Risk column uses *inverted* color polarity vs. opportunity/EQ: green (≤35), amber (35-60), red (>60). Hovering the cell shows the per-component breakdown ("Risk 42.3 — 60d Vol: 35 | DD from 52w high: 12 | Illiquidity: 8 | Beta deviation: 18 | Leverage: 55 | ...").

---

## 13. Institutional Analysis Layer

Adds institutional-grade valuation depth, factor transparency, event awareness, and a backtesting learning loop on top of the core screening engine.

### 13.1 Valuation Depth

`tradingagents/dataflows/yfinance_extended.py`:

- **`compute_intrinsic_value(ticker)`** — DCF model returning `fair_value`, `margin_of_safety_pct`, `implied_growth_rate` (market-implied via bisection), `implied_vs_consensus` (delta vs analyst consensus), and `dcf_sensitivity_grid` (3×3 WACC × terminal-growth fair-value table; WACC/terminal-growth offsets configurable via `valuation.dcf_sensitivity` in `default_config.py`).
- **`compute_scenario_analysis(ticker)`** — bull/base/bear scenarios plus `blended_fair_value` and `blended_upside_pct` (probability-weighted composite using `valuation.scenario_weights`; default 25/50/25).
- **`compute_peer_comps(ticker)`** — P/E, P/S, EV/EBITDA vs. up to 10 sector peers (fetched concurrently), with sector medians and percentile ranks for the subject ticker.

`analyses` persists `intrinsic_value_data`, `scenario_analysis`, and `peer_comps` (JSON); `fundamentals_analyst.py` calls `compute_peer_comps` and stores the result in agent state. `research.py` also calls `_persist_catalyst_events` to parse ISO dates from `catalyst_pipeline` text and upsert them into `event_calendar`.

### 13.2 Factor Scorecard

`tradingagents/screening/engine.py::compute_factor_scorecard(ticker_data)` returns institutional factor families, each scored 0–100 or `None` when no members are present:

| Family | Signals used |
|---|---|
| Value | `valuation_gap`, `price_vs_target` |
| Quality | `quality_factor` |
| Flow | `smart_money`, `insider_buying` |
| Income | `income_factor` |
| Momentum | `volume_surge`, `rsi_oversold/overbought`, `ma_crossover`, `relative_strength`, `bollinger_squeeze`, `trend_strength`, `pead_drift`, `residual_momentum_12_1` |
| Revisions | `estimate_momentum`, `rating_momentum` |
| Catalyst | `earnings_proximity` |
| Risk | `options_sentiment` (display label: Options Sentiment) |
| Macro-Fit | `macro_fit` (clamped to [0, 100]) |

`factor_scorecard` is embedded in `screening_results.signals` as a private `_factor_scorecard` JSON key for calibration access. Agent state initializes `factor_scorecard: None`; `propagation.py` seeds it and `trading_graph.py` logs it.

### 13.3 Event Calendar

`tradingagents/reporting/database.py` `event_calendar` table stores upcoming corporate events unified across two feed paths:

1. **Analysis path** (`research.py::_persist_catalyst_events`) — parses ISO dates from the LLM-generated `catalyst_pipeline` text plus structured `earnings_profile` data.
2. **Screening path** (`engine.scan()`) — upserts earnings profiles and ex-dividend dates post-scan for every enhanced ticker.

`get_upcoming_events(ticker, max_days, event_types)` returns events sorted by days-to-event ascending. Operator access: `make upcoming-events [TICKER=] [DAYS=90]` or `GET /api/screening/upcoming-events`.

`get_corporate_actions(ticker)` fetches dividend and stock-split history with days-to-event; `catalyst_approaching` alert type fires when ex-dividend or split is within a configurable threshold.

### 13.4 Portfolio Risk Summary

`tradingagents/screening/long_horizon.py::LongHorizonService.compute_portfolio_risk_summary(results)` calculates:

- `weighted_beta` — portfolio-level beta weighted by `target_weight`.
- `sector_concentration` — sector weight percentages.
- `sector_hhi` — Herfindahl-Hirschman Index (> 0.25 = concentrated).
- `factor_exposures` — weighted-average factor scorecard scores across the portfolio.
- `top_factor` — dominant factor family.

Result is stored in long-horizon run metadata under `meta.portfolio_risk` and surfaced via `make portfolio-risk RUN_ID=`.

### 13.5 Calibration Learning Loop

`tradingagents/backtesting/calibration.py` adds two sliced calibration functions:

- **`compute_calibration_by_factor(db_path)`** — confidence-accuracy curves segmented by the dominant factor family in the `_factor_scorecard` embedded in each screening result. Identifies whether the model is better calibrated in Value regimes vs. Momentum regimes, etc.
- **`compute_calibration_by_decision(db_path)`** — same slicing by normalized BUY/HOLD/SELL decision class. Detects LLM base-rate skew (e.g., systematic BUY-side overconfidence).

`tradingagents/screening/engine.py` adds:

- **`compute_signal_performance_by_factor`** — aggregates per-signal hit rates into the 6 factor families.
- **`compute_adaptive_weights_by_factor`** — adaptive weight aggregation at family level.

New endpoints: `GET /api/screening/signal-performance-by-factor`, `/calibration-by-factor`, `/calibration-by-decision`, `/reverse-dcf-accuracy`.

CLI access: `make calibration-all` (bundle), `make calibration-by-decision`, `make calibration-by-factor`, `make reverse-dcf-accuracy`.

