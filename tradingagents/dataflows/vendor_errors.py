"""Vendor failure semantics — distinct from empty market data (#1386)."""

from __future__ import annotations

import re
from typing import Optional

VENDOR_UNAVAILABLE_PREFIX = "[Vendor unavailable:"

_UNAVAILABLE_PREFIX_RE = re.compile(r"^\[Vendor unavailable:", re.IGNORECASE)


class VendorUnavailableError(RuntimeError):
    """Raised when a vendor cannot serve data (rate limit, outage, breaker open)."""


def format_vendor_unavailable(method: str, vendor: str, reason: str) -> str:
    m = (method or "unknown").strip()
    v = (vendor or "unknown").strip()
    r = (reason or "unavailable").strip()
    return f"{VENDOR_UNAVAILABLE_PREFIX} {v} — {r}] (method={m})"


def is_vendor_unavailable_payload(payload: object) -> bool:
    if payload is None:
        return False
    if isinstance(payload, VendorUnavailableError):
        return True
    text = str(payload).strip()
    return bool(_UNAVAILABLE_PREFIX_RE.match(text))


def vendor_unavailable_failure_reason(payload: object) -> Optional[str]:
    if not is_vendor_unavailable_payload(payload):
        return None
    return f"vendor_unavailable:{str(payload)[:240]}"


RETRY_LATER_NOTE = (
    "Retry later. This is a temporary vendor outage, not evidence the symbol is delisted."
)


def annotate_vendor_payload(payload: object) -> str:
    """Pass vendor text through, and tell the model a rate-limit is not a delisting."""
    text = "" if payload is None else str(payload)
    if not is_vendor_unavailable_payload(text):
        return text
    if "not evidence the symbol is delisted" in text:
        return text
    return f"{text} {RETRY_LATER_NOTE}"


def should_persist_ticker_health_failure(reason: str) -> bool:
    """Transient vendor misses must not increment delist/eviction counters."""
    text = str(reason or "")
    if text.startswith("vendor_unavailable:"):
        return False
    return not is_vendor_unavailable_payload(text)
