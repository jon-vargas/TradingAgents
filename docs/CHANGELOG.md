# Changelog

Minimal release log for major project milestones.

---

## Unreleased

### All modes (one-pass meta screening)

- New watchlist scan strategy `multi_sleeve`: one `ScreeningEngine.scan()` scores Opportunity, Reversal, Early momentum, and Base. The lead is the first desk that clears (reversal, momentum, base, then opportunity). Results group by that desk. Location and secondary-desk chips sit on the row (`tradingagents/screening/multi_sleeve.py`).
- Screener **Scan mode → All modes (one pass)**; Results and History View; excluded from Scan All and Cross-Watchlist consolidators.

### Parallel analysts and report run settings

- Analysis analysts (market, social, news, fundamentals) run in **parallel** by default; wall time follows the slowest analyst. Set `TRADINGAGENTS_ANALYST_LAYOUT=sequential` to restore the legacy chain if TPM limits spike.
- Each analyst uses a private subgraph message loop; parent state merges reports only. Orchestrator context (ticker, provenance, Perplexity budget) is bound for worker threads during `propagate` / CLI runs.
- Checkpoints include `layout=parallel|sequential` in the run signature so sequential checkpoints do not resume on the parallel graph.
- HTML/PDF reports and `analyses.run_settings` store an allowlisted run block (analysts, mode, risk profile, debate rounds, data/tool vendors).

### Upstream analysis ports (0.5.2 focused slice)

- Analyst tool-round cap with shared finalize helper; routers exit when reports are written.
- Vendor-unavailable strings (rate limits / exhausted vendors) distinct from empty market; not cached; screening health skips transient `vendor_unavailable:` failures.
- Historical trade dates: skip live identity, historical cache keys, live-only tool gates.
- Portfolio `Rating:` label + `final_rating` on state; scorecards prefer REVIEW/BUY/SELL/HOLD from the label when set.
- Opt-in LangGraph SQLite checkpoints (`TRADINGAGENTS_CHECKPOINT_ENABLED`); CLI stream and `propagate` share checkpoint scope.

### AI & AI Infrastructure focus watchlist

- Added curated built-in **AI & AI Infrastructure** (125 US listings). The first pass covered platforms, AI software, accelerators, foundry and equipment, memory, servers, networking, power gear, data-center REITs, and neoclouds. A second pass added the physical gaps: fiber and materials (including Corning and Lumen), test, connectors, on-site power, backup generation, site and fiber construction, and advanced packaging. Perplexity cross-check added data-center HVAC (Carrier, Johnson Controls), cabling (Belden), and interconnect (Semtech). Miners and broad regulated utilities stay off; FRMI was not added. The list is in `postclose.exclude_watchlist_names`, so it is not part of the daily auto scan.

### Healthcare & Life Sciences built-in watchlist

- Added curated built-in **Healthcare & Life Sciences** (~105 US equities: payers/services, pharma, biotech, medtech, tools/CRO/diagnostics, healthcare REITs) in `database.py` with monthly builtin-refresh limits in `default_config.py`. Documented in `USER_MANUAL.md` (Curated thematic equity watchlists).
- Polish: registry metadata (`category`, `cadence`, …) enriched on `get_watchlists` / API; screener **Themes** grouping; `make qa-healthcare-watchlist-smoke`; `validate_matched_presets` entry; deprecated **Biotech Pre-Catalyst** → Healthcare in `deprecated_watchlists_map.json`.
- Post-close scheduler policy (`screening.scheduler.postclose`): exclude list, weekly cadence for heavy/redundant watchlists, priority order for thematic desks; status API exposes `eligible_today`.

### Screening OHLCV cache (Yahoo-only performance)

- Added [`tradingagents/dataflows/screening_ohlcv_cache.py`](tradingagents/dataflows/screening_ohlcv_cache.py): per-ticker JSON bars, SPY singleton cache, shared chunk keys, and `ScreeningOhlcvSession` to defer `yfinance_extended` calls during bulk OHLCV.
- [`ScreeningEngine`](tradingagents/screening/engine.py) assembles chunks from per-ticker hits, downloads misses only, and wraps the OHLCV loop in `ScreeningOhlcvSession`.
- [`scripts/prewarm_screen_all.py`](scripts/prewarm_screen_all.py) now warms the same DataCache entries as the engine (default batch size 60).
- New screening config: `metadata_resolve_during_scan` / `metadata_cached_max_age_days`, `intraday_cache_policy` (`ttl_only` default).
- Scheduler intraday refresh skips full cache wipe under `ttl_only`; `full_invalidate` clears `screening_prices`, `screening_ohlcv_ticker`, and `screening_spy`.

### Perplexity Agent API migration

- Migrated [`tradingagents/dataflows/perplexity_api.py`](tradingagents/dataflows/perplexity_api.py) from Sonar Chat Completions to the Perplexity Agent API (`responses.create`) with preset mapping (`sonar`→`fast`, `sonar-pro`→`low`).
- SEC filing summary/snapshot calls use explicit `web_search` tools with SEC domain and date filters; optional filing/transcript URLs are inlined in prompts.
- Added `perplexityai` dependency; optional `PERPLEXITY_API_MODE=sonar` rollback through 2026-09-27.
- Preset calls omit `temperature` (Agent API returns 400 when it is set on a preset). SEC snapshots request up to 4000 output tokens so the JSON payload can finish.

### Profile-aware analysis guardrails

- Auto investment profiles now resolve per ticker from explicit input, watchlist defaults, or fresh ticker metadata; profile provenance is persisted and displayed in reports and history.
- Growth-profile overlays adjust signal emphasis for high-growth, momentum/speculative, and commodity/cyclical securities while metadata-Auto classifications retain the selected client risk mandate’s base limits.
- Added a warn-only weak-SELL guardrail for decisions that conflict with strong street/scenario evidence on insufficiently bearish composites; the warning lowers confidence without overriding the decision.
- Corrected Perplexity SEC after-date formatting and marine-shipping classification, so deep reports retrieve SEC snapshots and tickers such as ZIM receive the commodity/cyclical lens.

