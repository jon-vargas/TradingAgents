# TradingView Scripts

Pine Script indicators and operator guides for discretionary chart work. **Not** connected to the Python TradingAgents runtime — paste into TradingView manually.

---

## Two toolkits (do not mix on one workflow)

| Toolkit | Guide | Scripts | Timeframe | Use for |
|---------|-------|---------|-----------|---------|
| **Swing Regime Toolkit** | [`GUIDE-swing-regime-toolkit.md`](GUIDE-swing-regime-toolkit.md) | 4 indicators (load all on one chart) | 1W / 1D / 1h | Swing bias, setup, timing — any liquid ticker |
| **Intraday Regime Toolkit** | [`GUIDE-intraday-toolkit.md`](GUIDE-intraday-toolkit.md) | `intraday.pine` | 1m–60m RTH | SPY/QQQ session trading — VWAP, OR, RVOL, Impulse |

---

## Swing Regime Toolkit — load order

1. `swing-regime-overlay.pine` — **Regime & Structure Overlay** (price)
2. `swing-regime-momentum.pine` — **ATR Momentum Oscillator**
3. `swing-regime-rsi-stack.pine` — **Multi-RSI Stack**
4. `swing-regime-capital-flow.pine` — **Capital Flow Split**

---

## Verification docs

| File | Applies to |
|------|------------|
| `verification-checklist.md` | Intraday toolkit (manual TV compile + replay) |
| `verification-results.md` | Intraday post-fix log |

---

## TradingAgents app integration

The webapp exports ticker lists and deep links to TradingView (`tradingview_links.py`, screener export buttons). It does **not** load or run these Pine scripts.
