"""Warn-only SELL guardrail behavior."""

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.signal_processing import evaluate_sell_guardrail, validate_decision


def _conflicted_state(*, fundamental_bearish=False):
    stance = "bearish" if fundamental_bearish else "bullish"
    return {
        "analyst_ratings": {
            "recommendation_key": "strong_buy",
            "upside_pct": 40.0,
            "number_of_analysts": 12,
        },
        "fundamentals_report": (
            f'SIGNAL_JSON: {{"section":"Fundamentals","stance":"{stance}","confidence":0.8}}'
        ),
        "investment_plan": (
            f'SIGNAL_JSON: {{"section":"Research","stance":"{stance}","confidence":0.8}}'
        ),
    }


def test_conflicted_weak_sell_warns_without_overriding_decision():
    result = validate_decision(
        "SELL",
        -0.04,
        "growth",
        DEFAULT_CONFIG,
        state=_conflicted_state(),
    )
    assert result["final_decision"] == "SELL"
    assert result["sell_guardrail"]
    assert result["guardrail_triggered"] is True


def test_strongly_bearish_sell_does_not_warn():
    assert (
        evaluate_sell_guardrail(
            "SELL",
            -0.35,
            "growth",
            state=_conflicted_state(),
            config=DEFAULT_CONFIG,
        )
        is None
    )


def test_dual_bearish_fundamentals_and_research_suppress_warning():
    assert (
        evaluate_sell_guardrail(
            "SELL",
            -0.04,
            "growth",
            state=_conflicted_state(fundamental_bearish=True),
            config=DEFAULT_CONFIG,
        )
        is None
    )
