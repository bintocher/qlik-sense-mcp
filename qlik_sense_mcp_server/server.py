"""Main MCP Server for Qlik Sense APIs.

The tools themselves live in the `tools` package, grouped by the API they
speak to; importing them here is what registers them with the MCP host.
This module keeps only the process entry points and re-exports the names
that tools, tests and existing integrations expect to find on
`qlik_sense_mcp_server.server`.

Runs on either MCP SDK line: 1.x (`FastMCP`) or 2.x (`MCPServer`) — the
host object is selected in `tools.context`.
"""

import asyncio
import sys

from . import __version__
from .tools import context
from .tools.context import (  # noqa: F401  (public surface of this module)
    MCP_SDK_MAJOR,
    _cert_only_tool,
    _CERT_ONLY_TOOLS_ENABLED,
    _init_clients,
    _mcp_host,
    _mcp_port,
    logger,
    mcp,
)
from .tools.helpers import (  # noqa: F401  (public surface of this module)
    _check,
    _describe_call,
    _engine_serialised,
    _err,
    _ok,
    _timed,
    _to_bool,
    _to_tribool,
    _wildcard_to_regex,
)

# Importing the tool modules is what registers every @mcp.tool().
# They are also re-exported below so `srv.get_apps(...)` keeps working.
from .tools.repository import get_about, get_apps, get_app_details  # noqa: F401
from .tools.engine import (  # noqa: F401
    engine_create_hypercube,
    engine_get_field_range,
    engine_query,
    get_app_field,
    get_app_field_statistics,
    get_app_object,
    get_app_script,
    get_app_sheet_objects,
    get_app_sheets,
    get_app_variables,
    search_app,
)
from .tools.tasks import (  # noqa: F401
    create_task,
    create_task_schedule,
    delete_task,
    delete_task_schedule,
    get_failed_tasks_with_logs,
    get_task_details,
    get_task_dependencies,
    get_task_executions,
    get_task_schedule,
    get_task_script_log,
    get_tasks,
    start_task,
    update_task,
    update_task_schedule,
)


def __getattr__(name):
    """Expose the live clients as attributes of this module.

    `config`, `repo_api`, `engine_api` and `jwt_session` are rebuilt by
    `_init_clients()`, so binding them once at import time would hand out
    stale objects. Reading them from the context on each access keeps
    every consumer — including a test that re-imports this module —
    looking at what the server is actually using.
    """
    if name in ("config", "repo_api", "engine_api", "jwt_session"):
        return getattr(context, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ═══════════════════════════════════════════════════════════════════════════
# Entry points
# ═══════════════════════════════════════════════════════════════════════════


async def async_main():
    """Async main entry point — runs streamable HTTP transport."""
    logger.info(
        "Starting qlik-sense-mcp-server v%s (streamable-http on :%d/mcp, mcp SDK %d.x)",
        __version__, _mcp_port, MCP_SDK_MAJOR,
    )
    if MCP_SDK_MAJOR >= 2:
        await mcp.run_streamable_http_async(host=_mcp_host, port=_mcp_port)
    else:
        await mcp.run_streamable_http_async()


async def _run_stdio():
    """Run in stdio mode for backward-compat."""
    logger.info("Starting qlik-sense-mcp-server v%s (stdio)", __version__)
    await mcp.run_stdio_async()


def main():
    """CLI entry point."""
    if len(sys.argv) > 1:
        if sys.argv[1] in ("--help", "-h"):
            _print_help()
            return
        if sys.argv[1] in ("--version", "-v"):
            sys.stderr.write(f"qlik-sense-mcp-server {__version__}\n")
            return
        if sys.argv[1] == "--stdio":
            asyncio.run(_run_stdio())
            return
    asyncio.run(async_main())


def _print_help():
    sys.stderr.write(f"""
Qlik Sense MCP Server v{__version__} — Model Context Protocol server for Qlik Sense Enterprise APIs

USAGE:
    qlik-sense-mcp-server              Start with HTTP streaming on :8000/mcp
    qlik-sense-mcp-server --stdio      Start with stdio transport (legacy)
    qlik-sense-mcp-server --help       Show this help
    qlik-sense-mcp-server --version    Show version

TOOLS ({len(mcp._tool_manager._tools)} registered in the current auth mode):
    Repository: get_about, get_apps, get_app_details
    Engine:     get_app_script, get_app_field_statistics, engine_get_field_range,
                engine_create_hypercube, get_app_field, get_app_variables,
                get_app_sheets, get_app_sheet_objects, get_app_object,
                search_app
    Tasks:      get_tasks, get_task_details, get_task_dependencies, start_task,
                create_task, update_task, delete_task, get_task_schedule,
                create_task_schedule, update_task_schedule, delete_task_schedule,
                get_task_executions, get_task_script_log, get_failed_tasks_with_logs
                (certificate mode only — QRS task administration is not
                 available to a JWT analyst identity)

GitHub: https://github.com/bintocher/qlik-sense-mcp
""")


if __name__ == "__main__":
    main()
