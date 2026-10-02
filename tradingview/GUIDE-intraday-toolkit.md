# SPY/QQQ Intraday Regime Toolkit — Operator Guide

Script: `intraday.pine`  
TradingView name: **SPY/QQQ Intraday Regime Toolkit**  
Type: **Indicator** (not Strategy)

This is a discretionary RTH toolkit for **SPY / QQQ** (and similar US equity index ETFs). It marks regime, participation, opening-range structure, and Impulse runs. It does **not** place trades or backtest P&L.

---

## What this is vs the Swing Regime Toolkit

| Toolkit | Timeframe | Job |
|---------|-----------|-----|
| **This file** (`intraday.pine`) | Intraday: 1m–60m | Session VWAP, OR, TOD RVOL, Impulse, 5m decision clock |
| **Swing Regime Toolkit** | Weekly / daily / hourly | Gold/grey regime, MA ribbon, exhaustion stars, momentum / RSI / flow panes |

Use this toolkit for **intraday index trading**. Use the Swing Regime Toolkit for **swing / multi-day** charts. Do not mix them as one system.

---

## Load and layout

1. Pine Editor → **Indicator** → paste `intraday.pine` → Save → Add to chart.
2. Recommended layout: **three panes of the same symbol**
   - **30m** — bias
   - **5m** — decide (main)
   - **1m** — time only
3. Session: default **0930–1600 America/New_York**.
4. Opening Range: default **OR 15**. On a **30m** chart set **OR Minutes = 30** or OR stays disabled (`off·try OR30`).

Supported chart TFs: **1, 2, 3, 5, 10, 15, 30, 45, 60** minutes.

---

## How to read the HUD (compact table)

Default compact rows:

| Row | Meaning |
|-----|---------|
| **Reg** | Regime: Trend Up / Trend Down / Balanced / Transition / warming |
| **RVol** | Time-of-day relative volume vs floor `1.0` and continuation `1.25` |
| **Sig** | Live setup or Impulse state (`IMP↑ n/N`, `latched`, blocked reason) |
| **Imp** | Impulse run progress or latched |
| **OR** | `locked` / `building` / `idle` / `off·try OR30` |

Full table (uncheck **Compact table**) adds Session, Gap, VWAP Δ, Reject, ONH/L, HTF, ADR, Internals, Lead, Risk.

---

## Regime (the bias layer)

Evaluated after a few RTH bars/minutes — **not** gated on OR lock.

| Regime | What it means | What you trade |
|--------|----------------|----------------|
| **Trend Up** | Price above VWAP, slope up, DI/ADX support | Longs only (OR+, VP+, Impulse long) |
| **Trend Down** | Opposite | Shorts only |
| **Balanced** | Quiet, near VWAP | Mean-reversion only (failed-OR, VWAP band) |
| **Transition** | Chop / compression | Core families silent; Impulse optional |
| **warming** | Too early in session | Wait |

Quiet grind days can still print **Trend Up** with RVOL &lt; 1.0 (scorecard needs 3 structural dims, not RVOL).

---

## RVOL (participation)

TOD RVOL = this session’s cumulative volume vs the average of prior **full** RTH days at the **same minute from the open**. Current session is never in its own baseline.

| RVOL | Meaning | Size / families |
|------|---------|-----------------|
| **warming** | Not enough history | No volume-gated trades |
| **&lt; 1.0** | Light participation | OR/MR blocked; **Impulse may still fire** |
| **1.0–1.24** | Floor pass | Failed-OR / band MR OK; **no OR+/VP+** → half size |
| **≥ 1.25** | Continuation pass | OR+ and VWAP-pullback allowed → normal size |

---

## Chart roles (always)

| Chart | Job |
|-------|-----|
| **30m `Reg`** | Which side is allowed |
| **5m `Reg` + Impulse + VWAP** | Is there a setup **now** |
| **1m** | Entry/exit **timing only** — never the thesis |

---

## Markers

