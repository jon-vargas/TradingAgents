"""Weekly multi-timeframe enrichment for the Scan All leaderboard."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from tradingagents.dataflows.yfinance_extended import get_weekly_technicals
from tradingagents.screening.weekly_alignment import (
    score_weekly_alignment,
    weekly_trend_label,
)


@dataclass
class WeeklyHydrateResult:
    rows: List[Dict[str, Any]]
    hydrated_tickers: Set[str]
    insufficient_history_tickers: Set[str]
    skipped_existing_count: int


def _signals_and_meta(row: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    signals = row.get("signals")
    if not isinstance(signals, dict):
        signals = {}
        row["signals"] = signals
    meta = signals.get("_screening_meta")
    if not isinstance(meta, dict):
        meta = {}
        signals["_screening_meta"] = meta
    return signals, meta


def needs_weekly_hydrate(row: Dict[str, Any]) -> bool:
    """Return whether a row lacks a reliable, computed weekly reading.

    Legacy rows used 0.0 as a missing-data placeholder. Without the explicit
    ``weekly_computed`` marker, it remains eligible for a one-time hydrate.
    """
    signals = row.get("signals") if isinstance(row.get("signals"), dict) else {}
    meta = signals.get("_screening_meta") if isinstance(signals, dict) else {}
    if not isinstance(meta, dict) or meta.get("weekly_computed") is not True:
        return True
    return signals.get("weekly_trend_alignment") is None


def merge_hydrated_rows(
    all_rows: List[Dict[str, Any]],
    hydrate_result: WeeklyHydrateResult,
) -> List[Dict[str, Any]]:
    """Replace rows in ``all_rows`` with hydrated copies where available."""
    updated = {
        str(row.get("ticker") or "").upper(): row
        for row in hydrate_result.rows
    }
    merged: List[Dict[str, Any]] = []
    for row in all_rows:
        ticker = str(row.get("ticker") or "").upper()
        merged.append(updated.get(ticker, row))
    return merged


def collect_leaderboard_mtf_targets(
    rows: List[Dict[str, Any]],
    *,
    top: int,
    stratified_top_k: int,
    headline_cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Rows surfaced in the Scan All UI that should have weekly MTF populated.

    Includes the rotated headline table, the raw global top-N, and the head of
    each stratified cap-tier bucket. Names can land in those views after overlay
    re-scoring even when they were outside the pre-sort hydrate pool.
    """
    from tradingagents.screening.scan_all_overlays import headline_top_opportunities

    targets: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def _add(row: Dict[str, Any]) -> None:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker or ticker in seen:
            return
        seen.add(ticker)
        targets.append(row)

    for row in headline_top_opportunities(rows, top, headline_cfg=headline_cfg):
        _add(row)
    for row in rows[: max(0, top)]:
        _add(row)

    stratified_buckets: Dict[str, List[Dict[str, Any]]] = {
        "mega_large": [],
        "mid": [],
        "small_micro": [],
    }
    for row in rows:
        tier = str(row.get("market_cap_tier") or "unknown").lower()
        if tier in ("mega", "large"):
            stratified_buckets["mega_large"].append(row)
        elif tier == "mid":
            stratified_buckets["mid"].append(row)
        elif tier in ("small", "micro"):
            stratified_buckets["small_micro"].append(row)

    per_band = max(1, stratified_top_k)
    for band_rows in stratified_buckets.values():
        for row in band_rows[:per_band]:
            _add(row)

    return [row for row in targets if needs_weekly_hydrate(row)]