### Market regime & sector alignment accuracy

- Market regime classification (`get_macro_snapshot`) now uses a widened dead zone (+2%/-5% around the 200-SMA) and requires 50-SMA trend confirmation for "bull", instead of flipping the instant price ticks across the 200-SMA — reduces whipsaw in the regime-driven signal-weight tilt.
- Sector momentum scoring (`macro_overlay.compute_sector_momentum`) is now a continuous min-max normalization of blended relative returns instead of a discrete rank→bucket table, so adjacent sectors no longer jump 25+ points apart for a negligible return difference.
- A sector whose ETF fetch fails entirely is excluded from the ranking/scoring pool (neutral score, sentinel rank) and flagged via a new `sector_data_quality` field, instead of being silently blended in as a 0.0 ("tracked the market exactly") return that could distort every other sector's rank.
- The regime-driven signal-weight shift's "reduced strength when macro_fit present" gate is now evaluated per ticker (checking that ticker's own macro_fit deviation) rather than as a single any-ticker batch-wide flag, which previously made "reduced strength" the de facto permanent state once macro overlay was enabled on any sizeable batch.
- Added `tests/test_macro_overlay_regime_sector.py` covering the regime boundary math, continuous sector scoring, missing/partial ETF data handling, and per-ticker regime strength selection.

### Scan All hardening + weekly MTF

- Fixed the consolidated `sector_medians` response path and recompute sector-relative scores after post-consolidate ranking changes.
- Unknown weekly data is now `null`, not a bearish `0.0`; computed weekly readings carry an explicit `weekly_computed` marker.
- Scan All hydrates weekly data for a bounded pre-display candidate pool, reapplies canonical overlays, and exposes MTF confluence, counter-trend filtering, detail panel metrics, and hydrate telemetry.
- Union Scan All now clamps to the engine hard cap, reports truncation honestly, stamps projected watchlist presets, and stamps strategy provenance for reliable batch membership.
- Cross-watchlist UI now exposes market-cap stratification, explicit bearish short candidates, watchlist breadth, and weekly MTF context. Analyze actions preserve the originating screening run and screening context.
- Intraday 4h MTF remains a deferred B4 follow-up: top 20–40 only, short cache TTL, breaker-aware, disabled by default.

### Institutional Screener Enhancement (Phases 0–6)

Single buildout: canonical scoring, weekly alignment, Scan All overlays, screener→analysis handoff, and UI polish.

**Phase 0 — Lean cleanup:**
- Canonical `tradingagents/screening/opportunity_score.py` shared by webapp, CLI, and Scan All overlays.
- Shared `weekly_alignment.py` used by screening engine and signal aggregator.
- Alerts read `event_calendar` first for `earnings_approaching` (`source: calendar|live`).
- Macro double-count guard: reduced regime shift when macro overlay present.
- Factor scorecard schema bump: **7 families** (Catalyst split from Revisions); JSON key `"Risk"` unchanged.

**Phases 1–5 — Overlays:**
- Funnel-gated `weekly_trend_alignment` signal with direction/opp dampeners.
- Scan All liquidity gate (dollar ADV written during scan → metadata gate/penalty).
- Sector-relative score column + sort in consolidated view.
- Event blackout penalty on Scan All; `earnings_play` preset exempt.
- Opt-in adaptive weight blend in `engine.scan()` (`screening.adaptive_blend`).

**Phase 6 — Effectiveness:**
- Multi-metric `valuation_gap` (P/E + P/S + EV/EBITDA blend).
- `signal_coverage_pct` + `tier_reached` on screening results.
- `ScreeningContextPacket` handoff → `batch_analyze` → `#screening-summary` in reports.
- Preset Performance calibration panel on screener; 16-signal copy fix.

**New/updated modules:** `opportunity_score.py`, `weekly_alignment.py`, `scan_all_overlays.py`, `context_packet.py`; `analyses.screening_context_json` column.

### Frontend institutional parity + Makefile operations refresh

Closes the last gap between the new backend institutional capabilities and what operators can see in the UI. No new backend logic — all changes are in templates, one new webapp endpoint, and Makefile housekeeping.

**Screener expand panel (institutional analysis):**
- DCF panel now shows **market-implied growth rate** (`implied_growth_rate`) and its delta vs. analyst consensus (`implied_vs_consensus`), plus a color-coded **3×3 WACC × terminal-growth sensitivity table** (center cell = base case, green/amber/red relative to current price).
- Scenario panel now shows the **probability-weighted blended fair value** (`blended_fair_value` / `blended_upside_pct`) below the bull/base/bear table.
- New **Peer Comparables** full-width panel: P/E, P/S, EV/EBITDA vs. up to 8 sector peers with median row and percentile rank annotations (`p25` = cheaper than 75% of peers).
- New **Upcoming Events** panel: next 8 entries from `event_calendar` within 90 days (earnings, ex-dividend, stock splits, catalysts) with urgency color coding.

**Long-horizon view:**
- New **Portfolio Risk Summary** card renders below the holdings table when a completed run has `meta.portfolio_risk`: weighted beta, sector HHI (with Diversified/Moderate/Concentrated label), sector concentration bars, factor exposure bar chart, and top-factor badge.

**Signal Performance — factor families:**
- The Signal Performance card now auto-appends a **Factor Family Performance** section (6 color-coded cards: Value, Quality, Momentum, Revisions, Risk, Macro-Fit) with hit rate, avg return, sample count, and adaptive weight, sourced from `/api/screening/signal-performance-by-factor`.

