import logging
import time
import json

logger = logging.getLogger("tradingagents.agents.managers.research_manager")


def create_research_manager(llm, memory):
    def research_manager_node(state) -> dict:
        history = state["investment_debate_state"].get("history", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        investment_debate_state = state["investment_debate_state"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        # Quantitative signal summary (analyst signals only — excludes
        # Research and Trading Plan which don't exist yet at this stage)
        signal_summary_text = ""
        try:
            from tradingagents.graph.signal_aggregator import compute_signal_summary
            from tradingagents.dataflows.config import get_config
            config = get_config()
            data_completeness = config.get("data_completeness", 1.0)
            company_name = state["company_of_interest"]
            signal_result = compute_signal_summary(
                state, company_name,
                exclude_sections={"Research", "Trading Plan"},
                data_completeness=data_completeness,
            )
            signal_summary_text = signal_result.get("text_block", "")
        except Exception as e:
            logger.warning("Signal summary unavailable for Research Manager: %s", e)

        # Source data reference appendix
        source_data_ref = ""
        try:
            from tradingagents.agents.managers.risk_manager import _build_source_data_reference
            source_data_ref = _build_source_data_reference(state)
        except Exception as e:
            logger.debug("Source data reference unavailable: %s", e)

        # Investment profile directive (if provided)
        profile = state.get("investment_profile") or {}
        manager_focus = profile.get("research_manager_focus", "")
        profile_block = ""
        if manager_focus:
            profile_block = (
                f"\n📋 **INVESTMENT PROFILE FOCUS** ({profile.get('display_name', 'Default')}):\n"
                f"{manager_focus}\n"
                "Apply these decision criteria throughout your evaluation.\n\n"
            )

        signal_block = ""
        if signal_summary_text:
            signal_block = f"\n{signal_summary_text}\n"

        screening_block = ""
        screening_text = state.get("screening_summary_text") or ""
        if screening_text:
            screening_block = (
                f"\n📊 **SCREENING CONTEXT** (from pre-analysis screener):\n"
                f"{screening_text}\n"
                "Treat this as prior quantitative ranking context — reconcile with your own analysis.\n"
            )

        source_block = ""
        if source_data_ref:
            source_block = f"\n{source_data_ref}\n"

        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear investment plan for the trader.
{profile_block}{screening_block}{signal_block}
Commit to **BUY** or **SELL** only when the debate's strongest arguments clearly warrant a change in exposure. Choose **HOLD** when evidence is balanced, materially conflicting, ambiguous, or insufficient; do not manufacture a direction to appear decisive. Weigh the bull and bear cases on their merits, independent of which side spoke first or last.

Summarize the key points from both sides concisely, focusing on the most compelling evidence or reasoning. Your recommendation—Buy, Sell, or Hold—must be clear and actionable.

Additionally, develop a detailed investment plan for the trader. This should include:

Your Recommendation: A stance supported by the most convincing arguments.
Rationale: An explanation of why these arguments lead to your conclusion.
Strategic Actions: Concrete steps for implementing the recommendation.
Take into account your past mistakes on similar situations. Use these insights to refine your decision-making and ensure you are learning and improving. Present your analysis conversationally, as if speaking naturally, without special formatting. 

Here are your past reflections on mistakes:
\"{past_memory_str}\"

Here is the debate:
Debate History:
{history}
{source_block}
Start your decision with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** on its own line, then immediately this SIGNAL_JSON line, then the reasoning:
SIGNAL_JSON: {{"section":"Research","stance":"bullish|bearish|neutral","confidence":0.75,"key_factors":["factor1","factor2"]}}
Use the 0.0 to 1.0 scale for confidence (e.g. 0.75 = 75% confident). Never omit these two leading lines."""
        response = llm.invoke(prompt)

        new_investment_debate_state = {
            "judge_decision": response.content,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": response.content,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": response.content,
        }

    return research_manager_node
