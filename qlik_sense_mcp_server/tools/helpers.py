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
from ..engine.canonical import explain_engine_error

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
    "error_category", "error", "unknown_fields", "invalid_expressions",
    "tool", "request",
)

# What a refusal does not carry: advice on what to do next, and lists the
# caller can ask for itself. Deciding the next step is the caller's work -
# the server states what is wrong and stops there. A suggested spelling is
# worse than none: a name offered by the server is taken on trust and
# rarely checked.
_ADVICE_KEYS = ("hint", "next_actions", "did_you_mean", "allowed_values",
                "available_values", "available_columns", "accepted_forms")


def _err(msg: str, **extra: Any) -> str:
    """An error a caller can act on without a second guess.

    Ordered deliberately: the category first, what is wrong second, then
    the echo of what was sent. No advice on what to do about it: choosing
    the next step is the caller's work, and a list the caller might need
    it can ask for with a call of its own.
    """
    payload = {"error": explain_engine_error(msg)}
    payload.update(extra)
    # Qlik's refusal over the per-user session limit reads like any other
    # connection failure; its category says it is neither the query nor
    # the network.
    if "OnMaxParallelSessionsExceeded" in str(msg):
        payload["error_category"] = "session_limit"
    for key in _ADVICE_KEYS:
        payload.pop(key, None)

    ordered = {key: payload[key] for key in _ERROR_KEY_ORDER if key in payload}
    for key, value in payload.items():
        if key not in ordered:
            ordered[key] = value
    # The echo of the request may carry the models the schema declared;
    # they are not JSON on their own.
    return json.dumps(ordered, indent=2, ensure_ascii=False, default=_plain)



def _plain(value: Any) -> Any:
    """Whatever it is, as JSON sees it."""
    dumped = getattr(value, "model_dump", None)
    if dumped is not None:
        return dumped(exclude_none=True, by_alias=True)
    return str(value)


# Keys that hold bulk data. Indenting these costs more than the whole rest
# of the reply: a 500x6 hypercube is 48620 characters at indent=2 against
# 22575 compact, and none of those newlines carry meaning to a reader that
# is a language model.
_BULK_KEYS = ("rows", "field_values", "values", "executions", "apps",
              "tasks", "objects", "sheets", "fields", "tables", "call_list")


