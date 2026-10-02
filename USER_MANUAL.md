# TradingAgents User Manual

Canonical operator runbook for local usage.

---

## 1. Start / Stop

```bash
make ui
make status
make health
make limiter-status
make logs
make stop
make restart
```

Default URL: `http://localhost:8000`

`make limiter-status` reports the yfinance rate-limit breaker state (`ok` or `rate_limited:Ns`) and its total trips for the current process.

---

## 2. Core Analysis

### Single ticker

```bash
make analyze TICKER=NVDA DATE=2026-03-25 MODE=standard RISK=growth PROFILE=large_cap_core
```

### Batch

```bash
make batch-analyze TICKERS="NVDA AAPL MSFT" DATE=2026-03-25 MODE=standard RISK=growth PROFILE=high_growth
```

### Deep mode — Perplexity call contract

`MODE=deep` is the only analysis path that spends Perplexity credits. Quick and Standard do not call Perplexity.

Perplexity uses the **Agent API** (`responses.create`) by default. Legacy Sonar Chat Completions retire **2026-09-27**; set `PERPLEXITY_API_MODE=sonar` only as a pre-sunset fallback. Tier mapping: **fast** (formerly `sonar`), **low** (formerly `sonar-pro`). SEC snapshots use explicit SEC-domain web search, not preset-only calls. Optional filing/transcript URLs are passed in the prompt (Agent API has no `file_url` upload).

**Live smoke (optional):** with `PERPLEXITY_API_KEY` set, run `make perplexity-smoke TICKER=AAPL` to exercise deep research + SEC snapshot JSON.

| Artifact | Equity deep run | Commodity ETF deep run |
|---|---|---|
| SEC filings snapshot (prefetch) | 1 live call (first pass) | 1 live call |
| Earnings transcript snapshot (prefetch) | 1 live call | skipped |
| `get_deep_research` (news analyst) | 1 live call | 1 live call |
| Catalyst pipeline | 0–1 live call (fast tier, cache-first; skipped when the last live slot is reserved for `get_deep_research` or monthly budget is tight) | 0–1 |
| **Typical first-pass total** | **≤ 3 live calls** | **≤ 2 live calls** |

Re-runs within cache TTL reuse snapshots (7 days for SEC/transcript JSON; 6 hours for deep research) and usually spend **0–1** live calls.

Desk monthly budget is `perplexity.monthly_limit` in config (default **100**). Discover and auto-discovery draw from a reserved sub-budget (`perplexity.discovery_reserve`, default **10**); deep analysis uses the remainder. Check live counters at `/api/usage`.

Screening (`make screen`, Scan All) does **not** use Perplexity for Opportunity or Reversal. Early momentum's bounded enrich pass may use a dedicated Perplexity budget on the top N names (not the Deep analysis budget).

### Historical trade dates

When `DATE=` (or the web UI analysis date) is **strictly before today** in US/Eastern,
the run is treated as **historical**: live Yahoo identity lookup is skipped. Sector and
industry are taken from stored ticker metadata only when that row’s `last_updated` date
is on or before the trade date. Vendor cache keys include the trade date, and live-only
tools (deep Perplexity research, insider sentiment/transactions without a filing date)
return an explicit not-available message instead of mixing in current data. A Yahoo
rate-limit or open breaker returns a vendor-unavailable message that says to retry later;
it is not treated as a delisting.

### Analyst tool-round cap

Analyst agents cap tool loops with `min(analysis.max_tool_rounds, token_budget.analyst.*_max_tool_messages)`.
Default ceiling: `TRADINGAGENTS_MAX_TOOL_ROUNDS=20`. When the cap is hit, the analyst
finalizes with a text report even if the model still emits tool calls.

### Portfolio `Rating:` line

The risk manager must emit `Rating: BUY|SELL|HOLD|REVIEW` in English on its own line
(in addition to `DECISION_JSON`). Reports and scorecards prefer this label when present;
`DECISION_JSON.decision` remains the stored BUY/SELL/HOLD for DB/UI when JSON is valid.

### Parallel analysts (default)

Analysis analysts run **in parallel** (fan-out from start, join at bull researcher). Analyst wall time is roughly the **slowest** selected analyst, not the sum. If provider TPM limits spike, set `TRADINGAGENTS_ANALYST_LAYOUT=sequential` to use the legacy one-after-another chain without code changes. Screening and Scan All stay sequential.

Reports include a **Run** header row (analysts, analysis mode, risk profile, debate rounds, data/tool vendors) alongside the model row. The same JSON is stored on each analysis row as `run_settings`.

### Resumable checkpoints (opt-in)

Set `TRADINGAGENTS_CHECKPOINT_ENABLED=1` (default off) to persist graph progress per ticker
under `{data_cache_dir}/checkpoints/`. Resume requires the same ticker, trade date, and
run signature (analysts, analyst layout, debate rounds, models, analysis mode, risk profile, investment
profile key). Only one concurrent checkpointed run per ticker/date is supported.

### Risk and investment profiles

`RISK` is the client mandate: Aggressive, Growth, or Conservative. It controls the
decision evidence threshold and the base beta, drawdown, and VaR gates.

`PROFILE` is the security-type lens. When omitted or set to Auto in the web UI,
TradingAgents resolves it per ticker from fresh cached metadata (sector,
capitalization, profitability, dividend status, and beta). A manual `PROFILE`
selection or watchlist default always wins. Auto classification changes analyst
focus and signal weighting, but does not loosen the selected risk-profile limits.

