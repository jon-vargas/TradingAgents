# TradingAgents/graph/trading_graph.py

import logging
import os
from pathlib import Path
import json
from contextlib import contextmanager
from datetime import date
from typing import Dict, Any, Tuple, List, Optional

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI

from langgraph.prebuilt import ToolNode

from tradingagents.agents import *
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.agents.utils.memory import FinancialSituationMemory
from tradingagents.agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.provenance import (
    get_events as get_provenance,
    clear_events as clear_provenance,
    start_run as start_provenance_run,
    end_run as end_provenance_run,
)
from tradingagents.agents.utils.tool_context import (
    set_current_ticker,
    reset_current_ticker,
)
from tradingagents.utils.snapshot_utils import extract_snapshot_from_messages
from tradingagents.utils.llm_usage import LLMUsageTracker


def _prefetch_sec_filings_snapshot(ticker: str, trade_date: str) -> str:
    """Fetch a graph-owned SEC snapshot in deep mode so QC does not depend on leftover tool budget."""
    from tradingagents.dataflows.config import get_config

    if not get_config().get("use_perplexity"):
        return ""
    try:
        from datetime import datetime as dt

        from tradingagents.dataflows.interface import get_sec_filings_snapshot as fetch_sec

        after_date = None
        try:
            as_of = dt.strptime(str(trade_date)[:10], "%Y-%m-%d")
            after_date = as_of.replace(year=as_of.year - 1).strftime("%Y-%m-%d")
        except Exception:
            after_date = None
        raw = fetch_sec(ticker, ticker, after_date=after_date)
        if not raw or str(raw).lstrip().startswith("["):
            return ""
        return str(raw)
    except Exception as exc:
        logger.debug("SEC snapshot prefetch skipped for %s: %s", ticker, exc)
        return ""


def _prefetch_earnings_transcript_snapshot(
    ticker: str,
    company_name: str,
    *,
    analysis_mode: str,
    instrument_identity: Optional[Dict[str, Any]] = None,
) -> str:
    """Fetch a graph-owned transcript snapshot in deep mode (equities only)."""
    if str(analysis_mode or "").lower().strip() != "deep":
        return ""
    from tradingagents.dataflows.config import get_config
    from tradingagents.dataflows.instrument_identity import is_commodity_etf

    if is_commodity_etf(ticker, instrument_identity):
        return ""
    if not get_config().get("use_perplexity"):
        return ""
    try:
        from tradingagents.dataflows.interface import get_earnings_transcript_snapshot as fetch_transcript

        raw = fetch_transcript(ticker, company_name or ticker)
        if not raw or str(raw).lstrip().startswith("["):
            return ""
        return str(raw)
    except Exception as exc:
        logger.debug("Transcript snapshot prefetch skipped for %s: %s", ticker, exc)
        return ""

# Import the new abstract tool methods from agent_utils
from tradingagents.agents.utils.agent_utils import (
    get_stock_data,
    get_indicators,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_news,
    get_insider_sentiment,
    get_insider_transactions,
    get_global_news,
    get_deep_research,
    get_sec_filings_snapshot,
    get_earnings_transcript_snapshot,
    get_verified_market_snapshot,
)

from .checkpointer import (
    checkpoint_step,
    clear_checkpoint,
    get_checkpointer,
    thread_id,
)
from .conditional_logic import ConditionalLogic
from .setup import GraphSetup
from .propagation import Propagator
from .reflection import Reflector
from .signal_processing import SignalProcessor, validate_decision

logger = logging.getLogger("tradingagents.graph.trading_graph")


