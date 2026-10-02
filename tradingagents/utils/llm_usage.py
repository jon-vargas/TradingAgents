"""LLM usage and cost tracking utilities.

Captures token usage from LangChain model responses and estimates cost using
provider/model pricing tables (with optional runtime overrides).
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


# USD per 1M tokens. Each entry can declare:
#   "input"          — uncached prompt tokens
#   "output"         — completion tokens
#   "cached_input"   — cached prompt tokens (OpenAI bills these at 50% of input;
#                       Anthropic bills cache_read at 10%). When omitted the
#                       estimator defaults to input * 0.5, matching the OpenAI
#                       convention (safe upper bound for other providers).
#
# These defaults are intentionally conservative and can be overridden via
# config["model_pricing_usd_per_1m"].
DEFAULT_MODEL_PRICING_USD_PER_1M: Dict[str, Dict[str, float]] = {
    # OpenAI (cached_input = 50% of input per OpenAI prompt-caching pricing)
    "gpt-5.6-sol": {"input": 5.0, "cached_input": 0.5, "output": 30.0},
    "gpt-5.6-terra": {"input": 2.0, "cached_input": 0.2, "output": 12.0},
    "gpt-5.6-luna": {"input": 0.2, "cached_input": 0.02, "output": 1.2},
    "gpt-5.5": {"input": 5.0, "cached_input": 2.5, "output": 30.0},
    "gpt-5.4": {"input": 2.5, "cached_input": 1.25, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.375, "output": 4.5},
    # Legacy/dated OpenAI IDs observed in existing runs. These are best-effort
    # fallback estimates and should be overridden in config for billing-grade
    # accounting if your OpenAI contract differs.
    "gpt-5.2": {"input": 2.5, "cached_input": 1.25, "output": 15.0},
    "gpt-5": {"input": 2.5, "cached_input": 1.25, "output": 15.0},
    "gpt-4o": {"input": 2.5, "cached_input": 1.25, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    # Anthropic — cache_read priced at 10% of base input
    "claude-opus-4.7": {"input": 5.0, "cached_input": 0.5, "output": 25.0},
    "claude-sonnet-4.6": {"input": 3.0, "cached_input": 0.3, "output": 15.0},
    "claude-haiku-4.5": {"input": 1.0, "cached_input": 0.1, "output": 5.0},
    # Google Gemini (context caching ~25% of input)
    "gemini-3.1-pro": {"input": 2.0, "cached_input": 0.5, "output": 12.0},
    "gemini-3-flash": {"input": 0.5, "cached_input": 0.125, "output": 3.0},
    "gemini-3.1-flash-lite": {"input": 0.25, "cached_input": 0.0625, "output": 1.5},
    # DeepSeek
    "deepseek-v4-flash": {"input": 0.14, "cached_input": 0.014, "output": 0.28},
    "deepseek-v4-pro": {"input": 1.74, "cached_input": 0.174, "output": 3.48},
    # Perplexity (legacy Sonar ids + Agent API presets)
    "sonar": {"input": 1.0, "output": 1.0},
    "sonar-pro": {"input": 3.0, "output": 15.0},
    "sonar-reasoning-pro": {"input": 2.0, "output": 8.0},
    "fast": {"input": 1.0, "output": 1.0},
    "low": {"input": 3.0, "output": 15.0},
    "medium": {"input": 2.0, "output": 8.0},
    "high": {"input": 5.0, "output": 25.0},
}


def _num(val: Any) -> int:
    """Best-effort int coercion for token counts."""
    if val is None:
        return 0
    try:
        return max(0, int(round(float(val))))
    except (TypeError, ValueError):
        return 0


def _to_model_key(model_name: str) -> str:
    return (model_name or "").strip().lower()


def _extract_cached_input_tokens(m: Dict[str, Any]) -> int:
    """Find cached input tokens across every key shape we've observed.

    LangChain normalizes provider responses into ``input_token_details`` with
    keys that vary by provider. Raw OpenAI responses use
    ``prompt_tokens_details.cached_tokens``. Anthropic uses
    ``cache_read_input_tokens`` (and ``cache_creation_input_tokens`` for
    writes — those are billed separately and are NOT a cost saving).
    """
    if not isinstance(m, dict):
        return 0
    cached = max(
        _num(m.get("cached_input_tokens")),
        _num(m.get("cache_read_input_tokens")),
        _num(m.get("cached_tokens")),
    )
    details = m.get("input_token_details")
    if isinstance(details, dict):
        # LangChain normalised key for OpenAI: input_token_details.cache_read
        cached = max(
            cached,
            _num(details.get("cache_read")),
            _num(details.get("cached_tokens")),
            _num(details.get("cached")),
        )
    pt_details = m.get("prompt_tokens_details")
    if isinstance(pt_details, dict):
        cached = max(cached, _num(pt_details.get("cached_tokens")))
    return cached


def _extract_usage_from_mapping(m: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """Extract (input, output, total, cached_input) from a usage-ish mapping."""
    if not isinstance(m, dict):
        return 0, 0, 0, 0
    inp = _num(m.get("input_tokens", m.get("prompt_tokens")))
    out = _num(m.get("output_tokens", m.get("completion_tokens")))
    total = _num(m.get("total_tokens"))
    cached_in = _extract_cached_input_tokens(m)
    if total <= 0:
        total = inp + out
    return inp, out, total, cached_in


@dataclass
class _ModelAccum:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    retry_events: int = 0
    error_events: int = 0
    total_latency_ms: float = 0.0
    estimated_cost_usd: float = 0.0


class LLMUsageTracker(BaseCallbackHandler):
    """LangChain callback handler that accumulates token/cost usage."""

    def __init__(
        self,
        provider: str = "",
        pricing_overrides: Optional[Dict[str, Dict[str, float]]] = None,
    ):
        super().__init__()
        self.provider = (provider or "").strip().lower()
        self.pricing = dict(DEFAULT_MODEL_PRICING_USD_PER_1M)
        if isinstance(pricing_overrides, dict):
            for k, v in pricing_overrides.items():
                if isinstance(v, dict):
                    self.pricing[str(k).strip().lower()] = {
                        "input": float(v.get("input", 0.0) or 0.0),
                        "output": float(v.get("output", 0.0) or 0.0),
                    }

        self._lock = threading.Lock()
        # Pricing-version tag bumped when the cost formula changes so consumers
        # can tell whether a stored estimated_cost_usd was computed under the
        # cached-discount aware formula. Keep in sync with snapshot().
        self._pricing_version = "2026-05-cached"
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._calls = 0
            self._input_tokens = 0
            self._output_tokens = 0
            self._total_tokens = 0
            self._cached_input_tokens = 0
            self._retry_events = 0
            self._error_events = 0
            self._total_latency_ms = 0.0
            self._total_cost_usd = 0.0
            self._per_model: Dict[str, _ModelAccum] = {}
            self._unknown_price_models: set[str] = set()
            self._run_model: Dict[str, str] = {}
            self._run_started_at: Dict[str, float] = {}
            # Fallback stack for callback paths that do not emit run_id.
            self._anon_starts: List[Tuple[float, str]] = []

    def _lookup_price(self, model_name: str) -> Optional[Dict[str, float]]:
        key = _to_model_key(model_name)
        if not key:
            return None
        if key in self.pricing:
            return self.pricing[key]
        # Prefix fallback (e.g. "gpt-5.4-2026-04-15" -> "gpt-5.4")
        for base, price in self.pricing.items():
            if key.startswith(base):
                return price
        return None

    def _estimate_cost(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> float:
        """Estimate USD cost, applying provider cached-prompt discount.

        ``cached_input_tokens`` represents tokens served from the provider's
        prompt cache. They are subtracted from the regular input bucket and
        billed at the model's ``cached_input`` rate (defaults to 50% of input
        when the price table doesn't override it — the OpenAI convention).
        """
        price = self._lookup_price(model_name)
        if not price:
            return 0.0
        input_rate = float(price.get("input", 0.0) or 0.0)
        output_rate = float(price.get("output", 0.0) or 0.0)
        cached_rate = float(price.get("cached_input", input_rate * 0.5) or 0.0)
        cached = max(0, min(int(cached_input_tokens or 0), int(input_tokens or 0)))
        non_cached = max(0, int(input_tokens or 0) - cached)
        return (
            (non_cached / 1_000_000.0) * input_rate
            + (cached / 1_000_000.0) * cached_rate
            + (max(0, int(output_tokens or 0)) / 1_000_000.0) * output_rate
        )

    def _extract_model_from_serialized(self, serialized: Dict[str, Any]) -> str:
        model_name = ""
        if isinstance(serialized, dict):
            kwargs_map = serialized.get("kwargs") or {}
            if isinstance(kwargs_map, dict):
                model_name = str(kwargs_map.get("model") or kwargs_map.get("model_name") or "").strip()
        return model_name

    def _mark_start(self, model_name: str, run_id: Any = None) -> None:
        key = str(run_id) if run_id is not None else ""
        model_key = _to_model_key(model_name) or "unknown_model"
        if key:
            with self._lock:
                self._run_model[key] = model_key
                self._run_started_at[key] = time.perf_counter()
        else:
            with self._lock:
                self._anon_starts.append((time.perf_counter(), model_key))

    def _pop_start(self, run_id: Any = None, model_hint: str = "") -> Tuple[float, str]:
        key = str(run_id) if run_id is not None else ""
        with self._lock:
            if key and key in self._run_started_at:
                started = self._run_started_at.pop(key)
                model = self._run_model.pop(key, None) or "unknown_model"
                return started, model
            # Fallback: match anonymous start by model_hint, else FIFO.
            hint = _to_model_key(model_hint)
            if self._anon_starts:
                if hint:
                    for idx in range(len(self._anon_starts) - 1, -1, -1):
                        st, mk = self._anon_starts[idx]
                        if mk == hint:
                            self._anon_starts.pop(idx)
                            return st, mk
                st, mk = self._anon_starts.pop(0)
                return st, mk
        return 0.0, "unknown_model"

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: Any,
        run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        self._mark_start(self._extract_model_from_serialized(serialized), run_id=run_id)

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: Any,
        run_id: Any = None,
        **kwargs: Any,
    ) -> Any:
        # Chat models (e.g., ChatOpenAI/ChatAnthropic) emit this callback path.
        self._mark_start(self._extract_model_from_serialized(serialized), run_id=run_id)

    def on_retry(self, retry_state: Any, *, run_id: Any = None, **kwargs: Any) -> Any:
        key = str(run_id) if run_id is not None else ""
        with self._lock:
            self._retry_events += 1
            model_key = self._run_model.get(key) if key else None
            if model_key:
                acc = self._per_model.get(model_key) or _ModelAccum()
                acc.retry_events += 1
                self._per_model[model_key] = acc

    def on_llm_error(self, error: BaseException, run_id: Any = None, **kwargs: Any) -> Any:
        key = str(run_id) if run_id is not None else ""
        with self._lock:
            self._error_events += 1
            model_key = self._run_model.get(key) if key else None
            if model_key:
                acc = self._per_model.get(model_key) or _ModelAccum()
                acc.error_events += 1
                self._per_model[model_key] = acc
        # Consume start marker so anonymous stacks don't leak across calls.
        self._pop_start(run_id=run_id)

    def on_chat_model_error(self, error: BaseException, run_id: Any = None, **kwargs: Any) -> Any:
        self.on_llm_error(error, run_id=run_id, **kwargs)

    def _handle_end(self, response: Any, run_id: Any = None, **kwargs: Any) -> None:
        model_name = ""
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        cached_input_tokens = 0
        latency_ms = 0.0

        llm_output = (getattr(response, "llm_output", None) or {}) if response else {}
        if isinstance(llm_output, dict):
            model_name = (
                str(llm_output.get("model_name") or llm_output.get("model") or "").strip()
            )
            input_tokens, output_tokens, total_tokens, cached_input_tokens = _extract_usage_from_mapping(
                llm_output.get("token_usage") or llm_output.get("usage") or {}
            )

        # Fallback to generation message metadata (works for many chat models).
        generations = getattr(response, "generations", None) or []
        if generations and generations[0]:
            gen0 = generations[0][0]
            message = getattr(gen0, "message", None)
            if message is not None:
                usage_md = getattr(message, "usage_metadata", None) or {}
                in2, out2, tot2, cached2 = _extract_usage_from_mapping(usage_md)
                if (in2 + out2 + tot2) > 0:
                    input_tokens, output_tokens, total_tokens = in2, out2, tot2
                    cached_input_tokens = max(cached_input_tokens, cached2)

                resp_md = getattr(message, "response_metadata", None) or {}
                if isinstance(resp_md, dict):
                    if not model_name:
                        model_name = str(
                            resp_md.get("model_name") or resp_md.get("model") or ""
                        ).strip()
                    in3, out3, tot3, cached3 = _extract_usage_from_mapping(
                        resp_md.get("token_usage") or resp_md.get("usage") or {}
                    )
                    if (in3 + out3 + tot3) > 0 and (input_tokens + output_tokens + total_tokens) == 0:
                        input_tokens, output_tokens, total_tokens = in3, out3, tot3
                        cached_input_tokens = max(cached_input_tokens, cached3)

        if total_tokens <= 0:
            total_tokens = input_tokens + output_tokens

        started_at, model_key_from_start = self._pop_start(run_id=run_id, model_hint=model_name)
        if started_at > 0:
            latency_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)

        if not model_name and model_key_from_start and model_key_from_start != "unknown_model":
            model_name = model_key_from_start

        cost = self._estimate_cost(
            model_name, input_tokens, output_tokens, cached_input_tokens
        )
        model_key = _to_model_key(model_name) or "unknown_model"

        with self._lock:
            self._calls += 1
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._total_tokens += total_tokens
            self._cached_input_tokens += cached_input_tokens
            self._total_latency_ms += latency_ms
            self._total_cost_usd += cost

            acc = self._per_model.get(model_key) or _ModelAccum()
            acc.calls += 1
            acc.input_tokens += input_tokens
            acc.output_tokens += output_tokens
            acc.total_tokens += total_tokens
            acc.cached_input_tokens += cached_input_tokens
            acc.total_latency_ms += latency_ms
            acc.estimated_cost_usd += cost
            self._per_model[model_key] = acc

            if model_key != "unknown_model" and self._lookup_price(model_key) is None:
                self._unknown_price_models.add(model_key)

    def on_llm_end(self, response: LLMResult, run_id: Any = None, **kwargs: Any) -> Any:  # noqa: D401
        self._handle_end(response, run_id=run_id, **kwargs)

    def on_chat_model_end(self, response: Any, run_id: Any = None, **kwargs: Any) -> Any:
        self._handle_end(response, run_id=run_id, **kwargs)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            by_model = {
                m: {
                    "calls": a.calls,
                    "input_tokens": a.input_tokens,
                    "output_tokens": a.output_tokens,
                    "total_tokens": a.total_tokens,
                    "cached_input_tokens": a.cached_input_tokens,
                    "retry_events": a.retry_events,
                    "error_events": a.error_events,
                    "avg_latency_ms": round((a.total_latency_ms / a.calls), 3) if a.calls else 0.0,
                    "total_latency_ms": round(a.total_latency_ms, 3),
                    "estimated_cost_usd": round(a.estimated_cost_usd, 8),
                }
                for m, a in self._per_model.items()
            }
            return {
                "provider": self.provider,
                "calls": int(self._calls),
                "input_tokens": int(self._input_tokens),
                "output_tokens": int(self._output_tokens),
                "total_tokens": int(self._total_tokens),
                "cached_input_tokens": int(self._cached_input_tokens),
                "retry_events": int(self._retry_events),
                "error_events": int(self._error_events),
                "avg_latency_ms": round((self._total_latency_ms / self._calls), 3) if self._calls else 0.0,
                "total_latency_ms": round(float(self._total_latency_ms), 3),
                "total_cost_usd": round(float(self._total_cost_usd), 8),
                "by_model": by_model,
                "unknown_price_models": sorted(self._unknown_price_models),
                "pricing_version": self._pricing_version,
            }