| Marker | Family | Typical gate |
|--------|--------|----------------|
| **OR+ / OR−** | Opening-range continuation | Trend + RVOL ≥ 1.25 + HTF |
| **VP+ / VP−** | VWAP pullback continuation | Trend + RVOL ≥ 1.25 + HTF |
| **FR+ / FR−** | Failed OR reversal | Balanced + RVOL ≥ 1.0 |
| **Diamonds** | VWAP band mean-reversion | Balanced + RSI + RVOL ≥ 1.0 |
| **IMP+ / IMP−** | Impulse Run (triangles) | Soft RVOL; leg latch; TF gap |

Labels `IMP+`/`IMP−` are **off** by default (triangles stay). Toggle **Show IMP+/IMP- text labels**.

---

## Impulse Run (quiet-trend catcher)

Fires on consecutive grind/impulse bars. Soft RVOL (blocked only while warming). **One marker per directional leg** until re-arm.

**Re-arm** (any one): VWAP-side cross, K opposing bars, or R×ATR pullback from the leg extreme.  
**Min gap** after a fire: 1m = 12 bars, 2–3m = 8, 5m = 3.

| TF | Entry bars | HTF (default Auto) |
|----|------------|---------------------|
| 1m | 2 | Required (5m bias) |
| 5m | 2 | Off (quiet days still mark) |
| 15m / 30m | 2 | Off |

**Impulse vs regime:** `IMP−` while `Reg` is Trend Up is a **pullback warning**, not a regime flip. Do not full-short on 30m Impulse alone.

Optional: **Impulse in Trend regime only** — Impulse only in Trend Up/Down.

---

## Long / short cheat sheet

### Long — green light
1. 30m **Trend Up**
2. 5m **Trend Up** + **IMP↑** (or latched long)
3. Close **above** session VWAP
4. Not chasing into obvious resistance
5. RVOL ≥ 1.25 → full size OR+/VP+; else Impulse-only, **half size**

### Long — red light
- 30m Trend Down or Transition
- 5m IMP↓ building
- Below VWAP
- 1m Impulse alone
- RVOL warming

**Invalidation:** 5m close below VWAP, or 5m IMP↓.

### Short — green light
1. 30m **Trend Down**
2. 5m **Trend Down** + **IMP↓**
3. Close **below** VWAP
4. Same RVOL size rules as longs

### Short — red light
- 30m still **Trend Up** (even if 30m printed IMP−)
- 5m still Trend Up or IMP↑
- Above VWAP
- 1m Impulse alone

**Invalidation:** 5m close above VWAP, or 5m IMP↑.

### When panels disagree
- 30m Up + 5m IMP↓ → **no new short**; tighten longs / wait
- 30m Down + 5m IMP↑ → **no new long**
- Trust **5m** over **1m**

---

## HTF filter

- **OR continuation / VWAP pullback:** confirmed higher-TF bias on by default (1m→5m, 5m→15m, 30m→60m).
- **Failed-OR / band MR:** never HTF-gated.
- **Impulse:** **Auto (≤3m)** by default.

There is **no** multi-chart consensus. Each pane computes independently.

---

## Risk references

Invalidation / T1 / T2 lines update on the last alert and **fade** (1m: 15 bars, 5m: 10, higher: 5). Consider turning **Show invalidation / target refs** off on 1m layouts.

---

## Optional (off by default)

- Prior-close anchored VWAP
- $TICK / $ADD internals (`USI:TICK`, `USI:ADD`)
- SPY vs QQQ leadership
- Keltner, RSI divergence, rejected-signal dots

---

## Troubleshooting

**Blue wall of Pine source on the chart**  
Not the script. Object Tree → **Drawings** → delete the Text object (or Remove Drawings → All Drawings). Drawings sync to your TV account, so they survive reloads and other windows.

**30m OR disabled**  
Set **OR Minutes = 30**.

**Too many 1m triangles**  
Expected vs 5m. Raise Impulse cooldown / keep HTF Auto / use 5m as the decision chart.

---

## Alerts

Keep: `Impulse_Run_Long`, `Impulse_Run_Short`, plus OR / VP / Failed OR / VWAP Band conditions.  
Early Impulse alert IDs were removed — recreate TV alerts if you still subscribe to old names.

---

## Not this toolkit

- Weekly/daily gold ribbon (see `GUIDE-swing-regime-toolkit.md`)
- Strategy Tester / simulated P&L
- Fill quality / order-book imbalance
