"""Screening context handoff for deep analysis."""
from __future__ import annotations

from typing import Any, Dict, Optional, TypedDict


class ScreeningContextPacket(TypedDict, total=False):
    ticker: str
    opp_score: float
    composite: float
    entry_quality: Optional[float]
    macro_fit: Optional[float]
    risk_score: Optional[float]
    direction: str
    factor_scorecard: Optional[Dict[str, float]]
    event_risk_flags: list
    liquidity_pass: Optional[bool]
    weekly_alignment: Optional[float]
    mtf_confluence: Optional[str]
    weekly_counter_trend: Optional[bool]
    weekly_detail_summary: Optional[str]
    sector_relative_score: Optional[float]
    signal_coverage_pct: Optional[float]
    avg_dollar_volume_usd: Optional[float]
    watchlist_breadth: Optional[int]
    batch_percentile: Optional[float]
    preset: str
    run_id: int
    scan_rank: int
    reversal_phase: Optional[str]
    reversal_side: Optional[str]
    reversal_score: Optional[float]
    reversal_reasons: list
    resolved_preset: str
    ma_crossover: Optional[float]


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_context_packet(row: Dict[str, Any], *, run_id: int, preset: str = "") -> ScreeningContextPacket:
    """Build packet from a screening result row dict."""
    sigs = row.get("signals") or {}
    weekly = sigs.get("weekly_trend_alignment") if isinstance(sigs, dict) else None
    meta = sigs.get("_screening_meta") if isinstance(sigs, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    fs = row.get("factor_scorecard")
    weekly_detail = row.get("weekly_detail") or {}
    if not isinstance(weekly_detail, dict):
        weekly_detail = {}
    if not fs and isinstance(sigs, dict):
        raw = sigs.get("_factor_scorecard")
        if isinstance(raw, str):
            import json
            try:
                fs = json.loads(raw)
            except Exception:
                fs = None
    reversal = row.get("reversal_buildup")
    if not isinstance(reversal, dict) and isinstance(sigs, dict):
        reversal = sigs.get("_reversal_buildup") if isinstance(sigs.get("_reversal_buildup"), dict) else {}
    if not isinstance(reversal, dict):
        reversal = {}
    return ScreeningContextPacket(
        ticker=str(row.get("ticker", "")).upper(),
        opp_score=float(row.get("opportunity_score") or 0),
        composite=float(row.get("composite_score") or 0),
        entry_quality=row.get("entry_quality"),
        macro_fit=row.get("macro_fit"),
        risk_score=row.get("risk_score"),
        direction=str(row.get("direction") or ""),
        factor_scorecard=fs,
        event_risk_flags=list(row.get("event_risk_flags") or []),
        liquidity_pass=row.get("liquidity_pass"),
        weekly_alignment=weekly,
        mtf_confluence=row.get("mtf_confluence"),
        weekly_counter_trend=row.get("weekly_counter_trend"),
        weekly_detail_summary=weekly_detail.get("detail"),
        sector_relative_score=row.get("sector_relative_score"),
        signal_coverage_pct=row.get("signal_coverage_pct") or meta.get("signal_coverage_pct"),
        avg_dollar_volume_usd=row.get("avg_dollar_volume_usd"),
        watchlist_breadth=row.get("watchlist_breadth"),
        batch_percentile=row.get("batch_percentile"),
        preset=preset or str(row.get("preset") or ""),
        run_id=run_id,
        scan_rank=int(row.get("rank") or row.get("scan_rank") or 0),
        reversal_phase=row.get("reversal_phase") or reversal.get("phase"),
        reversal_side=row.get("reversal_side") or reversal.get("side"),
        reversal_score=row.get("reversal_score") if row.get("reversal_score") is not None else reversal.get("score"),
        reversal_reasons=list(row.get("reversal_reasons") or reversal.get("reasons") or []),
        resolved_preset=str(meta.get("resolved_preset") or row.get("resolved_preset") or preset or ""),
        ma_crossover=_to_float(sigs.get("ma_crossover") if isinstance(sigs, dict) else None),
    )


def format_screening_context(packet: ScreeningContextPacket) -> str:
    """Human-readable block for prompts and reports."""
    lines = [
        f"Ticker: {packet.get('ticker')}",
        f"Scan run #{packet.get('run_id')} rank #{packet.get('scan_rank')}",
        f"Opportunity score: {packet.get('opp_score')} | Composite: {packet.get('composite')}",
        f"Direction: {packet.get('direction')} | Preset: {packet.get('preset') or 'default'}",
    ]
    if packet.get("resolved_preset"):
        lines.append(f"Resolved book: {packet.get('resolved_preset')}")
    if packet.get("macro_fit") is not None:
        lines.append(f"Macro fit: {packet.get('macro_fit')}")
    if packet.get("entry_quality") is not None:
        lines.append(f"Entry quality: {packet.get('entry_quality')}")
    if packet.get("ma_crossover") is not None:
        ma_x = packet.get("ma_crossover")
        lines.append(f"MA crossover: {ma_x}")
        try:
            if float(ma_x) <= 0.05:
                lines.append("Note: overlay rank is not a buy ticket — MA crossover is not confirmed.")
        except (TypeError, ValueError):
            pass
    if packet.get("sector_relative_score") is not None:
        lines.append(f"Sector-relative opp delta: {packet.get('sector_relative_score')}")
    if packet.get("signal_coverage_pct") is not None:
        lines.append(f"Signal coverage: {packet.get('signal_coverage_pct')}%")
    flags = packet.get("event_risk_flags") or []
    if flags:
        lines.append(f"Event flags: {', '.join(flags)}")
    if packet.get("liquidity_pass") is False:
        lines.append("Liquidity: below floor")
    wa = packet.get("weekly_alignment")
    if wa is not None:
        lines.append(f"Weekly alignment: {wa:.2f}")
    if packet.get("mtf_confluence"):
        lines.append(f"MTF confluence: {packet['mtf_confluence']}")
    if packet.get("weekly_counter_trend"):
        lines.append("MTF warning: daily setup is counter-trend to weekly")
    if packet.get("weekly_detail_summary"):
        lines.append(f"Weekly detail: {packet['weekly_detail_summary']}")
    if packet.get("avg_dollar_volume_usd") is not None:
        lines.append(f"Average dollar volume: {packet['avg_dollar_volume_usd']:.0f}")
    if packet.get("watchlist_breadth") is not None:
        lines.append(f"Watchlist breadth: {packet['watchlist_breadth']}")
    if packet.get("batch_percentile") is not None:
        lines.append(f"Batch percentile: {packet['batch_percentile']}")
    if packet.get("reversal_phase"):
        lines.append(
            f"Reversal: {packet.get('reversal_side') or '-'} / {packet.get('reversal_phase')} "
            f"(score {packet.get('reversal_score')})"
        )
        reasons = packet.get("reversal_reasons") or []
        if reasons:
            lines.append(f"Reversal reasons: {', '.join(str(x) for x in reasons)}")
    return "\n".join(lines)
