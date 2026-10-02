import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
import csv
import json

logger = logging.getLogger("tradingagents.backtesting.engine")

from tradingagents.dataflows.y_finance import get_YFin_data_online
from tradingagents.reporting import ResearchDatabase, get_db, Analysis, BacktestRun
from tradingagents.backtesting.metrics import (
    compute_researcher_attribution,
    horizon_correct,
    summarize_analyses,
)


def _parse_price_series(csv_text: str) -> List[Dict[str, str]]:
    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return []

    reader = csv.DictReader(lines)
    series: List[Dict[str, str]] = []
    for row in reader:
        # yfinance normally names the index "Date", but handle unnamed index too
        date_val = row.get("Date") or row.get("") or ""
        if not date_val.strip():
            continue
        # Normalize: if the key was "" (unnamed index), remap to "Date"
        if "Date" not in row and "" in row:
            row["Date"] = row.pop("")
        series.append(row)
    return series


def _fetch_price_series(ticker: str, start_date: str, end_date: str) -> List[Dict[str, str]]:
    data = get_YFin_data_online(ticker, start_date, end_date)
    if data.startswith("No data found"):
        return []
    return _parse_price_series(data)


def _find_anchor_index(series: List[Dict[str, str]], analysis_date: str) -> Optional[int]:
    if not series:
        return None
    target = datetime.strptime(analysis_date[:10], "%Y-%m-%d")
    last_idx = None
    for idx, row in enumerate(series):
        date_str = row["Date"].split(" ")[0].split("T")[0]
        try:
            row_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if row_date > target:
            break
        last_idx = idx
    return last_idx


def _find_index_by_calendar_date(
    series: List[Dict[str, str]], target_date: datetime
) -> Optional[int]:
    """Find the first row on or after *target_date* (calendar-day targeting)."""
    for idx, row in enumerate(series):
        date_str = row["Date"].split(" ")[0].split("T")[0]
        try:
            row_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if row_date >= target_date:
            return idx
    return None


def _extract_close(row: Dict[str, str]) -> Optional[float]:
    try:
        return float(row.get("Close", ""))
    except ValueError:
        return None


def _apply_costs(return_value: Optional[float], slippage_bps: float, cost_bps: float) -> Optional[float]:
    if return_value is None:
        return None
    total_bps = max(0.0, slippage_bps) + max(0.0, cost_bps)
    # Apply round-trip costs (entry + exit)
    cost = (total_bps * 2.0) / 10000.0
    return return_value - cost


def _compute_returns(
    price_at: Optional[float],
    future_price: Optional[float],
    slippage_bps: float = 0.0,
    cost_bps: float = 0.0,
) -> Optional[float]:
    if price_at is None or future_price is None or price_at == 0 or future_price == 0:
        return None
    raw_return = (future_price - price_at) / price_at
    return _apply_costs(raw_return, slippage_bps, cost_bps)


def _infer_correctness(
    decision: str,
    return_value: Optional[float],
    lookahead_days: int = 30,
) -> Optional[bool]:
    return horizon_correct(decision, return_value, lookahead_days)


