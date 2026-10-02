"""Per-analysis Perplexity live-call budget and prefetch ownership flags.

The budget lives in one mutable dict on a ContextVar. ``copy_context`` shares
that dict, so parallel analyst threads increment one run cap instead of each
getting a private counter.
"""
from __future__ import annotations

from contextvars import ContextVar
from threading import Lock
from typing import Dict, Optional

_OPTIONAL_LIVE_METHODS = frozenset({
    "catalyst",
    "get_earnings_deep_dive",
    "get_sec_deep_dive",
})

_budget: ContextVar[Optional[Dict[str, object]]] = ContextVar(
    "perplexity_budget",
    default=None,
)
_lock = Lock()


def _fresh_state(
    *,
    max_live_calls: int = 99,
    sec_prefetched: bool = False,
    transcript_prefetched: bool = False,
    block_overlap_prose: bool = False,
) -> Dict[str, object]:
    return {
        "cap": max(0, int(max_live_calls)),
        "count": 0,
        "sec_prefetched": bool(sec_prefetched),
        "transcript_prefetched": bool(transcript_prefetched),
        "block_overlap_prose": bool(block_overlap_prose),
        "research_satisfied": False,
    }


def _state() -> Dict[str, object]:
    state = _budget.get()
    if state is None:
        state = _fresh_state()
        _budget.set(state)
    return state


def reset_run_budget(
    *,
    max_live_calls: int = 3,
    sec_prefetched: bool = False,
    transcript_prefetched: bool = False,
    block_overlap_prose: bool = False,
) -> None:
    """Initialize per-run Perplexity counters (call at graph propagate start)."""
    _budget.set(
        _fresh_state(
            max_live_calls=max_live_calls,
            sec_prefetched=sec_prefetched,
            transcript_prefetched=transcript_prefetched,
            block_overlap_prose=block_overlap_prose,
        )
    )


def mark_snapshot_ownership(
    *,
    sec_prefetched: bool = False,
    transcript_prefetched: bool = False,
    block_overlap_prose: bool = False,
) -> None:
    """Update prefetch ownership flags after graph prefetch completes."""
    with _lock:
        state = _state()
        state["sec_prefetched"] = bool(sec_prefetched)
        state["transcript_prefetched"] = bool(transcript_prefetched)
        state["block_overlap_prose"] = bool(block_overlap_prose)


def mark_research_satisfied() -> None:
    """Record that get_deep_research already returned (cache hit or live)."""
    with _lock:
        _state()["research_satisfied"] = True


def research_satisfied() -> bool:
    with _lock:
        return bool(_state()["research_satisfied"])


def get_live_cap() -> int:
    with _lock:
        return int(_state()["cap"])


def get_live_count() -> int:
    with _lock:
        return int(_state()["count"])


def has_live_budget(method: str, *, research_available: Optional[bool] = None) -> bool:
    """Return False when per-run live cap is exhausted or snapshot is graph-owned.

    Optional tools (catalyst, demoted prose) must leave a live slot for
    ``get_deep_research`` until that artifact is already cached or recorded.
    """
    with _lock:
        state = _state()
        if method == "get_sec_filings_snapshot" and state["sec_prefetched"]:
            return False
        if method == "get_earnings_transcript_snapshot" and state["transcript_prefetched"]:
            return False
        if method in {"get_earnings_deep_dive", "get_sec_deep_dive"} and state["block_overlap_prose"]:
            return False

        remaining = int(state["cap"]) - int(state["count"])
        if remaining <= 0:
            return False

        if method in _OPTIONAL_LIVE_METHODS:
            research_ok = bool(state["research_satisfied"])
            if research_available is not None:
                research_ok = research_ok or bool(research_available)
            needed_for_research = 0 if research_ok else 1
            if remaining <= needed_for_research:
                return False
        return True


def register_live_call(method: str) -> None:
    with _lock:
        state = _state()
        state["count"] = int(state["count"]) + 1
        if method == "get_deep_research":
            state["research_satisfied"] = True


def overlap_prose_tools_blocked() -> bool:
    with _lock:
        return bool(_state()["block_overlap_prose"])


def is_valid_snapshot_payload(raw: object) -> bool:
    if not raw:
        return False
    return not str(raw).lstrip().startswith("[")
