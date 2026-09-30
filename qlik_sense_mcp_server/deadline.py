"""The time limit of the tool call in progress.

An MCP client gives up on a call it has heard nothing from for a while -
Claude Code after 300 seconds - and then drops the whole session, not just
the call: the model is left without tools for the rest of its task. A call
the server stops itself, with an error that says what to narrow, costs one
retry instead.

The limit is set once per tool call and read by the Engine transport, which
shortens its socket timeout to whatever is left. A context variable rather
than an argument: the requests that need it are many layers below the tool,
and each call runs in its own worker thread with its own copy.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0


def tool_timeout_seconds() -> float:
    """QLIK_TOOL_TIMEOUT, or the default; 0 or less turns the limit off."""
    raw = os.getenv("QLIK_TOOL_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else DEFAULT_TOOL_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TOOL_TIMEOUT_SECONDS


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.at = time.monotonic() + seconds
        # Set when a request was cut short by this limit. A tool that
        # swallows the error of one step reports the next step's failure
        # ("not connected"), and this is how the caller still tells a
        # time limit from a broken connection.
        self.expired = False


_current: ContextVar[Optional[_Deadline]] = ContextVar("qlik_tool_deadline", default=None)


@contextmanager
def call_deadline(seconds: float) -> Iterator[_Deadline]:
    """Run the enclosed tool body under a limit of `seconds`."""
    deadline = _Deadline(seconds)
    token = _current.set(deadline if seconds > 0 else None)
    try:
        yield deadline
    finally:
        _current.reset(token)


def remaining() -> Optional[float]:
    """Seconds left for the current tool call; None outside a limited call."""
    deadline = _current.get()
    if deadline is None:
        return None
    return deadline.at - time.monotonic()


def mark_expired() -> float:
    """Record that the limit cut a request short; return the limit."""
    deadline = _current.get()
    if deadline is None:
        return 0.0
    deadline.expired = True
    return deadline.seconds
