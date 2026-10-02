# Verification Results — Post-Fix Pass (2026-08-06)

Script: [`intraday.pine`](intraday.pine)  
Against: plan + prior Post-Implementation QA (`feedback.md`)

## Code fixes applied (this pass)

| Issue | Fix | Evidence |
|-------|-----|----------|
| CRITICAL HTF tuple / `ta.vwap` | `f_htfBiasConfirmed()` returns `[bull[1], bear[1]]`; uses `f_rthSessionVwap()`; `request.security(..., f_htfBiasConfirmed(), lookahead_on)` | `intraday.pine` HTF block (~493–501); `f_rthSessionVwap` (~203–214) |
| SIGNIFICANT ADR short sessions | ADR push requires `sessionMergeEligible and scratchDirty and f_scratchComplete()` | ADR block (~416–418) |
| SIGNIFICANT dual lower_tf on 1m | Single UDT `LtfBar` call, **only when `chartMinutes > 1`** | `type LtfBar` (top); `ltfBars = chartMinutes > 1 ? request.security_lower_tf(...)` (~352–359) |
| SIGNIFICANT ETH `close[1]` gap/PDC | Persist PDH/PDL/PDC at `sessionEnd` via `lastRthClose` | (~163–170), (~196) |
| ADVISORY optional request budget | Internals / leadership `request.security` gated on toggles | (~503–520) |

## Static compile readiness (repo)

Cannot execute TradingView’s Pine compiler from this environment. Static gates checked:

- [x] No `strategy(`; `//@version=6` + `indicator(`
- [x] No `f_htfBias()[1]` anti-pattern; no HTF `ta.vwap`
- [x] Grep: only one `security_lower_tf` site; conditional on `chartMinutes > 1`
- [x] ADR and TOD RVOL share `f_scratchComplete()`
- [x] `request.*` count when defaults off: overnight + HTF (+ lower_tf only if TF>1m) ≈ 2–3; with optionals on: +TICK +ADD +SPY +QQQ

## TradingView compile + replay (operator checklist)

Paste `intraday.pine` into TradingView Pine Editor and record results below.

### Compile

| Step | Result (fill) | Notes |
|------|---------------|-------|
| Add to SPY 5m | ☐ pass / ☐ fail | |
| No red compile errors | ☐ pass / ☐ fail | |
| Status table visible | ☐ pass / ☐ fail | |
| 10m + OR15 → `OR=disabled` | ☐ pass / ☐ fail | |
| Change N → `RVOL=warming` | ☐ pass / ☐ fail | |

### Multi-TF smoke (same session)

| TF | OR lock | HTF label | todRvol vs 1m | Result |
|----|---------|-----------|---------------|--------|
| 1m | ☐ | HTF=5 ☐ | baseline ☐ | ☐ |
| 5m | ☐ | HTF=15 ☐ | match clock ☐ | ☐ |
| 15m | ☐ | HTF=60 ☐ | match ☐ | ☐ |
| 30m | ☐ | HTF=60 ☐ | match ☐ | ☐ |

### Five-session replay

| # | Type | Pass? | Notes |
|---|------|-------|-------|
| 1 | Balance | ☐ | No OR continuation in Balanced |
| 2 | Trend | ☐ | Continuation only with trend |
| 3 | Gap open | ☐ | Gap bucket + RSI on band MR |
| 4 | Lunch chop | ☐ | Transition → zero setups |
| 5 | Event / 2nd | ☐ | Cooldown / one-per-level |

### Governance asserts

- [ ] No continuation in Balanced
- [ ] No mean-reversion in Trend
- [ ] Zero setups in Transition
- [ ] RVOL warming blocks volume-gated alerts
- [ ] ONH/ONL unavailable ≠ RTH fake
- [ ] Target1 on profit side or ATR-fallback / stop-too-wide

---

**Repo status after code fix:** ship-blocking CRITICAL/SIGNIFICANT code defects addressed.  
**Remaining for full ship:** operator must tick TradingView compile + replay tables above (runtime evidence).

---

## Impulse de-spam implementation (2026-08-06)

| Contract item | Status | Evidence |
|---------------|--------|----------|
| TF-safe `impulseEntryBars` | done | helpers ~156–168 |
| Leg latch + extremes | done | Impulse block ~817–879 |
| Re-arm K/R + VWAP cross | done | `f_adaptiveImpulseRearmCloses/Atr`; re-arm if ~857–871 |
| Session latch clear | done | `sessionStart` block |
| Opposite clears other latch | done | fire handlers ~914+ |
| Remove early markers/alerts | done | no `Impulse_Early_*`; single IMP plotshapes |
| Remove Impulse cooldown | done | no `lastImpulseBar` |
| Risk refs on entry | done | `f_calcRisk` on `impulseLongAlert`/`impulseShortAlert` |

**TradingView compile + July 31 / reversal / choppy replay:** operator-only (blank ticks in checklist). Static contract verified in repo.
