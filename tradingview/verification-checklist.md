# SPY/QQQ Intraday Indicator — Verification Checklist

Script: [`intraday.pine`](intraday.pine)  
Plan: SPY/QQQ Intraday Indicator Plan (Build Ready)

## Static verification (completed in repo)

- [x] Pine v6 `indicator` (not strategy); ~915 lines; no `break`/`strategy()`
- [x] Module order: TF/OR matrix → references → TOD RVOL ring buffer → regime → signals → cooldowns → risk refs → table/alerts
- [x] TOD RVOL: canonical 1m path (`security_lower_tf` volume+time on TF>1m); keys `1..390`; rolling `matrix(391,N)` eviction; `N` bounded 5..255
- [x] Regime eligibility: `confirmedRthBars >= minRegimeBars and elapsedRthMinutes >= minRegimeMinutes` (not OR-gated)
- [x] Transition = no setup alerts; Trend = continuation only; Balanced = mean-reversion only
- [x] OR families disabled on invalid OR / 45–60m; VWAP families remain regime-gated
- [x] RSI extremes default on mean reversion; optional pivot divergence
- [x] Directional target guard + ATR fallback; stop-too-wide note
- [x] Status table + footer disclaimer `Regime = realized price/volume only`
- [x] All alerts use confirmed-bar booleans; 8 stable `alertcondition` ids
- [x] Optional internals / leadership / AVWAP off by default
- [x] `request.*` budget: 8 unique calls (overnight, 1m vol, 1m time, HTF, TICK, ADD, SPY, QQQ) — under 40

## TradingView compile (run manually)

1. Open TradingView Pine Editor → paste `intraday.pine` → **Save** / **Add to chart** on **SPY 5m**.
2. Confirm: no compile errors; status table visible; VWAP + OR plot.
3. Change OR to 15 on a **10m** chart → table shows `OR=disabled (TF mismatch)` and suggests OR30.
4. Change TOD RVOL lookback `N` → expect fresh `RVOL=warming` after recalc.

## Multi-TF smoke (same session, SPY or QQQ)

| TF | Check |
|----|--------|
| 1m | OR lock after OR minutes; HTF=5; holds≈3; `todRvol` populates after warm-up |
| 5m | OR lock; HTF=15; `todRvol` at clock minute ≈ 1m chart value |
| 15m | Single-bar OR15 locks at first RTH bar close; HTF=60 |
| 30m | OR30 only; HTF=60; adaptive hold 0–1 |

## Five-session replay checklist

| # | Session type | Pass criteria |
|---|--------------|---------------|
| 1 | Ordinary / balance | Balanced regime appears; mean-rev only (no OR continuation) |
| 2 | Clear trend | Trend Up/Down; OR/VWAP pullback only in trend direction |
| 3 | Gap / news open | Gap bucket matches open vs prior close; RSI required on band MR |
| 4 | Lunch chop | Transition / Compression or Chop; **zero** setup alerts while Transition |
| 5 | Event or 2nd trend/balance | Same governance; no alert spam (cooldown / one-per-level) |

## Governance asserts

- [ ] No continuation alerts in Balanced
- [ ] No mean-reversion alerts in Trend
- [ ] Zero setup alerts in Transition
- [ ] `RVOL=warming` blocks volume-gated alerts
- [ ] ONH/ONL unavailable does **not** substitute RTH high/low
- [ ] Risk Target1 always on profit side of entry (or `Target=ATR-fallback` / stop-too-wide)

## Notes

TradingView replay cannot be executed from this repo environment. After the manual compile + checklist above, mark validate-replay done in your workflow tracker.

---

## Impulse de-spam (2026-08-06) — operator checklist

Plan: `de-spam-impulse-markers` (one entry-quality marker per directional leg)

### Static (completed in repo)

- [x] `impulseEntryBars = earlyEff > 0 ? earlyEff : barsReq` (15m/30m entry at `barsReq`)
- [x] Leg latch + re-arm (VWAP cross | K opposing | R×ATR from leg extreme)
- [x] Adaptive K/R helpers; sessionStart clears latches
- [x] Opposite-direction fire clears other latch
- [x] Early `i+`/`i-` plots and `Impulse_Early_*` alerts removed
- [x] Surviving alerts: `Impulse_Run_Long` / `Impulse_Run_Short` only
- [x] Impulse-specific `lastImpulseBar` cooldown removed; latch is anti-spam
- [x] Risk refs still bind to `impulseLongAlert` / `impulseShortAlert`

### TradingView replay (fill)

| Check | Pass criteria | Result |
|-------|---------------|--------|
| Compile | Pine v6, no errors | ☐ |
| July 31 1m | ≤3 same-direction IMP markers per major leg; ≥1 early in grind | ☐ |
| July 31 1m pauses | 1-bar counter does not re-fire same direction | ☐ |
| Reversal | VWAP cross / opposing leg → ≥1 opposite marker | ☐ |
| July 31 30m | ≥1 Impulse on clear trend leg | ☐ |
| Choppy day | Soft-RVOL Impulse available; no consecutive-bar spam | ☐ |
