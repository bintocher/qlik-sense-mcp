"""MCP SDK compatibility (since v1.7.0).

MCP SDK 2.0 removed `mcp.server.fastmcp` and replaced the FastMCP host
with `MCPServer`. Because the dependency was declared as `mcp>=1.1.0`,
every fresh install silently resolved to the new SDK and died at import.
These tests pin down the contract the server relies on, so a future SDK
change fails here rather than in a user's terminal.
"""

import asyncio
import importlib.metadata

import pytest

from qlik_sense_mcp_server import server as srv


def test_sdk_major_matches_the_installed_package():
    installed = importlib.metadata.version("mcp")
    major = int(installed.split(".")[0])
    assert srv.MCP_SDK_MAJOR == major, (
        f"server.py selected the {srv.MCP_SDK_MAJOR}.x host but mcp {installed} "
        f"is installed"
    )


def test_host_object_exposes_everything_the_server_uses():
    """Both SDK lines must provide these — main() and _print_help() call them."""
    for attribute in ("tool", "run_stdio_async", "run_streamable_http_async"):
        assert hasattr(srv.mcp, attribute), f"MCP host is missing {attribute}()"
    # _print_help() counts tools through this private registry; it exists on
    # FastMCP (1.x) and on MCPServer (2.x) alike.
    assert isinstance(srv.mcp._tool_manager._tools, dict)


def test_streamable_http_accepts_the_bind_address_for_this_sdk():
    """2.x moved host/port from the constructor into the run call."""
    import inspect

    params = inspect.signature(srv.mcp.run_streamable_http_async).parameters
    if srv.MCP_SDK_MAJOR >= 2:
        assert "host" in params and "port" in params
    else:
        assert srv.mcp.settings.port == srv._mcp_port


def test_tools_are_registered_and_callable():
    tools = asyncio.run(srv.mcp.list_tools())
    names = {t.name for t in tools}
    assert "engine_create_hypercube" in names
    assert "get_about" in names


def test_hypercube_schema_exposes_the_ranking_parameters():
    """The whole point of the tool — these must survive an SDK swap."""
    tool = next(t for t in asyncio.run(srv.mcp.list_tools())
                if t.name == "engine_create_hypercube")
    # 1.x calls it inputSchema, 2.x input_schema.
    schema = getattr(tool, "input_schema", None) or tool.inputSchema
    properties = schema["properties"]
    for name in ("app_id", "dimensions", "measures", "limit", "sort_by",
                 "sort_order", "exclude_null_dimensions"):
        assert name in properties, f"{name} missing from the published schema"
    assert schema.get("required") == ["app_id"]


def test_docstring_reaches_the_published_description():
    """LLMs only ever see the description — an empty one breaks every tool."""
    tool = next(t for t in asyncio.run(srv.mcp.list_tools())
                if t.name == "engine_create_hypercube")
    assert tool.description and "sort_by" in tool.description
