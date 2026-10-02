# Feature audit — reference baselines (TradingAgents)

Use these as **starting** expectation rows. Adapt to the focused surface; delete N/A rows rather than forcing a Pass.

## Screening — single watchlist / screener UI

| Expectation |
|-------------|
| Run mode (Opportunity vs Reversal) isolated; criteria.strategy matches lens |
| Rank order matches lens sort key (reversal: phase band then score; opportunity: composite/sleeve rules) |
| Default phase filter documented and matches product intent |
| Show selector / scroll / sticky header behave for large result sets |
| Illiquid / blackout filters applied consistently server + client |
| `_reversal_buildup` or opportunity fields lifted on API read paths |
| Synthetic tests cover phase gates and sort keys (`tests/test_reversal_buildup.py`) |

## Cross-watchlist board

| Expectation |
|-------------|
| Latest **matching-lens** run per watchlist (not latest-any then filter) |
| Overlay vs single vs scan_all badges distinct |
| Opportunity and Reversal lenses do not mix ranks |
| Age window / `board_max_age_days` honored |
| 404 / empty states explain missing runs clearly |
| Consolidator uses `reversal_sort_key` for reversal rows |

## Scan All / MTF

| Expectation |
|-------------|
| Breaker / rate-limit guards on yfinance batch fetch |
| Weekly MTF overlay optional and config-gated |
| Percentile / z-score cross-section applied once, not double-counted |
| Persisted strategy metadata matches run mode |
| `tests/test_scan_all_mtf.py` covers contract regressions |

## Reversal buildup classifier

| Expectation |
|-------------|
| Phase gates: hist-turn required for confirm; volume-without-turn → watching |
| Recency is penalty not floor; stale_extreme tagged |
| Overlay demotes (risk, weak composite, lens conflict) do not mix opportunity score |
| Config tunables in `default_config.py` and module defaults stay in sync |
| Feature payload includes short-side fields when applicable |

## Movers / long horizon / discovery

| Expectation |
|-------------|
| Strategy excluded from wrong Scan All unions when configured |
| Scheduler locks / lineage per horizon where documented |
| API endpoints return stable JSON shapes for UI |
| Empty / error paths do not silently return stale leaderboard data |

## Webapp API + templates

| Expectation |
|-------------|
| Route handlers validate inputs; 4xx on bad watchlist/run ids |
| Enrichment does not resort rows unless documented |
| Settings persisted in `webapp/settings.json` or DB as designed |
| Report/analyze pages render embedded CSS without layout bleed |

## Analysis / report pipeline

| Expectation |
|-------------|
| Agent graph completes with signal JSON guardrails |
| Report QC / context QC hooks run on output |
| Golden HTML fixtures updated only when rendering contract intentionally changes |
| PDF/export paths do not drop decision scorecard fields |

## Cross-cutting honesty checks

| Check |
|-------|
| Docs saying “latest per watchlist” / “lens-aware” match `webapp/app.py` + `database.py` |
| Makefile targets referenced in docs actually exist |
| No Pass on live `research.db` claims without query evidence or explicit Partial |
| No live Yahoo in unit tests |
