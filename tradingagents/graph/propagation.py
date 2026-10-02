# TradingAgents/graph/propagation.py

from typing import Dict, Any, Optional
from tradingagents.agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)


class Propagator:
    """Handles state initialization and propagation through the graph."""

    def __init__(self, max_recur_limit=100):
        """Initialize with configuration parameters."""
        self.max_recur_limit = max_recur_limit

    def create_initial_state(
        self, company_name: str, trade_date: str,
        investment_profile: Dict[str, Any] = None,
        risk_profile: str = "growth",
        screening_context: Optional[Dict[str, Any]] = None,
        instrument_context: str = "",
        instrument_identity: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create the initial state for the agent graph."""
        ctx = screening_context or {}
        summary = ctx.get("summary_text") or ""
        return {
            "messages": [("human", company_name)],
            "company_of_interest": company_name,
            "trade_date": str(trade_date),
            "instrument_context": instrument_context or "",
            "instrument_identity": instrument_identity or {},
            "investment_profile": investment_profile or {},
            "investment_profile_key": (investment_profile or {}).get("profile_key"),
            "investment_profile_resolved_from": (investment_profile or {}).get("resolved_from"),
            "risk_profile": risk_profile,
            "screening_context": ctx,
            "screening_summary_text": summary,
            "investment_debate_state": InvestDebateState(
                {
                    "bull_history": "",
                    "bear_history": "",
                    "history": "",
                    "current_response": "",
                    "judge_decision": "",
                    "count": 0,
                }
            ),
            "risk_debate_state": RiskDebateState(
                {
                    "risky_history": "",
                    "safe_history": "",
                    "neutral_history": "",
                    "history": "",
                    "latest_speaker": "",
                    "current_risky_response": "",
                    "current_safe_response": "",
                    "current_neutral_response": "",
                    "judge_decision": "",
                    "count": 0,
                }
            ),
            "market_report": "",
            "fundamentals_report": "",
            "sentiment_report": "",
            "news_report": "",
            "earnings_quality": None,
            "intrinsic_value": None,
            "scenario_analysis": None,
            "catalyst_pipeline": None,
            "peer_comps": None,
            "factor_scorecard": None,
            "macro_snapshot": None,
        }

    def get_graph_args(self) -> Dict[str, Any]:
        """Get arguments for the graph invocation."""
        return {
            "stream_mode": "values",
            "config": {"recursion_limit": self.max_recur_limit},
        }
