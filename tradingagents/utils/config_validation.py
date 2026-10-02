from typing import Dict, List


def validate_config(config: Dict) -> List[str]:
    """Validate required config keys and basic types."""
    errors: List[str] = []
    required_fields = {
        "project_dir": str,
        "llm_provider": str,
        "deep_think_llm": str,
        "quick_think_llm": str,
        "backend_url": str,
        "analysis_mode": str,
        "data_vendors": dict,
        "enable_cache": bool,
        "enable_usage_tracking": bool,
        "enable_provenance": bool,
    }

    for key, expected in required_fields.items():
        if key not in config:
            errors.append(f"missing_{key}")
            continue
        value = config.get(key)
        if expected is bool:
            if not isinstance(value, bool):
                errors.append(f"{key}_invalid_type")
        elif not isinstance(value, expected):
            errors.append(f"{key}_invalid_type")

    if config.get("max_debate_rounds", 0) < 0:
        errors.append("max_debate_rounds_invalid")
    if config.get("max_risk_discuss_rounds", 0) < 0:
        errors.append("max_risk_discuss_rounds_invalid")

    token_budget = config.get("token_budget")
    if token_budget is not None:
        if not isinstance(token_budget, dict):
            errors.append("token_budget_invalid_type")
        else:
            analyst = token_budget.get("analyst", {})
            tools = token_budget.get("tools", {})
            llm = token_budget.get("llm", {})
            adaptive = token_budget.get("adaptive_degrade", {})

            if analyst and not isinstance(analyst, dict):
                errors.append("token_budget_analyst_invalid_type")
            if tools and not isinstance(tools, dict):
                errors.append("token_budget_tools_invalid_type")
            if llm and not isinstance(llm, dict):
                errors.append("token_budget_llm_invalid_type")
            if adaptive and not isinstance(adaptive, dict):
                errors.append("token_budget_adaptive_invalid_type")

            def _validate_positive_int(section: Dict, key: str, code: str):
                if key in section:
                    value = section.get(key)
                    if not isinstance(value, int) or value <= 0:
                        errors.append(code)

            if isinstance(analyst, dict):
                _validate_positive_int(analyst, "max_context_tokens", "analyst_max_context_tokens_invalid")
                _validate_positive_int(analyst, "max_messages", "analyst_max_messages_invalid")
                _validate_positive_int(analyst, "market_max_tool_messages", "analyst_market_max_tool_messages_invalid")
                _validate_positive_int(analyst, "social_max_tool_messages", "analyst_social_max_tool_messages_invalid")
                _validate_positive_int(analyst, "news_max_tool_messages", "analyst_news_max_tool_messages_invalid")
                _validate_positive_int(analyst, "fundamentals_max_tool_messages", "analyst_fundamentals_max_tool_messages_invalid")
                _validate_positive_int(analyst, "market_max_indicators", "analyst_market_max_indicators_invalid")
                _validate_positive_int(analyst, "fundamentals_enriched_context_max_tokens", "analyst_fundamentals_enriched_context_max_tokens_invalid")

            if isinstance(tools, dict):
                for key, value in tools.items():
                    if not isinstance(value, int) or value <= 0:
                        errors.append(f"tools_{key}_invalid")

            if isinstance(llm, dict):
                _validate_positive_int(llm, "max_retries", "llm_max_retries_invalid")
                _validate_positive_int(llm, "quick_max_output_tokens", "llm_quick_max_output_tokens_invalid")
                _validate_positive_int(llm, "deep_max_output_tokens", "llm_deep_max_output_tokens_invalid")

            if isinstance(adaptive, dict):
                if "enabled" in adaptive and not isinstance(adaptive.get("enabled"), bool):
                    errors.append("adaptive_enabled_invalid_type")
                if "retry_on_tpm" in adaptive and not isinstance(adaptive.get("retry_on_tpm"), bool):
                    errors.append("adaptive_retry_on_tpm_invalid_type")
                if "feature_flag" in adaptive and not isinstance(adaptive.get("feature_flag"), bool):
                    errors.append("adaptive_feature_flag_invalid_type")
                _validate_positive_int(adaptive, "cooldown_multiplier_max", "adaptive_cooldown_multiplier_max_invalid")
                _validate_positive_int(adaptive, "cooldown_retry_sleep_seconds", "adaptive_cooldown_retry_sleep_seconds_invalid")

    return errors
