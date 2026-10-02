"""Decision scorecard: thesis / setup / risk-budget plus profile-limit gates."""
from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional, Tuple

from tradingagents.reporting.position_action import (
    extract_decision_json_from_text,
    format_decision_label,
    parse_position_action_from_state,
)

PROFILE_LIMITS: Dict[str, Dict[str, float]] = {
    "aggressive": {"beta": 2.5, "max_drawdown_pct": 40.0, "var_95_pct": 4.0},
    "growth": {"beta": 1.8, "max_drawdown_pct": 25.0, "var_95_pct": 3.0},
    "balanced": {"beta": 1.8, "max_drawdown_pct": 25.0, "var_95_pct": 3.0},
    "conservative": {"beta": 1.2, "max_drawdown_pct": 15.0, "var_95_pct": 2.0},
}


def resolve_effective_risk_limits(
    risk_profile: str,
    investment_profile_key: Optional[str] = None,
    resolved_from: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """Resolve source-aware limits without silently broadening Auto mandates."""
    profile = str(risk_profile or "growth").lower().strip()
    base_profile = "growth" if profile == "balanced" else profile
    base = dict(PROFILE_LIMITS.get(base_profile) or PROFILE_LIMITS["growth"])

    cfg = config
    if cfg is None:
        from tradingagents.default_config import DEFAULT_CONFIG

        cfg = DEFAULT_CONFIG
    overrides_cfg = (cfg or {}).get("profile_risk_limit_overrides", {})
    source = str(resolved_from or "").lower().strip()
    if source in {"metadata_cache", "metadata_refresh"} and not overrides_cfg.get(
        "allow_metadata_resolved_risk_relaxation", False
    ):
        return base

    key = str(investment_profile_key or "").strip()
    override = (overrides_cfg.get(base_profile) or {}).get(key)
    if not isinstance(override, dict):
        return base
    return {
        metric: float(override.get(metric, base[metric]))
        for metric in ("beta", "max_drawdown_pct", "var_95_pct")
    }

_STAGED_LANGUAGE = re.compile(
    r"(staged|one[- ]third|1/3|⅓|add (the )?(second|final)|satellite)",
    re.I,
)
_LOCKUP_MILLION = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*million\s+(?:[\w-]+\s+){0,8}shares",
    re.I,
)
_VOLUME_MILLION = re.compile(
    r"volume(?:\s+of)?\s+\*?(\d[\d,]*(?:\.\d+)?)\s*million",
    re.I,
)


def _stance(text: str) -> str:
    blob = (text or "").upper()
    if "STANCE\":\"BULLISH" in blob or '"STANCE": "BULLISH"' in blob:
        return "bullish"
    if "STANCE\":\"BEARISH" in blob or '"STANCE": "BEARISH"' in blob:
        return "bearish"
    if "FINAL TRANSACTION PROPOSAL: **BUY**" in blob:
        return "bullish"
    if "FINAL TRANSACTION PROPOSAL: **SELL**" in blob:
        return "bearish"
    return "neutral"


def _score_box(status: str) -> str:
    key = (status or "").lower()
    if key == "pass":
        return "pass"
    if key == "fail":
        return "fail"
    return "watch"


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_risk_metrics_from_text(*texts: str) -> Dict[str, Optional[float]]:
    blob = "\n".join(t for t in texts if t) or ""
    out: Dict[str, Optional[float]] = {
        "beta": None,
        "max_drawdown_pct": None,
        "var_95_pct": None,
    }
    beta = re.search(r"\bbeta[:\s]+(-?\d+(?:\.\d+)?)", blob, re.I)
    if beta:
        out["beta"] = float(beta.group(1))
    dd = re.search(r"max(?:imum)?\s+drawdown[:\s]+(-?\d+(?:\.\d+)?)\s*%", blob, re.I)
    if dd:
        out["max_drawdown_pct"] = abs(float(dd.group(1)))
    var = re.search(r"VaR[^0-9\n-]{0,24}(-?\d+(?:\.\d+)?)\s*%", blob, re.I)
    if var:
        out["var_95_pct"] = abs(float(var.group(1)))
    return out


def resolve_risk_metrics(state: Dict[str, Any], ticker: str) -> Dict[str, Optional[float]]:
    stored = state.get("risk_metrics") if isinstance(state.get("risk_metrics"), dict) else {}
    parsed = extract_risk_metrics_from_text(
        state.get("final_trade_decision") or "",
        state.get("risk_assessment") or "",
        ((state.get("risk_debate_state") or {}).get("judge_decision") if isinstance(state.get("risk_debate_state"), dict) else "") or "",
    )
    metrics = {
        "beta": _num(stored.get("beta")) if stored else None,
        "max_drawdown_pct": _num(stored.get("max_drawdown_pct")) if stored else None,
        "var_95_pct": _num(stored.get("var_95_pct")) if stored else None,
    }
    for key in metrics:
        if metrics[key] is None:
            metrics[key] = parsed.get(key)
        elif key in {"max_drawdown_pct", "var_95_pct"}:
            metrics[key] = abs(float(metrics[key]))
    return metrics