**Backtest page — new calibration panels (all collapsed by default):**
- **Calibration by Decision** — BUY/HOLD/SELL confidence-accuracy curves, detects LLM decision-class bias.
- **Calibration by Factor Family** — per-factor-regime calibration curves.
- **Reverse-DCF Accuracy** — implied vs. consensus growth error distribution with summary stats and per-ticker table.

**Alerts:**
- Added **Catalyst Approaching** quick-template to the alerts template grid (maps to `catalyst_approaching` type, 14-day threshold default).

**Settings page:**
- Fixed `default_risk_profile` silently ignored on load and save. Backend `SettingsUpdate` model now accepts the field.

**New backend endpoints:**
- `GET /api/ticker/{symbol}/peer-comps` — wraps `compute_peer_comps`.
- `GET /api/screening/upcoming-events` — wraps `db.get_upcoming_events` with optional `ticker` and `days_ahead` params.

**Makefile operations refresh:**
- New targets: `institutional-data TICKER=`, `upcoming-events [TICKER=] [DAYS=]`, `portfolio-risk RUN_ID=`, `calibration-by-decision`, `calibration-by-factor`, `reverse-dcf-accuracy`, `calibration-all` (bundle).
- Added `backfill-asset-class-dry/apply` and new calibration commands to `help` (they existed but were undocumented).
- `db-info` now includes `event_calendar` in the table survey.
- `stabilization-gate` expanded: `test_institutional_parity.py`, `test_builtin_refresh_tiered_validation.py`, `test_signal_pipeline.py`, `test_screening_signals.py`, `test_ranking_refinements.py`, `test_calibration.py`.
- `lint` fallback list extended: `backtesting/calibration.py`, `backtesting/engine.py`, `research.py`, `reporting/attribution.py`, `dataflows/provenance.py`, `dataflows/risk_metrics.py`, `utils/llm_usage.py`, `utils/snapshot_utils.py`.
- Removed stale "Feature 14" annotation from `profiles` target.
- `clean-cache-type` types list updated with `peer_comps`, `corporate_actions`, `insider_net_buy`.

---

### Institutional Feature Parity (Plan B)

Four-phase pass adding institutional-grade analytical depth across the valuation, factor, event, and learning-loop tiers. All capabilities persist through the existing `research.db` and flow through the multi-agent graph.

**Phase 1 — Valuation depth:**
- `compute_intrinsic_value` (`tradingagents/dataflows/yfinance_extended.py`) now returns:
  - `implied_growth_rate` — market-implied FCF growth via reverse-DCF bisection.
  - `implied_vs_consensus` — implied growth minus analyst consensus (earningsGrowth / revenueGrowth).
  - `dcf_sensitivity_grid` — 3×3 fair-value grid keyed `wacc{dw}pp_tg{dt}pp` over configurable WACC and terminal-growth offsets (from `valuation.dcf_sensitivity` in `default_config.py`).
- `compute_scenario_analysis` now returns `blended_fair_value` and `blended_upside_pct` — probability-weighted price target using `valuation.scenario_weights` (bull 25% / base 50% / bear 25% default).
- `compute_peer_comps` — new function in `yfinance_extended.py` that pulls P/E, P/S, EV/EBITDA for up to 10 sector peers concurrently (ThreadPoolExecutor), computes sector medians, and returns percentile ranks for the subject ticker.
- `Analysis` dataclass gains `peer_comps: str` field; `database.py` persists it and `fundamentals_analyst.py` populates it in the agent state.

**Phase 2 — Factor transparency:**
- `compute_factor_scorecard` (`tradingagents/screening/engine.py`) returns 6 institutional factor families — Value, Quality, Momentum, Revisions, Risk, Macro-Fit — each scored 0–100 with Macro-Fit clamped to [0, 100].
- Transcript KPI parser handles `EARNINGS_TRANSCRIPT_SNAPSHOT_JSON`, `_RAW`, and missing-data gracefully.
- Insider net-buy clustering in `smart_money` signal.
- `factor_scorecard` is embedded in the `signals` JSON field of `screening_results` (private `_factor_scorecard` key) for downstream calibration access. Agent state and `propagation.py` initialize `factor_scorecard: None`; `trading_graph.py` logs it.

**Phase 3 — Event awareness:**
- New `event_calendar` table with `upsert_event_calendar` and `get_upcoming_events` methods in `database.py`.
- `get_corporate_actions` (`yfinance_extended.py`) fetches dividend and split history with days-to-event calculation.
- `catalyst_approaching` alert type registered in `alert_evaluator.py` with `_check_catalyst_approaching`.
- `research.py` now calls `_persist_catalyst_events` after every analysis to parse ISO dates from `catalyst_pipeline` text and upsert structured events into `event_calendar`.
- Screening `engine.scan()` upserts earnings profiles and ex-dividend events into `event_calendar` post-scan.
- `compute_portfolio_risk_summary` added to `LongHorizonService` (`long_horizon.py`): weighted beta, sector HHI, sector concentration percentages, portfolio-level factor exposures (weighted average of individual scorecards), top factor. Output is persisted in long-horizon run metadata under `portfolio_risk`.

**Phase 4 — Learning loop:**
- `compute_signal_performance_by_factor` and `compute_adaptive_weights_by_factor` in `engine.py` — aggregate per-signal hit rates and adaptive weights into the 6 factor families.
- `compute_calibration_by_factor` and `compute_calibration_by_decision` in `tradingagents/backtesting/calibration.py` — confidence-accuracy calibration curves sliced by dominant factor family or by BUY/HOLD/SELL decision class.
- New webapp endpoints: `/api/screening/signal-performance-by-factor`, `/api/screening/calibration-by-factor`, `/api/screening/calibration-by-decision`, `/api/screening/reverse-dcf-accuracy`.

**Tests:**
- `tests/test_institutional_parity.py` — 40-test suite covering all four phases end-to-end.

---

### Lean System Cleanup (Plan A)

Removes accumulated dead code, stale configuration, and superseded test files to reduce noise and maintenance surface.

