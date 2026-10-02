from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
import logging
from tradingagents.agents.utils.agent_utils import get_news, structured_signal_instruction
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.instrument_identity import get_instrument_context_from_state
from tradingagents.utils.token_management import count_tokens, compact_messages_for_budget
from tradingagents.agents.analysts.analyst_finalize import invoke_analyst_chain

logger = logging.getLogger("tradingagents.agents.analysts.social_media_analyst")

_MAX_TOOL_MESSAGES = 4
_MAX_CONTEXT_TOKENS = 16000
_MAX_CONTEXT_MESSAGES = 18


def create_social_media_analyst(llm):
    def social_media_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        config = get_config()
        token_budget = config.get("token_budget", {})
        analyst_budget = token_budget.get("analyst", {})
        observability = token_budget.get("observability", {})

        max_tool_messages = max(1, int(analyst_budget.get("social_max_tool_messages", _MAX_TOOL_MESSAGES)))
        max_context_tokens = max(2000, int(analyst_budget.get("max_context_tokens", _MAX_CONTEXT_TOKENS)))
        max_context_messages = max(4, int(analyst_budget.get("max_messages", _MAX_CONTEXT_MESSAGES)))
        log_token_metrics = bool(observability.get("log_token_metrics", True))

        tools = [
            get_news,
        ]

        # Investment profile directive (if provided)
        profile = state.get("investment_profile") or {}
        profile_directive = profile.get("sentiment_focus", "")
        profile_section = ""
        if profile_directive:
            profile_section = (
                f"\n\n📋 **INVESTMENT PROFILE FOCUS** ({profile.get('display_name', 'Default')}):\n"
                f"{profile_directive}\n"
                "Apply this analytical lens throughout your analysis while still covering all standard metrics."
            )

        macro_context = ""
        try:
            from tradingagents.dataflows.yfinance_extended import format_macro_context
            macro_context = format_macro_context(macro_snapshot=state.get("macro_snapshot"))
        except Exception as e:
            logger.debug("Macro context unavailable for %s: %s", ticker, e)

        system_message = (
            "You are a news sentiment researcher/analyst tasked with analyzing recent company news and public sentiment for a specific company over the past week. You will be given a company's name your objective is to write a comprehensive long report detailing your analysis, insights, and implications for traders and investors on this company's current state after analyzing the sentiment tone of recent news coverage, identifying shifts in media narrative, and assessing public perception. Use the get_news(ticker, start_date, end_date) tool to search for company-specific news and sentiment-relevant discussions. Do not simply state the trends are mixed, provide detailed and finegrained analysis and insights that may help traders make decisions."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + (f"\n{macro_context}" if macro_context else "")
            + profile_section
            + structured_signal_instruction("Sentiment"),
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
        if log_token_metrics:
            approx_tokens = sum(
                count_tokens(str(getattr(message, "content", "")))
                for message in invoke_messages
            )
            logger.info(
                "Social analyst context: approx_tokens=%d tool_messages=%d compacted_messages=%d",
                approx_tokens,
                tool_messages,
                len(invoke_messages),
            )

        return invoke_analyst_chain(
            analyst_type="social",
            config=config,
            default_cap=_MAX_TOOL_MESSAGES,
            invoke_messages=invoke_messages,
            prompt=prompt,
            llm=llm,
            tools=tools,
        )

    return social_media_analyst_node
