"""Validate per-session reuse semantics of the JWT worker."""

import os
import runpy
import sys
from io import StringIO
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def test_worker_reuses_qs_session_without_periodic_bootstrap(monkeypatch):
    from qlik_sense_mcp_server import incoming_worker
    events = []
    session = SimpleNamespace(_ttl=1500, ensure_standalone=lambda: events.append("authenticated"))
    context = SimpleNamespace(jwt_session=session, repo_api=object(), engine_api=object(),
                              shutdown=lambda: events.append("shutdown"))
    tools = ModuleType("qlik_sense_mcp_server.tools")
    tools.context = context
    server = ModuleType("qlik_sense_mcp_server.server")
    server.mcp = SimpleNamespace(streamable_http_app=lambda: object())
    uvicorn = ModuleType("uvicorn")
    uvicorn.Config = lambda *args, **kwargs: object()

    class FakeServer:
        def __init__(self, config):
            self.started = False
        async def serve(self):
            self.started = True

    uvicorn.Server = FakeServer
    class NoThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass

    monkeypatch.setattr(sys, "argv", ["worker", "/tmp/fake.sock"])
    monkeypatch.setattr(sys, "stdin", StringIO("example.jwt.token\n"))
    monkeypatch.setattr(incoming_worker.threading, "Thread", NoThread)
    monkeypatch.setitem(sys.modules, "qlik_sense_mcp_server.tools", tools)
    monkeypatch.setitem(sys.modules, "qlik_sense_mcp_server.server", server)
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setenv("QLIK_VERIFY_SSL", "true")
    with patch.dict(os.environ):
        incoming_worker.main()
        assert os.environ["QLIK_VERIFY_SSL"] == "true"
        assert os.environ["QLIK_SESSION_CACHE"] == "false"
        assert os.environ["QLIK_PROXY_LOGOUT"] == "false"
    assert session._ttl == float("inf")
    assert events == ["authenticated", "shutdown"]
