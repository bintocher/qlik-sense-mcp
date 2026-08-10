"""Response envelope, argument coercion and the tool decorators.

Everything here is about the contract with the caller rather than about
Qlik: how a reply is shaped, how a failure is reported, how long a call
took, and which calls have to take the Engine lock.
"""

import functools
import inspect
import json
import re
import time
from typing import Any, Dict, Optional


from . import context

logger = context.logger


def _err(msg: str, **extra: Any) -> str:
    d = {"error": msg}
    d.update(extra)
    return json.dumps(d, indent=2, ensure_ascii=False)



def _ok(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)



def _check() -> Optional[str]:
    """Return error string if clients are not ready, else None."""
    if context.repo_api is None:
        return _err("Qlik Sense configuration missing – set QLIK_SERVER_URL, QLIK_USER_DIRECTORY, QLIK_USER_ID etc.")
    return None



def _describe_call(sig: Optional[inspect.Signature], args, kwargs) -> Dict[str, Any]:
    """
    Reconstruct the arguments a tool was called with, for error replies.

    A bare "timed out after 180s" is useless to the caller: the whole
    point of the echo is that the LLM can see WHICH query it sent, spot
    the expensive dimension or the missing set-analysis filter, and
    retry with something cheaper.
    """
    if sig is not None:
        try:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except Exception:
            pass
    return {"args": list(args), "kwargs": dict(kwargs)}



def _engine_serialised(func):
    """Run one Engine-backed tool call at a time.

    The Streamable HTTP transport serves several MCP clients from one
    process, but there is exactly one Engine WebSocket and one open
    document behind it. Two overlapping calls interleave frames on that
    socket: strict id-matching makes each throw away the other's reply,
    and `ensure_app` can switch documents halfway through a call. Holding
    the client's reentrant lock for the whole tool body is the only
    boundary that makes the sequence atomic.

    Nothing is lost by serialising: Qlik gives this session a single
    document regardless, so the concurrency was never real.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if context.engine_api is None:
            return func(*args, **kwargs)
        with context.engine_api.transaction():
            return func(*args, **kwargs)

    # functools.wraps copies __qualname__, so the wrapper is otherwise
    # indistinguishable from the tool it guards. Tests assert on this flag
    # to catch a new Engine tool that forgets the decorator.
    wrapper.__engine_serialised__ = True
    return wrapper



def _timed(func):
    """
    Decorator for MCP tools: measures wall-clock time and injects
    `tool_call_seconds` as the first key of the JSON response.

    Works with tools that return a JSON string (via _ok / _err).
    If the result is not a JSON dict, wraps it into one.

    Any error reply — raised exception or an `{"error": ...}` payload
    returned by the tool itself — is annotated with the exact request
    that produced it (`tool` + `request`).
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        sig = None

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            result = func(*args, **kwargs)
        except Exception as ex:
            elapsed = round(time.monotonic() - t0, 3)
            logger.exception("Tool %s raised after %.3fs", func.__name__, elapsed)
            return json.dumps(
                {
                    "tool_call_seconds": elapsed,
                    "error": str(ex) or repr(ex),
                    "error_type": type(ex).__name__,
                    "tool": func.__name__,
                    "request": _describe_call(sig, args, kwargs),
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        elapsed = round(time.monotonic() - t0, 3)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except Exception:
                return json.dumps(
                    {"tool_call_seconds": elapsed, "result": result},
                    indent=2,
                    ensure_ascii=False,
                )
            if isinstance(parsed, dict):
                new_dict = {"tool_call_seconds": elapsed}
                new_dict.update(parsed)
                if "error" in new_dict:
                    # Tools report expected failures (timeout, bad field,
                    # limit exceeded) as a payload rather than an
                    # exception — echo the request for those too.
                    new_dict.setdefault("tool", func.__name__)
                    new_dict.setdefault("request", _describe_call(sig, args, kwargs))
                return json.dumps(new_dict, indent=2, ensure_ascii=False, default=str)
            return json.dumps(
                {"tool_call_seconds": elapsed, "result": parsed},
                indent=2,
                ensure_ascii=False,
            )
        return json.dumps(
            {"tool_call_seconds": elapsed, "result": result},
            indent=2,
            ensure_ascii=False,
        )
    return wrapper



def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y"):
            return True
        if v in ("false", "0", "no", "n"):
            return False
    return default



def _to_tribool(value: Any) -> Optional[bool]:
    """Three-state flag: True, False, or None meaning "do not filter".

    `_to_bool` cannot express the third state — it folds anything
    unrecognised into its default. That is why `published="both"`, which
    the tool documents as "return both", quietly returned published apps
    only: "both" is neither true nor false, so it landed on the default
    True.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y"):
            return True
        if v in ("false", "0", "no", "n"):
            return False
    return None



def _wildcard_to_regex(pattern: str, case_sensitive: bool) -> re.Pattern:
    escaped = re.escape(pattern).replace("\\*", ".*").replace("%", ".*")
    return re.compile(f"^{escaped}$", 0 if case_sensitive else re.IGNORECASE)