Reports show the resolved profile and its source. `sell_guardrail_weak` is a
warn-only QA flag: it marks a SELL that conflicts with strong street or scenario
valuation evidence while its composite is insufficiently bearish. It does not
override the decision in the initial rollout; it lowers displayed confidence.

### Institutional data (no server required)

```bash
make institutional-data TICKER=NVDA
```

Prints the following for the given ticker without requiring the web server:

- **DCF fair value** and margin of safety
- **Implied growth rate** (market-implied via reverse-DCF) and delta vs. analyst consensus
- **3×3 sensitivity grid** (WACC × terminal growth)
- **Scenario analysis** — bull/base/bear targets and probability-weighted blended fair value
- **Peer comparables** — P/E, P/S, EV/EBITDA vs. sector peers with sector medians and percentile rank

### Upcoming events (no server required)

```bash
make upcoming-events                    # all tickers, 90-day default
make upcoming-events TICKER=AAPL        # single ticker
make upcoming-events TICKER=AAPL DAYS=30
```

Queries the `event_calendar` table and prints upcoming earnings, ex-dividend dates, stock splits, and catalyst events.

---

## 3. Screening and Watchlists

```bash
make watchlists
make screen WATCHLIST="AAPL MSFT NVDA" PRESET=momentum_hunter
make screen-watchlist NAME="NASDAQ 100"
make screen-watchlist ID=2 PRESET=value_fisher
make screen-watchlist NAME="NASDAQ 100" MODE=reversal_buildup
make screen-all TOP=20
make screen-all-prewarm
make screen-all-async
make screen-all-async MODE=reversal_buildup
make screen-all-status
make screen-all-watch
make screen-backtest RUN_ID=1
make watchlist-metrics
make watchlist-migration-precheck
make watchlist-parity-diff LEFT="S&P 500" RIGHT="S&P 500"
make builtins-refresh-propose
make builtins-refresh-proposals
make builtins-refresh-apply ID=7
```

Notes:

