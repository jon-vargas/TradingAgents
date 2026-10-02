import json
from typing import Any, Dict, Iterable, List, Optional


def _iter_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_text(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_text(item)


def _strip_prefix(raw: str, marker_prefix: Optional[str] = None) -> str:
    if not isinstance(raw, str):
        return str(raw) if raw is not None else ""
    text = raw.strip()
    if marker_prefix:
        idx = text.find(marker_prefix)
        if idx != -1:
            text = text[idx + len(marker_prefix) :].strip()
    if text.startswith("JSON:"):
        text = text[len("JSON:") :].strip()
    if text.startswith("RAW:"):
        text = text[len("RAW:") :].strip()
    return text


def normalize_snapshot_text(raw: str, marker_prefix: Optional[str] = None) -> str:
    """Normalize snapshot payload to a JSON string or raw text."""
    if raw is None or not isinstance(raw, str):
        return ""
    text = _strip_prefix(raw, marker_prefix)
    try:
        parsed = json.loads(text)
        return json.dumps(parsed)
    except (json.JSONDecodeError, TypeError):
        return text


def snapshot_text_to_dict(raw: str, marker_prefix: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Convert snapshot payload to a dict, preserving raw content."""
    if raw is None or not isinstance(raw, str) or not raw:
        return None
    text = normalize_snapshot_text(raw, marker_prefix)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"raw": text}


def extract_snapshot_from_messages(messages: List[Any], marker_prefix: str) -> str:
    """Extract and normalize snapshot payload from message content."""
    for message in reversed(messages or []):
        content = getattr(message, "content", "")
        for text in _iter_text(content):
            if marker_prefix in text:
                return normalize_snapshot_text(text, marker_prefix)
    return ""
