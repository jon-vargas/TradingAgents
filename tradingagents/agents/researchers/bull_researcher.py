from langchain_core.messages import AIMessage
import time
import json

from tradingagents.agents.utils.debate_text import opponent_argument_or_opening
from tradingagents.utils.token_management import truncate_to_token_limit

_REPORT_TOKEN_LIMIT = 3000
_HISTORY_TOKEN_LIMIT = 4000


def create_bull_researcher(llm, memory):
    def bull_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = truncate_to_token_limit(investment_debate_state.get("history", ""), _HISTORY_TOKEN_LIMIT)
        bull_history = investment_debate_state.get("bull_history", "")

        current_response = investment_debate_state.get("current_response", "")
        opponent_argument = opponent_argument_or_opening(current_response, "bear analyst")

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
        bull_frame = profile.get("bull_thesis_frame", "")
        profile_block = ""
        if bull_frame:
            profile_block = (
                f"\n📋 **INVESTMENT PROFILE FOCUS** ({profile.get('display_name', 'Default')}):\n"
                f"{bull_frame}\n"
                "Frame your bullish argument through this lens while still addressing all standard factors.\n"
            )

        prompt = f"""You are a Bull Analyst advocating for investing in the stock. Your task is to build a strong, evidence-based case emphasizing growth potential, competitive advantages, and positive market indicators. Leverage the provided research and data to address concerns and counter bearish arguments effectively.
{profile_block}

Key points to focus on:
- Growth Potential: Highlight the company's market opportunities, revenue projections, and scalability.
- Competitive Advantages: Emphasize factors like unique products, strong branding, or dominant market positioning.
- Positive Indicators: Use financial health, industry trends, and recent positive news as evidence.
- Bear Counterpoints: Critically analyze the bear argument with specific data and sound reasoning, addressing concerns thoroughly and showing why the bull perspective holds stronger merit.
- Engagement: Present your argument in a conversational style, engaging directly with the bear analyst's points and debating effectively rather than just listing data.

Resources available:
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
Company fundamentals report: {fundamentals_report}
Conversation history of the debate: {history}
Last bear argument: {opponent_argument}
Reflections from similar situations and lessons learned: {past_memory_str}
Use this information to deliver a compelling bull argument, refute the bear's concerns, and engage in a dynamic debate that demonstrates the strengths of the bull position. You must also address reflections and learn from lessons and mistakes you made in the past.
"""

        response = llm.invoke(prompt)

        argument = f"Bull Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bear_history": investment_debate_state.get("bear_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bull_node
