"""Validate per-token worker setup and safe startup diagnostics without Qlik."""

import base64
import io
import json
import os
import sys
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from qlik_sense_mcp_server import incoming_worker


class WorkerSetupTests(unittest.TestCase):
    def test_tls_is_preserved_and_guard_installed_before_tool_import(self):
        payload = base64.urlsafe_b64encode(json.dumps({"exp":time.time()+60}).encode()).rstrip(b"=").decode()
        token = f"header.{payload}.signature"
        events = []
        config = ModuleType("qlik_sense_mcp_server.config")
        config.QlikSenseConfig = SimpleNamespace(from_env=lambda: SimpleNamespace(validate_runtime=lambda: events.append("config")))
        tools = ModuleType("qlik_sense_mcp_server.tools")
        tools.context = object()
        tools.helpers = object()
        session_module = ModuleType("qlik_sense_mcp_server.worker_session")
        class Guard:
            def __init__(self, *args): pass
            def initialize(self): events.append("auth")
            def close(self): events.append("close")
        session_module.WorkerSessionGuard = Guard
        wrappers = ModuleType("qlik_sense_mcp_server.worker_tools")
        wrappers.install_tool_guard = lambda *args: events.append("guard")
        server = ModuleType("qlik_sense_mcp_server.server")
        def app():
            self.assertEqual(events, ["config","auth","guard"])
            return object()
        server.mcp = SimpleNamespace(streamable_http_app=app)
        uvicorn = ModuleType("uvicorn")
        uvicorn.Config = lambda *args, **kwargs: object()
        class Host:
            def __init__(self, config): self.started=False
            async def serve(self): self.started=True
        uvicorn.Server=Host
        class Thread:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
        import qlik_sense_mcp_server as package
        with patch.dict(sys.modules, {
            config.__name__: config, tools.__name__: tools,
            session_module.__name__: session_module, wrappers.__name__: wrappers,
            server.__name__: server, "uvicorn": uvicorn,
        }), patch.object(package, "server", server, create=True), patch.object(sys, "argv", ["worker","/tmp/mock.sock"]), patch.object(sys, "stdin", io.StringIO(token+"\n")), patch.object(sys, "stdout", io.StringIO()) as output, patch.object(incoming_worker.threading, "Thread", Thread), patch.dict(os.environ, {"QLIK_VERIFY_SSL":"true"}):
            incoming_worker.main()
            self.assertEqual(os.environ["QLIK_VERIFY_SSL"], "true")
            self.assertEqual(os.environ["QLIK_SESSION_CACHE"], "false")
            self.assertEqual(os.environ["QLIK_PROXY_LOGOUT"], "false")
            self.assertEqual(json.loads(output.getvalue())["status"], "ready")
        self.assertEqual(events[-1], "close")

    def test_malformed_token_reports_authentication_without_importing_mcp(self):
        with patch.object(sys,"argv",["worker","/tmp/mock.sock"]), patch.object(sys,"stdin",io.StringIO("bad.token\n")), patch.object(sys,"stdout",io.StringIO()) as output:
            incoming_worker.main()
        self.assertEqual(json.loads(output.getvalue()), {"status":"authentication"})


if __name__ == "__main__": unittest.main()