def _without_advice(value: Any) -> Any:
    """The same reply, with the advice taken out of every refusal in it.

    A batch carries its refusals inside `results`, so they never pass
    through `_err`. The rule is the same wherever a refusal appears: state
    what is wrong, and leave the next step to the caller.
    """
    if isinstance(value, list):
        return [_without_advice(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned = {key: _without_advice(item) for key, item in value.items()}
    if "error" in cleaned:
        for key in _ADVICE_KEYS:
            cleaned.pop(key, None)
        # A bare Engine code says nothing to a reader; its meaning is part
        # of the error, not advice.
        if isinstance(cleaned["error"], str):
            cleaned["error"] = explain_engine_error(cleaned["error"])
    return cleaned


def _ok(obj: Any) -> str:
    """Serialise a successful reply: readable envelope, compact bulk.

    The envelope stays indented because a model reads the first keys of a
    reply most carefully, and the structure helps. The bulk arrays go on
    one line each — every row of a hypercube used to be spread over one
    line per cell.
    """
    if not isinstance(obj, dict):
        return json.dumps(obj, indent=2, ensure_ascii=False, default=_plain)

    obj = _without_advice(obj)
    compact = {}
    for key, value in obj.items():
        if key in _LINE_KEYS and isinstance(value, list) and len(value) > 1:
            compact[key] = _LinesList(value)
        elif key in _LINE_KEYS and isinstance(value, dict):
            # A section of lists, such as `usage`: each list a line an item.
            compact[key] = {k: _LinesList(v) if isinstance(v, list) and len(v) > 1 else v
                            for k, v in value.items()}
        elif key in _BULK_KEYS and isinstance(value, list) and len(value) > 3:
            compact[key] = _CompactList(value)
        elif key in _BULK_KEYS and isinstance(value, dict) and "rows" in value:
            # A bulk key answered as {columns, rows} — the wide-model form
            # of `fields`. Without this its rows keep the indentation and
            # the table ends up longer than the objects it replaced:
            # measured, 61 fields cost 9.8k indented against 6.3k as
            # objects, and 4.4k once the rows go on one line each.
            inner = dict(value)
            if isinstance(inner["rows"], list):
                inner["rows"] = _CompactList(inner["rows"])
            compact[key] = inner
        else:
            compact[key] = value
    return json.dumps(compact, indent=2, ensure_ascii=False, cls=_CompactEncoder,
                      default=_plain)


class _CompactList(list):
    """Marker: serialise this list without the indentation."""


# Lists read an item at a time: one table with its fields, one measure,
# one link per line. Indented, a measure spreads over eight lines of mostly
# spaces; all on one line, a hundred of them are a wall.
_LINE_KEYS = ("tables", "measures", "dimensions", "links", "islands", "bookmarks",
              "usage", "report_filters")


class _LinesList(list):
    """Marker: one compact item per line."""


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
            if isinstance(value, _LinesList):
                key = f"__compact_{len(placeholders)}__"
                placeholders[key] = "[\n    " + ",\n    ".join(
                    json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
                    for item in value) + "\n  ]"
                return key
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
            try:
                result = func(*args, **kwargs)
            except ConnectionError as exc:
                result = exc
            if not _connection_lost(result):
                if isinstance(result, Exception):
                    raise result
                return result
            # The socket was gone when the call reached for it: the proxy
            # closes an idle or expired session without a word, the socket
            # still reads as open, and the first request of the call fails.
            # Measured: a call right after the JWT session renewed failed
            # with "Not connected to Engine API", the next with OpenDoc
            # 1003, the third worked. The call is made once more on a new
            # connection, and only when the time limit leaves room.
            from .. import deadline
            left = deadline.remaining()
            if left is not None and left <= 1:
                if isinstance(result, Exception):
                    raise result
                return result
            logger.warning("%s lost the Engine connection (%s); calling it "
                           "once more on a new one", func.__name__,
                           _lost_reason(result))
            context.engine_api.disconnect()
            return func(*args, **kwargs)

    # functools.wraps copies __qualname__, so the wrapper is otherwise
    # indistinguishable from the tool it guards. Tests assert on this flag
    # to catch a new Engine tool that forgets the decorator.
    wrapper.__engine_serialised__ = True
    return wrapper


# What a call says when the socket under it was gone, as opposed to what a
# slow or wrong query says. A timeout is not among them: repeating a heavy
# query repeats the wait.
_LOST_CONNECTION = ("Not connected to Engine API", "socket is already closed",
                    "Connection to remote host was lost",
                    "Connection is already closed", "'code': 1003")


def _lost_reason(result: Any) -> str:
    if isinstance(result, Exception):
        return str(result)
    try:
        parsed = json.loads(result) if isinstance(result, str) else {}
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("error") or "")


def _connection_lost(result: Any) -> bool:
    """Whether a call failed because the Engine socket was gone."""
    reason = _lost_reason(result)
    if not reason or "timed out" in reason:
        return False
    if "'code': 1003" in reason and "OpenDoc" not in reason:
        return False
    return any(sign in reason for sign in _LOST_CONNECTION)



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



# How often a long call tells the client it is still working. Well inside
# the silence a client tolerates, and rare enough not to be noise.
PROGRESS_INTERVAL_SECONDS = 10.0

# The Engine requests of a call stop themselves at the limit; this margin
# is for whatever cannot be stopped from inside - a connect, a QRS call -
# after which the call is abandoned from outside.
_BACKSTOP_GRACE_SECONDS = 5.0


def _tool():
    """Register a tool with the MCP host, bounded in time.

    Returns the function unchanged, so Python callers (the tests, anything
    importing `server`) keep calling it synchronously. What the host gets
    is `_bounded(func)`.
    """
    def decorator(func):
        context.mcp.tool(**_unstructured())(_bounded(func))
        return func
    return decorator


def _unstructured() -> Dict[str, Any]:
    """Registration options that keep a reply to one layer of JSON.

    Every tool returns a JSON document as text. An SDK that infers
    structured output from the `-> str` annotation also sends that text as
    `structuredContent: {"result": "<the same JSON as a string>"}`, and a
    client that prefers the structured copy shows the model JSON escaped
    inside a string - every quote doubled, the reply a third longer, and
    nothing it can index. Declining structured output leaves the text
    content, which is the JSON itself. SDKs older than the option never
    added the copy, so they need nothing.
    """
    try:
        accepted = inspect.signature(context.mcp.tool).parameters
    except (TypeError, ValueError):  # pragma: no cover - a host without a signature
        return {}
    return {"structured_output": False} if "structured_output" in accepted else {}


def _bounded(func):
    """The async form of a tool that the MCP host runs.

    A plain function registered with the host runs on its event loop, so a
    call waiting on a slow Engine froze the server for every client, sent
    nothing, and after 300 silent seconds the client dropped the session.
    Here the tool body runs in a worker thread under the QLIK_TOOL_TIMEOUT
    limit (`deadline`), the loop stays free, and every
    PROGRESS_INTERVAL_SECONDS the client is sent a progress notification -
    when it asked for them with a progress token; otherwise there is
    nothing to send. At the limit the reply is an error that says so, and
    the next call starts on a clean connection.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        sig = None
    uses_engine = getattr(func, "__engine_serialised__", False)

    @functools.wraps(func)
    async def runner(*args, **kwargs):
        import anyio

        from .. import deadline

        limit = deadline.tool_timeout_seconds()
        state = {}

        def work():
            with deadline.call_deadline(limit) as bound:
                state["bound"] = bound
                return func(*args, **kwargs)

        if limit <= 0:
            return await anyio.to_thread.run_sync(work)

        ctx = _request_context()
        started = time.monotonic()
        result = None

        async def heartbeat():
            while True:
                await anyio.sleep(PROGRESS_INTERVAL_SECONDS)
                await _report_progress(ctx, time.monotonic() - started, limit)

        with anyio.move_on_after(limit + _BACKSTOP_GRACE_SECONDS):
            async with anyio.create_task_group() as group:
                if ctx is not None:
                    group.start_soon(heartbeat)
                result = await anyio.to_thread.run_sync(work, abandon_on_cancel=True)
                group.cancel_scope.cancel()

        bound = state.get("bound")
        if result is None:
            # Abandoned from outside. The thread may still hold the Engine
            # lock in a blocked recv(); breaking the socket lets it finish.
            if uses_engine and context.engine_api is not None:
                context.engine_api.abort_socket()
            return _time_limit_reply(func.__name__, limit, sig, args, kwargs,
                                     time.monotonic() - started)
        if bound is not None and bound.expired:
            try:
                parsed = json.loads(result)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and "error" in parsed:
                return _time_limit_reply(func.__name__, limit, sig, args, kwargs,
                                         parsed.get("tool_call_seconds"))
        return result

    return runner


def _time_limit_reply(name, limit, sig, args, kwargs, elapsed) -> str:
    """The reply for a call the time limit stopped.

    Replaces whatever the tool made of it: a step that swallowed the
    timeout leaves the next one to fail with "not connected", which would
    send the caller looking for a network fault.
    """
    return _err(
        f"The call did not finish within its {limit:.0f}s time limit "
        f"(QLIK_TOOL_TIMEOUT) and was stopped; the connection is fine. Ask "
        f"for less in one call: fewer objects, fields or dimensions, a "
        f"filter on the large table, or explicit field names.",
        error_category="time_limit",
        tool=name,
        request=_describe_call(sig, args, kwargs),
        tool_call_seconds=round(elapsed, 3) if isinstance(elapsed, (int, float)) else elapsed,
    )


def _request_context():
    """The MCP request context of the running call, or None outside one."""
    get_context = getattr(context.mcp, "get_context", None)
    if get_context is None:
        return None
    try:
        ctx = get_context()
        return ctx if getattr(ctx, "request_context", None) is not None else None
    except Exception:
        return None


async def _report_progress(ctx, elapsed: float, limit: float) -> None:
    try:
        await ctx.report_progress(
            round(elapsed), limit,
            f"still working, {elapsed:.0f}s of at most {limit:.0f}s")
    except Exception as exc:  # a lost notification must not fail the call
        logger.debug("Progress notification failed: %s", exc)


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