def hydrate_weekly_mtf(
    rows: Iterable[Dict[str, Any]],
    *,
    cap: int,
    as_of_date: Optional[str] = None,
) -> WeeklyHydrateResult:
    """Hydrate up to ``cap`` missing weekly readings in ranked row order.

    A successful network call is not enough to mark a result as computed:
    yfinance returns a valid but sparse dict for tickers with insufficient
    weekly history. Those rows intentionally remain unknown (None), avoiding
    a false bearish 0.0 signal and opportunity-score dampener.
    """
    output = [dict(row) for row in rows]
    hydrated: Set[str] = set()
    insufficient: Set[str] = set()
    skipped_existing = 0
    used = 0
    hydrate_rows: List[tuple[str, Dict[str, Any]]] = []

    for row in output:
        ticker = str(row.get("ticker") or "").upper()
        row["ticker"] = ticker
        if not needs_weekly_hydrate(row):
            skipped_existing += 1
            continue
        if used >= max(0, cap):
            continue
        used += 1
        hydrate_rows.append((ticker, row))

    weekly_by_ticker: Dict[str, Dict[str, Any]] = {}
    executor: Optional[ThreadPoolExecutor] = None
    futures = {}
    if hydrate_rows:
        try:
            executor = ThreadPoolExecutor(max_workers=min(6, len(hydrate_rows)))
            futures = {
                executor.submit(get_weekly_technicals, ticker, as_of_date=as_of_date): ticker
                for ticker, _row in hydrate_rows
            }
            for future in as_completed(futures, timeout=90):
                ticker = futures[future]
                try:
                    weekly_by_ticker[ticker] = future.result()
                except Exception:
                    weekly_by_ticker[ticker] = {}
        except FuturesTimeoutError:
            # Partial weekly data is useful; unfinished requests must not pin the
            # full Scan All job when Yahoo is rate-limiting or returns bad crumbs.
            pass
        finally:
            for future in futures:
                if not future.done():
                    future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    for ticker, row in hydrate_rows:
        signals, meta = _signals_and_meta(row)
        weekly_data = weekly_by_ticker.get(ticker, {})

        # Match weekly_trend_label's no-data sentinel. Confluence is also
        # required because score_weekly_alignment maps a missing value to 0.0.
        if (
            not weekly_data
            or weekly_data.get("weekly_close") is None
            or weekly_data.get("confluence_score") is None
        ):
            signals.pop("weekly_trend_alignment", None)
            meta["weekly_computed"] = False
            insufficient.add(ticker)
            continue

        alignment = score_weekly_alignment(weekly_data)
        label = weekly_trend_label(weekly_data)
        signals["weekly_trend_alignment"] = alignment
        meta["weekly_computed"] = True
        row["weekly_detail"] = {
            "label": label.get("label"),
            "score": label.get("score"),
            "detail": label.get("detail"),
            "weekly_rsi": weekly_data.get("weekly_rsi"),
            "weekly_macd_histogram": weekly_data.get("weekly_macd_histogram"),
            "confluence_score": weekly_data.get("confluence_score"),
            "weekly_close": weekly_data.get("weekly_close"),
            "weekly_sma10": weekly_data.get("weekly_sma10"),
            "weekly_sma20": weekly_data.get("weekly_sma20"),
            "fetched_at": weekly_data.get("fetched_at"),
        }
        row["weekly_mtf_source"] = "scan_all_hydrate"
        hydrated.add(ticker)

    return WeeklyHydrateResult(
        rows=output,
        hydrated_tickers=hydrated,
        insufficient_history_tickers=insufficient,
        skipped_existing_count=skipped_existing,
    )


def compute_mtf_confluence(
    row: Dict[str, Any],
    *,
    weekly_bullish_threshold: float = 0.65,
    weekly_bearish_threshold: float = 0.35,
) -> Dict[str, Any]:
    """Classify agreement between the daily direction and weekly alignment."""
    signals = row.get("signals") if isinstance(row.get("signals"), dict) else {}
    weekly = signals.get("weekly_trend_alignment")
    direction = str(row.get("direction") or "neutral").lower()
    if weekly is None:
        return {"mtf_confluence": "unknown", "mtf_confluence_detail": "Weekly trend unavailable"}

    weekly_value = float(weekly)
    weekly_direction = (
        "bullish"
        if weekly_value >= weekly_bullish_threshold
        else "bearish"
        if weekly_value <= weekly_bearish_threshold
        else "mixed"
    )
    if direction in {"bullish", "bearish"} and weekly_direction == direction:
        confluence = "aligned"
    elif direction == "bullish" and weekly_direction == "bearish":
        confluence = "counter_trend"
    elif direction == "bearish" and weekly_direction == "bullish":
        confluence = "counter_trend"
    else:
        confluence = "mixed"
    return {
        "mtf_confluence": confluence,
        "mtf_confluence_detail": (
            f"Daily {direction} · Weekly {weekly_direction} ({weekly_value:.2f})"
        ),
    }


def apply_mtf_confluence_flags(
    rows: Iterable[Dict[str, Any]],
    *,
    weekly_bullish_threshold: float = 0.65,
    weekly_bearish_threshold: float = 0.35,
) -> List[Dict[str, Any]]:
    """Annotate rows with confluence metadata without creating another score."""
    output: List[Dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        info = compute_mtf_confluence(
            row,
            weekly_bullish_threshold=weekly_bullish_threshold,
            weekly_bearish_threshold=weekly_bearish_threshold,
        )
        row.update(info)
        row["weekly_counter_trend"] = info["mtf_confluence"] == "counter_trend"
        output.append(row)
    return output
