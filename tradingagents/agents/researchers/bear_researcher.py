from langchain_core.messages import AIMessage
import time
import json

from tradingagents.agents.utils.debate_text import opponent_argument_or_opening
from tradingagents.utils.token_management import truncate_to_token_limit

_REPORT_TOKEN_LIMIT = 3000
_HISTORY_TOKEN_LIMIT = 4000


def create_bear_researcher(llm, memory):
    def bear_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = truncate_to_token_limit(investment_debate_state.get("history", ""), _HISTORY_TOKEN_LIMIT)
        bear_history = investment_debate_state.get("bear_history", "")

        current_response = investment_debate_state.get("current_response", "")
        opponent_argument = opponent_argument_or_opening(current_response, "bull analyst")

        market_research_report = truncate_to_token_limit(state["market_report"], _REPORT_TOKEN_LIMIT)
        sentiment_report = truncate_to_token_limit(state["sentiment_report"], _REPORT_TOKEN_LIMIT)
        news_report = truncate_to_token_limit(state["news_report"], _REPORT_TOKEN_LIMIT)
        fundamentals_report = truncate_to_token_limit(state["fundamentals_report"], _REPORT_TOKEN_LIMIT)

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        # Investment profile directive (if provided)
        profile = state.get("investment_profile") or {}
        bear_frame = profile.get("bear_thesis_frame", "")
        profile_block = ""
        if bear_frame:
            profile_block = (
                f"\n📋 **INVESTMENT PROFILE FOCUS** ({profile.get('display_name', 'Default')}):\n"
                f"{bear_frame}\n"
                "Frame your bearish argument through this lens while still addressing all standard factors.\n"
            )

        prompt = f"""You are a Bear Analyst making the case against investing in the stock. Your goal is to present a well-reasoned argument emphasizing risks, challenges, and negative indicators. Leverage the provided research and data to highlight potential downsides and counter bullish arguments effectively.
{profile_block}

Key points to focus on:

- Risks and Challenges: Highlight factors like market saturation, financial instability, or macroeconomic threats that could hinder the stock's performance.
- Competitive Weaknesses: Emphasize vulnerabilities such as weaker market positioning, declining innovation, or threats from competitors.
- Negative Indicators: Use evidence from financial data, market trends, or recent adverse news to support your position.
- Bull Counterpoints: Critically analyze the bull argument with specific data and sound reasoning, exposing weaknesses or over-optimistic assumptions.
- Engagement: Present your argument in a conversational style, directly engaging with the bull analyst's points and debating effectively rather than simply listing facts.

Resources available:

Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
Company fundamentals report: {fundamentals_report}
Conversation history of the debate: {history}
Last bull argument: {opponent_argument}
Reflections from similar situations and lessons learned: {past_memory_str}
Use this information to deliver a compelling bear argument, refute the bull's claims, and engage in a dynamic debate that demonstrates the risks and weaknesses of investing in the stock. You must also address reflections and learn from lessons and mistakes you made in the past.
"""

        response = llm.invoke(prompt)

        argument = f"Bear Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
