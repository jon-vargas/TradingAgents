import functools
import time
import json

from tradingagents.utils.token_management import truncate_to_token_limit

_REPORT_TOKEN_LIMIT = 3000


def create_trader(llm, memory):
    def trader_node(state, name):
        company_name = state["company_of_interest"]
        investment_plan = state["investment_plan"]
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        truncated_market_report = truncate_to_token_limit(
            (market_research_report or "").strip(),
            _REPORT_TOKEN_LIMIT,
        )

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        if past_memories:
            for i, rec in enumerate(past_memories, 1):
                past_memory_str += rec["recommendation"] + "\n\n"
        else:
            past_memory_str = "No past memories found."

        # Risk profile context for position sizing
        risk_profile = state.get("risk_profile", "growth")
        risk_profile_block = ""
        if risk_profile == "aggressive":
            risk_profile_block = "\n\nClient Risk Tolerance: AGGRESSIVE - Client accepts higher volatility for growth potential. Size positions for conviction. Wider stop-losses acceptable."
        elif risk_profile == "conservative":
            risk_profile_block = "\n\nClient Risk Tolerance: CONSERVATIVE - Client prioritizes capital preservation. Smaller positions, tighter stops, higher margin of safety required."
        else:
            risk_profile_block = (
                "\n\nClient Risk Tolerance: GROWTH - Client seeks balanced risk/reward. Standard position sizing. "
                "Reasonable stop-losses. If a lockup or other supply event is live, quantify eligible shares vs "
                "session volume. Prefer HOLD (do not initiate) for the core book when beta/drawdown/VaR are outside "
                "limits; any staged BUY ladder is a satellite spec, not the core recommendation."
            )

        # Investment profile directive (if provided)
        profile = state.get("investment_profile") or {}
        trader_focus = profile.get("trader_focus", "")
        profile_block = ""
        if trader_focus:
            profile_block = (
                f"\n\n📋 **INVESTMENT PROFILE FOCUS** ({profile.get('display_name', 'Default')}):\n"
                f"{trader_focus}\n"
                "Apply this trading lens throughout your analysis."
            )

        screening_text = state.get("screening_summary_text") or ""
        screening_block = ""
        if screening_text:
            screening_block = (
                f"\n\nPrior screener context (rank, opportunity score, flags):\n{screening_text}"
            )

        if truncated_market_report:
            grounding = (
                "Ground concrete price levels (entry, stop-loss, position sizing) in the technical "
                "market report's price structure — current price, support/resistance, ATR, and "
                "volatility — and use the research plan for direction and strategy. "
            )
            report_section = f"Technical Market Report:\n{truncated_market_report}\n\n"
        else:
            grounding = ""
            report_section = ""

        context = {
            "role": "user",
            "content": (
                f"Based on a comprehensive analysis by a team of analysts, here is an investment plan "
                f"tailored for {company_name}. This plan incorporates insights from current technical "
                f"market trends, macroeconomic indicators, and social media sentiment. Use this plan "
                f"as a foundation for evaluating your next trading decision.\n\n"
                f"{report_section}"
                f"Proposed Investment Plan: {investment_plan}{screening_block}\n\n"
                f"Leverage these insights to make an informed and strategic decision."
            ),
        }

        messages = [
            {
                "role": "system",
                "content": f"""You are a trading agent analyzing market data to make investment decisions. Based on your analysis, provide a specific recommendation to buy, sell, or hold. Start the response with 'FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**' so the decision survives output-token caps. Do not forget to utilize lessons from past decisions to learn from your mistakes. Here is some reflections from similar situations you traded in and the lessons learned: {past_memory_str}
{risk_profile_block}{profile_block}
{grounding}State entry price and stop-loss as absolute price levels in the instrument's quote currency (for example 39.18), never a percentage or a range; convert a percentage distance to the price level it implies, or omit the field if you cannot state a number.

Start the trading plan with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** on its own line, then immediately this SIGNAL_JSON line, then the plan:
SIGNAL_JSON: {{"section":"Trading Plan","stance":"bullish|bearish|neutral","confidence":0.75,"key_factors":["factor1","factor2"]}}
Use the 0.0 to 1.0 scale for confidence (e.g. 0.75 = 75% confident). Never omit these two leading lines.""",
            },
            context,
        ]

        result = llm.invoke(messages)

        return {
            "messages": [result],
            "trader_investment_plan": result.content,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
