# Upstream 0.5.2 port backlog (focused slice)

Cherry-picked themes from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) without merging `origin/main`.

| Item | Status | Notes |
|------|--------|--------|
| Analyst tool-round cap (#1420) | done | `analyst_finalize.py`, `conditional_logic`, `analysis.max_tool_rounds` |
| Vendor unavailable semantics (#1386) | done | `vendor_errors.py`, `route_to_vendor`, yfinance limiter paths |
| Historical run integrity (0.5.0+) | done | `run_date.py`, historical bootstrap, cache keys, live-only gates |
| Rating label parsing (#1383, #1435) | done | `rating.py`, risk manager prompt, `final_rating`, scorecard |
| Sqlite checkpoint hygiene | done | `checkpointer.py`, CLI + `propagate` scope, default off |

Remaining upstream (not in this slice): full portfolio/crypto graph split, 5-tier rating enum everywhere, memory as-of filtering (#1251), and other 0.5.x UI/API deltas.
