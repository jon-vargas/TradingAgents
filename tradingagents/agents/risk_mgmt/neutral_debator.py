import logging
import time
import json

from tradingagents.utils.token_management import truncate_to_token_limit

logger = logging.getLogger("tradingagents.agents.risk_mgmt.neutral_debator")

_REPORT_TOKEN_LIMIT = 2500
_HISTORY_TOKEN_LIMIT = 4000


def create_neutral_debator(llm):
    def neutral_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = truncate_to_token_limit(risk_debate_state.get("history", ""), _HISTORY_TOKEN_LIMIT)
        neutral_history = risk_debate_state.get("neutral_history", "")

        current_risky_response = risk_debate_state.get("current_risky_response", "")
        current_safe_response = risk_debate_state.get("current_safe_response", "")

        market_research_report = truncate_to_token_limit(state["market_report"], _REPORT_TOKEN_LIMIT)
        sentiment_report = truncate_to_token_limit(state["sentiment_report"], _REPORT_TOKEN_LIMIT)
        news_report = truncate_to_token_limit(state["news_report"], _REPORT_TOKEN_LIMIT)
        fundamentals_report = truncate_to_token_limit(state["fundamentals_report"], _REPORT_TOKEN_LIMIT)

        trader_decision = truncate_to_token_limit(state["trader_investment_plan"], 3000)

        # Fetch quantitative risk metrics for data-driven arguments
        risk_context = ""
        options_context = ""
        try:
            from tradingagents.dataflows.yfinance_extended import format_risk_context, format_options_context
            risk_context = format_risk_context(state["company_of_interest"])
            options_context = format_options_context(state["company_of_interest"])
        except Exception as e:
            logger.debug("Risk/options data unavailable for neutral debater: %s", e)

        quant_block = ""
        if risk_context or options_context:
            quant_block = f"\n\nQuantitative Risk Data (use these numbers in your arguments):\n{risk_context}\n{options_context}"

        prompt = f"""As the Neutral Risk Analyst, your role is to provide a balanced perspective, weighing both the potential benefits and risks of the trader's decision or plan. You prioritize a well-rounded approach, evaluating the upsides and downsides while factoring in broader market trends, potential economic shifts, and diversification strategies. Here is the trader's decision:

{trader_decision}

Your task is to challenge both the Risky and Safe Analysts, pointing out where each perspective may be overly optimistic or overly cautious. Use insights from the following data sources to support a moderate, sustainable strategy to adjust the trader's decision:

Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}{quant_block}
Here is the current conversation history: {history} Here is the last response from the risky analyst: {current_risky_response} Here is the last response from the safe analyst: {current_safe_response}. If there are no responses from the other viewpoints, do not hallucinate and just present your point.

Engage actively by analyzing both sides critically, addressing weaknesses in the risky and conservative arguments to advocate for a more balanced approach. Challenge each of their points to illustrate why a moderate risk strategy might offer the best of both worlds, providing growth potential while safeguarding against extreme volatility. Focus on debating rather than simply presenting data, aiming to show that a balanced view can lead to the most reliable outcomes. Output conversationally as if you are speaking without any special formatting."""

        response = llm.invoke(prompt)

        argument = f"Neutral Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "risky_history": risk_debate_state.get("risky_history", ""),
            "safe_history": risk_debate_state.get("safe_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "latest_speaker": "Neutral",
            "current_risky_response": risk_debate_state.get(
                "current_risky_response", ""
            ),
            "current_safe_response": risk_debate_state.get("current_safe_response", ""),
            "current_neutral_response": argument,
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return neutral_node
