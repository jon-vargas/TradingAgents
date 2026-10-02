"""Copy parent run ContextVars into analyst subgraph invocations (#1255 QA)."""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import Any, Optional, TypeVar

T = TypeVar("T")

_bound_run_context: Optional[contextvars.Context] = None


def bind_run_context() -> None:
    """Snapshot the current context on the orchestrator thread for worker nodes."""
    global _bound_run_context
    _bound_run_context = contextvars.copy_context()


def clear_run_context() -> None:
    global _bound_run_context
    _bound_run_context = None


def run_with_parent_context(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run ``fn`` with orchestrator context (ticker, provenance, budgets)."""
    base = _bound_run_context
    if base is None:
        return fn(*args, **kwargs)
    # Each worker gets its own Context instance (same var bindings). Reusing
    # ``base.run`` from parallel analyst threads raises "already entered".
    return base.copy().run(fn, *args, **kwargs)
