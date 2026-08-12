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

# Log the body of each reply, not just the call. Auditing "what did the
# model see" needs both halves of the exchange.
_LOG_REPLIES = __import__("os").getenv("QLIK_LOG_REPLIES", "").lower() == "true"
# Enough of a reply to see what the caller was given; a full 10k-row page
# in the log helps nobody and buries the next entry.
_LOG_REPLY_CHARS = 4000


# What a reader needs first, in the order it needs it. A model reads the
# head of a reply most carefully, so the category and the fix go before the
# echo of the request and long before the timings.
_ERROR_KEY_ORDER = (
    "error_category", "error", "did_you_mean", "allowed_values",
    "unknown_fields", "invalid_expressions", "available_columns",
    "next_actions", "hint", "tool", "request",
)


def _err(msg: str, **extra: Any) -> str:
    """An error a caller can act on without a second guess.

    Ordered deliberately: the category first, then what to do about it,
    then the echo of what was sent. Timings and tracebacks last — they
    matter to a human reading a log, not to whoever has to fix the call.
    """
    payload = {"error": msg}
    payload.update(extra)

    ordered = {key: payload[key] for key in _ERROR_KEY_ORDER if key in payload}
    for key, value in payload.items():
        if key not in ordered:
            ordered[key] = value
    return json.dumps(ordered, indent=2, ensure_ascii=False)



# Keys that hold bulk data. Indenting these costs more than the whole rest
# of the reply: a 500x6 hypercube is 48620 characters at indent=2 against
# 22575 compact, and none of those newlines carry meaning to a reader that
# is a language model.
_BULK_KEYS = ("rows", "field_values", "values", "executions", "apps",
              "tasks", "objects", "sheets", "fields", "tables", "call_list")


def _ok(obj: Any) -> str:
    """Serialise a successful reply: readable envelope, compact bulk.

    The envelope stays indented because a model reads the first keys of a
    reply most carefully, and the structure helps. The bulk arrays go on
    one line each — every row of a hypercube used to be spread over one
    line per cell.
    """
    if not isinstance(obj, dict):
        return json.dumps(obj, indent=2, ensure_ascii=False)

    compact = {}
    for key, value in obj.items():
        if key in _BULK_KEYS and isinstance(value, list) and len(value) > 3:
            compact[key] = _CompactList(value)
        else:
            compact[key] = value
    return json.dumps(compact, indent=2, ensure_ascii=False, cls=_CompactEncoder)


class _CompactList(list):
    """Marker: serialise this list without the indentation."""


class _CompactEncoder(json.JSONEncoder):
    def __init__(self, *args, **kwargs):
        kwargs.pop("indent", None)
        super().__init__(*args, indent=2, **kwargs)
        self._placeholders = {}

    def default(self, o):
        if isinstance(o, _CompactList):
            key = f"__compact_{id(o)}__"
            self._placeholders[key] = json.dumps(
                list(o), ensure_ascii=False, separators=(",", ":"), default=str)
            return key
        return str(o)

    def encode(self, o):
        # Lists are handled by the C encoder before default() ever sees them,
        # so swap them out here and put the compact text back afterwards.
        placeholders = {}

        def swap(value):
            if isinstance(value, _CompactList):
                key = f"__compact_{len(placeholders)}__"
                placeholders[key] = json.dumps(
                    list(value), ensure_ascii=False, separators=(",", ":"), default=str)
                return key
            if isinstance(value, dict):
                return {k: swap(v) for k, v in value.items()}
            if isinstance(value, list):
                return [swap(v) for v in value]
            return value

        text = super().encode(swap(o))
        for key, compact_text in placeholders.items():
            text = text.replace(f'"{key}"', compact_text)
        return text



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
        # Log what was asked, not just what failed. When a model drives
        # these tools, the arguments are the interesting part — which field
        # it guessed, how many rows it asked for, what set analysis it
        # wrote — and without this the only record of a successful call is
        # a duration.
        logger.info("tool %s %s", func.__name__, _describe_call(sig, args, kwargs))
        try:
            result = func(*args, **kwargs)
            if _LOG_REPLIES and isinstance(result, str):
                # QLIK_LOG_REPLIES exists for auditing what a model was
                # actually told. Off by default: replies carry data, and
                # data does not belong in a log unless someone asked.
                logger.info("reply %s %s", func.__name__,
                            result[:_LOG_REPLY_CHARS])
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
                if "error" in parsed:
                    # Tools report expected failures (timeout, bad field,
                    # limit exceeded) as a payload rather than an
                    # exception — echo the request for those too. The
                    # duration goes last: on an error the category and the
                    # fix are what the reader needs from the first line.
                    parsed.setdefault("tool", func.__name__)
                    parsed.setdefault("request", _describe_call(sig, args, kwargs))
                    parsed["tool_call_seconds"] = elapsed
                    return _err(parsed.pop("error"), **parsed)
                new_dict = {"tool_call_seconds": elapsed}
                new_dict.update(parsed)
                return _ok(new_dict)
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



