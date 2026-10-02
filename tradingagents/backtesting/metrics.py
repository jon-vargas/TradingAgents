"""Read-time SSOT for analysis-decision backtest metrics.

Persisted `actual_return_*` / `alpha_*` stay unsigned tape (stock close-to-close
and stock − SPY). Strategy P&L, directional accuracy, and 7d return/vol are
derived here. HOLD is excluded from signed means (not 0-filled).
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, Iterable, List, Optional

HOLD_THRESHOLDS = {7: 0.02, 14: 0.03, 30: 0.05}
HORIZONS = (7, 14, 30)
RETURN_VOL_MIN_N = 10


def normalize_decision(decision: Optional[str]) -> Optional[str]:
    """Map a label to BUY|SELL|HOLD. HOLD is checked before BUY."""
    if not decision:
        return None
    text = str(decision).strip().upper()
    if not text:
        return None
    if text.startswith("HOLD") or "HOLD (" in text or "**HOLD**" in text:
        return "HOLD"
    if "BUY" in text:
        return "BUY"
    if "SELL" in text:
        return "SELL"
    if "HOLD" in text:
        return "HOLD"
    return None


def signed_return(decision: Optional[str], r: Optional[float]) -> Optional[float]:
    """BUY → r, SELL → −r, HOLD/unknown → None (excluded from the mean)."""
    if r is None:
        return None
    kind = normalize_decision(decision)
    if kind == "BUY":
        return float(r)
    if kind == "SELL":
        return -float(r)
    return None


def horizon_correct(
    decision: Optional[str],
    r: Optional[float],
    days: int = 30,
) -> Optional[bool]:
    """Per-horizon hit. BUY/SELL require a matching sign; r == 0 is not a hit."""
    if r is None:
        return None
    kind = normalize_decision(decision)
    if kind == "BUY":
        return r > 0
    if kind == "SELL":
        return r < 0
    if kind == "HOLD":
        threshold = HOLD_THRESHOLDS.get(days, 0.05)
        return abs(r) < threshold
    return None


def return_vol(signed_returns: Iterable[float], min_n: int = RETURN_VOL_MIN_N) -> Optional[float]:
    """mean / stdev of a single-horizon signed series. Not Sharpe. None if n < min_n."""
    series = [float(x) for x in signed_returns]
    if len(series) < min_n:
        return None
    stdev = statistics.stdev(series)
    if stdev <= 0:
        return None
    return statistics.mean(series) / stdev


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _pct(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(value * 100.0, digits)


def _rate(hits: int, n: int) -> Optional[float]:
    if n <= 0:
        return None
    return hits / n


def parse_explicit_stance(text: Optional[str]) -> Optional[str]:
    """BUY/SELL/HOLD from an explicit decision header. No keyword scan."""
    if not text:
        return None
    from tradingagents.graph.signal_processing import extract_explicit_decision

    return extract_explicit_decision(text)


def parse_research_stance(text: Optional[str]) -> Optional[str]:
    """Research-manager stance: explicit decision, else SIGNAL_JSON stance only."""
    explicit = parse_explicit_stance(text)
    if explicit:
        return explicit
    if not text:
        return None
    from tradingagents.reporting.attribution import _extract_signal_json

    signal = _extract_signal_json(text)
    stance = str(signal.get("stance") or "").strip().lower()
    if not stance:
        return None
    if any(token in stance for token in ("buy", "bull")):
        return "BUY"
    if any(token in stance for token in ("sell", "bear")):
        return "SELL"
    if "hold" in stance or "neutral" in stance:
        return "HOLD"
    return None


def _stance_hit(stance: Optional[str], tape_return: Optional[float]) -> Optional[bool]:
    if stance not in ("BUY", "SELL") or tape_return is None:
        return None
    if tape_return == 0:
        return False
    if stance == "BUY":
        return tape_return > 0
    return tape_return < 0


def _empty_agent_row() -> Dict[str, Any]:
    return {"wins": 0, "losses": 0, "win_rate": 0.0, "n": 0, "skipped": 0}


def compute_researcher_attribution(analyses: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """Score bull/bear explicit stances (and research SIGNAL_JSON) vs 7d tape.

    Unparsed researchers are skipped. This is not a tautology of the final decision.
    """
    rows = {
        "bull": {"hits": 0, "misses": 0, "skipped": 0},
        "bear": {"hits": 0, "misses": 0, "skipped": 0},
        "research": {"hits": 0, "misses": 0, "skipped": 0},
    }

    for analysis in analyses:
        tape = getattr(analysis, "actual_return_7d", None)
        if tape is None:
            continue

        bull_stance = parse_explicit_stance(getattr(analysis, "bull_summary", "") or "")
        bear_stance = parse_explicit_stance(getattr(analysis, "bear_summary", "") or "")
        research_text = (
            getattr(analysis, "investment_decision", "")
            or getattr(analysis, "investment_plan", "")
            or ""
        )
        research_stance = parse_research_stance(research_text)

        for key, stance in (
            ("bull", bull_stance),
            ("bear", bear_stance),
            ("research", research_stance),
        ):
            if stance is None:
                rows[key]["skipped"] += 1
                continue
            hit = _stance_hit(stance, tape)
            if hit is None:
                rows[key]["skipped"] += 1
                continue
            if hit:
                rows[key]["hits"] += 1
            else:
                rows[key]["misses"] += 1

    def _pack(raw: Dict[str, int]) -> Dict[str, Any]:
        n = raw["hits"] + raw["misses"]
        win_rate = round(raw["hits"] / n, 4) if n else 0.0
        return {
            "wins": raw["hits"],
            "losses": raw["misses"],
            "win_rate": win_rate,
            "n": n,
            "skipped": raw["skipped"],
        }

    return {
        "bull": _pack(rows["bull"]),
        "bear": _pack(rows["bear"]),
        "research": _pack(rows["research"]),
    }


def summarize_analyses(analyses: Iterable[Any]) -> Dict[str, Any]:
    """Canonical book metrics from stored tape + final decision (fractions, not %)."""
    items = list(analyses)
    tape: Dict[int, List[float]] = {d: [] for d in HORIZONS}
    tape_alpha: Dict[int, List[float]] = {d: [] for d in HORIZONS}
    signed: Dict[int, List[float]] = {d: [] for d in HORIZONS}
    signed_alpha: Dict[int, List[float]] = {d: [] for d in HORIZONS}
    dir_hits: Dict[int, int] = {d: 0 for d in HORIZONS}
    dir_n: Dict[int, int] = {d: 0 for d in HORIZONS}
    hold_hits: Dict[int, int] = {d: 0 for d in HORIZONS}
    hold_n: Dict[int, int] = {d: 0 for d in HORIZONS}
    mixed_hits = 0
    mixed_n = 0

    by_decision_rows: Dict[str, List[Any]] = {"BUY": [], "SELL": [], "HOLD": []}
    by_profile_rows: Dict[str, List[Any]] = {
        "aggressive": [],
        "growth": [],
        "conservative": [],
    }

    for analysis in items:
        kind = normalize_decision(getattr(analysis, "decision", None))
        if kind in by_decision_rows:
            by_decision_rows[kind].append(analysis)
        profile = getattr(analysis, "risk_profile", "") or ""
        if profile in by_profile_rows:
            by_profile_rows[profile].append(analysis)

        was_correct = getattr(analysis, "was_correct", None)
        if was_correct is not None:
            mixed_n += 1
            if was_correct:
                mixed_hits += 1

        for days in HORIZONS:
            r = getattr(analysis, f"actual_return_{days}d", None)
            a = getattr(analysis, f"alpha_{days}d", None)
            if r is not None:
                tape[days].append(float(r))
                sr = signed_return(kind, r)
                if sr is not None:
                    signed[days].append(sr)
                    hit = horizon_correct(kind, r, days)
                    dir_n[days] += 1
                    if hit:
                        dir_hits[days] += 1
                elif kind == "HOLD":
                    hold_n[days] += 1
                    if horizon_correct(kind, r, days):
                        hold_hits[days] += 1
            if a is not None:
                tape_alpha[days].append(float(a))
                sa = signed_return(kind, a)
                if sa is not None:
                    signed_alpha[days].append(sa)

    signed_win_n = len(signed[7])
    signed_wins = sum(1 for r in signed[7] if r > 0)

    summary: Dict[str, Any] = {
        "n": len(items),
        "n_7d": len(tape[7]),
        "n_14d": len(tape[14]),
        "n_30d": len(tape[30]),
        "n_signed_7d": len(signed[7]),
        "n_signed_14d": len(signed[14]),
        "n_signed_30d": len(signed[30]),
        "avg_tape_return_7d": _mean(tape[7]),
        "avg_tape_return_14d": _mean(tape[14]),
        "avg_tape_return_30d": _mean(tape[30]),
        "avg_tape_alpha_7d": _mean(tape_alpha[7]),
        "avg_tape_alpha_14d": _mean(tape_alpha[14]),
        "avg_tape_alpha_30d": _mean(tape_alpha[30]),
        "avg_signed_return_7d": _mean(signed[7]),
        "avg_signed_return_14d": _mean(signed[14]),
        "avg_signed_return_30d": _mean(signed[30]),
        "avg_signed_alpha_7d": _mean(signed_alpha[7]),
        "avg_signed_alpha_14d": _mean(signed_alpha[14]),
        "avg_signed_alpha_30d": _mean(signed_alpha[30]),
        "directional_accuracy_7d": _rate(dir_hits[7], dir_n[7]),
        "directional_accuracy_14d": _rate(dir_hits[14], dir_n[14]),
        "directional_accuracy_30d": _rate(dir_hits[30], dir_n[30]),
        "directional_hits_7d": dir_hits[7],
        "directional_n_7d": dir_n[7],
        "hold_accuracy_7d": _rate(hold_hits[7], hold_n[7]),
        "hold_hits_7d": hold_hits[7],
        "hold_n_7d": hold_n[7],
        "win_rate": _rate(signed_wins, signed_win_n),
        "mixed_horizon_accuracy": _rate(mixed_hits, mixed_n),
        "mixed_horizon_n": mixed_n,
        "return_vol_7d": return_vol(signed[7]),
        "implied_spy_return_7d": None,
        "agent_attribution": compute_researcher_attribution(items),
        "by_decision": {},
        "by_risk_profile": {},
    }

    tape7 = summary["avg_tape_return_7d"]
    alpha7 = summary["avg_tape_alpha_7d"]
    if tape7 is not None and alpha7 is not None:
        summary["implied_spy_return_7d"] = tape7 - alpha7

    for key, subset in by_decision_rows.items():
        sub_tape = [
            float(a.actual_return_7d)
            for a in subset
            if getattr(a, "actual_return_7d", None) is not None
        ]
        sub_signed = [
            sr
            for a in subset
            if (sr := signed_return(key, getattr(a, "actual_return_7d", None))) is not None
        ]
        sub_signed_alpha = [
            sa
            for a in subset
            if (sa := signed_return(key, getattr(a, "alpha_7d", None))) is not None
        ]
        if key == "HOLD":
            hits = sum(
                1
                for a in subset
                if horizon_correct("HOLD", getattr(a, "actual_return_7d", None), 7)
            )
            n_scored = sum(
                1 for a in subset if getattr(a, "actual_return_7d", None) is not None
            )
            accuracy = _rate(hits, n_scored)
            avg_signed = None
            avg_signed_alpha = None
            wins = hits
        else:
            hits = sum(
                1
                for a in subset
                if horizon_correct(key, getattr(a, "actual_return_7d", None), 7)
            )
            n_scored = len(sub_signed)
            accuracy = _rate(hits, n_scored)
            avg_signed = _mean(sub_signed)
            avg_signed_alpha = _mean(sub_signed_alpha)
            wins = hits
        summary["by_decision"][key] = {
            "count": len(subset),
            "n_7d": len(sub_tape),
            "wins": wins,
            "accuracy": accuracy,
            "avg_signed_return_7d": avg_signed,
            "avg_signed_alpha_7d": avg_signed_alpha,
            "avg_tape_return_7d": _mean(sub_tape),
        }

    for profile, subset in by_profile_rows.items():
        if not subset:
            continue
        sub_signed = [
            sr
            for a in subset
            if (sr := signed_return(getattr(a, "decision", None), getattr(a, "actual_return_7d", None)))
            is not None
        ]
        hits = sum(
            1
            for a in subset
            if normalize_decision(getattr(a, "decision", None)) in ("BUY", "SELL")
            and horizon_correct(
                getattr(a, "decision", None),
                getattr(a, "actual_return_7d", None),
                7,
            )
        )
        mixed = [a for a in subset if getattr(a, "was_correct", None) is not None]
        mixed_wins = sum(1 for a in mixed if a.was_correct)
        summary["by_risk_profile"][profile] = {
            "count": len(subset),
            "n_signed_7d": len(sub_signed),
            "wins": hits,
            "accuracy": _rate(hits, len(sub_signed)),
            "avg_signed_return_7d": _mean(sub_signed),
            "mixed_horizon_accuracy": _rate(mixed_wins, len(mixed)),
        }

    return summary


def stats_api_payload(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Percent-scaled payload for `/api/backtest/stats` and dashboard backtest cards."""
    by_decision = {}
    for key, row in (summary.get("by_decision") or {}).items():
        if not row.get("count"):
            continue
        accuracy = row.get("accuracy")
        signed_ret = row.get("avg_signed_return_7d")
        tape_ret = row.get("avg_tape_return_7d")
        signed_alpha = row.get("avg_signed_alpha_7d")
        headline_ret = tape_ret if key == "HOLD" else signed_ret
        headline_alpha = None if key == "HOLD" else signed_alpha
        by_decision[key] = {
            "count": row.get("count", 0),
            "n_7d": row.get("n_7d", 0),
            "wins": row.get("wins", 0),
            "accuracy": _pct(accuracy, 1) if accuracy is not None else 0,
            "avg_return_7d": _pct(headline_ret) if headline_ret is not None else 0,
            "avg_signed_return_7d": _pct(signed_ret) if signed_ret is not None else None,
            "avg_alpha_7d": _pct(headline_alpha) if headline_alpha is not None else None,
            "avg_signed_alpha_7d": _pct(signed_alpha) if signed_alpha is not None else None,
            "avg_tape_return_7d": _pct(tape_ret) if tape_ret is not None else 0,
        }

    by_profile = {}
    for key, row in (summary.get("by_risk_profile") or {}).items():
        acc = row.get("accuracy")
        signed_ret = row.get("avg_signed_return_7d")
        by_profile[key] = {
            "count": row.get("count", 0),
            "n_signed_7d": row.get("n_signed_7d", 0),
            "wins": row.get("wins", 0),
            "accuracy": _pct(acc, 1) if acc is not None else 0,
            "avg_return_7d": _pct(signed_ret) if signed_ret is not None else 0,
            "avg_signed_return_7d": _pct(signed_ret) if signed_ret is not None else 0,
        }

    dir_acc = summary.get("directional_accuracy_7d")
    win_rate = summary.get("win_rate")
    mixed = summary.get("mixed_horizon_accuracy")
    hold_acc = summary.get("hold_accuracy_7d")

    return {
        "total_backtested": summary.get("n", 0),
        "n_7d": summary.get("n_7d", 0),
        "n_14d": summary.get("n_14d", 0),
        "n_30d": summary.get("n_30d", 0),
        "n_signed_7d": summary.get("n_signed_7d", 0),
        "directional_accuracy_7d": _pct(dir_acc, 1) if dir_acc is not None else 0,
        "directional_hits_7d": summary.get("directional_hits_7d", 0),
        "directional_n_7d": summary.get("directional_n_7d", 0),
        "accuracy": _pct(dir_acc, 1) if dir_acc is not None else 0,
        "win_rate": _pct(win_rate, 1) if win_rate is not None else 0,
        "mixed_horizon_accuracy": _pct(mixed, 1) if mixed is not None else 0,
        "hold_accuracy_7d": _pct(hold_acc, 1) if hold_acc is not None else None,
        "hold_hits_7d": summary.get("hold_hits_7d", 0),
        "hold_n_7d": summary.get("hold_n_7d", 0),
        "avg_signed_return_7d": _pct(summary.get("avg_signed_return_7d")),
        "avg_signed_return_14d": _pct(summary.get("avg_signed_return_14d")),
        "avg_signed_return_30d": _pct(summary.get("avg_signed_return_30d")),
        "avg_signed_alpha_7d": _pct(summary.get("avg_signed_alpha_7d")),
        "avg_signed_alpha_14d": _pct(summary.get("avg_signed_alpha_14d")),
        "avg_signed_alpha_30d": _pct(summary.get("avg_signed_alpha_30d")),
        "avg_tape_return_7d": _pct(summary.get("avg_tape_return_7d")),
        "avg_tape_return_14d": _pct(summary.get("avg_tape_return_14d")),
        "avg_tape_return_30d": _pct(summary.get("avg_tape_return_30d")),
        "avg_tape_alpha_7d": _pct(summary.get("avg_tape_alpha_7d")),
        "avg_tape_alpha_14d": _pct(summary.get("avg_tape_alpha_14d")),
        "avg_tape_alpha_30d": _pct(summary.get("avg_tape_alpha_30d")),
        "avg_return_7d": _pct(summary.get("avg_tape_return_7d")) or 0,
        "avg_return_14d": _pct(summary.get("avg_tape_return_14d")) or 0,
        "avg_return_30d": _pct(summary.get("avg_tape_return_30d")) or 0,
        "avg_alpha_7d": _pct(summary.get("avg_tape_alpha_7d")) or 0,
        "avg_alpha_14d": _pct(summary.get("avg_tape_alpha_14d")) or 0,
        "avg_alpha_30d": _pct(summary.get("avg_tape_alpha_30d")) or 0,
        "implied_spy_return_7d": _pct(summary.get("implied_spy_return_7d")),
        "return_vol_7d": (
            round(summary["return_vol_7d"], 4) if summary.get("return_vol_7d") is not None else None
        ),
        "by_decision": by_decision,
        "by_risk_profile": by_profile,
        "agent_attribution": summary.get("agent_attribution") or {
            "bull": _empty_agent_row(),
            "bear": _empty_agent_row(),
            "research": _empty_agent_row(),
        },
    }


def empty_stats_payload() -> Dict[str, Any]:
    return stats_api_payload(summarize_analyses([]))