def _gate_tolerance_pct(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config
    if cfg is None:
        from tradingagents.default_config import DEFAULT_CONFIG

        cfg = DEFAULT_CONFIG
    try:
        return max(0.0, float((cfg or {}).get("risk_gate_tolerance_pct", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _commodity_etf_from_state(state: Dict[str, Any], ticker: str) -> bool:
    from tradingagents.dataflows.instrument_identity import is_commodity_etf

    identity = state.get("instrument_identity") if isinstance(state.get("instrument_identity"), dict) else None
    return is_commodity_etf(ticker, identity)


def resolve_scorecard_limits(
    state: Dict[str, Any],
    ticker: str,
    risk_profile: str,
    investment_profile_key: Optional[str] = None,
    resolved_from: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, float], bool]:
    """Return (limits, skip_beta_gate) for scorecard evaluation."""
    limits_key = investment_profile_key
    if _commodity_etf_from_state(state, ticker):
        limits_key = "commodity_cyclical"
    limits = resolve_effective_risk_limits(
        risk_profile,
        limits_key,
        resolved_from,
        config,
    )
    skip_beta = _commodity_etf_from_state(state, ticker)
    return limits, skip_beta


def evaluate_profile_gates(
    metrics: Dict[str, Optional[float]],
    risk_profile: str,
    investment_profile_key: Optional[str] = None,
    resolved_from: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    limits: Optional[Dict[str, float]] = None,
    skip_beta: bool = False,
    gate_tolerance_pct: Optional[float] = None,
) -> List[Dict[str, Any]]:
    limits = limits or resolve_effective_risk_limits(
        risk_profile,
        investment_profile_key,
        resolved_from,
        config,
    )
    if gate_tolerance_pct is None:
        gate_tolerance_pct = _gate_tolerance_pct(config)
    rows = []
    mapping = (
        ("Beta", "beta", limits["beta"], "≤"),
        ("Max drawdown", "max_drawdown_pct", limits["max_drawdown_pct"], "≤"),
        ("VaR 95%", "var_95_pct", limits["var_95_pct"], "≤"),
    )
    for label, key, limit, op in mapping:
        if skip_beta and key == "beta":
            rows.append(
                {
                    "label": label,
                    "limit": limit,
                    "observed": metrics.get(key),
                    "pass": None,
                    "op": "N/A",
                    "skipped": True,
                }
            )
            continue
        observed = metrics.get(key)
        effective_limit = limit * (1.0 + gate_tolerance_pct) if gate_tolerance_pct else limit
        failed = observed is not None and observed > effective_limit
        rows.append(
            {
                "label": label,
                "limit": limit,
                "effective_limit": effective_limit if gate_tolerance_pct else limit,
                "observed": observed,
                "pass": None if observed is None else (not failed),
                "op": op,
            }
        )
    return rows


def _thesis_status(state: Dict[str, Any]) -> Tuple[str, str]:
    fund = _stance(state.get("fundamentals_report") or "")
    research = _stance(state.get("investment_plan") or "")
    if fund == "bullish" or research == "bullish":
        return "pass", "Operating trajectory / revisions support the long-term case."
    if fund == "bearish" and research == "bearish":
        return "fail", "Fundamentals and research both lean against the thesis."
    return "watch", "Thesis is mixed — not a standalone reason to buy or sell."


def _setup_status(state: Dict[str, Any]) -> Tuple[str, str]:
    market = _stance(state.get("market_report") or "")
    news = _stance(state.get("news_report") or "")
    lockup = extract_lockup_adv(state)
    if lockup.get("ratio") and lockup["ratio"] >= 1.0:
        return "fail", f"Live supply event (~{lockup['ratio']:.1f}× session volume) and/or weak tape."
    if market == "bearish":
        return "fail", "Technicals do not confirm entry."
    if market == "neutral" or news == "neutral":
        return "watch", "Setup is incomplete (below confirmation levels or event overhang)."
    return "pass", "Tape and news support an actionable entry."


def _risk_budget_status(gates: List[Dict[str, Any]]) -> Tuple[str, str]:
    failed = [g for g in gates if g.get("pass") is False]
    if failed:
        names = ", ".join(g["label"] for g in failed)
        return "fail", f"Outside profile limits: {names}."
    if any(g.get("pass") is None for g in gates):
        return "watch", "Risk metrics incomplete — treat as unconstrained until filled."
    return "pass", "Beta, drawdown, and VaR are inside this book's limits."


def extract_lockup_adv(state: Dict[str, Any]) -> Dict[str, Any]:
    blob = "\n".join(
        [
            state.get("news_report") or "",
            state.get("sentiment_report") or "",
            state.get("market_report") or "",
            state.get("final_trade_decision") or "",
        ]
    )
    shares_m = None
    volume_m = None
    sm = _LOCKUP_MILLION.search(blob)
    if sm:
        shares_m = float(sm.group(1).replace(",", ""))
    vm = _VOLUME_MILLION.search(blob)
    if vm:
        volume_m = float(vm.group(1).replace(",", ""))
    ratio = None
    if shares_m and volume_m and volume_m > 0:
        ratio = shares_m / volume_m
    return {"eligible_million": shares_m, "volume_million": volume_m, "ratio": ratio}


def extract_satellite_ladder(state: Dict[str, Any], decision: str, position_action: str) -> str:
    trader = state.get("trader_investment_plan") or ""
    if not trader or not _STAGED_LANGUAGE.search(trader):
        return ""
    if str(decision or "").upper() not in {"HOLD", "SELL"} and str(position_action or "").upper() != "AVOID":
        return ""
    stripped = re.sub(r"SIGNAL_JSON:\s*\{.*?\}", "", trader, count=1, flags=re.S)
    stripped = re.sub(r"FINAL TRANSACTION PROPOSAL: \*\*\w+\*\*", "", stripped)
    text = stripped.strip()
    if len(text) > 1600:
        text = text[:1600].rsplit(" ", 1)[0] + "…"
    return text


def compute_decision_scorecard(
    state: Dict[str, Any],
    decision: str,
    ticker: str,
    risk_profile: str = "growth",
) -> Dict[str, Any]:
    action = parse_position_action_from_state(state) or ""
    if not action:
        dj = extract_decision_json_from_text(state.get("final_trade_decision") or "")
        action = str(dj.get("position_action") or "")
    final_rating = str(state.get("final_rating") or "").strip().upper()
    if final_rating == "REVIEW":
        label = "REVIEW"
    elif final_rating in {"BUY", "SELL", "HOLD"}:
        label = format_decision_label(final_rating, action)
    else:
        label = format_decision_label(decision, action)
    metrics = resolve_risk_metrics(state, ticker)
    profile = state.get("investment_profile") if isinstance(state.get("investment_profile"), dict) else {}
    profile_key = state.get("investment_profile_key") or profile.get("profile_key")
    resolved_from = state.get("investment_profile_resolved_from") or profile.get("resolved_from")
    effective_limits = state.get("effective_risk_limits")
    skip_beta = False
    if not isinstance(effective_limits, dict):
        effective_limits, skip_beta = resolve_scorecard_limits(
            state,
            ticker,
            risk_profile,
            profile_key,
            resolved_from,
        )
    elif _commodity_etf_from_state(state, ticker):
        skip_beta = True
    gates = evaluate_profile_gates(
        metrics,
        risk_profile,
        profile_key,
        resolved_from,
        limits=effective_limits,
        skip_beta=skip_beta,
    )
    thesis, thesis_why = _thesis_status(state)
    setup, setup_why = _setup_status(state)
    budget, budget_why = _risk_budget_status(gates)
    dj = extract_decision_json_from_text(state.get("final_trade_decision") or "")
    try:
        composite = float(dj.get("composite_signal")) if dj.get("composite_signal") is not None else None
    except (TypeError, ValueError):
        composite = None
    lockup = extract_lockup_adv(state)
    ladder = extract_satellite_ladder(state, decision, action)
    screening = state.get("screening_context") if isinstance(state.get("screening_context"), dict) else {}
    return {
        "decision_label": label,
        "position_action": action,
        "thesis": thesis,
        "thesis_why": thesis_why,
        "setup": setup,
        "setup_why": setup_why,
        "risk_budget": budget,
        "risk_budget_why": budget_why,
        "gates": gates,
        "composite": composite,
        "lockup": lockup,
        "satellite_ladder": ladder,
        "screening": screening,
        "risk_profile": str(risk_profile or "growth"),
        "investment_profile_key": profile_key,
        "investment_profile_resolved_from": resolved_from,
        "effective_risk_limits": effective_limits,
        "standalone": not bool(screening.get("run_id") or state.get("screening_summary_text")),
    }


def _fmt_obs(value: Optional[float], suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.2f}{suffix}"


def render_decision_scorecard_html(card: Dict[str, Any]) -> str:
    boxes = [
        ("Thesis", card.get("thesis"), card.get("thesis_why")),
        ("Setup", card.get("setup"), card.get("setup_why")),
        ("Risk budget", card.get("risk_budget"), card.get("risk_budget_why")),
    ]
    box_html = []
    for title, status, why in boxes:
        cls = _score_box(str(status))
        box_html.append(
            "<div class='score-box {cls}'>"
            "<div class='score-box-kicker'>{title}</div>"
            "<div class='score-box-status'>{status}</div>"
            "<div class='score-box-why'>{why}</div>"
            "</div>".format(
                cls=html.escape(cls),
                title=html.escape(title),
                status=html.escape(str(status or "watch").upper()),
                why=html.escape(str(why or "")),
            )
        )
    rows = []
    for gate in card.get("gates") or []:
        observed = gate.get("observed")
        limit = gate.get("limit")
        passed = gate.get("pass")
        mark = "—" if passed is None else ("Pass" if passed else "Fail")
        cls = "muted" if passed is None else ("ok" if passed else "bad")
        suffix = "" if "Beta" in str(gate.get("label")) else "%"
        rows.append(
            "<tr class='{cls}'><td>{label}</td><td>{op} {limit}{suffix}</td>"
            "<td>{obs}</td><td>{mark}</td></tr>".format(
                cls=cls,
                label=html.escape(str(gate.get("label"))),
                op=html.escape(str(gate.get("op") or "≤")),
                limit=html.escape(f"{limit:g}" if isinstance(limit, (int, float)) else "—"),
                suffix=suffix,
                obs=html.escape(_fmt_obs(observed, suffix)),
                mark=html.escape(mark),
            )
        )
    composite = card.get("composite")
    composite_note = ""
    if isinstance(composite, (int, float)):
        composite_note = (
            f"<p class='score-note'>Composite {composite:+.2f} is a <em>thesis/signal</em> reading, "
            "not an entry instruction. Decision = min(thesis, setup, risk budget).</p>"
        )
    lockup = card.get("lockup") or {}
    lockup_html = ""
    if lockup.get("eligible_million") and lockup.get("volume_million"):
        ratio = lockup.get("ratio")
        lockup_html = (
            "<div class='lockup-note'><strong>Supply shock:</strong> "
            f"{lockup['eligible_million']:.1f}M eligible shares vs "
            f"{lockup['volume_million']:.1f}M session volume"
            + (f" ({ratio:.1f}× one day’s tape)." if ratio else ".")
            + " Do not buy the unlock; reassess after absorption.</div>"
        )
    screening = card.get("screening") or {}
    screen_html = ""
    if screening.get("run_id"):
        screen_html = (
            "<div class='lockup-note'><strong>Screener overlay:</strong> run #{run} · "
            "Opp {opp} / Comp {comp} / Entry {entry} · book {book}. "
            "Overlay rank is not a buy ticket.</div>".format(
                run=html.escape(str(screening.get("run_id"))),
                opp=html.escape(str(screening.get("opp_score") if screening.get("opp_score") is not None else "—")),
                comp=html.escape(str(screening.get("composite") if screening.get("composite") is not None else "—")),
                entry=html.escape(str(screening.get("entry_quality") if screening.get("entry_quality") is not None else "—")),
                book=html.escape(str(screening.get("preset") or screening.get("resolved_preset") or "adaptive")),
            )
        )
    elif card.get("standalone"):
        screen_html = (
            "<div class='lockup-note'><strong>Standalone analysis</strong> — "
            "no screener packet was attached. Rank on the board is not validated here.</div>"
        )
    ladder = card.get("satellite_ladder") or ""
    ladder_html = ""
    if ladder:
        ladder_html = (
            "<div class='satellite-appendix'>"
            "<h3>Satellite spec (outside core book)</h3>"
            "<p>Core decision remains {label}. The trader ladder below is optional and "
            "explicitly outside this profile’s risk limits.</p>"
            "<pre>{ladder}</pre>"
            "</div>".format(
                label=html.escape(str(card.get("decision_label") or "HOLD")),
                ladder=html.escape(ladder),
            )
        )
    profile = html.escape(str(card.get("risk_profile") or "growth").title())
    investment_profile = html.escape(
        str(card.get("investment_profile_key") or "Generic / legacy").replace("_", " ").title()
    )
    resolved_from = html.escape(
        str(card.get("investment_profile_resolved_from") or "legacy").replace("_", " ")
    )
    return (
        "<div class='decision-scorecard'>"
        f"<div class='score-grid'>{''.join(box_html)}</div>"
        f"{composite_note}"
        f"{lockup_html}"
        f"{screen_html}"
        "<div class='lockup-note'><strong>Investment profile:</strong> "
        f"{investment_profile} ({resolved_from})</div>"
        "<table class='gate-table'><caption>Profile limits ({profile})</caption>"
        "<thead><tr><th>Metric</th><th>Limit</th><th>Observed</th><th>Gate</th></tr></thead>"
        "<tbody>{rows}</tbody></table>"
        f"{ladder_html}"
        "</div>"
    ).format(profile=profile, rows="".join(rows))
