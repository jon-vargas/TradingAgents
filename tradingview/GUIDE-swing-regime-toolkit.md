# Swing Regime Toolkit — Operator Guide

Four **indicators** for **multi-day swing trading** (1W / 1D / 1h). They provide gold/grey regime, MA ribbon, exhaustion stars, momentum, RSI alignment, and capital-flow panes. Styled as a visual analogue of invite-only Black/Gold suite charts — **not** the original author’s formulas.

| File | TradingView name | Pane | Role |
|------|------------------|------|------|
| `swing-regime-overlay.pine` | Regime & Structure Overlay [Swing Regime] | Price overlay | **Bias** — regime, ribbon, control line, swing boxes, stars |
| `swing-regime-momentum.pine` | ATR Momentum Oscillator [Swing Regime] | Oscillator | **Impulse** — ATR-normalized MACD |
| `swing-regime-rsi-stack.pine` | Multi-RSI Stack [Swing Regime] | Oscillator | **Alignment** — 6-period RSI stack (trend vs chop) |
| `swing-regime-capital-flow.pine` | Capital Flow Split [Swing Regime] | Oscillator | **Participation** — steady vs fast money |

Type: **Indicator** (not Strategy). Pine cannot put four panes in one script — add all four to the **same chart**.

ABC wave labels on sample charts may still be **hand-drawn**. **Swing range boxes** are coded; Elliott labels are not.

---

## What this is vs the Intraday Toolkit

| Toolkit | Timeframe | Job |
|---------|-----------|-----|
| **Swing Regime Toolkit** (this pack) | **1W / 1D / 1h** (or 1M / 1W / 1D, or 1W / 1D / 4h) | Multi-day regime, ribbon, exhaustion stars |
| **Intraday Toolkit** (`intraday.pine`) | 1m–60m RTH | VWAP, OR, TOD RVOL, Impulse |

Do not use the Swing Regime Toolkit as a 1m/5m index scalper. Use `intraday.pine` for that.

---

## Load on one chart

1. Pine Editor → **Indicator** (not Strategy) for each file.
2. Add all **four** to the chart.
3. Stack panes: **Regime overlay** (price) → **ATR Momentum** → **Multi-RSI Stack** → **Capital Flow Split**. Hide extra MACD/histogram panes — the pack is these four only.
4. On the overlay: leave **Regime candles (bull / bear)** on (Style tab → that plot enabled). Green/red candles mean this was unchecked.
5. Default styling is **subtle** (high fill transparency, tiny stars). Tune under **Regime Colors & Opacity** and **Exhaustion Stars** if you want more pop.
6. Optional regime volume: Overlay → **Volume regime columns**, then Style → Vol → *Move to existing pane* (merge with Volume). Leave off if volume bars sit on price.
7. Repeat on each timeframe pane if you use a 3-chart layout.

**Ticker:** any liquid name with enough history (UBER, SPY, HOOD, small caps). Logic is **price + volume** (EMAs, RSI, MFI when volume exists). Thin or brand-new listings: Control (EMA 100) is weak until you have enough bars.

Keep **Star pivot left/right** and RSI thresholds the same on all four scripts per pane so stars line up. Each script has its own **Exhaustion stars** checkbox — leave ★ on price and turn them off on the oscillator panes (or the reverse). Overlay **Trend + / −** is independent of stars.

---

## Chart layout

Recommended: **1W | 1D | 1h**

| Pane | Job |
|------|-----|
| **Weekly** | Bias — gold vs grey regime, Control Line |
| **Daily** | Decide — stars and ribbon tests |
| **Hourly** | Time only — more stars; do not override weekly |

Alternatives: 1M/1W/1D (position), 1W/1D/4h (less 1h noise).

---

## How to read each layer

### Regime & Structure Overlay (price)

| Visual | Meaning |
|--------|---------|
| **Gold candles + gold regime band** | Bullish regime. Gold **holds** until **both** short-term stack (EMA 8 vs 30) **and** Control fail. It does **not** flip on a one-bar disagreement. |
| **Grey candles + grey band** | Regime off — no trend-follow longs until both layers turn back up |
| **MA Stack** (3→60) | Fans in impulse, compresses in chop |
| **Regime band (EMA 8–30)** | Inner fill + outer Keltner envelope (balance range around price) |
| **Thick red Control Line** | Deep trend floor (EMA 100). Bull-regime pullbacks often tag it. Lose it = regime at risk |
| **Swing range box** | Latest swing high **and** swing low within **max bars between pivots** (default 20). Updates as new pivots print; clears on a confirmed close outside. Often **blank on 1W** until you raise that length. Style (border, fill, width, extend) is under **Swing Range Box** in indicator settings |
| **Cyan ★ below** | Exhaustion low — confirmed swing low with fast RSI washed out (default 36) → **setup** |
| **Magenta ★ above** | Exhaustion high — confirmed swing high with stretched RSI → **trim / don’t add** |
| **White + / −** | `+` above gold up-weeks; `−` below grey down-weeks — **not** entries |

Cyan/magenta stars are the **same bar** on overlay + all three oscillators (price-pivot based, offset to the swing), unless you hide them per script.

### ATR Momentum Oscillator
ATR-normalized MACD cloud (line vs signal). **Expands** when impulse is large vs ATR; **tightens** in chop. Gold above 0, grey below.

