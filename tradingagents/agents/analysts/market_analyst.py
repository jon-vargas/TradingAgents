import logging
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from tradingagents.agents.utils.agent_utils import (
    get_stock_data,
    get_indicators,
    get_verified_market_snapshot,
    structured_signal_instruction,
)
from tradingagents.dataflows.instrument_identity import get_instrument_context_from_state
from tradingagents.dataflows.config import get_config
from tradingagents.utils.token_management import count_tokens, compact_messages_for_budget
from tradingagents.agents.analysts.analyst_finalize import invoke_analyst_chain

logger = logging.getLogger("tradingagents.agents.analysts.market_analyst")

_MAX_TOOL_MESSAGES = 6
_MAX_CONTEXT_TOKENS = 16000
_MAX_CONTEXT_MESSAGES = 18
_MAX_INDICATORS = 4


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]
        config = get_config()
        token_budget = config.get("token_budget", {})
        analyst_budget = token_budget.get("analyst", {})
        observability = token_budget.get("observability", {})

        max_tool_messages = max(1, int(analyst_budget.get("market_max_tool_messages", _MAX_TOOL_MESSAGES)))
        max_context_tokens = max(2000, int(analyst_budget.get("max_context_tokens", _MAX_CONTEXT_TOKENS)))
        max_context_messages = max(4, int(analyst_budget.get("max_messages", _MAX_CONTEXT_MESSAGES)))
        max_indicators = max(1, int(analyst_budget.get("market_max_indicators", _MAX_INDICATORS)))
        log_token_metrics = bool(observability.get("log_token_metrics", True))

        tools = [
            get_stock_data,
            get_indicators,
            get_verified_market_snapshot,
        ]
        instrument_context = get_instrument_context_from_state(state)

        # Fetch weekly technicals for multi-timeframe context
        weekly_context = ""
        try:
            from tradingagents.dataflows.yfinance_extended import format_weekly_technicals_context
            weekly_context = format_weekly_technicals_context(ticker, as_of_date=current_date)
        except Exception as e:
            logger.debug("Weekly technicals unavailable for %s: %s", ticker, e)

        macro_context = ""
        try:
            from tradingagents.dataflows.yfinance_extended import format_macro_context
            macro_context = format_macro_context(macro_snapshot=state.get("macro_snapshot"))
        except Exception as e:
            logger.debug("Macro context unavailable for %s: %s", ticker, e)

        # Investment profile directive (if provided)
        profile = state.get("investment_profile") or {}
        profile_directive = profile.get("technical_focus", "")
        profile_section = ""
        if profile_directive:
            profile_section = (
                f"\n\n📋 **INVESTMENT PROFILE FOCUS** ({profile.get('display_name', 'Default')}):\n"
                f"{profile_directive}\n"
                "Apply this analytical lens throughout your analysis while still covering all standard metrics."
            )

        system_message = (
            "You are a market analyst. Use tools to produce a detailed technical report for traders. "
            f"Always call get_stock_data first, then call get_indicators for up to {max_indicators} complementary indicators. "
            "Call get_verified_market_snapshot and treat it as the source of truth for exact OHLCV and indicator values. "
            "Avoid redundant indicators and prioritize signal diversity.\n\n"
            "Allowed indicators (exact names): close_50_sma, close_200_sma, close_10_ema, macd, macds, macdh, rsi, "
            "boll, boll_ub, boll_lb, atr, vwma, kdjk, kdjd, dx, wr, close_10_roc.\n\n"
            "Focus on trend, momentum, volatility, support/resistance, and multi-timeframe confluence. "
            "Use concrete numbers from tool outputs. Do not state 'mixed' without specifics."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
            + (f"\n\nThe following weekly-timeframe data has been pre-fetched for multi-timeframe confluence analysis. Compare daily signals against these weekly levels to assess trend alignment:{weekly_context}" if weekly_context else "")
            + (f"\n{macro_context}" if macro_context else "")
            + profile_section
            + structured_signal_instruction("Market")
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(ticker=ticker)
        prompt = prompt.partial(instrument_context=instrument_context)

        invoke_messages = compact_messages_for_budget(
            state["messages"],
            max_tokens=max_context_tokens,
            max_messages=max_context_messages,
            model=config.get("quick_think_llm", "gpt-5.6-luna"),
        )
        tool_messages = sum(
            1 for message in state["messages"] if getattr(message, "type", "") == "tool"
        )
        indicator_tool_messages = sum(
            1
            for message in state["messages"]
            if getattr(message, "type", "") == "tool"
            and getattr(message, "name", "") == "get_indicators"
        )
        if log_token_metrics:
            approx_tokens = sum(
                count_tokens(str(getattr(message, "content", "")))
                for message in invoke_messages
            )
            logger.info(
                "Market analyst context: approx_tokens=%d tool_messages=%d indicator_tool_messages=%d compacted_messages=%d",
                approx_tokens,
                tool_messages,
                indicator_tool_messages,
                len(invoke_messages),
            )

        return invoke_analyst_chain(
            analyst_type="market",
            config=config,
            default_cap=_MAX_TOOL_MESSAGES,
            invoke_messages=invoke_messages,
            prompt=prompt,
            llm=llm,
            tools=tools,
            extra_finalize_trigger=indicator_tool_messages >= max_indicators,
        )

    return market_analyst_node
