from langchain_core.messages import HumanMessage, RemoveMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_sentiment,
    get_insider_transactions,
    get_global_news,
    # Perplexity deep research tools (for "deep" analysis mode)
    get_deep_research,
    get_earnings_analysis,
    get_sec_analysis,
    get_sec_filings_snapshot,
    get_earnings_transcript_snapshot,
)
from tradingagents.agents.utils.market_data_validation_tools import (
    get_verified_market_snapshot,
)
from tradingagents.dataflows.instrument_identity import get_instrument_context_from_state


def structured_signal_instruction(section: str) -> str:
    """Ask agents to emit SIGNAL_JSON first so output-token caps cannot drop it."""
    return (
        "\n\nStart the final written report with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** "
        "on its own line, then immediately this SIGNAL_JSON line, then the analysis:\n"
        f'SIGNAL_JSON: {{"section":"{section}","stance":"bullish|bearish|neutral","confidence":0.75,"key_factors":["factor1","factor2"]}}\n'
        "Use the 0.0 to 1.0 scale for confidence (e.g. 0.75 = 75% confident). "
        "Never omit these two leading lines. "
        "Ground the analysis with at least one named source "
        "(Yahoo Finance, yfinance, Finnhub, Perplexity, Alpha Vantage, URL, or [n]) "
        "and one dated print (YYYY-MM-DD or Month DD, YYYY)."
    )


def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add a context-anchored placeholder.

        A bare ``Continue`` is sometimes treated as the user task by
        OpenAI-compatible providers. Anchor the next analyst to the resolved
        instrument and analysis date instead.
        """
        messages = state["messages"]
        removal_operations = [RemoveMessage(id=m.id) for m in messages]
        instrument_context = get_instrument_context_from_state(state)
        trade_date = state.get("trade_date", "the requested date")
        placeholder = HumanMessage(
            content=(
                f"Proceed with your assigned analysis for this workflow. "
                f"{instrument_context} The analysis date is {trade_date}."
            )
        )
        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        