**Configuration keys removed** (`tradingagents/default_config.py`):
- `screening.movers.auto_queue_top_enabled`, `auto_alerts_enabled`, `auto_backtest_enabled` — never wired into any runtime path; manual-only operation is the default.
- `screening.long_horizon.automation` block (`auto_scan_enabled`, `auto_underwrite_enabled`, `auto_allocate_enabled`) — automation removed; flags had no effect.
- `screening.auto_discovery.run_day` — scheduler used cron internally; this field was ignored.
- `screening.builtin_refresh.proposal_only` — proposal workflow is always proposal-first by design; flag was dead.

**Unused functions removed:**
- `tradingagents/agents/utils/agent_utils.py`: `get_competitive_analysis`, `get_macro_context`, `get_management_quality`, `get_revenue_concentration`, `get_market_sentiment` — superseded by dedicated dataflow tools.
- `tradingagents/dataflows/yfin_utils.py` (file deleted): `get_YFin_data_window` and related helpers — replaced by `yfinance_extended.py`.
- `tradingagents/agents/risk_mgmt/aggresive_debator.py` (typo file deleted): duplicate of the correctly-spelled `aggressive_debator.py`.
- `tradingagents/utils/llm_usage.py`: `UsageTracker.get_recommendations` — unreferenced helper.

**Superseded test/script files removed:**
- `scripts/qa/test.py`, `scripts/qa/test_automation.py`, `scripts/qa/test_investment_profiles.py` — manual experiments that were replaced by the formal `tests/` suite.

**Documentation:**
- `USER_MANUAL.md`: removed stale entries for all deleted config keys (`long_horizon.automation.*`).

---

### Universe broadening (ETFs + ADRs) + risk_score dimension

This pass widens the screening universe to include 163 new ETFs across four categories plus 69 top ADRs, and adds a standalone `risk_score` dimension (0-100, higher = riskier) so operators can filter/sort by risk independently of "is this a good opportunity".

- **Asset-class classification.** `tradingagents/screening/ticker_resolver.py::classify_asset_class(info)` categorises tickers as `"etf"` / `"adr"` / `"equity"` / `"unknown"` from yfinance `.info` (quoteType + country + long-name heuristics). `ticker_metadata` gains `asset_class TEXT` + `country TEXT` columns (additive migration with `idx_ticker_meta_asset_class`). `resolve_profile_and_preset` now short-circuits to `etf_technical` for any ETF regardless of sector/market-cap/beta — ETFs lack PE / analyst targets / estimate revisions, so running them through the standard equity decision tree produces garbage signals.
- **New built-in watchlists (5).** Registered in `tradingagents/reporting/database.py::_builtin_watchlist_registry()`:
  - `ETFs - Factor & Style` (39) — SPY/QQQ/IWM core + factor tilts (MTUM, VLUE, USMV, QUAL) + style exposures (IVE/IVW, DIA, SCHD)
  - `ETFs - Thematic & Industry` (56) — thematic (ARKK, CIBR, BOTZ, ICLN, LIT) + industry (XRT, XHB, KRE, ITA, JETS)
  - `ETFs - Bonds & Rates` (33) — duration (TLT, IEF, SHY), credit (LQD, HYG), TIPS (TIP, VTIP), rate plays (TLH, EDV)
  - `ETFs - Countries & Regions` (35) — developed (EWJ, EWG, EWU), EM (EEM, VWO, MCHI, EWZ, INDA), regions (VGK, ILF)
  - `ADRs - Top` (69) — TSM, ASML, NVO, BABA, JD, BHP, RIO, NTES, SE, SONY, etc. `target_size=69` matches actual count post-dedup so `builtin_refresh` stops flagging false warnings.
- **`etf_technical` preset.** New preset in `tradingagents/default_config.py::screening.presets` with heavy Tier 1 weights (volume_surge, rsi_oversold/overbought, ma_crossover, relative_strength, bollinger_squeeze, trend_strength) and zero weight on fundamental signals (price_vs_target, insider_buying, estimate_momentum, rating_momentum, valuation_gap, pead_drift). ETFs don't have the underlying data for these, so zeroing their weights prevents them from dragging composite scores down vs. equity peers in a mixed watchlist.
- **ETF sparse-fundamentals short-circuit.** `ScreeningEngine._has_sparse_fundamentals(info)` now returns `True` for any `quoteType in {ETF, MUTUALFUND, CLOSEDENDFUND}`, so ETFs skip the Tier 2 auxiliary fetches (analyst ratings, estimate revisions, options chain, earnings calendar, ownership, intrinsic value) — each of which would miss for ETFs, log a 404, and waste a yfinance call against the circuit breaker.
- **Risk score (standalone dimension).** `ScreeningEngine._compute_risk_score(close, info, asset_class)` returns `(risk_score, components_dict)` in the range `[0, 100]` (higher = riskier). Eight weighted sub-components:
  | Component | Weight | Source | Range |
  |---|---|---|---|
  | volatility | 0.20 | 60d annualised stdev of daily returns | 10% → 0, 50%+ → 100 |
  | drawdown | 0.15 | current drawdown from 252d high | 0% → 0, 50%+ → 100 |
  | liquidity | 0.15 | log10(dollar volume), inverted | $100M/day → 0, $1M/day → 100 |
  | beta | 0.10 | `\|beta - 1.0\|` | 0 → 0, 1.5+ → 100 |
  | leverage *(equity-only)* | 0.15 | yfinance `debtToEquity` | 0 → 0, 300 → 100 |
  | profitability *(equity-only)* | 0.10 | inverted `profitMargins` | +20% → 0, -20% → 100 |
  | short_pressure *(equity-only)* | 0.10 | `shortPercentOfFloat` (fallback: `shortRatio`) | 0 → 0, 30%+ → 100 |
  | asset_class_baseline | 0.05 | ETF=20, ADR=40, equity=50, unknown=55 | fixed |
  Weights renormalize when components are missing (partial data still produces a usable score). ETFs skip the three equity-only components because yfinance's values for pooled vehicles are misleading (the SPY "debtToEquity" field is the market-cap-weighted average of the basket, not SPY's leverage). Persisted via new columns `screening_results.risk_score REAL`, `risk_components TEXT` (JSON), `asset_class TEXT` — all additive migrations.
