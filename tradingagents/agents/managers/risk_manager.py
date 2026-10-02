import logging
import time
import json

from tradingagents.utils.token_management import truncate_to_token_limit

logger = logging.getLogger("tradingagents.agents.managers.risk_manager")


# ── Shared preamble (identical across all profiles) ─────────────────────
_ROLE_PREAMBLE = """You are the Risk Management Judge. Your job is to evaluate the debate between three risk analysts — Risky, Neutral, and Safe/Conservative — together with the quantitative signal summary below, and reach an evidence-based investment decision: BUY, HOLD, or SELL.

Approach every decision objectively. Your risk profile sets *evidence thresholds*, not conclusions. Analyze the data first, then apply the thresholds."""

_EVIDENCE_PROTOCOL = """
**Evidence Assessment Protocol (follow in order):**

1. Review the Quantitative Signal Summary below. Note how many of the signal dimensions lean bullish, neutral, or bearish, and the composite score.
2. Review the analyst debate history. Identify the strongest factual arguments from each side.
3. Cross-reference debater claims against the Source Data Reference appendix.
4. Identify any critical red flags (fundamental deterioration, broken thesis, margin collapse, declining revenue).
5. Identify any strong catalysts (earnings beat, analyst upgrades, estimate revisions up, expanding margins).
6. Apply the Decision Thresholds for your risk profile.
7. State which dimensions support your decision and which contradict it.
"""

_DECISION_JSON_INSTRUCTION = """
**Required Output**: Begin your response with exactly ONE line in this format, then write the reasoning:
DECISION_JSON: {"decision":"BUY|HOLD|SELL","position_action":"FULL_BUY|ADD|HOLD|REDUCE|FULL_SELL|AVOID","conviction":"high|medium|low","dimensions_bullish":N,"dimensions_bearish":N,"composite_signal":X.XX,"key_factors":["factor1","factor2","factor3"],"primary_risk":"..."}
On its own line in English (after the JSON line), include exactly: Rating: BUY|SELL|HOLD|REVIEW
Do not omit this leading line. Use position_action to reflect sizing nuance:
- FULL_BUY / ADD for new or increased long exposure
- HOLD for maintain / no change
- REDUCE for partial trim (e.g. 50-80%) when thesis is weaker but not broken
- FULL_SELL for exit / avoid new longs entirely
- AVOID for do not initiate (especially when already flat)
When risk analysts recommend reducing exposure rather than full liquidation, use decision SELL with position_action REDUCE.
When already flat and the book should not initiate, use decision HOLD with position_action AVOID (shown as HOLD (Avoid)).
Do not convert a staged BUY ladder into the core decision when a hard risk-limit or live supply event fails. Keep HOLD/AVOID as the core book action and describe any ladder as satellite-only.
Quantify lockups as eligible shares / session volume when both numbers appear in the source data (e.g. 319M eligible / 119.5M volume ≈ 2.7× ADV).
After the JSON line, write a short **Decision: BUY/HOLD/SELL** section that matches the JSON.
"""


# ── Profile-specific threshold blocks ──────────────────────────────────

AGGRESSIVE_THRESHOLDS = """
**Risk Profile: AGGRESSIVE (opportunity-capture, higher risk tolerance)**

Decision Thresholds:
- BUY: At least 1 dimension bullish AND no fundamental deterioration. Composite signal > -0.1. Treat technical weakness as potential entry opportunity for quality companies.
- HOLD: Only when composite signal is genuinely near zero AND no clear directional catalyst exists.
- SELL: Clear fundamental deterioration (declining revenue, broken thesis, margin collapse) across 2+ dimensions.

Risk Tolerance: Apply the Effective Risk Limits supplied below. Volatility is the price of growth.
"""

GROWTH_THRESHOLDS = """
**Risk Profile: GROWTH (balanced, risk-aware)**

Decision Thresholds:
- BUY: At least 2 dimensions bullish AND composite signal > +0.15 AND no unresolved critical risks AND beta/drawdown/VaR inside the limits below.
- HOLD: Mixed signals (1 bullish, 1 bearish) OR composite near zero OR single-dimension bullish case only OR any hard risk-limit breach.
- HOLD + AVOID: Use when already flat and a live supply event (lockup) or profile-limit fail means do not initiate. Composite may still look bullish — that is a thesis reading, not an entry.
- SELL / REDUCE only if Research or Trader is SELL or the fundamental thesis is broken (declining revenue, margin collapse, broken thesis). Not "2+ bearish dimensions" alone.

Risk Tolerance: Apply the Effective Risk Limits supplied below. A single breach vetoes BUY. Balance growth opportunity with capital protection.
"""

