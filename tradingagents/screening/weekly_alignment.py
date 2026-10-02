"""Shared weekly trend alignment scoring for screening and analysis."""
from __future__ import annotations

from typing import Any, Dict, Optional


def _weekly_confluence(weekly_data: Optional[Dict[str, Any]]) -> Optional[float]:
    """Raw confluence score from weekly technicals (typically 0-1)."""
    if not weekly_data:
        return None
    confluence = weekly_data.get("confluence_score")
    if confluence is None:
        return None
    try:
        return float(confluence)
    except (TypeError, ValueError):
        return None


def score_weekly_alignment(weekly_data: Optional[Dict[str, Any]]) -> float:
    """Map weekly confluence to [0, 1] for screening composite signals."""
    confluence = _weekly_confluence(weekly_data)
    if confluence is None:
        return 0.0
    return max(0.0, min(1.0, float(confluence)))


def weekly_trend_label(weekly_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map weekly confluence to signed score + label for analysis aggregator."""
    if not weekly_data or not weekly_data.get("weekly_close"):
        return {"score": 0.0, "label": "N/A", "detail": "No data"}

    confluence = _weekly_confluence(weekly_data)
    rsi = weekly_data.get("weekly_rsi")
    detail_parts = []
    if rsi is not None:
        detail_parts.append(f"Weekly RSI {rsi:.0f}")
    if confluence is not None:
        detail_parts.append(f"confluence {confluence:.2f}")
    detail = ", ".join(detail_parts) if detail_parts else "limited data"

    if confluence is not None:
        score = max(-1.0, min(1.0, (confluence - 0.5) * 5.0))
    else:
        score = 0.0

    if abs(score) < 0.05:
        label = "NEUTRAL"
    elif score > 0:
        label = "BULLISH"
    else:
        label = "BEARISH"

    return {"score": round(score, 4), "label": label, "detail": detail}


def weekly_trend_label_from_ticker(ticker: str, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """Fetch weekly technicals and return aggregator label dict."""
    try:
        from tradingagents.dataflows.yfinance_extended import get_weekly_technicals

        data = get_weekly_technicals(ticker, as_of_date=as_of_date)
        return weekly_trend_label(data)
    except Exception as exc:
        return {"score": 0.0, "label": "N/A", "detail": str(exc)}
