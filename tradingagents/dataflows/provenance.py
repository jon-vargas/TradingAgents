from contextvars import ContextVar, Token
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional

_provenance_events: ContextVar[List[Dict[str, Any]]] = ContextVar(
    "provenance_events",
    default=[],
)
_lock = Lock()


def start_run() -> Token:
    """Initialize a per-run provenance buffer."""
    return _provenance_events.set([])


def end_run(token: Optional[Token] = None) -> None:
    """End a per-run provenance buffer and restore previous context."""
    if token is not None:
        _provenance_events.reset(token)
    else:
        _provenance_events.set([])


def _get_events_list() -> List[Dict[str, Any]]:
    events = _provenance_events.get()
    if events is None:
        events = []
        _provenance_events.set(events)
    return events


def record_event(event: Dict[str, Any], max_events: int = 500) -> None:
    if not event:
        return
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    with _lock:
        events = _get_events_list()
        events.append(event)
        if max_events and len(events) > max_events:
            excess = len(events) - max_events
            if excess > 0:
                del events[:excess]


def get_events() -> List[Dict[str, Any]]:
    with _lock:
        return list(_get_events_list())


def clear_events() -> None:
    with _lock:
        _get_events_list().clear()
