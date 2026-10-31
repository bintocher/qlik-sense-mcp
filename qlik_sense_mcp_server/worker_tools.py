"""Gate tool execution behind one worker session without changing tool schemas."""

import functools
import inspect


def install_tool_guard(helpers, guard):
    """Wrap the sync body before MCP registration; never replay tool calls."""
    original = helpers._bounded

    def guarded_bounded(function):
        @functools.wraps(function)
        def execute(*args, **kwargs):
            try:
                return guard.execute(function, *args, **kwargs)
            except Exception:
                return helpers._err("Qlik session unavailable; retry the tool call",
                                    error_category="authentication_or_session")
        # The outer legacy backstop otherwise aborts context.engine_api after
        # an abandoned thread finishes, which can race a replacement client.
        # GuardSession owns the per-call cleanup and lock; disabling that
        # global backstop avoids aborting another generation's Engine socket.
        execute.__engine_serialised__ = False
        return original(execute)

    helpers._bounded = guarded_bounded
    return original
