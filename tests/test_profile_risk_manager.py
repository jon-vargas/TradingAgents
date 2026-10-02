"""Risk-manager prompt tests for profile-aware effective limits."""

from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.agents.managers.risk_manager import create_risk_manager


class _Memory:
    def get_memories(self, *_args, **_kwargs):
        return []


class _Llm:
    def __init__(self):
        self.prompt = ""

    def invoke(self, prompt):
        self.prompt = prompt
        return SimpleNamespace(
            content='DECISION_JSON: {"decision":"HOLD","position_action":"HOLD","conviction":"low"}'
        )


def test_risk_manager_injects_graph_resolved_effective_limits():
    llm = _Llm()
    node = create_risk_manager(llm, _Memory(), "growth")
    state = {
        "company_of_interest": "TEST",
        "risk_profile": "growth",
        "investment_profile": {
            "profile_key": "high_growth",
            "resolved_from": "explicit",
            "display_name": "High Growth",
        },
        "effective_risk_limits": {
            "beta": 2.2,
            "max_drawdown_pct": 35.0,
            "var_95_pct": 3.5,
        },
        "risk_debate_state": {
            "history": "",
            "risky_history": "",
            "safe_history": "",
            "neutral_history": "",
            "latest_speaker": "",
            "current_risky_response": "",
            "current_safe_response": "",
            "current_neutral_response": "",
            "judge_decision": "",
            "count": 0,
        },
        "market_report": "",
        "news_report": "",
        "fundamentals_report": "",
        "sentiment_report": "",
        "trader_investment_plan": "",
        "investment_plan": "",
    }
    with (
        patch(
            "tradingagents.graph.signal_aggregator.compute_signal_summary",
            return_value={"text_block": "Composite Signal: +0.20"},
        ),
        patch("tradingagents.dataflows.config.get_config", return_value={}),
        patch("tradingagents.dataflows.yfinance_extended.format_risk_context", return_value=""),
        patch("tradingagents.dataflows.yfinance_extended.format_options_context", return_value=""),
        patch("tradingagents.dataflows.risk_metrics.compute_risk_metrics", return_value={}),
    ):
        result = node(state)

    assert "Effective Risk Limits" in llm.prompt
    assert "beta ≤ 2.2" in llm.prompt
    assert result["effective_risk_limits"]["max_drawdown_pct"] == 35.0
