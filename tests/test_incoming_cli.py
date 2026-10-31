"""Unified startup selection tests that do not import MCP or Qlik clients."""

import os
import sys
import unittest
from types import ModuleType
from unittest.mock import patch

from qlik_sense_mcp_server import cli


class CliTests(unittest.TestCase):
    def route(self, mode, module_name):
        calls = []
        module = ModuleType(module_name)
        module.main = lambda: calls.append(mode)
        with patch.dict(os.environ, {"QLIK_MCP_MODE": mode}), patch.object(sys, "argv", ["qlik-sense-mcp-server"]), patch.dict(sys.modules, {module_name: module}):
            cli.main()
        self.assertEqual(calls, [mode])

    def test_legacy(self):
        self.route("legacy", "qlik_sense_mcp_server.server")

    def test_incoming(self):
        self.route("incoming-jwt", "qlik_sense_mcp_server.incoming_gateway")

    def test_invalid_mode_and_stdio_are_rejected(self):
        for mode, argv in (("invalid", ["server"]), ("incoming-jwt", ["server", "--stdio"])):
            with patch.dict(os.environ, {"QLIK_MCP_MODE": mode}), patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit): cli.main()


if __name__ == "__main__": unittest.main()
