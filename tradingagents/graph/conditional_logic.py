# TradingAgents/graph/conditional_logic.py

from tradingagents.agents.utils.agent_states import AgentState


class ConditionalLogic:
    """Handles conditional logic for determining graph flow."""

    def __init__(self, max_debate_rounds=1, max_risk_discuss_rounds=1):
        """Initialize with configuration parameters."""
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_discuss_rounds = max_risk_discuss_rounds

    @staticmethod
    def _analyst_done(state: AgentState, report_key: str, finalized_key: str) -> bool:
        if state.get(finalized_key):
            return True
        report = state.get(report_key)
        return bool(isinstance(report, str) and report.strip())

    def _should_continue_analyst(
        self,
        state: AgentState,
        tools_node: str,
        clear_node: str,
        report_key: str,
        finalized_key: str,
    ) -> str:
        messages = state["messages"]
        last_message = messages[-1]
        if getattr(last_message, "tool_calls", None):
            return tools_node
        if self._analyst_done(state, report_key, finalized_key):
            return clear_node
        return clear_node

    def should_continue_market(self, state: AgentState):
        """Determine if market analysis should continue."""
        return self._should_continue_analyst(
            state,
            "tools_market",
            "Msg Clear Market",
            "market_report",
            "analyst_finalized_market",
        )

    def should_continue_social(self, state: AgentState):
        """Determine if social media analysis should continue."""
        return self._should_continue_analyst(
            state,
            "tools_social",
            "Msg Clear Social",
            "sentiment_report",
            "analyst_finalized_social",
        )

    def should_continue_news(self, state: AgentState):
        """Determine if news analysis should continue."""
        return self._should_continue_analyst(
            state,
            "tools_news",
            "Msg Clear News",
            "news_report",
            "analyst_finalized_news",
        )

    def should_continue_fundamentals(self, state: AgentState):
        """Determine if fundamentals analysis should continue."""
        return self._should_continue_analyst(
            state,
            "tools_fundamentals",
            "Msg Clear Fundamentals",
            "fundamentals_report",
            "analyst_finalized_fundamentals",
        )

    def should_continue_debate(self, state: AgentState) -> str:
        """Determine if debate should continue."""
        inv_debate = state.get("investment_debate_state") or {}
        count = inv_debate.get("count", 0)
        current_response = inv_debate.get("current_response", "") or ""

        if count >= 2 * self.max_debate_rounds:
            return "Research Manager"
        if current_response.startswith("Bull"):
            return "Bear Researcher"
        return "Bull Researcher"

    def should_continue_risk_analysis(self, state: AgentState) -> str:
        """Determine if risk analysis should continue."""
        risk_debate = state.get("risk_debate_state") or {}
        count = risk_debate.get("count", 0)
        latest_speaker = risk_debate.get("latest_speaker", "") or ""

        if count >= 3 * self.max_risk_discuss_rounds:
            return "Risk Judge"
        if latest_speaker.startswith("Risky"):
            return "Safe Analyst"
        if latest_speaker.startswith("Safe"):
            return "Neutral Analyst"
        return "Risky Analyst"
