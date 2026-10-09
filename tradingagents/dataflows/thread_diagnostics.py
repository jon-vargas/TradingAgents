"""Thread stack introspection for diagnosing hung scans.

Python cannot kill a thread, so when a scan stalls the only useful evidence is
*where* each thread is blocked.  These helpers format that without py-spy.
"""

from __future__ import annotations

import sys
import threading
import traceback
from typing import Any, Dict, List, Optional


def _frames_for(ident: Optional[int]) -> List[str]:
    if ident is None:
        return []
    frame = sys._current_frames().get(ident)
    if frame is None:
        return []
    return [line.rstrip("\n") for line in traceback.format_stack(frame)]


def thread_stack(thread: Optional[threading.Thread], max_chars: int = 4000) -> str:
    """Formatted current stack of one thread ('' if it has exited)."""
    if thread is None:
        return ""
    text = "\n".join(_frames_for(thread.ident))
    return text[-max_chars:] if len(text) > max_chars else text


def find_thread(name_prefix: str) -> Optional[threading.Thread]:
    for t in threading.enumerate():
        if t.name.startswith(name_prefix):
            return t
    return None


def all_thread_stacks(max_chars_per_thread: int = 6000) -> List[Dict[str, Any]]:
    """Snapshot of every live thread: name, daemon flag, and its stack."""
    out: List[Dict[str, Any]] = []
    for t in threading.enumerate():
        text = "\n".join(_frames_for(t.ident))
        if len(text) > max_chars_per_thread:
            text = text[-max_chars_per_thread:]
        out.append({"name": t.name, "daemon": t.daemon, "alive": t.is_alive(), "stack": text})
    return out


def format_all_thread_stacks(max_chars_per_thread: int = 6000) -> str:
    parts = []
    for row in all_thread_stacks(max_chars_per_thread):
        parts.append(f"--- thread {row['name']} (daemon={row['daemon']}) ---\n{row['stack']}")
    return "\n".join(parts)
