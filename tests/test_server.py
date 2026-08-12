"""Tests for server module (FastMCP-based since v1.4.0)."""

import json
import re
from pathlib import Path

from qlik_sense_mcp_server import __version__
from qlik_sense_mcp_server import server as srv


class TestErrorEnvelope:
    def test_err_basic(self):
        result = srv._err("something went wrong")
        parsed = json.loads(result)
        assert parsed == {"error": "something went wrong"}

    def test_err_with_extras(self):
        result = srv._err("failed", app_id="abc-123", details="more info")
        parsed = json.loads(result)
        assert parsed["error"] == "failed"
        assert parsed["app_id"] == "abc-123"
        assert parsed["details"] == "more info"

    def test_err_always_has_error_key(self):
        parsed = json.loads(srv._err("test"))
        assert "error" in parsed

    def test_ok_wraps_dict(self):
        parsed = json.loads(srv._ok({"foo": "bar"}))
        assert parsed == {"foo": "bar"}


class TestVersion:
    def test_version_format(self):
        parts = __version__.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit()

    def test_version_matches_pyproject(self):
        # Single source of truth: __version__ must equal the version in
        # pyproject.toml. Both are bumped together by bump2version on release
        # (see .bumpversion.cfg). Avoids a hardcoded literal that silently
        # breaks CI on every version bump.
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
        assert match is not None, "version not found in pyproject.toml"
        assert __version__ == match.group(1)


class TestFastMCPRegistration:
    def test_mcp_instance_exists(self):
        assert srv.mcp is not None
        # FastMCP instance should expose a _tool_manager with registered tools.
        assert hasattr(srv.mcp, "_tool_manager")

    def test_tools_count(self):
        # 14 analysis tools plus 14 for task administration, which follow
        # certificate mode. Per-mode visibility is covered in
        # tests/test_tool_registration.py.
        assert len(srv.mcp._tool_manager._tools) == 28

    def test_core_tools_registered(self):
        tool_names = set(srv.mcp._tool_manager._tools.keys())
        expected = {
            # Repository
            "get_about",
            "get_apps",
            "get_app_details",
            # Engine
            "get_app_script",
            "get_app_field_statistics",
            "engine_query",
            "engine_create_hypercube",
            "engine_get_field_range",
            "get_app_field",
            "get_app_variables",
            "get_app_sheets",
            "get_app_sheet_objects",
            "get_app_object",
            "search_app",
        }
        missing = expected - tool_names
        assert not missing, f"missing tools: {missing}"


class TestTimedDecorator:
    def test_timed_injects_seconds_key(self):
        @srv._timed
        def fake_tool():
            return srv._ok({"foo": "bar"})

        result = fake_tool()
        parsed = json.loads(result)
        # tool_call_seconds must be the first key of the response
        assert next(iter(parsed.keys())) == "tool_call_seconds"
        assert isinstance(parsed["tool_call_seconds"], float)
        assert parsed["foo"] == "bar"

    def test_timed_handles_exceptions(self):
        @srv._timed
        def broken_tool():
            raise ValueError("boom")

        result = broken_tool()
        parsed = json.loads(result)
        assert parsed["error"] == "boom"
        assert parsed["error_type"] == "ValueError"
        assert parsed["tool"] == "broken_tool"
        assert "tool_call_seconds" in parsed

    def test_timed_preserves_non_dict_strings(self):
        @srv._timed
        def scalar_tool():
            return "plain text"

        parsed = json.loads(scalar_tool())
        # Non-JSON / non-dict payloads get wrapped under `result`
        assert parsed["result"] == "plain text"
        assert "tool_call_seconds" in parsed