CONSERVATIVE_THRESHOLDS = """
**Risk Profile: CONSERVATIVE (capital preservation)**

Decision Thresholds:
- BUY: 3+ dimensions bullish AND composite signal > +0.35 AND favorable risk/reward AND limited downside AND quantitative risk metrics within tolerance.
- HOLD: Default when conviction is insufficient. Mixed signals stay HOLD.
- SELL: Any meaningful fundamental deterioration OR 2+ dimensions bearish.

Risk Tolerance: Apply the Effective Risk Limits supplied below. The first rule is don't lose money.
"""


def _build_prompt(
    thresholds_block: str,
    signal_summary: str,
    trader_plan: str,
    past_memory_str: str,
    history: str,
    source_data_ref: str,
    risk_context: str,
    options_context: str,
    profile_block: str,
) -> str:
    """Assemble the full Risk Manager prompt from components."""
    sections = [
        _ROLE_PREAMBLE,
        thresholds_block,
        _EVIDENCE_PROTOCOL,
        signal_summary,
    ]

    if risk_context:
        sections.append(risk_context)

    if options_context:
        sections.append(options_context)

    sections.append(f"""
**Trader's Proposed Plan:**
{trader_plan}
""")

    if past_memory_str.strip():
        sections.append(f"""
**Lessons from Past Decisions:**
{past_memory_str}
""")

    sections.append(f"""
---
**Analysts Debate History:**
{history}
---
""")

    if source_data_ref:
        sections.append(source_data_ref)

    if profile_block:
        sections.append(profile_block)

    sections.append(_DECISION_JSON_INSTRUCTION)

    return "\n".join(sections)


def _truncate_report(text: str, max_tokens: int = 1000) -> str:
    """Truncate a report keeping intro and tail (for SIGNAL_JSON)."""
    if not text:
        return ""
    truncated = truncate_to_token_limit(text, max_tokens)
    if len(truncated) < len(text):
        tail = text[-600:] if len(text) > 600 else ""
        if tail and tail not in truncated:
            truncated = truncated + "\n...\n" + tail
    return truncated


def _build_source_data_reference(state: dict) -> str:
    """Build a truncated appendix of raw analyst reports for cross-referencing."""
    parts = ["--- SOURCE DATA REFERENCE ---"]
    for label, field in [
        ("Market Report", "market_report"),
        ("Fundamentals Report", "fundamentals_report"),
        ("News Report", "news_report"),
        ("Sentiment Report", "sentiment_report"),
    ]:
        raw = state.get(field, "")
        if raw:
            parts.append(f"[{label}]:\n{_truncate_report(raw)}")
    if len(parts) > 1:
        return "\n\n".join(parts)
    return ""


