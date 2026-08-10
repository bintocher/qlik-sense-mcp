"""Tool visibility per authentication mode (since v1.6.0).

Reload-task tools hit QRS endpoints that need repository-admin rights, so
they are registered in certificate mode only. Advertising them to a JWT
analyst just invites calls that can only ever return 403.
"""

import importlib

import pytest

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.tools import engine as engine_tools
from qlik_sense_mcp_server.tools import repository as repository_tools
from qlik_sense_mcp_server.tools import tasks as task_tools


CERT_ONLY_TOOLS = {
    "get_tasks", "get_task_details", "start_task", "create_task",
    "update_task", "delete_task", "get_task_schedule",
    "create_task_schedule", "get_task_executions", "get_task_script_log",
    "get_failed_tasks_with_logs", "get_task_dependencies",
    "update_task_schedule", "delete_task_schedule",
}

ALWAYS_AVAILABLE_TOOLS = {
    "get_about", "get_apps", "get_app_details", "get_app_script",
    "get_app_field_statistics", "engine_get_field_range", "get_app_field",
    "get_app_variables", "get_app_sheets", "get_app_sheet_objects",
    "get_app_object", "engine_create_hypercube",
}


def _reimport():
    """Rebuild the whole tool surface from scratch.

    Registration happens at import time and depends on the environment,
    so the reload has to start at `context` — it builds the clients and
    the MCP host — and then re-run every tool module against that fresh
    host. Reloading only `server` would re-export tools registered on the
    previous host and show the previous auth mode.
    """
    module = importlib.reload(context)
    for tool_module in (repository_tools, engine_tools, task_tools):
        importlib.reload(tool_module)
    return importlib.reload(srv)


@pytest.fixture
def reload_server(monkeypatch):
    """Re-import the server under a given environment, then restore it."""
    def _reload(**env):
        for key in ("QLIK_SERVER_URL", "QLIK_JWT_TOKEN", "QLIK_USER_DIRECTORY",
                    "QLIK_USER_ID", "QLIK_CLIENT_CERT_PATH", "QLIK_CLIENT_KEY_PATH"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return _reimport()

    yield _reload
    # Leave the modules in their original state for the rest of the suite.
    monkeypatch.undo()
    _reimport()


def test_jwt_mode_hides_reload_task_tools(reload_server):
    module = reload_server(
        QLIK_SERVER_URL="https://qlik.example.com/jwt",
        QLIK_JWT_TOKEN="header.payload.signature",
    )
    assert module.config.auth_mode == "jwt"
    names = set(module.mcp._tool_manager._tools)
    assert not (names & CERT_ONLY_TOOLS), "task tools must be hidden in JWT mode"
    assert ALWAYS_AVAILABLE_TOOLS <= names
    assert len(names) == len(ALWAYS_AVAILABLE_TOOLS)


def test_certificate_mode_exposes_every_tool(reload_server):
    module = reload_server(
        QLIK_SERVER_URL="https://qlik.example.com",
        QLIK_USER_DIRECTORY="COMPANY",
        QLIK_USER_ID="svc_mcp",
    )
    assert module.config.auth_mode == "certificate"
    names = set(module.mcp._tool_manager._tools)
    assert CERT_ONLY_TOOLS <= names
    assert ALWAYS_AVAILABLE_TOOLS <= names


def test_unconfigured_server_still_exposes_every_tool(reload_server):
    """`--help` and a misconfigured host must not silently lose tools."""
    module = reload_server()
    names = set(module.mcp._tool_manager._tools)
    assert CERT_ONLY_TOOLS <= names
    assert ALWAYS_AVAILABLE_TOOLS <= names
