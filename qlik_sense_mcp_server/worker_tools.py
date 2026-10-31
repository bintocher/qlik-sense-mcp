"""Gate tool execution behind one worker session without changing tool schemas."""

import functools


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
        # The legacy outer backstop aborts context.engine_api after a timeout.
        # In a coordinated worker that global may already be a new generation.
        # Keep the deadline and timeout reply, but not this global socket abort;
        # the executing thread holds the session guard until it actually exits.
        execute.__engine_serialised__ = False
        return original(execute)

    helpers._bounded = guarded_bounded
    return original