class BacktestEngine:
    def __init__(self, db_path: str = "./research.db"):
        self.db = get_db(db_path)

    def run(
        self,
        ticker: Optional[str] = None,
        limit: int = 50,
        lookahead_days: Optional[List[int]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        slippage_bps: float = 0.0,
        transaction_cost_bps: float = 0.0,
    ) -> Dict[str, Any]:
        lookahead_days = lookahead_days or [7, 14, 30]
        if start_date or end_date:
            analyses = self.db.get_analyses_by_date_range(
                start_date=start_date,
                end_date=end_date,
                ticker=ticker,
                limit=limit,
            )
        elif ticker:
            analyses = self.db.get_analyses_by_ticker(ticker, limit)
        else:
            analyses = self.db.get_recent_analyses(limit)

        updated = 0
        skipped = 0
        skip_reasons: Dict[str, int] = {}
        for analysis in analyses:
            updated_this, _ret_value, _was_correct, skip_reason = self._update_analysis_outcomes(
                analysis,
                lookahead_days,
                slippage_bps=slippage_bps,
                transaction_cost_bps=transaction_cost_bps,
            )
            if updated_this:
                updated += 1
            else:
                skipped += 1
                if skip_reason:
                    skip_reasons[skip_reason] = skip_reasons.get(skip_reason, 0) + 1

        book = summarize_analyses(analyses)
        tape_7d = book.get("avg_tape_return_7d") or 0.0
        win_rate = book.get("win_rate") or 0.0
        accuracy = book.get("directional_accuracy_7d") or 0.0
        signed_7d = book.get("avg_signed_return_7d")
        vol_7d = book.get("return_vol_7d")
        avg_alpha_30d = book.get("avg_tape_alpha_30d")
        if avg_alpha_30d is not None:
            avg_alpha_30d = round(avg_alpha_30d, 4)

        summary = {
            "avg_return": round(tape_7d, 4),
            "win_rate": round(win_rate, 4),
            "accuracy": round(accuracy, 4),
            "avg_signed_return_7d": round(signed_7d, 4) if signed_7d is not None else None,
            "return_vol_7d": round(vol_7d, 4) if vol_7d is not None else None,
            "directional_accuracy_7d": round(accuracy, 4),
            "strategy_sharpe": None,
            "strategy_sortino": None,
            "updated": updated,
            "skipped": skipped,
            "skip_reasons": skip_reasons,
            "slippage_bps": slippage_bps,
            "transaction_cost_bps": transaction_cost_bps,
            "agent_attribution": book.get("agent_attribution") or {},
            "avg_alpha_30d": avg_alpha_30d,
        }

        run_id = self.db.save_backtest_run(
            BacktestRun(
                ticker=(ticker or ""),
                start_date=start_date or "",
                end_date=end_date or "",
                limit_count=limit,
                lookahead_days=json.dumps(lookahead_days),
                slippage_bps=slippage_bps,
                transaction_cost_bps=transaction_cost_bps,
                updated_count=updated,
                skipped_count=skipped,
                avg_return=summary.get("avg_return", 0.0),
                win_rate=summary.get("win_rate", 0.0),
                accuracy=summary.get("accuracy", 0.0),
                strategy_sharpe=None,
                strategy_sortino=None,
                avg_alpha_30d=avg_alpha_30d,
                avg_signed_return_7d=summary.get("avg_signed_return_7d"),
                return_vol_7d=summary.get("return_vol_7d"),
            )
        )
        summary["backtest_run_id"] = run_id
        return summary

    def _update_analysis_outcomes(
        self,
        analysis: Analysis,
        lookahead_days: List[int],
        slippage_bps: float = 0.0,
        transaction_cost_bps: float = 0.0,
    ) -> Tuple[bool, Optional[float], Optional[bool], Optional[str]]:
        offsets = sorted(set(lookahead_days + [7, 14, 30]))
        if not offsets:
            return False, None, None, "no_offsets"

        max_days = max(offsets)
        analysis_date = analysis.analysis_date
        try:
            analysis_date_dt = datetime.strptime(analysis_date, "%Y-%m-%d")
        except ValueError:
            return False, None, None, "invalid_date"
        # Fetch enough extra calendar days to cover weekends/holidays
        end_date = (analysis_date_dt + timedelta(days=max_days + 10)).strftime("%Y-%m-%d")
        series = _fetch_price_series(analysis.ticker, analysis_date, end_date)
        if not series:
            return False, None, None, "no_price_series"

        anchor_index = _find_anchor_index(series, analysis_date)
        if anchor_index is None:
            return False, None, None, "no_anchor"

        price_at = _extract_close(series[anchor_index])
        if price_at is None or price_at == 0:
            return False, None, None, "missing_price_at"
        prices: Dict[int, Optional[float]] = {}
        for offset in offsets:
            target_dt = analysis_date_dt + timedelta(days=offset)
            idx = _find_index_by_calendar_date(series, target_dt)
            prices[offset] = _extract_close(series[idx]) if idx is not None else None

        returns = {
            offset: _compute_returns(price_at, prices.get(offset), slippage_bps, transaction_cost_bps)
            for offset in offsets
        }

        result_return = None
        result_offset = 30
        for offset in sorted(set(lookahead_days), reverse=True):
            value = returns.get(offset)
            if value is not None:
                result_return = value
                result_offset = offset
                break
        if result_return is None:
            for fallback in (30, 14, 7):
                value = returns.get(fallback)
                if value is not None:
                    result_return = value
                    result_offset = fallback
                    break
        if result_return is None:
            return False, None, None, "no_returns"

        # Issue 24: Benchmark-relative alpha (SPY)
        spy_series = _fetch_price_series("SPY", analysis_date, end_date)
        spy_anchor = _find_anchor_index(spy_series, analysis_date) if spy_series else None
        spy_price_at = _extract_close(spy_series[spy_anchor]) if spy_anchor is not None else None
        alphas: Dict[int, Optional[float]] = {}
        for offset in [7, 14, 30]:
            stock_ret = returns.get(offset)
            if stock_ret is None or spy_price_at is None or spy_price_at == 0:
                alphas[offset] = None
                continue
            spy_target_dt = analysis_date_dt + timedelta(days=offset)
            spy_idx = _find_index_by_calendar_date(spy_series, spy_target_dt) if spy_series else None
            spy_future = _extract_close(spy_series[spy_idx]) if spy_idx is not None else None
            if spy_future is None or spy_future == 0:
                alphas[offset] = None
            else:
                spy_ret = (spy_future - spy_price_at) / spy_price_at
                alphas[offset] = stock_ret - spy_ret

        ret_7 = returns.get(7)
        ret_14 = returns.get(14)
        ret_30 = returns.get(30)
        was_correct = _infer_correctness(analysis.decision, result_return, result_offset)

        analysis.price_at_analysis = price_at
        analysis.price_after_7d = prices.get(7)
        analysis.price_after_14d = prices.get(14)
        analysis.price_after_30d = prices.get(30)
        analysis.actual_return_7d = ret_7
        analysis.actual_return_14d = ret_14
        analysis.actual_return_30d = ret_30
        analysis.alpha_7d = alphas.get(7)
        analysis.alpha_14d = alphas.get(14)
        analysis.alpha_30d = alphas.get(30)
        analysis.was_correct = was_correct

        self.db.update_backtest_outcomes(
            analysis_id=analysis.id,
            price_at_analysis=price_at,
            price_after_7d=prices.get(7),
            price_after_14d=prices.get(14),
            price_after_30d=prices.get(30),
            actual_return_7d=ret_7,
            actual_return_14d=ret_14,
            actual_return_30d=ret_30,
            was_correct=was_correct,
            alpha_7d=alphas.get(7),
            alpha_14d=alphas.get(14),
            alpha_30d=alphas.get(30),
        )

        return True, result_return, was_correct, None

    @staticmethod
    def _compute_strategy_metrics(
        returns_list: List[float],
        risk_free_annual: float = 0.0,
    ) -> Dict[str, Optional[float]]:
        """Deprecated mixed-horizon Sharpe. New runs persist 7d return/vol instead."""
        del returns_list, risk_free_annual
        return {"strategy_sharpe": None, "strategy_sortino": None}

    @staticmethod
    def _summarize(returns: List[float], correctness: List[bool]) -> Dict[str, float]:
        """Legacy helper: unsigned mean of the provided series (tests / callers)."""
        if returns:
            avg_return = sum(returns) / len(returns)
            win_rate = sum(1 for r in returns if r > 0) / len(returns)
        else:
            avg_return = 0.0
            win_rate = 0.0

        if correctness:
            accuracy = sum(1 for value in correctness if value) / len(correctness)
        else:
            accuracy = 0.0

        return {
            "avg_return": round(avg_return, 4),
            "win_rate": round(win_rate, 4),
            "accuracy": round(accuracy, 4),
        }

    @staticmethod
    def _compute_agent_attribution(analyses: List[Analysis]) -> Dict[str, Dict[str, Any]]:
        """Researcher stance vs 7d tape (explicit decision / SIGNAL_JSON, not final BUY/SELL)."""
        return compute_researcher_attribution(analyses)


# =============================================================================
# Screening Backtest
# =============================================================================

def backtest_screening_run(
    run_id: int,
    lookahead_days: List[int] = None,
    db_path: str = "research.db",
) -> Dict[str, Any]:
    """
    Backtest a screening run: compute forward returns for all results
    and measure correlation between composite scores and actual returns.

    Args:
        run_id: Screening run ID
        lookahead_days: Forward return periods (default: [7, 14, 30])
        db_path: Path to database

    Returns:
        Dict with hit rates, correlation, signal accuracy, and per-ticker returns
    """
    if lookahead_days is None:
        lookahead_days = [7, 14, 30]

    db = get_db(db_path)
    results = db.get_screening_results(run_id)

    if not results:
        return {"error": "No results found for this screening run", "run_id": run_id}

    run_info = db.get_screening_run(run_id) if hasattr(db, "get_screening_run") else None
    if not run_info:
        runs = db.get_screening_runs(limit=500)
        run_info = next((r for r in runs if r["id"] == run_id), None)
    if not run_info:
        return {"error": "Screening run not found", "run_id": run_id}

    run_date = run_info["run_at"].split("T")[0]

    updated = 0
    skipped = 0
    ticker_results = []

    for result in results:
        ticker = result["ticker"]
        score = result.get("composite_score", 0)
        direction = result.get("direction", "")

        # Fetch price data from run_date forward
        end_date_str = (
            datetime.strptime(run_date, "%Y-%m-%d") + timedelta(days=max(lookahead_days) + 10)
        ).strftime("%Y-%m-%d")

        try:
            series = _fetch_price_series(ticker, run_date, end_date_str)
        except Exception as e:
            logger.debug("Price fetch failed for %s: %s", ticker, e)
            skipped += 1
            continue

        if not series:
            skipped += 1
            continue

        # Find base price (first trading day on or after run_date)
        base_price = None
        for row in series:
            try:
                base_price = float(row.get("Close", 0))
                break
            except (ValueError, TypeError):
                continue

        if not base_price:
            skipped += 1
            continue

        # Compute forward returns
        returns = {}
        for days in lookahead_days:
            target_date = datetime.strptime(run_date, "%Y-%m-%d") + timedelta(days=days)
            forward_price = None
            # Find closest price on or after target date
            for row in series:
                try:
                    row_date = datetime.strptime(row["Date"].split(" ")[0], "%Y-%m-%d")
                    if row_date >= target_date:
                        forward_price = float(row.get("Close", 0))
                        break
                except (ValueError, TypeError, KeyError):
                    continue

            if forward_price:
                ret = (forward_price - base_price) / base_price
                returns[f"return_{days}d"] = round(ret, 6)
            else:
                returns[f"return_{days}d"] = None

        # Update database
        try:
            db.update_screening_result_returns(
                result["id"],
                return_7d=returns.get("return_7d"),
                return_14d=returns.get("return_14d"),
                return_30d=returns.get("return_30d"),
            )
            updated += 1
        except Exception as e:
            logger.warning("Failed to update screening result returns for %s: %s", ticker, e)

        ticker_results.append({
            "ticker": ticker,
            "score": score,
            "direction": direction,
            **returns,
        })

    # Compute metrics
    import numpy as np

    scores = [r["score"] for r in ticker_results]
    metrics = {}

    for days in lookahead_days:
        key = f"return_{days}d"
        rets = [r[key] for r in ticker_results if r.get(key) is not None]

        if len(rets) < 5:
            metrics[f"{days}d"] = {"correlation": None, "hit_rate": None, "avg_return": None}
            continue

        # Correlation between score and forward return
        valid_scores = [r["score"] for r in ticker_results if r.get(key) is not None]
        corr = float(np.corrcoef(valid_scores, rets)[0, 1]) if len(rets) > 1 else None

        # Hit rate: % of top 20 by score that had positive returns
        sorted_by_score = sorted(
            [r for r in ticker_results if r.get(key) is not None],
            key=lambda x: x["score"], reverse=True
        )
        top_n = sorted_by_score[:min(20, len(sorted_by_score))]
        positive = sum(1 for r in top_n if (r.get(key) or 0) > 0)
        hit_rate = round(positive / len(top_n) * 100, 1) if top_n else 0

        avg_return = round(float(np.mean(rets)) * 100, 2) if rets else 0

        metrics[f"{days}d"] = {
            "correlation": round(corr, 4) if corr is not None and not np.isnan(corr) else None,
            "hit_rate": hit_rate,
            "avg_return": avg_return,
            "sample_size": len(rets),
        }

    return {
        "run_id": run_id,
        "run_date": run_date,
        "updated": updated,
        "skipped": skipped,
        "metrics": metrics,
        "ticker_results": ticker_results[:50],  # Cap for API response size
    }
