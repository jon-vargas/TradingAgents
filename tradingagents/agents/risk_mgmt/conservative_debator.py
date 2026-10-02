import logging
from langchain_core.messages import AIMessage
import time
import json

from tradingagents.utils.token_management import truncate_to_token_limit

logger = logging.getLogger("tradingagents.agents.risk_mgmt.conservative_debator")

_REPORT_TOKEN_LIMIT = 2500
_HISTORY_TOKEN_LIMIT = 4000


def create_safe_debator(llm):
    def safe_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = truncate_to_token_limit(risk_debate_state.get("history", ""), _HISTORY_TOKEN_LIMIT)
        safe_history = risk_debate_state.get("safe_history", "")

        current_risky_response = risk_debate_state.get("current_risky_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

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
            logger.debug("Risk/options data unavailable for safe debater: %s", e)

        quant_block = ""
        if risk_context or options_context:
            quant_block = f"\n\nQuantitative Risk Data (use these numbers in your arguments):\n{risk_context}\n{options_context}"

        prompt = f"""As the Safe/Conservative Risk Analyst, your primary objective is to protect assets, minimize volatility, and ensure steady, reliable growth. You prioritize stability, security, and risk mitigation, carefully assessing potential losses, economic downturns, and market volatility. When evaluating the trader's decision or plan, critically examine high-risk elements, pointing out where the decision may expose the firm to undue risk and where more cautious alternatives could secure long-term gains. Here is the trader's decision:

{trader_decision}

Your task is to actively counter the arguments of the Risky and Neutral Analysts, highlighting where their views may overlook potential threats or fail to prioritize sustainability. Respond directly to their points, drawing from the following data sources to build a convincing case for a low-risk approach adjustment to the trader's decision:

Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}{quant_block}
Here is the current conversation history: {history} Here is the last response from the risky analyst: {current_risky_response} Here is the last response from the neutral analyst: {current_neutral_response}. If there are no responses from the other viewpoints, do not hallucinate and just present your point.

Engage by questioning their optimism and emphasizing the potential downsides they may have overlooked. Address each of their counterpoints to showcase why a conservative stance is ultimately the safest path for the firm's assets. Focus on debating and critiquing their arguments to demonstrate the strength of a low-risk strategy over their approaches. Output conversationally as if you are speaking without any special formatting."""

        response = llm.invoke(prompt)

        argument = f"Safe Analyst: {response.content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "risky_history": risk_debate_state.get("risky_history", ""),
            "safe_history": safe_history + "\n" + argument,
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Safe",
            "current_risky_response": risk_debate_state.get(
                "current_risky_response", ""
            ),
            "current_safe_response": argument,
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return safe_node
