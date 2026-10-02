import logging
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from tradingagents.agents.utils.agent_utils import get_fundamentals, get_balance_sheet, get_cashflow, get_income_statement, get_insider_sentiment, get_insider_transactions, structured_signal_instruction
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.instrument_identity import get_instrument_context_from_state
from tradingagents.utils.token_management import truncate_to_token_limit, count_tokens, compact_messages_for_budget
from tradingagents.agents.analysts.analyst_finalize import invoke_analyst_chain

logger = logging.getLogger("tradingagents.agents.analysts.fundamentals_analyst")

_ENRICHED_CONTEXT_MAX_TOKENS = 6000
_MAX_TOOL_MESSAGES = 5
_MAX_CONTEXT_TOKENS = 16000
_MAX_CONTEXT_MESSAGES = 18


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        company_name = state["company_of_interest"]
        config = get_config()
        token_budget = config.get("token_budget", {})
        analyst_budget = token_budget.get("analyst", {})
        observability = token_budget.get("observability", {})

        max_tool_messages = max(1, int(analyst_budget.get("fundamentals_max_tool_messages", _MAX_TOOL_MESSAGES)))
        max_context_tokens = max(2000, int(analyst_budget.get("max_context_tokens", _MAX_CONTEXT_TOKENS)))
        max_context_messages = max(4, int(analyst_budget.get("max_messages", _MAX_CONTEXT_MESSAGES)))
        enriched_context_max_tokens = max(
            1000,
            int(
                analyst_budget.get(
                    "fundamentals_enriched_context_max_tokens",
                    _ENRICHED_CONTEXT_MAX_TOKENS,
                )
            ),
        )
        log_token_metrics = bool(observability.get("log_token_metrics", True))

        tools = [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
        ]

        # Fetch enriched context (analyst ratings, earnings, ownership, EQ, DCF)
        enriched_context = ""
        eq_data = None
        iv_data = None
        sa_data = None
        pc_data = None
        try:
            from tradingagents.dataflows.yfinance_extended import (
                format_analyst_context, format_macro_context, format_peer_context,
                format_estimate_revisions_context, format_rating_changes_context,
                get_earnings_quality, compute_intrinsic_value, compute_scenario_analysis,
            )
            enriched_context = format_analyst_context(ticker)
            macro_ctx = format_macro_context(macro_snapshot=state.get("macro_snapshot"))
            if macro_ctx:
                enriched_context = macro_ctx + "\n" + enriched_context
            peer_ctx = format_peer_context(ticker)
            if peer_ctx:
                enriched_context += "\n" + peer_ctx
            est_rev_ctx = format_estimate_revisions_context(ticker)
            if est_rev_ctx:
                enriched_context += "\n" + est_rev_ctx
            rating_ctx = format_rating_changes_context(ticker)
            if rating_ctx:
                enriched_context += "\n" + rating_ctx
            eq_data = get_earnings_quality(ticker)
            iv_data = compute_intrinsic_value(ticker)
            sa_data = compute_scenario_analysis(ticker)
            try:
                from tradingagents.dataflows.yfinance_extended import compute_peer_comps
                pc_data = compute_peer_comps(ticker)
            except Exception as _pc_err:
                pc_data = None
                logger.debug("peer_comps unavailable for %s: %s", ticker, _pc_err)
            enriched_context = truncate_to_token_limit(enriched_context, enriched_context_max_tokens)
        except Exception as e:
            logger.debug("Enriched context unavailable for %s: %s", ticker, e)

        # Investment profile directive (if provided)
        profile = state.get("investment_profile") or {}
        profile_directive = profile.get("fundamentals_focus", "")
        profile_section = ""
        if profile_directive:
            profile_section = (
                f"\n\n📋 **INVESTMENT PROFILE FOCUS** ({profile.get('display_name', 'Default')}):\n"
                f"{profile_directive}\n"
                "Apply this analytical lens throughout your analysis while still covering all standard metrics."
            )

        system_message = (
            "You are a researcher tasked with analyzing fundamental information over the past week about a company. Please write a comprehensive report of the company's fundamental information such as financial documents, company profile, basic company financials, and company financial history to gain a full view of the company's fundamental information to inform traders. Make sure to include as much detail as possible. Do not simply state the trends are mixed, provide detailed and finegrained analysis and insights that may help traders make decisions."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
            + " Use the available tools: `get_fundamentals` for comprehensive company analysis, `get_balance_sheet`, `get_cashflow`, and `get_income_statement` for specific financial statements."
            + (f"\n\nThe following enriched data has been pre-fetched for your analysis. Incorporate these insights into your report:{enriched_context}" if enriched_context else "")
            + profile_section
            + structured_signal_instruction("Fundamentals"),
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
                "Fundamentals analyst context: approx_tokens=%d tool_messages=%d compacted_messages=%d",
                approx_tokens,
                tool_messages,
                len(invoke_messages),
            )

        state_update = invoke_analyst_chain(
            analyst_type="fundamentals",
            config=config,
            default_cap=_MAX_TOOL_MESSAGES,
            invoke_messages=invoke_messages,
            prompt=prompt,
            llm=llm,
            tools=tools,
        )
        if eq_data and eq_data.get("grade"):
            state_update["earnings_quality"] = eq_data
        if iv_data and iv_data.get("fair_value"):
            state_update["intrinsic_value"] = iv_data
        if sa_data and sa_data.get("scenarios"):
            state_update["scenario_analysis"] = sa_data
        if pc_data and pc_data.get("multiples"):
            state_update["peer_comps"] = pc_data
        return state_update

    return fundamentals_analyst_node