### Multi-RSI Stack
RSI 5 / 8 / 13 / 14 / 21 / 34. **Bright glow** on the fast line when the stack is aligned (impulse). Grey / mixed = chop. Clustered high (~80) = stretched; clustered low (~20) = washed out.

### Capital Flow Split
**Gold line** = steady capital (slow MFI, or RSI if no volume). **White line** = inverted fast money (`100 − fast`), so the two **split wide** in a trend and **pinch** in chop. Gold fill = steady capital leading. Grey fill = fast money / washout dominating.

---

## How to use (any ticker)

### 1. Weekly = bias
- Gold + above Control → **longs only**
- Grey / under Control → **no new trend-longs**

### 2. Daily = decide
- Prefer **cyan ★** or a hold of the gold band after a dip **while weekly is gold**
- Daily **magenta ★** = don’t add; consider taking swing profits
- Daily momentum/flow **grey** while weekly still gold → **pause**, don’t auto-short

### 3. Hourly = time
- Only after weekly gold + daily setup
- Hourly **magenta ★** = don’t chase a new full-size long
- More stars on 1h is expected — raise **Star pivot left/right** (e.g. 3 → 5) if noisy

---

## Go / no-go

**Long**
1. Weekly **gold** + above Control
2. Daily **gold** or reclaiming the band
3. Cyan ★ or pullback that holds the ribbon / swing box
4. Skip if hourly is printing magenta ★ at the highs

**Don’t long**
- Weekly grey
- Daily losing Control and staying grey
- Magenta ★ on **weekly or daily**
- Hourly stars alone

**Short**
- Weekly **grey** and daily grey + failed reclaim
- Magenta ★ while weekly is still **gold** is usually **take profit / wait**, not a fresh short thesis

---

## Cheat sheet

```
Weekly gold + above red line  → longs allowed
Daily cyan ★ or band hold     → setup
1h cyan ★ / dip               → timing (optional)
Magenta ★ on W or D           → don’t add / take profits
1h magenta ★                  → don’t chase
Weekly grey                   → no trend-longs
Swing box break               → structure invalidated; wait for a new box
```

---

## Stars on / off

Every script has **Exhaustion stars (setup / trim)** (default **on**).

| Want | Do this |
|------|---------|
| ★ only on price | Overlay on; three oscillator panes **off** |
| ★ only on oscillators | Overlay off; leave the three panes on |
| Hide cyan or magenta only | Style tab → uncheck **Exhaustion low** or **Exhaustion high** |
| Hide `+` / `−` | Overlay → **Trend + / − marks**, or Style → Trend Plus / Trend Minus |

Turning stars off does **not** change regime, ribbon, momentum, RSI stack, or flow. It only hides the glyphs.

## Star logic (shared)

**Cyan (setup):** confirmed price pivot low **and** fast RSI (5) below oversold (default **36**). Does **not** require gold regime — washout lows still mark.  
**Magenta (trim):** confirmed price pivot high **and** fast RSI overbought **and** RSI 14 still stretched.  
Default pivot: **3 bars left / 3 bars right** (stars sit on the swing, not the confirmation bar).

This is **not** a VWAP-cross system and **not** a 1-bar MACD tick.

---

## Tuning

| Problem | Change |
|---------|--------|
| Gold ribbon too loud | Raise **Regime band fill transparency** (try 94–97) and **Outer envelope fill transparency** (96–98) |
| MA stack too bright | Raise **MA stack line transparency** (65–80) |
| Stars too loud | Lower star opacity in **Setup / Trim star** color pickers, or set **Star size** = Tiny |
| Candles too tinted | Raise **Regime candle tint transparency** (40–55) |
| Control line too heavy | Raise transparency in **Control line color** picker |
| Swing box too prominent | Lower fill opacity in **Fill color** (defaults to 94% transparent); try **Dashed** border |
| Too many 1h stars | Raise **Star pivot left/right** (all 4 scripts on that pane) |
| Too few cyan ★ | Raise **Oversold RSI (cyan setup)** (e.g. 36 → 40) |
| Duplicate ★ on every pane | Turn **Exhaustion stars** off on oscillators; leave overlay on |
| Ribbon too tight / too wide | **Outer envelope EMA length / ATR mult** (Core group) |
| Swing box missing on 1W | Raise **Max bars between pivots** (20 → 40) |

---

## Legacy names (renamed 2026-08)

If you saved older TradingView scripts, these were renamed for clarity:

| Old file | New file |
|----------|----------|
| `gold-regime-overlay.pine` | `swing-regime-overlay.pine` |
| `iron-momentum.pine` | `swing-regime-momentum.pine` |
| `neon-stack.pine` | `swing-regime-rsi-stack.pine` |
| `silver-flow.pine` | `swing-regime-capital-flow.pine` |
| Uranium Pack | **Swing Regime Toolkit** |

Re-add indicators from the new files in TradingView after upgrading.

---

## What this toolkit does not do

- Pixel-perfect clone of the invite-only original (those formulas are unpublished)
- Simulated P&L / Strategy Tester
- Intraday OR / TOD RVOL (see `GUIDE-intraday-toolkit.md`)
- Auto Elliott ABC labels (draw those by hand if you use them)
