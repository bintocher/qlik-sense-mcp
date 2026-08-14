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





class TestVersion:
    def test_version_format(self):
        parts = __version__.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit()



class TestFastMCPRegistration:
    def test_mcp_instance_exists(self):
        assert srv.mcp is not None
        # FastMCP instance should expose a _tool_manager with registered tools.
        assert hasattr(srv.mcp, "_tool_manager")




