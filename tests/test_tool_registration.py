"""Which tools the server advertises.

The default surface is the analysis tools and nothing else. Reload-task
administration is registered only when asked for by QLIK_TASK_TOOLS, and
only in certificate mode — those endpoints need repository-admin rights,
and a JWT analyst reaching them through the virtual proxy gets 403s.

A tool the caller cannot use is not free: its name and description sit in
the model's context, and a model that reads about task administration
tries it.
"""

import importlib

import pytest

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.tools import engine as engine_tools
from qlik_sense_mcp_server.tools import repository as repository_tools
from qlik_sense_mcp_server.tools import tasks as task_tools


TASK_TOOLS = {
    "get_tasks", "get_task_details", "start_task", "create_task",
    "update_task", "delete_task", "get_task_schedule",
    "create_task_schedule", "get_task_executions", "get_task_script_log",
    "get_failed_tasks_with_logs", "get_task_dependencies",
    "update_task_schedule", "delete_task_schedule",
}

ANALYSIS_TOOLS = {
    "get_about", "get_apps", "get_app_details", "get_app_script",
    "get_app_field_statistics", "engine_get_field_range", "get_app_field",
    "get_app_variables", "get_app_sheets", "get_app_sheet_objects",
    "get_app_object", "engine_query", "engine_create_hypercube",
    "search_app",
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
                    "QLIK_USER_ID", "QLIK_CLIENT_CERT_PATH",
                    "QLIK_CLIENT_KEY_PATH", "QLIK_TASK_TOOLS"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return _reimport()

    yield _reload
    # Leave the modules in their original state for the rest of the suite.
    monkeypatch.undo()
    _reimport()


class TestDefaultSurface:
    def test_jwt_mode_registers_the_analysis_tools_only(self, reload_server):
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com/jwt",
            QLIK_JWT_TOKEN="header.payload.signature",
        )
        assert module.config.auth_mode == "jwt"
        assert set(module.mcp._tool_manager._tools) == ANALYSIS_TOOLS




class TestTaskToolsSwitch:
    def test_jwt_mode_leaves_them_out_even_when_asked_for(self, reload_server):
        """QRS task administration cannot work as a JWT analyst identity."""
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com/jwt",
            QLIK_JWT_TOKEN="header.payload.signature",
            QLIK_TASK_TOOLS="true",
        )
        assert set(module.mcp._tool_manager._tools) == ANALYSIS_TOOLS


