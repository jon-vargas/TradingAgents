import logging
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from tradingagents.agents.utils.agent_utils import (
    get_news, 
    get_global_news,
    get_deep_research,
    get_sec_filings_snapshot,
    get_earnings_transcript_snapshot,
    get_insider_sentiment,
    get_insider_transactions,
    structured_signal_instruction,
)
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.instrument_identity import get_instrument_context_from_state
from tradingagents.dataflows.perplexity_budget import is_valid_snapshot_payload
from tradingagents.utils.token_management import count_tokens, compact_messages_for_budget
from tradingagents.agents.analysts.analyst_finalize import invoke_analyst_chain

logger = logging.getLogger("tradingagents.agents.analysts.news_analyst")

_MAX_TOOL_MESSAGES = 5
_MAX_CONTEXT_TOKENS = 16000
_MAX_CONTEXT_MESSAGES = 18


def select_deep_news_tools(*, has_sec_snapshot: bool, has_transcript_snapshot: bool):
    """Perplexity tools bound for Deep news. Snapshot tools omitted when already in state."""
    tools = [get_deep_research]
    if not has_sec_snapshot:
        tools.append(get_sec_filings_snapshot)
    if not has_transcript_snapshot:
        tools.append(get_earnings_transcript_snapshot)
    return tools


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        
        # Get configuration to determine analysis mode
        config = get_config()
        use_perplexity = config.get("use_perplexity", False)
        analysis_mode = config.get("analysis_mode", "standard")
        token_budget = config.get("token_budget", {})
        analyst_budget = token_budget.get("analyst", {})
        observability = token_budget.get("observability", {})

        max_tool_messages = max(1, int(analyst_budget.get("news_max_tool_messages", _MAX_TOOL_MESSAGES)))
        max_context_tokens = max(2000, int(analyst_budget.get("max_context_tokens", _MAX_CONTEXT_TOKENS)))
        max_context_messages = max(4, int(analyst_budget.get("max_messages", _MAX_CONTEXT_MESSAGES)))
        log_token_metrics = bool(observability.get("log_token_metrics", True))

        # Base tools for all modes
        tools = [
            get_news,
            get_global_news,
            get_insider_sentiment,
            get_insider_transactions,
        ]

        raw_sec = state.get("sec_filings_snapshot") or ""
        raw_transcript = state.get("earnings_transcript_snapshot") or ""
        has_sec_snapshot = is_valid_snapshot_payload(raw_sec)
        has_transcript_snapshot = is_valid_snapshot_payload(raw_transcript)
        
        # Add Perplexity deep research tools for "deep" mode
        if use_perplexity:
            tools.extend(
                select_deep_news_tools(
                    has_sec_snapshot=has_sec_snapshot,
                    has_transcript_snapshot=has_transcript_snapshot,
                )
            )

            logger.info(
                "News Analyst: Deep research mode (Perplexity tools: %s)",
                ", ".join(tool.name for tool in tools),
            )

            # Catalyst pipeline (deep mode only) — cache-first, budget-aware
            try:
                from tradingagents.dataflows.interface import get_catalyst_pipeline
                company_name = state.get("company_name", "")
                catalyst_data = get_catalyst_pipeline(ticker, company_name)
                if catalyst_data:
                    state["catalyst_pipeline"] = catalyst_data
            except Exception as e:
                logger.debug("Catalyst pipeline fetch skipped: %s", e)
        
        sec_context = ""
        if has_sec_snapshot:
            try:
                from tradingagents.reporting.pdf_generator import format_sec_filings_snapshot

                sec_context = format_sec_filings_snapshot(raw_sec)
            except Exception as e:
                logger.debug("SEC snapshot formatting skipped for %s: %s", ticker, e)

        transcript_context = ""
        if has_transcript_snapshot:
            try:
                from tradingagents.reporting.pdf_generator import format_earnings_transcript_snapshot

                transcript_context = format_earnings_transcript_snapshot(raw_transcript)
            except Exception as e:
                logger.debug("Transcript snapshot formatting skipped for %s: %s", ticker, e)
        # Investment profile directive (if provided)
        profile = state.get("investment_profile") or {}
        profile_directive = profile.get("news_focus", "")
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

        # Build system message based on available tools
        base_message = (
            "You are a news researcher tasked with analyzing recent news and trends over the past week. "
            "Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. "
        )
        
        tool_instructions = (
            "Use the available tools: get_news(ticker, start_date, end_date) for company-specific or targeted news searches, "
            "and get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news. "
            "Also check insider buying/selling patterns using get_insider_sentiment and get_insider_transactions if available. "
            "Large insider purchases are a bullish signal; clustered insider selling may indicate concerns. "
        )
        
        # Add Perplexity-specific instructions for deep mode
        if use_perplexity:
            snapshot_note = ""
            if has_sec_snapshot and has_transcript_snapshot:
                snapshot_note = (
                    "SEC and earnings transcript snapshots are already in context below — "
                    "do NOT call get_sec_filings_snapshot or get_earnings_transcript_snapshot.\n"
                )
            elif has_sec_snapshot:
                snapshot_note = (
                    "SEC filings snapshot is already in context below — "
                    "do NOT call get_sec_filings_snapshot.\n"
                )
            elif has_transcript_snapshot:
                snapshot_note = (
                    "Earnings transcript snapshot is already in context below — "
                    "do NOT call get_earnings_transcript_snapshot.\n"
                )

            tool_instructions += (
                "\n\n🔬 **DEEP RESEARCH MODE - PERPLEXITY REQUIRED**:\n"
                "You MUST use the Perplexity AI research tools for institutional-grade analysis:\n\n"
                "**REQUIRED FIRST CALL**: get_deep_research(ticker='{ticker}', company_name=None)\n"
                "This provides comprehensive research with earnings, analyst consensus, "
                "competitive analysis, and citations - all in ONE efficient call.\n\n"
                f"{snapshot_note}"
                "Do not call get_earnings_analysis or get_sec_analysis — those prose tools are "
                "disabled; deep research plus prefetched snapshots cover the same ground.\n\n"
                "⚠️ **ACTION REQUIRED**: Your FIRST tool call MUST be get_deep_research. "
                "This is a premium feature the user has enabled for comprehensive analysis. "
                "Do NOT skip this step - it provides critical data not available from other sources.\n"
                "Cite Finnhub or Perplexity by name and include at least one ISO date (YYYY-MM-DD) or filing URL.\n"
                "If any tool returns JSON snapshots, summarize the insights and DO NOT paste raw JSON into the report.\n"
            ).format(ticker=ticker)
        
        analysis_instructions = (
            "Do not simply state the trends are mixed, provide detailed and finegrained analysis and insights that may help traders make decisions."
            """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + structured_signal_instruction("News")
        )
        
        system_message = (
            base_message + tool_instructions + analysis_instructions
            + (f"\n{macro_context}" if macro_context else "")
            + (f"\n\nThe following SEC filings snapshot was pre-fetched. Summarize material items; do not paste raw JSON:\n{sec_context}" if sec_context else "")
            + (f"\n\nThe following earnings transcript snapshot was pre-fetched. Summarize guidance and KPIs; do not paste raw JSON:\n{transcript_context}" if transcript_context else "")
            + profile_section
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
                "News analyst context: approx_tokens=%d tool_messages=%d compacted_messages=%d mode=%s",
                approx_tokens,
                tool_messages,
                len(invoke_messages),
                analysis_mode,
            )

        out = invoke_analyst_chain(
            analyst_type="news",
            config=config,
            default_cap=_MAX_TOOL_MESSAGES,
            invoke_messages=invoke_messages,
            prompt=prompt,
            llm=llm,
            tools=tools,
        )
        catalyst = state.get("catalyst_pipeline")
        if catalyst and isinstance(out, dict):
            out["catalyst_pipeline"] = catalyst
        return out

    return news_analyst_node
