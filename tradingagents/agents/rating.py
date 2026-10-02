"""Canonical ``Rating:`` label parsing for portfolio decisions."""

from __future__ import annotations

import re

_RATING_LABEL_RE = re.compile(
    r"(?<![a-z])rating\b[^:\-\u2010-\u2015]*[:\-\u2010-\u2015]\s*(?:\*\*)?(BUY|SELL|HOLD|REVIEW)\b",
    re.IGNORECASE,
)


def extract_rating_from_label(text: str) -> str | None:
    """Extract BUY/SELL/HOLD/REVIEW from an explicit ``Rating:`` line only."""
    if not text or not isinstance(text, str):
        return None
    found = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if stripped.startswith('"') or stripped.startswith("'"):
            continue
        m = _RATING_LABEL_RE.search(line)
        if m:
            found = m.group(1).upper()
    return found


def normalize_rating_for_display(rating: str | None) -> str:
    if not rating:
        return ""
    token = str(rating).strip().upper()
    if token in {"BUY", "SELL", "HOLD", "REVIEW"}:
        return token
    return token