- Broad index watchlists (`S&P 500`, `S&P 400`, `S&P 600`, `Russell 2000`) are registry-backed. They ship empty with `source_mode_status=pending_first_refresh` and are materialized from `index_constituents` after the first built-in refresh (or the background bootstrap materialization on startup).
- **Curated thematic equity** built-ins (`Healthcare & Life Sciences`, `Energy & Commodities`, `AI Infrastructure Metals`, `AI & AI Infrastructure`) ship with a fixed ticker pool in code (`category=curated_thematic`, `monthly_validation` refresh). They are included in Scan All like any other enabled list, except **AI & AI Infrastructure**, which is a focus desk and is excluded from the post-close auto scan. See [Curated thematic equity watchlists](#curated-thematic-equity-watchlists) below.
- `screen-all` honors `screening.scan_all.mode`:
  - `union_buckets` (default): one union scan pass with per-bucket projections (faster at index-coverage scale)
  - `per_watchlist`: sequential per-list scans, each using its default preset
- The Cross-Watchlist leaderboard applies a bounded daily + weekly MTF pass after consolidation. It hydrates up to `screening.scan_all.weekly_mtf.hydrate_top_n_cap` candidates (60 by default; maximum 150), re-ranks with the canonical opportunity-score dampener, and marks unavailable weekly history as unknown rather than bearish. Use **Hide weekly counter-trend** to remove daily setups that conflict with a computed weekly trend.
- The Scan All job response reports `universe_requested`, `universe_scanned`, and `universe_truncated`. A truncated union is deliberate and visible; `union_max_tickers` is clamped to the engine hard cap.
- The browser/API Scan All workflow is the canonical consolidated workflow. `scripts/run_screen_all.py` remains a sequential operational CLI and does not produce the post-consolidate MTF leaderboard.
- `watchlist-metrics` surfaces universe coverage, overlap, hydration baselines, `scan_all_duration_seconds` p95 from `runtime_metrics`, and refresh-health fields (`builtin_refresh_invalid_ratio`, `tier1/2/3`, `format_rejected`, `ticker_health.stale_candidates`, `ticker_health.evicted_last_24h`).
- `screen-all-prewarm` bulk-loads OHLCV into the same DataCache keys the screener uses (per-ticker JSON + chunk cache, batch size 60 by default). Run after built-in refresh and before a large `screen-all` so first-run latency is mostly signal math, not Yahoo hydration. Optional: `--date YYYY-MM-DD` to match the scan as-of date.
- Screening config knobs (in `default_config.py` → `screening`): `metadata_resolve_during_scan` (`use_cached` | `refresh_stale`) and `metadata_cached_max_age_days` (default 30) reduce live Yahoo metadata calls during scans; `intraday_cache_policy` (`ttl_only` | `full_invalidate`) controls whether the scheduler hourly job wipes screening price caches.
- `screen-all-async` submits the same job the UI Scan All button does and returns a `job_id` immediately. Pass `MODE=reversal_buildup` for a reversal batch or `MODE=early_momentum` for Early momentum. Poll with `screen-all-status` or stream with `screen-all-watch`.
- On **Watchlist Screener**, **Scan All** follows **Scan mode**. Standard fills the **Opportunity** tab. Reversal buildup fills the **Reversal** tab (long vs short setups). Early momentum fills the **Momentum** tab (`score_final`). The three leaderboards do not share a rank.

### Curated thematic equity watchlists

These are **single comprehensive equity baskets** (not ETF lists). Ticker membership lives in `tradingagents/reporting/database.py` (`_builtin_watchlist_registry`). Builtin refresh validates symbols on a monthly cadence; churn caps are configured per list in `default_config.py` → `screening.scheduler.builtin_refresh`.

| Built-in name | Approx. size | Default scan preset | Role |
|---------------|-------------|---------------------|------|
| **Healthcare & Life Sciences** | 105 | *(none — per-ticker profile/preset resolution)* | One US healthcare equity universe: managed care and services, large pharma, biotech/specialty, medtech/devices, life-science tools/CROs/diagnostics, distribution, and healthcare REITs/facilities |
| **Energy & Commodities** | 25 | `commodity_cyclical` | Energy producers, oil services, and mining |
| **AI Infrastructure Metals** | 54 | `commodity_cyclical` | Metals and mining tied to AI/data-center buildout |
| **AI & AI Infrastructure** | 125 | *(none — per-ticker profile/preset resolution)* | Focus desk for the US-listed AI stack. Not on the post-close auto scan |

**Healthcare & Life Sciences** is intentionally **all-in-one** (not split into niche biotech-only or medtech-only lists). Representative symbols: payers/providers `UNH`, `ELV`, `HCA`; pharma `LLY`, `JNJ`, `ABBV`; biotech `VRTX`, `MRNA`, `ALNY`; medtech `ISRG`, `DXCM`, `PODD`; tools/CRO `TMO`, `DHR`, `IQV`; healthcare REITs `WELL`, `VTR`.

For **sector ETF** exposure (XLV, IBB, XBI, IHI, ARKG, etc.), use **ETFs - Thematic & Industry** alongside this equity list.

```bash
make screen-watchlist NAME="Healthcare & Life Sciences"
make screen-watchlist NAME="Healthcare & Life Sciences" MODE=reversal_buildup
```

On a database created **before** this list existed, restart the app (or any flow that runs watchlist seeding) so the new built-in row is inserted if the name was missing. Seeding adds missing built-in **names** only; it does not rewrite tickers on lists that already exist.

**AI & AI Infrastructure** is the equity stack: platforms, AI software, accelerators, foundry and chip equipment, memory, servers, optics, fiber, connectors, power and cooling, site construction, data-center real estate, and neocloud or powered-shell operators. It is not a miners list, not a broad utility list, and not a theme-ETF list. Five power names also sit on **AI Infrastructure Metals** (`CEG`, `VST`, `OKLO`, `SMR`, `BWXT`). The list is in `screening.scheduler.postclose.exclude_watchlist_names`, so the daily and Friday auto scans skip it. Choose it in the screener and use **Run Scan** when you want this book instead of My Watchlist. A manual Scan All still unions every enabled list, including this one.

```bash
make screen-watchlist NAME="AI & AI Infrastructure"
```

Built-in registry fields (`category`, `cadence`, `target_size`, …) are merged into `/api/watchlists` responses and `make watchlists` output even though they are not stored as DB columns — the screener groups **Healthcare & Life Sciences** under **Themes** with Energy, AI Metals, and AI & AI Infrastructure.

To smoke-test the full pipeline on this list alone (heavier than default CI smoke):

```bash
make qa-healthcare-watchlist-smoke
# or: SMOKE_WATCHLISTS="Healthcare & Life Sciences" SMOKE_BUDGET_SEC=300 make qa-screen-all-smoke
```

Legacy **Biotech Pre-Catalyst** maps to this list in `docs/migration/deprecated_watchlists_map.json` (built-in row is removed on seed; user lists are untouched).

### First-time watchlist population

Use this sequence after a fresh install or when broad index lists are still `pending_first_refresh`:

```bash
make db-backup
make ui
make builtins-refresh-propose
make builtins-refresh-proposals
make builtins-refresh-apply ID=<proposal_id>
make watchlists
make watchlist-metrics
make screen-all
make limiter-status
```

Checklist:

- Confirm `S&P 500`, `S&P 400`, `S&P 600`, and `Russell 2000` are non-zero in `make watchlists`.
- Run one baseline `make screen-all` in `per_watchlist` mode before keeping `union_buckets` as the default in production.
- If yfinance breaker trips (`make limiter-status`), wait out cooldown and rerun.
- For large universes, prefer `make screen-all-prewarm` followed by `make screen-all-async` so the synchronous CLI doesn't stall a terminal for minutes.

### Stale / delisted ticker cleanup

After the first full populate, `engine.scan()` records per-ticker fetch outcomes in `ticker_health`. Use the eviction CLI to prune dead symbols:

```bash
make evict-delisted-tickers-dry              # preview candidates
make evict-delisted-tickers EVICT_CONFIRM=1  # apply
```

Once evicted, tickers are skipped up-front by future scans (`screening.ticker_health.skip_evicted`, default on) so they don't keep burning yfinance budget. Set `screening.ticker_health.skip_evicted_within_days` (default `None` = always skip) to retry old evictions after a window — useful when a delisted symbol relists on a different venue.

Tuning knobs live under `screening.ticker_health` in `tradingagents/default_config.py` (`min_failures`, `min_days_since_success`, `track`, `auto_evict`, `skip_evicted`, `skip_evicted_within_days`).

Recommended hygiene cadence:

- After every weekly `builtins-refresh-apply`: run `make evict-delisted-tickers-dry` and apply if the candidate list is non-empty.
- `make watchlist-metrics` surfaces `ticker_health.stale_candidates` and `evicted_last_24h` — wire into your dashboard or scrape via `/api/watchlists/universe/metrics`.

### Resetting the scan-all p95 baseline after a perf fix

`/api/watchlists/universe/metrics` reports `scan_all_p95_sec` over a rolling window (default 7 days, configurable via `screening.p95_window_days`). After a major perf fix lands, regressed historical samples can keep the metric pinned high until they age out. To force convergence:

```bash
# Preview which samples would be removed (no-op)
make scan-p95-reset-dry                       # default: keep latest 1 sample

# Apply: keep N most recent samples for scan_all_duration_seconds
make scan-p95-reset PRUNE_CONFIRM=1 KEEP_NEWEST=1

# Or trim by age across all metric keys
make scan-metrics-prune-dry PRUNE_DAYS=14
make scan-metrics-prune PRUNE_CONFIRM=1 PRUNE_DAYS=14
```

Use `P95_METRIC=` and `PRUNE_METRIC=` to target specific keys (e.g. `expanded_scan_duration_seconds`). The underlying data is in `runtime_metrics` — these commands only delete duration samples, not refresh telemetry rows.

### Reading the scan-all output (ranking model v2)

The consolidated scan-all view shown in the UI and at `GET /api/screen-all/latest` blends four score dimensions:

| Column                 | Range  | Where it comes from                                                                                         |
| ---------------------- | ------ | ----------------------------------------------------------------------------------------------------------- |
| `composite_score`      | 0–100  | Weighted sum of screening signals using the active preset's weights (display-only). RSI column is oversold; overbought is direction-only. |
| `composite_fundamental`| 0–100  | Composite recomputed without the six EQ-overlap signals (RSI, BB squeeze, MA cross, trend, volume surge)    |
| `entry_quality`        | 0–100  | Technical setup score (mean reversion, RSI sweet spot, Bollinger position, volatility contraction, MACD phase, volume dryup) |
| `macro_fit`            | 0–100  | 3-layer overlay: market env × 0.30 + sector momentum × 0.40 + profile regime fit × 0.30                    |
| `opportunity_score`    | 0–100  | Blended score: 0.55 × composite_fundamental + 0.30 × entry_quality + 0.15 × macro_fit, with graduated penalties + regime-weighted macro |
| `direction`            | enum   | `bullish`, `bearish`, or `neutral` — neutral when weighted polarity magnitude is below `direction_conviction_threshold` (default 0.05) |

Operators should prefer `opportunity_score` over `composite_score` for cross-watchlist comparisons — it accounts for entry quality and the current regime. `composite_fundamental` is surfaced so you can tell whether a ticker's Composite is driven by fundamentals/catalyst signals or by price-action overlap with Entry Quality.

Key diagnostics in the consolidated response (`/api/screen-all/latest`):

- `watchlist_breadth` — number of distinct lists the ticker appeared in this batch (breadth-of-conviction)
- `batch_percentile` — rank within today's cohort (complements `percentile` which is the rolling 10-run baseline)
- `opportunity_z` — per-watchlist z-score used for dedup (prevents preset-shopping bias; not the optional within-sector signal z-score)
- `resolved_preset` — weights actually used on the row (union Scan All Book filter)
- `factor_scorecard` — Value / Quality / Flow / Income / Momentum / Revisions / Catalyst / Risk / Macro-Fit (`null` = no data)

**Operator books** (Run Scan dropdown + Cross-Watchlist Book filter): Default, Momentum Hunter, Value Fisher, Earnings Play, Smart Money Tracker, Commodity Cyclical, ETF Technical, Long Horizon 12–36m, Dividend Income, Short Squeeze, Quality Compounder. Hidden aliases (`small_cap_growth`, `long_horizon_6to12m`, movers presets) still resolve for stored watchlist defaults and Scan All.

**Discover Tickers** (Screener card, requires `PERPLEXITY_API_KEY`) is a hopper, not a ranked opportunity board. For early momentum, use the cap-split chips — **Micro momentum** ($25M–$300M), **Small-cap momentum** ($500M–$2B), **Mid-cap momentum** ($2B–$8B) — then add New names to a list and Run Scan with Momentum hunter. Discover cannot see the tape; a single “under $10B” chip will hug mid-caps. Ranged chips also reject names sitting on the top 10% of the band. Momentum chips drop funds/REITs/CEFs, liquid household names (SNAP, AMD, SMCI, …), and names that already doubled over 52 weeks or sit on a 52-week high after a large run. Other chips fill book-aligned themes (AI infra, earnings window, smart money, squeeze, factor ETFs, …). Results are gated on listing type, US exchange / OTC, market cap, and book-fit (household mega-caps on growth/flow chips; yield floor plus token-dividend block on Dividend growers; leveraged/inverse ETFs on the ETF chip), then flagged **New** vs **On desk**. Do not treat the Valid/Listing column as a buy signal. Scheduler auto-discovery uses the same canonical books.

- `sector_relative_score` — opportunity_score minus batch sector median (spot outperformers inside a weak sector)
- `stratified_top_opportunities.{mega_large,mid,small_micro}` — market-cap stratified best picks
- `short_candidates` — lowest-opp rows explicitly labeled `bearish` (actual short candidates, not just mechanical bottom-N)
- `laggards` — bottom-N by opp regardless of direction (the old `weakest_signals` field is kept as an alias)

**Liquidity policy (`screening.liquidity_policy`):**

| Key | Default | Units | Used by |
|-----|---------|-------|---------|
| `scan_all_min_dollar_adv_usd` | 2,000,000 | USD average daily dollar volume | Scan All post-process gate/penalty |
| `long_horizon_min_avg_volume_shares` | 750,000 | shares/day | Long-horizon workflow |
| `movers_min_avg_volume_shares` | 500,000 | shares/day | Movers intelligence |

Scan All uses **dollar ADV**; long-horizon and movers use **share volume** — do not compare the numeric thresholds directly. Mode is controlled by `screening.liquidity_gate_mode`: `warn` | `penalize` (default) | `exclude`.

**Adaptive weights:** The scheduler's Monday `auto_adaptive_weights` job computes optimized weights from backtested returns and stores them for the "AI Optimize" button. Scan All ranking is unchanged until `screening.adaptive_blend.enabled` is set to `true` (optional 25% blend with preset weights when `min_samples` met).

Three rules of thumb:

1. A row with `direction: neutral` and `opportunity_score` in the 40s is not a short candidate. Use `short_candidates` for bearish-conviction names.
2. If `opportunity_score` for two rows differs by ≤ 2 points, treat them as tied — the graduated penalties intentionally compress scores near the old cliff thresholds.
3. When macro_fit is at the tails (< 30 or > 70), expect opportunity_score to move more than the static 0.15 macro weight would suggest — this is the regime-weighted blend doing its job.

### Reading the Risk column

`risk_score` is a **separate dimension**, not a demerit applied to opportunity. It answers "how risky is this ticker, independent of whether it's a good opportunity today?" so you can combine the two yourself (e.g., sort by opp desc, then filter `risk_score <= 40` for a risk-off book).

| Risk band | Color   | Reading |
|-----------|---------|---------|
| 0–35      | Green   | Low risk (stable price, liquid, low leverage / short interest, diversified) |
| 35–60     | Amber   | Moderate risk (typical single equity) |
| 60–100    | Red     | Elevated risk (high vol, deep drawdown, illiquid, levered, or shorted) |

Hovering the Risk cell in the UI shows the **component breakdown** (volatility, drawdown, liquidity, beta deviation, leverage, profitability, short pressure, asset-class baseline) so you can see *why* a ticker is flagged risky — a name scoring 70 from high volatility alone reads very differently from one scoring 70 from leverage + short pressure.

**ETF caveat.** ETFs get `risk_score` based only on price-action + liquidity + beta + a baseline of 20 (vs. 50 for equities). The leverage / profitability / short-pressure components are intentionally skipped — yfinance's values for those fields on pooled vehicles are misleading (the SPY "debtToEquity" field is the basket's weighted average, not SPY's leverage). A low ETF risk_score means low *market risk*, not zero structural / counterparty risk.

**ADR caveat.** ADRs carry the full equity component set, but the `asset_class_baseline` component adds 40 (vs. 50 for domestic equities) to reflect FX + geopolitical overlay. Expect ADRs to score ~2 points lower on the baseline component than comparable US equities, which is the right direction — a Chinese or Russian ADR has *more* structural risk than a US peer, but the baseline floor captures that (not volatility alone).

### Asset-class badges in the UI

Next to each ticker symbol you'll see a small chip for non-equity rows:

- **ETF** (blue) — routes through the `etf_technical` preset, skips fundamentals-based signals (analyst targets, estimate revisions, insider buying, valuation gap, rating changes, PEAD — all zero-weighted). Composite scores for ETFs therefore reflect pure price action + relative strength.
- **ADR** (purple) — routes through the normal equity decision tree. The badge is a reminder that analyst estimates may lag (foreign coverage) and that FX moves can show up as an "execution" component inside the risk score.
- (no badge) — domestic equity. Uses the sector/market-cap-appropriate preset (high_growth, value_fisher, dividend_income, momentum_speculative, commodity_cyclical, or large_cap_core).

### Optional automation (off by default)

These jobs exist in the scheduler but are **not** enabled for unattended operation unless you opt in via `default_config.py` or Settings:

| Job | Config path | Default | Notes |
|-----|-------------|---------|-------|
| Theme auto-discovery | `screening.scheduler.auto_discovery.enabled` | `false` | Requires Perplexity budget; generates thematic watchlists |
| Movers post-close auto-scan | `screening.movers.auto_scan_postclose_enabled` | `false` | Movers remain manual-first (`manual_only=true`) |
| Adaptive weight blend in scans | `screening.adaptive_blend.enabled` | `false` | Weights job runs Monday; blend must be enabled separately |
| Scan All auto-analyze | `screening.scheduler.auto_analyze.enabled` | `false` | Each analysis ~5 min — keep `top_n` low |

Check **Settings → Optional Automation** or `GET /api/screening/scheduler/status` for last-run timestamps.

**Post-close watchlist policy (reduces redundancy):** Scheduled scans do **not** dedupe tickers across lists — each list is a separate `engine.scan()`. Overlap is trimmed by config instead:

- `screening.scheduler.postclose.exclude_watchlist_names` — never auto-scan these lists.
- `screening.scheduler.postclose.weekly_watchlist_names` + `weekly_run_day` (default **friday**) — full indices, legacy Top-N duplicates, and ETF research baskets run once per week, not daily.
- `screening.scheduler.postclose.priority_watchlist_names` — run first when the post-close time budget is tight (thematic desks + NASDAQ 100, etc.).
- `screening.scheduler.postclose.include_user_watchlist_names` — daily post-close/pre-market scans for named **user** lists (default: **My Watchlist**). Skipped when empty. Other custom lists are not auto-scanned unless you add their exact names here.

Defaults keep **My Watchlist** (when it has tickers), **Healthcare & Life Sciences**, thematic metals/energy, **Sector ETFs**, **NASDAQ 100**, **Dividend Aristocrats**, and **ADRs** on the **daily** path; **S&P / Russell full + Top-100 pairs** and **ETF research** lists move to **Friday** only. For ticker-level dedup and a single desk view, use **Scan All** (union mode), not post-close.

`GET /api/screening/scheduler/status` includes `postclose_policy.eligible_today` so you can see what tonight’s job will scan.

---

## 4. Movers Intelligence (manual-first)

```bash
make movers-scan TOP=25 INCLUDE_LOSERS=true
make movers-queue-top N=5 HORIZON=short
make movers-alerts HORIZON=medium TOP=5
```

Notes:

- `HORIZON` is `short` or `medium`
- Movers scheduler automation is disabled by default when `screening.movers.manual_only=true`

---

## 5. Long-Horizon Institutional Workflow (manual-first)

```bash
make long-horizon-prewarm PREWARM_MAX=600 PREWARM_SKIP_INFO=false
make long-horizon-scan LH_HORIZON=long_6to12m TOP=30 LH_USE_EXPANDED=true
make long-horizon-scan LH_HORIZON=long_6to12m TICKERS="AAPL MSFT NVDA" TOP=30 LH_USE_EXPANDED=false
make long-horizon-status
make long-horizon-runs
make long-horizon-underwrite RUN_ID=123 TOP=10
make long-horizon-allocate RUN_ID=123
make portfolio-risk RUN_ID=123
```

Notes:

- `LH_HORIZON` is `long_6to12m` or `long_12to36m`
- `LH_USE_EXPANDED=true` (default) allows scan without `WL_ID` or `TICKERS`
- `LH_USE_EXPANDED=false` requires either `WL_ID` or `TICKERS`
- `long-horizon-prewarm` warms expanded-universe metadata/info caches before large scans
- Long-horizon automation stays off by default (`screening.long_horizon.manual_only=true`)
- Universe policy excludes leveraged/inverse names when `exclude_leveraged=true`
- Allocation policy supports hard + soft constraints, including:
  - `market_cap_band_min.large_or_above`
  - `market_cap_band_max.small_or_below`
  - `relaxation_order` and `hard_constraints`
- Underwriting runs with bounded concurrency and shared timeout/token budgets
- Run metadata regime snapshot includes macro state + sector momentum fields
- `portfolio-risk RUN_ID=` prints the portfolio risk summary computed post-allocation: weighted beta, sector concentration (HHI + bars), factor exposures by family, and top factor badge. Requires the webapp server (`make ui`) and a completed allocation run.

Recommended for large expanded runs:

1. `make long-horizon-prewarm PREWARM_MAX=600`
2. `make long-horizon-scan LH_HORIZON=long_6to12m TOP=30 LH_USE_EXPANDED=true`
3. `make long-horizon-underwrite RUN_ID=<run_id> TOP=10`
4. `make long-horizon-allocate RUN_ID=<run_id>`
5. `make portfolio-risk RUN_ID=<run_id>`

---

## 6. Alerts, Backtesting, and Calibration

```bash
make alerts
make backtest
make backtest-report
make calibration-report
make calibration-by-decision
make calibration-by-factor
make reverse-dcf-accuracy
make calibration-all
make stabilization-gate
```

### Calibration commands

| Command | What it shows |
|---|---|
| `make calibration-report` | Global confidence-accuracy calibration curve across all backtested runs |
| `make calibration-by-decision` | Calibration sliced by BUY / HOLD / SELL — detects LLM decision-class over/underconfidence |
| `make calibration-by-factor` | Calibration sliced by the dominant factor family (Value, Quality, Momentum, Revisions, Risk, Macro-Fit) |
| `make reverse-dcf-accuracy` | Implied-growth vs. consensus error distribution — measures how accurate the reverse-DCF implied growth rates are relative to analyst estimates |
| `make calibration-all` | Runs `calibration-report` + all three sliced calibration commands in sequence |

All calibration commands run directly against `research.db`; no web server required.

### Analysis backtest metric contract

`/backtest`, `GET /api/backtest/stats`, dashboard backtest cards, `make backtest`, and `make backtest-report` share `tradingagents/backtesting/metrics.py`.

| KPI | Meaning |
|---|---|
| Signed return / signed alpha | BUY keeps stored `r` / `alpha`; SELL flips the sign; **HOLD is excluded** from the mean (not 0-filled) |
| 7d directional accuracy | BUY+SELL only: sign of that horizon’s stored return matches the call. `r == 0` is not a hit |
| HOLD accuracy | `\|r\| <` 2%/3%/5% at 7/14/30d — shown separately |
| Win rate | share of **signed** 7d returns `> 0` (not the same as accuracy or stored `was_correct`) |
| 7d return/vol | `mean / stdev` of signed 7d only, `n ≥ 10`. Not Sharpe/Sortino; not annualized |
| Tape return / tape alpha | unsigned close-to-close stock move and `stock − SPY` (SPY is uncosted; round-trip bps hit the stock only) |

Cards are **all stored outcomes**, not the Run Backtest **Max Analyses** cap. Calibration (`make calibration-report` and slices) still uses mixed-horizon stored `was_correct` until a follow-up. Researcher panels score explicit `Decision:` / `SIGNAL_JSON` on debate text vs 7d tape and skip unparsed rows.

Screening-run backtest (`make screen-backtest`) is a separate unsigned correlation/hit-rate surface.

### Stabilization gate

`make stabilization-gate` covers movers, refresh, long-horizon, watchlist-universe, TradingView export, yfinance-limiter, signal pipeline, and institutional parity regression checks, plus a `qa-screen-all-smoke` wall-clock-budgeted Sector ETFs + NASDAQ 100 scan (`SMOKE_BUDGET_SEC`, default 180s). Override the targets via `SMOKE_WATCHLISTS="A,B"` if you want the smoke to exercise a different list (missing watchlists are skipped gracefully so a bare DB still passes the gate).

`make qa-report-tests` runs the full `tests/` suite via pytest (falls back to `unittest discover` if pytest is unavailable).

### Alerts — Catalyst Approaching

The `catalyst_approaching` alert type fires when an ex-dividend date or stock split is within a configurable threshold (default 14 days). Add it from the Alerts page using the **Catalyst Approaching** quick template. The `days_before` condition is configurable per rule.

---

## 7. Database, Backups, Cleanup

```bash
make db-info
make db-backup
make db-vacuum
make db-cleanup-backups KEEP=5
make storage-report
make clean-old DAYS=90
make clean-logs
```

Recommended maintenance order:

1. `make db-backup`
2. `make storage-report`
3. `make clean-old DAYS=90`
4. `make db-cleanup-backups KEEP=5`
5. `make storage-report`

---

## 8. Screener Expand Panel

Clicking any ticker row in the screener expands a detail panel with the following sections:

| Section | Contents |
|---|---|
| **Reversal buildup** | Phase, side, score, RSI / MACD hist / 60d position / 10d up-volume, reason chips (reversal-mode scans) |
| **DCF Fair Value** | Fair value, margin of safety, implied growth rate, delta vs. consensus, 3×3 WACC × terminal-growth sensitivity grid |
| **Scenario Targets** | Bull / Base / Bear price targets with blended (probability-weighted) fair value |
| **Institutional Grid** | 4-quadrant table: earnings profile, sector medians, insider activity, analyst ratings |
| **Factor Scorecard** | 6-family bar chart: Value, Quality, Momentum, Revisions, Risk, Macro-Fit (each 0-100) |
| **Peer Comparables** | P/E, P/S, EV/EBITDA vs. up to 8 sector peers, with median row and percentile rank |
| **Upcoming Events** | Next 8 events from `event_calendar` within 90 days, color-coded by urgency (< 7 days = red, < 14 = amber) |

### Reading the Factor Scorecard

The scorecard translates 16 raw signals into 6 institutional-grade factor families for faster mental model:

- **Value** — how cheap is this vs. intrinsic value and analyst targets?
- **Quality** — earnings quality and insider conviction
- **Momentum** — price trend, RSI positioning, and relative strength
- **Revisions** — direction of analyst estimate and rating changes
- **Risk** — inverted risk score (higher = lower risk)
- **Macro-Fit** — regime alignment across market environment, sector momentum, and risk profile

All six are 0–100; green ≥ 65, amber 35–65, red ≤ 35.

### Reversal buildup mode

On **Watchlist Screener** (`/screener`), set **Scan mode** to **Reversal buildup** and run a single watchlist. The table ranks names that were recently oversold/overbought and are starting to turn (`early_turn`, `confirmed`). `watching` / `late` / `none` are hidden by default; illiquid and earnings-blackout filters default ON.

Phases:

- **Watching** — dislocation only, a turn that already left the setup zone, a fade with almost no RSI travel, or a short still rising on weekly RSI
- **Early turn** — RSI/MACD histogram recovering while still in the 60-day quartile, with at least 5 RSI points of travel
- **Confirmed** — SMA reclaim, volume bias, or 10-day break while still near the extreme (longs pos60 ≤ 0.35, shorts ≥ 0.65)
- **Late** — already stretched; hidden by default

CLI: `make screen-watchlist NAME="NASDAQ 100" MODE=reversal_buildup`

Single-list CLI runs persist when you pass `--id` or `--name`. Cross-watchlist reversal lives on the **Reversal** tab after `Scan All` with Scan mode = Reversal buildup (or `make screen-all-async MODE=reversal_buildup`). This mode is **not** auto-run by the post-close scheduler. The Opportunity Scan All board still excludes reversal runs.

### Early momentum mode

On **Watchlist Screener**, set **Scan mode** to **Early momentum** and Scan All (or Run Scan on one list). The **Momentum** tab ranks by `score_final` after a bounded enrich pass. This lens uses **EOD closes only** — an incomplete regular-session bar does not count as a confirmed breakout. Intraday chase is out of scope.

CLI: `make screen-all-async MODE=early_momentum`

### Base mode

On **Watchlist Screener**, set **Scan mode** to **Base**. This is a pre-break watchlist: names still inside a tight range, ranked by a coil score. It does not call direction and does not change Opportunity weights or Early Momentum `score_final`.

- **Base** — still inside the prior 20-session range, with a real multi-session box and an intact trend (rising 50-day, or 20-session excess versus SPY). A close through either edge drops off.
- **Early momentum** — post-break. A **Base** badge on a momentum row means the break was preceded by a qualified coil. That flag does not change the Mom rank.
- **Entry** — whether the current print is extended. It stays the pullback timer on every lens.

CLI: `make screen-watchlist NAME="NASDAQ 100" MODE=base_coil` or `make screen-all-async MODE=base_coil`. Single-list Base runs stay in Screening History. The Cross-Watchlist **Base** tab shows Scan All plus fresher overlays only.

### All modes (one pass)

On **Watchlist Screener**, set **Scan mode** to **All modes (one pass)** and use **Run Scan** on a watchlist. One OHLCV hydration scores Opportunity (composite), Reversal, Early momentum, and Base on every ticker. The Results table names a lead only when a sleeve clears the same bar as its desk. A specialized desk keeps the badge when it clears: reversal, then momentum, then base, then opportunity. Other desks that also cleared show under the badge. Reversal leads on early-turn or confirmed, and the badge reads reversal · long or reversal · short. Direction stays the opportunity bias and is separate from that side. Rows are grouped by that desk and ranked by the native score inside it. Lead %ile is the rank inside the winning desk, not a quality grade. A dash in a sleeve percent means that name did not qualify. Location sits beside Entry: pullback, extended, or at target. Entry remains the timing shape. Momentum leads at 65 or better (Confirmed). Opportunity leads when Entry is at least 60, the Actionable preset. Base leads only when more than one name is on the board. A row that clears none of those bars has no lead and sorts last. Percentiles appear once at least eight names qualify for that sleeve. There is no blended super-score. Opening a saved run applies these rules without a rescan.

- **Not** on Cross-Watchlist / Scan All in v1 — Scan All still runs one lens at a time.
- Screening History **View** loads the meta table; the History **Backtest** button still ranks by stored `composite_score`, which may differ from the All modes table order.
- The All modes table can hide direction conflicts and names with no eligible sleeve. Both toggles start off. The lead cell shows the winning sleeve’s native score. Expanding a row summarizes all four sleeves.
- Ticker-only Run Scan (no watchlist) embeds lifted rows in the job payload like other non-standard modes.

---

## 9. Operational Guardrails

### Manual-only defaults

- `screening.movers.manual_only=true`
- `screening.long_horizon.manual_only=true`

### Sign-off checklist before enabling stricter automation

- Run `make stabilization-gate`
- Verify movers scan and queue/alerts guard behavior
- Verify long-horizon scan, metadata hashes, underwrite, allocate
- Confirm non-strategy run guards reject invalid run IDs

---

## 10. Troubleshooting

### UI is up but actions fail

1. Run `make health`
2. Tail logs: `make logs`
3. Confirm `.env` keys are present (`OPENAI_API_KEY`, `FINNHUB_API_KEY`)

### Long-horizon scan fails with universe error

- If `LH_USE_EXPANDED=false`, provide `WL_ID` or `TICKERS`
- If `LH_USE_EXPANDED=true`, verify expanded sources/prefix filters and prewarm settings
- Ensure symbols are valid and liquid enough for policy thresholds
- Check exclusion reasons in run output (`excluded[].reason_codes`) for:
  - `excluded_leveraged`
  - `excluded_etf`, `excluded_otc`, `excluded_adr`
  - `price_below_floor`, `market_cap_below_floor`, `liquidity_below_floor`

### Long-horizon allocation looks too defensive

- Inspect allocation `reason_codes` for constraint/relaxation effects:
  - `clipped_single_name_cap`, `clipped_sector_cap`, `clipped_market_cap_band`
  - `enforced_market_cap_band_min`, `market_cap_band_min_unmet`
  - `relaxed_beta_min`, `relaxed_sector_soft_cap`
- Tune policy in `screening.long_horizon.allocation`:
  - `max_single_name_weight`, `max_sector_weight`
  - `market_cap_band_min`, `market_cap_band_max`
  - `allow_relaxations`, `relaxation_order`, `hard_constraints`

### SQLite lock contention

- Wait for current write-heavy job to finish
- Avoid overlapping manual scans and heavy cleanup at the same time

### yfinance "Too Many Requests" / 429 errors

- The alert monitor and screening fetch paths share a process-wide rate-limit breaker.
- Run `make limiter-status`:
  - `ok`: breaker closed, fetches proceed normally
  - `rate_limited:Ns`: breaker tripped; cycles are skipped for the remaining cooldown (5m → 30m exponential backoff)
- The alert monitor logs a single aggregated WARN per cycle (`Cycle rate-limited by yfinance: N ticker skips`) instead of per-ticker ERRORs.
- `engine.scan()` re-checks the breaker before every OHLCV chunk; tickers skipped in this state are marked `deferred_breaker_open` and *do not* count as ticker-health failures.
- If trips are frequent, reduce alert polling frequency or screening concurrency; caching (90-day history) is already in place for the alert path.
- `make health` returns `checks.yfinance_limiter` plus `yfinance_limiter_total_trips` for session-level tracking.

### Scan-all runs forever or produces no results

1. Run `make screen-all-prewarm` first — most "stuck" scans are cold OHLCV caches, not signal-math bottlenecks.
2. Submit asynchronously with `make screen-all-async` and poll `make screen-all-status` so the CLI returns immediately.
3. Inspect `make watchlist-metrics` for `refresh_health.invalid_ratio`, `format_rejected`, and `ticker_health.stale_candidates` — high values indicate symbol hygiene issues that `make evict-delisted-tickers-dry` can surface.
4. If `/health/ready` reports `ticker_health_stale_candidates` > 100, run the eviction CLI to prune dead rows before the next scan.

---

## 11. Where to Look Next

- System overview: `README.md`
- Architecture details: `docs/ARCHITECTURE.md`
- Release history: `docs/CHANGELOG.md`