def _openai_chat_completions_extra_body(
    model: str,
    *,
    for_tool_calling: bool,
    prompt_cache_key: str = "",
) -> Dict[str, Any]:
    """Build ``extra_body`` for OpenAI Chat Completions clients.

    GPT-5.5/5.6 on ``/v1/chat/completions`` reject ``bind_tools`` unless
    ``reasoning_effort`` is ``none``. Analyst agents always tool-call on the
    quick model, so we disable reasoning there. Deep agents synthesize text
    only and keep the model default reasoning effort.
    """
    extra: Dict[str, Any] = {}
    if prompt_cache_key:
        extra["prompt_cache_key"] = prompt_cache_key
    model_l = (model or "").lower()
    if for_tool_calling and model_l.startswith(("gpt-5.6", "gpt-5.5")):
        extra["reasoning_effort"] = "none"
    return extra


class TradingAgentsGraph:
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "social", "news", "fundamentals"],
        debug=False,
        config: Dict[str, Any] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(
            os.path.join(self.config["project_dir"], "dataflows/data_cache"),
            exist_ok=True,
        )

        llm_budget = self.config.get("token_budget", {}).get("llm", {})
        llm_retries = max(0, int(llm_budget.get("max_retries", 6)))
        quick_max_output = llm_budget.get("quick_max_output_tokens")
        deep_max_output = llm_budget.get("deep_max_output_tokens")
        self._llm_usage_enabled = bool(self.config.get("enable_usage_tracking", True))
        self._llm_usage_tracker = (
            LLMUsageTracker(
                provider=str(self.config.get("llm_provider", "")),
                pricing_overrides=self.config.get("model_pricing_usd_per_1m"),
            )
            if self._llm_usage_enabled
            else None
        )

        # Initialize LLMs (retries + output caps reduce cumulative TPM pressure)
        if self.config["llm_provider"].lower() == "openai" or self.config["llm_provider"] == "ollama" or self.config["llm_provider"] == "openrouter":
            deep_kwargs = {
                "model": self.config["deep_think_llm"],
                "base_url": self.config["backend_url"],
                "max_retries": llm_retries,
            }
            quick_kwargs = {
                "model": self.config["quick_think_llm"],
                "base_url": self.config["backend_url"],
                "max_retries": llm_retries,
            }
            if isinstance(deep_max_output, int) and deep_max_output > 0:
                deep_kwargs["max_tokens"] = deep_max_output
            if isinstance(quick_max_output, int) and quick_max_output > 0:
                quick_kwargs["max_tokens"] = quick_max_output
            # OpenAI prompt-cache routing key + GPT-5.6 tool-call compatibility.
            cache_key = ""
            if self.config["llm_provider"].lower() == "openai":
                cache_key = (self.config.get("openai_prompt_cache_key") or "").strip()
            deep_kwargs["extra_body"] = _openai_chat_completions_extra_body(
                self.config["deep_think_llm"],
                for_tool_calling=False,
                prompt_cache_key=cache_key,
            )
            quick_kwargs["extra_body"] = _openai_chat_completions_extra_body(
                self.config["quick_think_llm"],
                for_tool_calling=True,
                prompt_cache_key=cache_key,
            )
            if self._llm_usage_tracker is not None:
                deep_kwargs["callbacks"] = [self._llm_usage_tracker]
                quick_kwargs["callbacks"] = [self._llm_usage_tracker]
            self.deep_thinking_llm = ChatOpenAI(**deep_kwargs)
            self.quick_thinking_llm = ChatOpenAI(**quick_kwargs)
        elif self.config["llm_provider"].lower() == "anthropic":
            deep_kwargs = {
                "model": self.config["deep_think_llm"],
                "base_url": self.config["backend_url"],
                "max_retries": llm_retries,
            }
            quick_kwargs = {
                "model": self.config["quick_think_llm"],
                "base_url": self.config["backend_url"],
                "max_retries": llm_retries,
            }
            if isinstance(deep_max_output, int) and deep_max_output > 0:
                deep_kwargs["max_tokens"] = deep_max_output
            if isinstance(quick_max_output, int) and quick_max_output > 0:
                quick_kwargs["max_tokens"] = quick_max_output
            if self._llm_usage_tracker is not None:
                deep_kwargs["callbacks"] = [self._llm_usage_tracker]
                quick_kwargs["callbacks"] = [self._llm_usage_tracker]
            self.deep_thinking_llm = ChatAnthropic(**deep_kwargs)
            self.quick_thinking_llm = ChatAnthropic(**quick_kwargs)
        elif self.config["llm_provider"].lower() == "google":
            deep_kwargs = {"model": self.config["deep_think_llm"]}
            quick_kwargs = {"model": self.config["quick_think_llm"]}
            if isinstance(deep_max_output, int) and deep_max_output > 0:
                deep_kwargs["max_output_tokens"] = deep_max_output
            if isinstance(quick_max_output, int) and quick_max_output > 0:
                quick_kwargs["max_output_tokens"] = quick_max_output
            if self._llm_usage_tracker is not None:
                deep_kwargs["callbacks"] = [self._llm_usage_tracker]
                quick_kwargs["callbacks"] = [self._llm_usage_tracker]
            self.deep_thinking_llm = ChatGoogleGenerativeAI(**deep_kwargs)
            self.quick_thinking_llm = ChatGoogleGenerativeAI(**quick_kwargs)
        else:
            raise ValueError(f"Unsupported LLM provider: {self.config['llm_provider']}")
        
        # Initialize memories (persist across restarts unless reset_memory is True)
        reset = self.config.get("reset_memory", False)
        self.bull_memory = FinancialSituationMemory("bull_memory", self.config, reset_on_init=reset)
        self.bear_memory = FinancialSituationMemory("bear_memory", self.config, reset_on_init=reset)
        self.trader_memory = FinancialSituationMemory("trader_memory", self.config, reset_on_init=reset)
        self.invest_judge_memory = FinancialSituationMemory("invest_judge_memory", self.config, reset_on_init=reset)
        self.risk_manager_memory = FinancialSituationMemory("risk_manager_memory", self.config, reset_on_init=reset)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config.get("max_debate_rounds", 1),
            max_risk_discuss_rounds=self.config.get("max_risk_discuss_rounds", 1),
        )
        self.risk_profile = self.config.get("risk_profile", "conservative")
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.bull_memory,
            self.bear_memory,
            self.trader_memory,
            self.invest_judge_memory,
            self.risk_manager_memory,
            self.conditional_logic,
            self.risk_profile,
            config=self.config,
        )

        self.propagator = Propagator(
            max_recur_limit=self.config.get("max_recur_limit", 100),
        )
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        self.selected_analysts = tuple(selected_analysts)
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None
        self._resuming = False

    def _run_signature(self, investment_profile_key: str | None = None) -> str:
        from tradingagents.graph.setup import resolve_analyst_layout

        return "|".join(
            [
                "analysts=" + ",".join(self.selected_analysts),
                f"layout={resolve_analyst_layout(self.config)}",
                f"debate={self.config.get('max_debate_rounds', 1)}",
                f"risk={self.config.get('max_risk_discuss_rounds', 1)}",
                f"mode={self.config.get('analysis_mode', 'standard')}",
                f"risk_profile={self.config.get('risk_profile', 'growth')}",
                f"quick={self.config.get('quick_think_llm')}",
                f"deep={self.config.get('deep_think_llm')}",
                f"profile={investment_profile_key or 'none'}",
            ]
        )

    def run_settings(self) -> Dict[str, Any]:
        """Allowlisted run metadata for reports and persistence (#752)."""
        from tradingagents.graph.setup import resolve_analyst_layout

        try:
            from importlib.metadata import version

            pkg_version = version("tradingagents")
        except Exception:
            pkg_version = "0.1.0"
        cfg = self.config
        return {
            "version": pkg_version,
            "llm_provider": cfg.get("llm_provider"),
            "deep_think_llm": cfg.get("deep_think_llm"),
            "quick_think_llm": cfg.get("quick_think_llm"),
            "analysts": list(self.selected_analysts),
            "analysis_mode": cfg.get("analysis_mode"),
            "risk_profile": cfg.get("risk_profile"),
            "max_debate_rounds": cfg.get("max_debate_rounds"),
            "max_risk_discuss_rounds": cfg.get("max_risk_discuss_rounds"),
            "analyst_layout": resolve_analyst_layout(cfg),
            "data_vendors": dict(cfg.get("data_vendors") or {}),
            "tool_vendors": dict(cfg.get("tool_vendors") or {}),
        }

    def begin_checkpoint(
        self,
        company_name: str,
        trade_date: str,
        investment_profile_key: str | None = None,
    ) -> str | None:
        self._resuming = False
        if not self.config.get("checkpoint_enabled"):
            return None
        signature = self._run_signature(investment_profile_key)
        self._checkpointer_ctx = get_checkpointer(self.config["data_cache_dir"], company_name)
        saver = self._checkpointer_ctx.__enter__()
        self.graph = self.workflow.compile(checkpointer=saver)
        step = checkpoint_step(
            self.config["data_cache_dir"],
            company_name,
            str(trade_date),
            signature,
        )
        self._resuming = step is not None
        if step is not None:
            logger.info("Resuming analysis from step %s for %s on %s", step, company_name, trade_date)
        return thread_id(company_name, str(trade_date), signature)

    def checkpoint_input(self, init_state: Dict[str, Any]):
        return None if self._resuming else init_state

    def end_checkpoint(self) -> None:
        if self._checkpointer_ctx is not None:
            self._checkpointer_ctx.__exit__(None, None, None)
            self._checkpointer_ctx = None
            self.graph = self.workflow.compile()
        self._resuming = False

    @contextmanager
    def checkpoint_scope(
        self,
        company_name: str,
        trade_date: str,
        investment_profile_key: str | None = None,
    ):
        try:
            yield self.begin_checkpoint(company_name, trade_date, investment_profile_key)
        finally:
            self.end_checkpoint()

    def clear_checkpoint_on_success(
        self,
        company_name: str,
        trade_date: str,
        investment_profile_key: str | None = None,
    ) -> None:
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"],
                company_name,
                str(trade_date),
                self._run_signature(investment_profile_key),
            )

    def _graph_invoke_args(self, thread_id_value: str | None) -> Dict[str, Any]:
        args = self.propagator.get_graph_args()
        if thread_id_value:
            args["config"] = {
                **args.get("config", {}),
                "configurable": {"thread_id": thread_id_value},
            }
        return args

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources using abstract methods."""
        return {
            "market": ToolNode(
                [
                    # Core stock data tools
                    get_stock_data,
                    # Technical indicators
                    get_indicators,
                    get_verified_market_snapshot,
                ]
            ),
            "social": ToolNode(
                [
                    # News tools for social media analysis
                    get_news,
                ]
            ),
            "news": ToolNode(
                [
                    # News and insider information
                    get_news,
                    get_global_news,
                    get_insider_sentiment,
                    get_insider_transactions,
                    # Perplexity: research + snapshot fallback only (prose tools demoted)
                    get_deep_research,
                    get_sec_filings_snapshot,
                    get_earnings_transcript_snapshot,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    # Fundamental analysis tools
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                ]
            ),
        }

    def propagate(self, company_name, trade_date, investment_profile=None, screening_context=None):
        """Run the trading agents graph for a company on a specific date."""

        self.ticker = company_name
        if self._llm_usage_tracker is not None:
            self._llm_usage_tracker.reset()

        profile_key = (investment_profile or {}).get("profile_key")
        with self.checkpoint_scope(company_name, trade_date, profile_key) as thread_id_value:
            return self._propagate_inner(
                company_name,
                trade_date,
                investment_profile=investment_profile,
                screening_context=screening_context,
                thread_id_value=thread_id_value,
                profile_key=profile_key,
            )

    def _propagate_inner(
        self,
        company_name,
        trade_date,
        *,
        investment_profile=None,
        screening_context=None,
        thread_id_value=None,
        profile_key=None,
    ):
        from tradingagents.dataflows.instrument_identity import resolve_instrument_context_for_run
        from tradingagents.dataflows.run_date import is_historical_run

        identity, instrument_context = resolve_instrument_context_for_run(
            company_name, trade_date
        )

        # Initialize state
        init_agent_state = self.propagator.create_initial_state(
            company_name, trade_date,
            investment_profile=investment_profile,
            risk_profile=self.risk_profile,
            screening_context=screening_context,
            instrument_context=instrument_context,
            instrument_identity=identity,
        )
        init_agent_state["is_historical_run"] = is_historical_run(trade_date)
        init_agent_state["analysis_mode"] = str(self.config.get("analysis_mode") or "standard")
        try:
            from tradingagents.reporting.decision_scorecard import resolve_scorecard_limits

            profile = investment_profile if isinstance(investment_profile, dict) else {}
            effective_limits, _skip_beta = resolve_scorecard_limits(
                init_agent_state,
                company_name,
                self.risk_profile,
                profile.get("profile_key"),
                profile.get("resolved_from"),
                self.config,
            )
            init_agent_state["effective_risk_limits"] = effective_limits
        except Exception as exc:
            logger.debug("Initial effective risk-limit resolution skipped: %s", exc)
        from tradingagents.dataflows.yfinance_extended import get_macro_snapshot

        init_agent_state["macro_snapshot"] = get_macro_snapshot(as_of_date=trade_date)
        args = self._graph_invoke_args(thread_id_value)
        invoke_state = self.checkpoint_input(init_agent_state)

        from tradingagents.agents.utils.tool_context import (
            reset_current_trade_date,
            set_current_trade_date,
        )

        ticker_token = set_current_ticker(company_name)
        trade_date_token = set_current_trade_date(trade_date)
        token = start_provenance_run()
        from tradingagents.graph.analyst_context import bind_run_context, clear_run_context
        from tradingagents.dataflows.instrument_identity import is_commodity_etf
        from tradingagents.dataflows.perplexity_budget import (
            is_valid_snapshot_payload,
            mark_snapshot_ownership,
            reset_run_budget,
        )

        analysis_mode = str(self.config.get("analysis_mode") or "standard")
        use_perplexity = bool(self.config.get("use_perplexity"))
        is_commodity = is_commodity_etf(
            company_name, identity if isinstance(identity, dict) else None
        )
        per_run_cap = 2 if is_commodity else 3
        if use_perplexity and analysis_mode.lower() == "deep":
            reset_run_budget(max_live_calls=per_run_cap)
        try:
            init_agent_state["sec_filings_snapshot"] = _prefetch_sec_filings_snapshot(
                company_name, trade_date
            )
            init_agent_state["earnings_transcript_snapshot"] = _prefetch_earnings_transcript_snapshot(
                company_name,
                identity.get("company_name") if isinstance(identity, dict) else company_name,
                analysis_mode=analysis_mode,
                instrument_identity=identity if isinstance(identity, dict) else None,
            )
            if use_perplexity and analysis_mode.lower() == "deep":
                mark_snapshot_ownership(
                    sec_prefetched=is_valid_snapshot_payload(
                        init_agent_state.get("sec_filings_snapshot")
                    ),
                    transcript_prefetched=is_valid_snapshot_payload(
                        init_agent_state.get("earnings_transcript_snapshot")
                    ),
                    block_overlap_prose=True,
                )
            # Snapshot after ticker, provenance, and the Perplexity budget so
            # analyst worker threads share this run's context.
            bind_run_context()
            if self.debug:
                # Debug mode with tracing
                trace = []
                for chunk in self.graph.stream(invoke_state, **args):
                    if len(chunk["messages"]) == 0:
                        pass
                    else:
                        chunk["messages"][-1].pretty_print()
                        trace.append(chunk)

                final_state = trace[-1] if trace else init_agent_state
            else:
                # Standard mode without tracing
                final_state = self.graph.invoke(invoke_state, **args)

            # Attach data provenance for reporting and persistence
            final_state["data_provenance"] = get_provenance()
            if self._llm_usage_tracker is not None:
                llm_usage = self._llm_usage_tracker.snapshot()
                final_state["llm_usage_summary"] = llm_usage
                final_state["total_tokens"] = int(llm_usage.get("total_tokens") or 0)
                final_state["total_cost"] = float(llm_usage.get("total_cost_usd") or 0.0)

            # Prefer tool-message snapshots when the analyst fetched them; keep
            # the graph-owned prefetch otherwise.
            extracted_sec = extract_snapshot_from_messages(
                final_state.get("messages", []),
                "SEC_FILINGS_SNAPSHOT_",
            )
            final_state["sec_filings_snapshot"] = (
                extracted_sec or final_state.get("sec_filings_snapshot") or ""
            )
            extracted_transcript = extract_snapshot_from_messages(
                final_state.get("messages", []),
                "EARNINGS_TRANSCRIPT_SNAPSHOT_",
            )
            final_state["earnings_transcript_snapshot"] = (
                extracted_transcript
                or final_state.get("earnings_transcript_snapshot")
                or init_agent_state.get("earnings_transcript_snapshot")
                or ""
            )
            final_state["analysis_mode"] = init_agent_state.get("analysis_mode")
            final_state["instrument_identity"] = identity if isinstance(identity, dict) else {}
            final_state["effective_risk_limits"] = init_agent_state.get("effective_risk_limits")
            final_state["run_settings"] = self.run_settings()

            # Store current state for reflection
            self.curr_state = final_state

            # Log state (non-fatal — don't let logging crash the analysis)
            try:
                self._log_state(trade_date, final_state)
            except Exception as e:
                logger.warning("State logging failed (non-fatal): %s", e)

            # Process final signal (non-fatal — fall back to raw decision on failure)
            raw_decision = final_state.get("final_trade_decision", "")
            from tradingagents.agents.rating import extract_rating_from_label

            label_rating = extract_rating_from_label(raw_decision or "")
            if label_rating:
                final_state["final_rating"] = label_rating
            try:
                decision = self.process_signal(raw_decision)
            except Exception as e:
                logger.warning("Signal processing failed, using raw decision: %s", e)
                decision = raw_decision

            # Decision threshold guardrails
            try:
                from tradingagents.graph.signal_aggregator import compute_signal_summary
                from tradingagents.dataflows.config import get_config
                cfg = get_config()
                data_completeness = cfg.get("data_completeness", 1.0)
                sig_result = compute_signal_summary(
                    final_state,
                    company_name,
                    data_completeness=data_completeness,
                    config=cfg,
                )
                composite = sig_result.get("composite")
                risk_profile = cfg.get("risk_profile", "growth")
                guardrail = validate_decision(
                    decision,
                    composite,
                    risk_profile,
                    cfg,
                    state=final_state,
                )
                if guardrail["guardrail_triggered"]:
                    final_state["guardrail_original_decision"] = guardrail["original_decision"]
                    final_state["guardrail_override_reason"] = guardrail["override_reason"]
                if guardrail.get("sell_guardrail"):
                    final_state["sell_guardrail_triggered"] = True
                    final_state["sell_guardrail_reason"] = guardrail["sell_guardrail"]
                    final_state["sell_guardrail_severity"] = guardrail.get("severity")
                decision = guardrail["final_decision"]
            except Exception as e:
                logger.debug("Decision guardrail check skipped: %s", e)

            try:
                from tradingagents.reporting.position_action import enforce_position_action

                correction = enforce_position_action(final_state)
                if correction.get("adjusted"):
                    final_state["position_action_correction"] = correction
                    logger.info(
                        "Adjusted position_action %s -> %s for %s",
                        correction.get("from"),
                        correction.get("to"),
                        company_name,
                    )
            except Exception as e:
                logger.debug("Position action enforcement skipped: %s", e)

            try:
                from tradingagents.dataflows.config import get_config
                from tradingagents.reporting.book_action import (
                    decision_from_state,
                    enforce_book_action,
                )

                book_cfg = get_config()
                book_correction = enforce_book_action(final_state, book_cfg)
                if book_correction.get("adjusted"):
                    final_state["book_action_correction"] = book_correction
                    logger.info(
                        "Adjusted book action %s -> %s for %s (%s)",
                        book_correction.get("from_decision"),
                        book_correction.get("to_decision"),
                        company_name,
                        book_correction.get("reason"),
                    )
                decision = decision_from_state(final_state, decision)
            except Exception as e:
                logger.debug("Book action enforcement skipped: %s", e)

            self.clear_checkpoint_on_success(company_name, trade_date, profile_key)
            return final_state, decision
        except Exception as e:
            logger.error("Graph execution failed for %s on %s: %s", company_name, trade_date, e)
            raise
        finally:
            clear_run_context()
            clear_provenance()
            end_provenance_run(token)
            reset_current_trade_date(trade_date_token)
            reset_current_ticker(ticker_token)

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        inv_debate = final_state.get("investment_debate_state", {}) or {}
        risk_debate = final_state.get("risk_debate_state", {}) or {}

        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state.get("company_of_interest", ""),
            "trade_date": final_state.get("trade_date", ""),
            "market_report": final_state.get("market_report", ""),
            "sentiment_report": final_state.get("sentiment_report", ""),
            "news_report": final_state.get("news_report", ""),
            "fundamentals_report": final_state.get("fundamentals_report", ""),
            "investment_debate_state": {
                "bull_history": inv_debate.get("bull_history", ""),
                "bear_history": inv_debate.get("bear_history", ""),
                "history": inv_debate.get("history", ""),
                "current_response": inv_debate.get("current_response", ""),
                "judge_decision": inv_debate.get("judge_decision", ""),
            },
            "trader_investment_decision": final_state.get("trader_investment_plan", ""),
            "risk_debate_state": {
                "risky_history": risk_debate.get("risky_history", ""),
                "safe_history": risk_debate.get("safe_history", ""),
                "neutral_history": risk_debate.get("neutral_history", ""),
                "history": risk_debate.get("history", ""),
                "judge_decision": risk_debate.get("judge_decision", ""),
            },
            "investment_plan": final_state.get("investment_plan", ""),
            "final_trade_decision": final_state.get("final_trade_decision", ""),
            "sec_filings_snapshot": final_state.get("sec_filings_snapshot", ""),
            "earnings_transcript_snapshot": final_state.get("earnings_transcript_snapshot", ""),
            "earnings_quality": final_state.get("earnings_quality"),
            "intrinsic_value": final_state.get("intrinsic_value"),
            "scenario_analysis": final_state.get("scenario_analysis"),
            "catalyst_pipeline": final_state.get("catalyst_pipeline"),
            "peer_comps": final_state.get("peer_comps"),
            "factor_scorecard": final_state.get("factor_scorecard"),
            "run_settings": final_state.get("run_settings"),
        }

        # Save to file
        directory = Path(f"eval_results/{self.ticker}/TradingAgentsStrategy_logs/")
        directory.mkdir(parents=True, exist_ok=True)

        with open(
            f"eval_results/{self.ticker}/TradingAgentsStrategy_logs/full_states_log_{trade_date}.json",
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(self.log_states_dict, f, indent=4)

    def reflect_and_remember(self, returns_losses):
        """Reflect on decisions and update memory based on returns."""
        self.reflector.reflect_bull_researcher(
            self.curr_state, returns_losses, self.bull_memory
        )
        self.reflector.reflect_bear_researcher(
            self.curr_state, returns_losses, self.bear_memory
        )
        self.reflector.reflect_trader(
            self.curr_state, returns_losses, self.trader_memory
        )
        self.reflector.reflect_invest_judge(
            self.curr_state, returns_losses, self.invest_judge_memory
        )
        self.reflector.reflect_risk_manager(
            self.curr_state, returns_losses, self.risk_manager_memory
        )

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)