- **Frontend: Risk column + asset-class badges.** `webapp/templates/screener.html` adds a sortable **Risk** column to the single-watchlist results table and the scan-all "Top Opportunities" + "Laggards & Short Candidates" tables. Cell coloring is *inverted* vs. opportunity/EQ (low=green, high=red) to reflect the opposite polarity. `riskTooltip()` surfaces the per-component breakdown on hover. `assetClassBadge()` renders ETF/ADR chips next to the ticker symbol (equity = no badge to avoid visual noise on the majority case).
- **API: risk fields in both endpoints.** `/api/screen-all/latest` and `/api/screen/runs/{run_id}` now expose `risk_score`, `risk_components`, `asset_class` per row (automatic via `get_screening_results` deserialization).
- **Tests (`tests/test_risk_and_asset_class.py`, +15 tests).** Asset-class classification across ETF/ADR/equity/unknown payloads; preset short-circuit for ETFs regardless of sector; risk-score range invariants on full/partial/extreme inputs; ETF component-skip verification; sparse-fundamentals flagging for ETFs vs. healthy equities. All 15 pass.
- **Smoke validation.** End-to-end scan on `[AAPL, SPY, TSM]` with DB-backed metadata resolution: SPY tagged `etf` with risk_score=7.0 (baseline=20, tech-only components), AAPL tagged `equity` with risk_score=18.1 (full equity component set). Round-tripped through `save_screening_results` → `get_screening_results`: `risk_components` correctly re-hydrates from JSON into a dict.

### Ranking algorithm v2: direction neutrality, disentangled composite, graduated penalties, regime-weighted blend

This pass addresses the ranking-quality issues surfaced by the in-depth audit of `scan-all` output. It ships in a single wave so the changes can be validated together but is behind clearly-named config knobs so individual pieces can be rolled back without touching the others.