def create_risk_manager(llm, memory, risk_profile="conservative"):
    """
    Create a risk manager node with configurable risk profile.

    The risk profile sets evidence thresholds for BUY/HOLD/SELL decisions,
    not the conclusions themselves.
    """
    def risk_manager_node(state) -> dict:
        company_name = state["company_of_interest"]

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        market_research_report = state["market_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        sentiment_report = state["sentiment_report"]

        # Phase 3a fix: use Trader's actual output, not Research Manager's
        trader_plan = state.get("trader_investment_plan") or state.get("investment_plan", "")

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        # Phase 1: Compute quantitative signal summary
        signal_summary_text = ""
        signal_result = {}
        try:
            from tradingagents.graph.signal_aggregator import compute_signal_summary
            from tradingagents.dataflows.config import get_config
            config = get_config()
            data_completeness = config.get("data_completeness", 1.0)
            signal_result = compute_signal_summary(
                state, company_name, data_completeness=data_completeness,
            )
            signal_summary_text = signal_result.get("text_block", "")
        except Exception as e:
            logger.warning("Signal aggregation failed for %s: %s", company_name, e)

        # Fetch quantitative risk metrics
        risk_context = ""
        risk_metrics_payload = {}
        try:
            from tradingagents.dataflows.yfinance_extended import format_risk_context
            from tradingagents.dataflows.risk_metrics import compute_risk_metrics
            risk_metrics_payload = compute_risk_metrics(company_name) or {}
            risk_context = format_risk_context(company_name)
        except Exception as e:
            logger.debug("Risk metrics unavailable for %s: %s", company_name, e)

        # Fetch options intelligence
        options_context = ""
        try:
            from tradingagents.dataflows.yfinance_extended import format_options_context
            options_context = format_options_context(company_name)
        except Exception as e:
            logger.debug("Options data unavailable for %s: %s", company_name, e)

        # Phase 3b: Build source data reference appendix
        source_data_ref = _build_source_data_reference(state)

        # Investment profile directive
        profile_block = ""
        profile = state.get("investment_profile") or {}
        key_risks = profile.get("key_risks", "")
        risk_benchmark = profile.get("risk_benchmark", "")
        if key_risks or risk_benchmark:
            profile_block = f"\n**Investment Profile Focus** ({profile.get('display_name', 'Default')}):"
            if key_risks:
                profile_block += f"\nKey Risks to Evaluate: {key_risks}"
            if risk_benchmark:
                profile_block += f"\nBenchmark: {risk_benchmark}"
            profile_block += "\nApply this risk lens throughout your evaluation."

        # Resolve source-aware risk limits before the decision prompt is built.
        selected_risk_profile = state.get("risk_profile") or risk_profile
        effective_risk_limits = state.get("effective_risk_limits")
        if not isinstance(effective_risk_limits, dict):
            effective_risk_limits = {}
        try:
            profile_key = profile.get("profile_key") or state.get("investment_profile_key")
            resolved_from = profile.get("resolved_from") or state.get("investment_profile_resolved_from")
            if not effective_risk_limits:
                from tradingagents.dataflows.config import get_config
                from tradingagents.reporting.decision_scorecard import resolve_effective_risk_limits

                effective_risk_limits = resolve_effective_risk_limits(
                    selected_risk_profile,
                    profile_key,
                    resolved_from,
                    get_config(),
                )
            profile_block += (
                "\n\n**Effective Risk Limits** "
                f"(risk={selected_risk_profile}, profile={profile_key or 'generic'}, "
                f"source={resolved_from or 'none'}): "
                f"beta ≤ {effective_risk_limits['beta']:.1f}; "
                f"max drawdown ≤ {effective_risk_limits['max_drawdown_pct']:.0f}%; "
                f"VaR 95% ≤ {effective_risk_limits['var_95_pct']:.1f}%."
            )
        except Exception as exc:
            logger.debug("Effective risk-limit resolution failed for %s: %s", company_name, exc)

        # Select thresholds based on risk profile
        if selected_risk_profile == "aggressive":
            thresholds = AGGRESSIVE_THRESHOLDS
            logger.info("Using AGGRESSIVE risk profile (opportunity-capture)")
        elif selected_risk_profile in ("growth", "balanced"):
            thresholds = GROWTH_THRESHOLDS
            logger.info("Using GROWTH risk profile (balanced evidence thresholds)")
        else:
            thresholds = CONSERVATIVE_THRESHOLDS
            logger.info("Using CONSERVATIVE risk profile (capital preservation)")

        prompt = _build_prompt(
            thresholds_block=thresholds,
            signal_summary=signal_summary_text,
            trader_plan=trader_plan,
            past_memory_str=past_memory_str,
            history=history,
            source_data_ref=source_data_ref,
            risk_context=risk_context,
            options_context=options_context,
            profile_block=profile_block,
        )

        response = llm.invoke(prompt)

        new_risk_debate_state = {
            "judge_decision": response.content,
            "history": risk_debate_state["history"],
            "risky_history": risk_debate_state["risky_history"],
            "safe_history": risk_debate_state["safe_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_risky_response": risk_debate_state["current_risky_response"],
            "current_safe_response": risk_debate_state["current_safe_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": response.content,
            "risk_metrics": risk_metrics_payload,
            "effective_risk_limits": effective_risk_limits,
        }

    return risk_manager_node