- **Direction labeling: neutral band + weight normalization.** `tradingagents/screening/engine.py::_determine_direction` now normalizes the weighted polarity sum by the total directional weight and labels rows `"neutral"` when the magnitude falls below `screening.direction_conviction_threshold` (default `0.05`, in `tradingagents/default_config.py`). This eliminates the bullish-labeling bias that came from bullish-polarity signals outnumbering bearish ones 12:1 — a JNJ with a composite of 4.0 now correctly shows `neutral` rather than `bullish`. `ScreeningResult.direction` can now hold three values. Frontend (`webapp/templates/screener.html`) renders `neutral` as muted text in all three scan-all tables and the summary counts neutral separately when present.
- **Composite fundamental (disentangle).** New `ScreeningResult.composite_fundamental` field — Composite recomputed with the six EQ-overlap signals (`rsi_oversold/overbought`, `bollinger_squeeze`, `ma_crossover`, `trend_strength`, `volume_surge`) excluded and the remaining weights renormalized. `webapp.app._compute_opportunity_score` prefers `composite_fundamental` over `composite_score` when available so the Opportunity blend no longer double-counts the same technical setup once via Composite and again via Entry Quality. `screening_results` gets a new `composite_fundamental REAL` column (additive migration), persisted by `save_screening_results` and exposed via `/api/screen-all/latest`.
- **Graduated opportunity penalties (no cliffs).** Replaced the hard thresholds (`eq<25 → *0.75`, `macro<30 → *0.85`, confluence `>=55 → *1.10`) in `webapp.app._compute_opportunity_score` with piecewise-linear ramps: EQ penalty scales `0.70 → 1.00` across `[0, 40]`, macro penalty `0.80 → 1.00` across `[0, 45]`, confluence bonus `0 → +10%` across `min(dim)=50 → 65`. Two adjacent names with EQ values 24 vs 26 used to straddle a 25% opportunity-score cliff; they now differ by <2 points. JS fallback in `screener.html::computeOpportunityScore` mirrors the backend so client-side recompute stays consistent.
- **Regime-weighted macro blend.** `_compute_opportunity_score` now interpolates the macro weight between 0.15 (at `macro_fit==50`) and 0.30 (at the tails, `macro_fit==0` or `100`), redistributing the delta proportionally from composite + EQ. A macro-fit of 25 in a fragile tape therefore penalises more aggressively than the static 0.15 weight allowed, and a 75 in a risk-on tape helps more — while a 50 leaves the static weights unchanged. Mirrored in the JS fallback.
- **Preset-normalized cross-watchlist dedup.** `/api/screen-all/latest` now computes a per-watchlist z-score of every row's opportunity_score (`opportunity_z`) and dedups by z-score (tie-broken on raw score), removing the "preset shopping" bias where a ticker appearing in two lists would always be represented by whichever preset happened to score it higher. Raw opportunity scores still drive display ordering.
- **Watchlist breadth + batch percentile on dedup rows.** Every deduped row in `/api/screen-all/latest` gains `watchlist_breadth` (distinct list count), `mean_opportunity_across_lists`, and `batch_percentile` (rank within today's cohort). This complements the historical `percentile` (rolling 10-run baseline) already on `ScreeningResult` so callers can see "how this ticker stacks up in today's universe specifically".
- **Stratified top-N by market-cap tier.** `/api/screen-all/latest` now returns `stratified_top_opportunities` with separate `mega_large` / `mid` / `small_micro` buckets (populated from `ticker_metadata.market_cap_tier`). Prevents Russell-2000-heavy refreshes from flooding the headline leaderboard with 60-opportunity-score micro-caps — operators can now inspect best large-cap / mid-cap / small-cap at a glance.
- **Sector-relative scoring.** Every row in the consolidated view gets `sector_relative_score = opportunity_score - sector_median` for its sector (batch-local medians), surfacing names that are outperformers within a weak sector even when the absolute opp score looks mediocre. `sector_medians` is included in the response for transparency.
- **Short candidates vs. laggards.** `/api/screen-all/latest` gains `short_candidates` (bearish-only, sorted by lowest opp) alongside `laggards` (bottom-N by opp regardless of direction). The legacy `weakest_signals` key is kept as an alias of `laggards` for backward compatibility. UI heading renamed from "Weakest Signals" to "Laggards & Short Candidates" to reflect the distinction.
- **Tests (`tests/test_ranking_refinements.py`, +13 tests)**: direction neutral band under varied polarity mixes, composite-fundamental disentanglement math, graduated penalty monotonicity across the old-cliff boundary, confluence bonus ramp, `composite_fundamental` override in the opportunity blend, regime-weighted macro amplification at the tails, and graceful handling of missing inputs. Full suite is 162/162 green.

### Final scan-all polish: eviction-aware skip, p95 baseline tooling, frontend UX

- **Engine: skip already-evicted tickers up-front.** `tradingagents/screening/engine.py::scan()` now consults `ResearchDatabase.get_evicted_tickers()` before any OHLCV fetch and removes known-dead symbols (e.g., `ANSS` post-Synopsys merger) from the universe. Behaviour is opt-in via `screening.ticker_health.skip_evicted` (default True) and supports an optional `skip_evicted_within_days` window so old evictions can age out and be retried automatically. Validated live: NASDAQ 100 smoke run dropped from 38.8s → 11.5s once `ANSS` was filtered, with zero `YFTzMissingError` noise.
- **Database: bulk evicted-ticker lookup.** Added `ResearchDatabase.get_evicted_tickers(since_days=...)` for cheap set membership checks during scan setup (used by the engine guard above).
- **p95 baseline tooling.** `get_runtime_metric_p95()` accepts `since_days` to constrain the rolling window; `prune_runtime_metrics()` and a new `scripts/prune_runtime_metrics.py` give operators two ways to reset noisy p95 baselines after a perf fix lands:
  - `make scan-metrics-prune-dry` / `make scan-metrics-prune PRUNE_CONFIRM=1` — delete samples older than `PRUNE_DAYS` (default 14).
  - `make scan-p95-reset-dry` / `make scan-p95-reset PRUNE_CONFIRM=1 KEEP_NEWEST=N` — keep only the N most-recent samples for `scan_all_duration_seconds` (or any metric via `P95_METRIC=`).
  - `webapp/app.py::/api/watchlists/universe/metrics` now uses a 7-day p95 window (configurable via `screening.p95_window_days`) and reports the window in the response.
  - Demonstrated live by trimming the regressed 979s sample, dropping `scan_all_p95_sec` from 978.993 → 253.327.
- **Tier 2 auxiliary cap tightened** from 300 → 250 tickers in `tradingagents/default_config.py::screening.tier2_aux_max_tickers`. Fully end-to-end async run completed in **191s** (vs. 253s prior, vs. 979s before hardening) — under the 300s p95 budget with zero failures across all 13 watchlists / 2,946 union tickers / 3,192 result rows.
- **Frontend UX: scan-all clarity.** `webapp/templates/screener.html` adds a live elapsed-time badge alongside the watchlist counter, surfaces yfinance cooldown notices in the detail row, and reports total `duration_seconds` on completion using a humanised `Xm YYs` format.
- **Smoke target portability.** `scripts/qa/smoke_screen_all.py` now defaults to `Sector ETFs` + `NASDAQ 100` (always present after first refresh) instead of the never-seeded `Dow 30`, supports `SMOKE_WATCHLISTS=...` overrides, and skips missing names gracefully so CI keeps green.
- **Tests**: added `test_get_evicted_tickers_returns_marked_set` and `test_get_evicted_tickers_respects_since_window` to `tests/test_wave2_hardening.py`. 25 hardening tests pass; 36 pass across the broader scan-all sweep.

### Scan-all runtime optimization follow-up

- Fixed runtime metric query schema mismatch in `webapp/app.py` (`metric_key`/`duration_seconds`/`created_at`), restoring `refresh_health` and `/health/ready` metric surfaces that were degraded by `no such column: metric_value`.
- Reduced `screen-all` wall-clock time by tightening Tier 2 enrichment in `tradingagents/screening/engine.py`:
  - Enforce `hard_max_watchlist_size` **before** per-ticker metadata resolution, avoiding unnecessary resolver work for dropped tickers.
  - Added configurable caps: `screening.tier2_max_tickers` and `screening.tier2_aux_max_tickers` (in `tradingagents/default_config.py`) to bound expensive `.info` + estimate/rating enrichment.
  - Hardened `_fetch_info_with_retry` to cancel unfinished futures on global timeout (`shutdown(wait=False, cancel_futures=True)`), preventing threadpool shutdown waits from stalling runs.
- Fixed per-ticker preset breakdown logging to normalize `None` presets to `"default"` before sorting, eliminating intermittent `'<'' not supported between instances of 'NoneType' and 'str'` warnings.
- Operational validation: async union scan runtime improved from ~979s to ~253s with equivalent completion status (`watchlists_failed=0`), and stale ticker metadata eviction (`make evict-delisted-tickers EVICT_CONFIRM=1`) cleared 10 high-failure symbols from cache.

### Scan-all hardening waves (null-safety, ticker health, operability)

- **Wave 1 — Null-safe comparisons**: Introduced `_safe_num()` in `tradingagents/screening/ticker_resolver.py` and applied it across `classify_market_cap`, `classify_beta`, and `resolve_ticker_metadata` so yfinance values like `"N/A"`, `"Infinity"`, bools, or numeric strings coerce to `float | None` without raising. `market_cap` is now normalized to `int | None` at the emit site to prevent `str > int` comparison errors downstream.
- **Wave 1 — Sector-median robustness**: `tradingagents/screening/engine.py::_compute_live_sector_medians` and `_signal_valuation_gap` use `_safe_num` for forward PE and coerce `sector` to `str`, avoiding `NoneType < str` crashes when yfinance returns mixed types.
- **Wave 1 — Churn denominator sanity**: First-time populate of a `pending_first_refresh` built-in no longer emits the "High churn" warning. `tradingagents/screening/builtin_refresh.py::build_refresh_proposal` emits an `initial_population` note instead when the stored watchlist row is empty and `source_mode_status == "pending_first_refresh"`.
- **Wave 2 — Format regex gate**: Added `_VALID_SYMBOL_RE` + `_is_valid_symbol_format()` to the tiered validator so typo-tier rejects (e.g., `AMAZN`, `APPL`) never reach yfinance. Format rejects are counted as a new `format_rejected` telemetry bucket and reported via `builtin_refresh_symbol_rejections`.
- **Wave 2 — `ticker_health` ledger + eviction**: New `ticker_health` table in `tradingagents/reporting/database.py` with `record_ticker_failure`, `record_ticker_success`, `get_ticker_health`, `get_stale_ticker_candidates`, and `evict_stale_tickers(dry_run=...)`. `engine.scan()` records per-ticker fetch outcomes during OHLCV chunking. New `scripts/evict_delisted_tickers.py` + `make evict-delisted-tickers-dry` / `make evict-delisted-tickers` (with `EVICT_CONFIRM=1` apply guard).
- **Wave 3 — Chunk-level breaker guard**: `engine.scan()` re-checks `YFinanceLimiter.is_open()` before every batch and defers remaining tickers (`deferred_breaker_open`) rather than hammering yfinance during cooldowns.
- **Wave 3 — Scan-all operability**: Added `make screen-all-prewarm` (`scripts/prewarm_screen_all.py`) to bulk-download OHLCV for the union universe ahead of a scan, plus `make screen-all-async`, `make screen-all-status`, and `make screen-all-watch` targets that drive the async `/api/screen-all` submission path without blocking the CLI.
- **Wave 4 — Observability**: `/health/ready` and `/api/watchlists/universe/metrics` (`webapp/app.py`) now surface `builtin_refresh_invalid_ratio`, `tier1/2/3` split, `format_rejected`, and `ticker_health_stale_candidates` plus `evicted_last_24h`. Added `make qa-screen-all-smoke` (`scripts/qa/smoke_screen_all.py`) wired into `stabilization-gate` — scans Dow 30 + NASDAQ 100 against a wall-clock budget.
- **Wave 5 — Dividend Aristocrats resilience**: Broadened `_is_aristocrats_table` (matches streak/company+sector variations) and added `PROSHARES_NOBL_HOLDINGS_CSV` holdings fallback so `Dividend Aristocrats Top 50` keeps refreshing through Wikipedia schema drift.
- **Config**: Added `screening.ticker_health` block (`track`, `auto_evict`, `min_failures`, `min_days_since_success`) in `tradingagents/default_config.py`.
- **Tests**: `tests/test_wave1_hardening.py`, `tests/test_wave2_hardening.py`, `tests/test_wave3_hardening.py` (23 new tests; 143/143 project-wide green).

### Multi-source watchlist population + scan-all hardening

- Reworked built-in refresh symbol validation to a tiered flow in `tradingagents/screening/builtin_refresh.py`:
  - Tier 1: Alpha Vantage `LISTING_STATUS` set membership (24h cached)
  - Tier 2: Finnhub `/stock/symbol?exchange=US` set membership (24h cached)
  - Tier 3: yfinance fallback gated by `YFinanceLimiter` (chunked, breaker-aware)
- Added new provider helpers:
  - `tradingagents/dataflows/alpha_vantage_common.py:get_active_us_symbols()`
  - `tradingagents/dataflows/finnhub_api.py:get_us_symbol_set()`
- Moved market-cap hydration for curated Top-N refresh paths to metadata-first + API fallback:
  - `ticker_metadata` bulk cache first
  - Finnhub `/stock/profile2`, then Alpha Vantage `OVERVIEW`
  - yfinance market-cap fan-out removed from refresh path
- Added refresh runtime telemetry samples in `runtime_metrics`:
  - `builtin_refresh_duration_seconds`
  - `builtin_refresh_invalid_ratio`
  - `builtin_refresh_null_market_cap_count`
  - tier split counts in metric context (`tier1_validated`, `tier2_validated`, `tier3_validated`)
- Wired `YFinanceLimiter` into screening fetches (`tradingagents/screening/engine.py`) for all `yf.download` paths, including SPY benchmark fetch.
- Hardened `/api/screen-all` execution (`webapp/app.py`) with:
  - startup deferral when breaker is open
  - cooldown-aware inter-watchlist pacing in `per_watchlist` mode
- Added coverage tests:
  - `tests/test_builtin_refresh_tiered_validation.py`
  - `tests/test_engine_breaker_integration.py`
  - `tests/test_scan_all_breaker_guard.py`

### Watchlist and index coverage restructure

- Added SQL constituent registry table `index_constituents` with deterministic ordering support (`liquidity_rank`, market-cap tie breaks) and latest-snapshot materialization helpers.
- Expanded built-in index coverage surface to include registry-backed `S&P 500`, `S&P 400`, and `Russell 2000` watchlists in addition to existing top-N operator views.
- Added union `screen-all` mode (`scan_all.mode=union_buckets`) with per-watchlist projection from one union scan pass and bucket diagnostics in job results.
- Added watchlist QA/ops endpoints:
  - `GET /api/watchlists/universe/metrics` for overlap/hydration baselines and hard-budget metadata.
  - `GET /api/watchlists/migration/precheck` for clean-restart alias/scheduler safety checks.
- Added ETF-holdings ingestion fallback for Russell breadth via iShares IWM holdings CSV source.
- Source/legal note: ETF holdings are source-provider data; operators should validate provider terms and redistribution constraints before external publishing.

### Long-horizon policy and allocation hardening

- Enforced `exclude_leveraged` in long-horizon universe policy.
- Expanded allocation contract implementation:
  - `market_cap_band_min` support
  - explicit relaxation ordering (`market_cap_band_min`, `beta_min`, `sector_soft_cap`)
  - hard-constraint semantics for strict caps/floors.
- Reworked allocator metadata access to batch DB lookups (removed per-ticker N+1 pattern).

### Long-horizon underwriting and metadata

- Applied bounded underwriting concurrency with shared timeout and token budget enforcement.
- Preserved deterministic output ordering while allowing concurrent underwriting work.
- Upgraded regime snapshot metadata with macro state + sector momentum fields.

### yfinance rate-limit hardening

- Added a process-wide yfinance rate-limit breaker (`tradingagents/dataflows/yfinance_limiter.py`) with:
  - automatic 429 / "Too Many Requests" / `YFRateLimitError` detection
  - exponential backoff cooldown (5m initial → capped at 30m, reset on success)
  - inter-call spacing (100ms default) to smooth bursts
  - thread-safe singleton via `get_yfinance_limiter()`
- Wired the limiter into `AlertEvaluator._get_ticker_data` and cached 90-day history fetches in `DataCache` (`SCREENING_PRICES=1h` TTL), cutting yfinance calls from the alert monitor ~30× during market hours.
- `AlertMonitor._poll_loop` now skips cycles while the breaker is open and honors the remaining cooldown when choosing the next wait interval.
- Per-ticker "Failed to fetch data… Rate limited" ERROR spam is replaced with a single aggregated WARN line per cycle reporting the number of throttled skips.
- `/health/ready` now reports `yfinance_limiter` (ok / `rate_limited:Ns`) and `yfinance_limiter_total_trips` for operational visibility.

### Timestamp consistency updates

- Replaced long-horizon `utcnow()` usage with timezone-aware UTC timestamps in service/API paths.
- Completed project-wide sweep: migrated all remaining naive `datetime.utcnow()` callers in `webapp/app.py` (jobs lane, idempotency cache, global execution gate, analysis queue, heartbeat endpoints, movers status), `tradingagents/reporting/database.py` (snapshot index), `tradingagents/dataflows/provenance.py`, and `tradingagents/dataflows/perplexity_api.py` to `datetime.now(timezone.utc)`. All job/run/event timestamps are now timezone-aware ISO-8601 strings.

### Makefile QA and documentation pass

- Fixed an indentation bug in `scripts/report_fast_qa.py` that broke `make qa-report` / `make qa`.
- `make qa-report-tests` now uses `pytest tests/ -q` with graceful fallback to `unittest discover`.
- Expanded `make stabilization-gate` to include `tests/test_yfinance_limiter.py` alongside `tests/test_watchlist_universe_restructure.py`.
- Expanded `make lint` to cover `yfinance_limiter.py`, `alert_evaluator.py`, `long_horizon.py`, and `webapp/alert_monitor.py`.
- Expanded `make db-info` table list to include `runtime_metrics`, `builtin_refresh_proposals`, and `builtin_refresh_items`.
- Added operator-facing make targets:
  - `make limiter-status` — yfinance breaker state and session trip count
  - `make watchlist-metrics` — universe size / overlap / p95 diagnostics
  - `make watchlist-migration-precheck` — cleanup precheck for legacy watchlists
  - `make watchlist-parity-diff LEFT=... RIGHT=...` — deterministic watchlist diff
- Refreshed `README.md`, `USER_MANUAL.md`, and `docs/ARCHITECTURE.md` to reflect registry-backed watchlists, union-buckets `scan-all` mode, `runtime_metrics` telemetry, yfinance rate-limit breaker, and new API/Make surfaces.

---

## 2026-03-31

### Long-horizon performance and operations

- Added expanded-universe cache prewarm command:
  - `scripts/prewarm_long_horizon.py`
  - `make long-horizon-prewarm`
- Added updated long-horizon scan controls:
  - `LH_USE_EXPANDED=true|false` in `make long-horizon-scan`
  - expanded-universe scan path can run without explicit `WL_ID`/`TICKERS`
- Added two-pass expanded execution profile controls and metadata visibility:
  - fast shortlist pass + deep pass for larger universes
  - execution profile details persisted in long-horizon run metadata

### Documentation updates

- Updated `README.md` with prewarm + expanded scan workflow and recommended run sequence.
- Updated `USER_MANUAL.md` with new long-horizon command surface and troubleshooting notes.
- Updated `docs/ARCHITECTURE.md` long-horizon flow to reflect metadata-first filtering and two-pass execution.

---

## 2026-03-25

### Documentation consolidation

- Reduced docs to a minimal canonical set:
  - `README.md`
  - `USER_MANUAL.md`
  - `docs/ARCHITECTURE.md`
  - `docs/CHANGELOG.md`
- Removed redundant legacy docs to reduce overlap and maintenance burden.

### Long-horizon institutional workflow

- Added deterministic-first long-horizon scan workflow with:
  - horizon presets (`long_6to12m`, `long_12to36m`)
  - reproducibility metadata snapshots
  - risk overlay + deterministic constrained allocation
  - bounded top-N underwriting endpoint
- Added long-horizon API endpoints and Make command surface.

### Movers and UI hardening

- Movers table supports sorting and filtering in the Screener UI.
- Movers and long-horizon remain manual-first by default.

---

## 2026-03 (earlier)

- Built-in refresh proposal/apply guardrails and safer force-apply flow.
- Movers run lineage hardening and run-type guard checks.
- Backup lifecycle improvements (`backups/` + auto-prune).

