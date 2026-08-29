"""One Engine socket, several MCP clients.

The Streamable HTTP transport serves multiple clients from one process,
while the client keeps a single WebSocket and a single open document. If
two tool calls overlap on that socket, strict id-matching makes each throw
away the other's reply, and `ensure_app` can switch documents between one
call's CreateSessionObject and its GetLayout — the second call then reads
the first app's data believing it is its own. Nothing in the response says
so.
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _SharedSocketEngine(QlikEngineAPI):
    """Fails loudly if two threads are inside the session at once."""

    def __init__(self):
        import threading as _t
        self._lock = _t.RLock()
        self.ws = object()
        self.ws_timeout_seconds = 30.0
        self._cached_app_id = None
        self.overlaps = []
        self.sequence = []
        self._inside = 0
        self._guard = _t.Lock()

    def _enter(self, who):
        with self._guard:
            self._inside += 1
            if self._inside > 1:
                self.overlaps.append(who)

    def _leave(self):
        with self._guard:
            self._inside -= 1

    def ensure_app(self, app_id, no_data=False):
        self._enter(f"ensure_app:{app_id}")
        self.sequence.append(("open", app_id))
        time.sleep(0.01)  # widen the window a real Engine round-trip has
        self._cached_app_id = app_id
        self._leave()
        return 1

    def read(self, app_id):
        """Stands in for the request chain a tool runs after ensure_app."""
        self._enter(f"read:{app_id}")
        time.sleep(0.01)
        observed = self._cached_app_id
        self.sequence.append(("read", observed))
        self._leave()
        return observed


def _hammer(target, count=8):
    threads, results = [], [None] * count
    barrier = threading.Barrier(count)

    def run(i):
        barrier.wait()
        results[i] = target(i)

    for i in range(count):
        t = threading.Thread(target=run, args=(i,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


class TestTransaction:
    def test_calls_do_not_overlap(self):
        engine = _SharedSocketEngine()

        def call(i):
            with engine.transaction():
                engine.ensure_app(f"app-{i % 2}")
                return engine.read(f"app-{i % 2}")

        _hammer(call)
        assert engine.overlaps == [], f"overlapping access: {engine.overlaps}"






class TestToolsAreSerialised:
    @pytest.fixture
    def engine(self, monkeypatch):
        engine = _SharedSocketEngine()
        state = {"calls": 0}

        def get_fields(handle):
            engine._enter("get_fields")
            time.sleep(0.01)
            state["calls"] += 1
            engine._leave()
            return {"fields": []}

        stub = SimpleNamespace(
            transaction=engine.transaction,
            ensure_app=engine.ensure_app,
            get_fields=get_fields,
        )
        monkeypatch.setattr(context, "engine_api", stub)
        monkeypatch.setattr(context, "repo_api", SimpleNamespace(
            get_app_by_id=lambda app_id: {"id": app_id, "name": "App", "published": False}))
        return engine

    def test_concurrent_tool_calls_are_serialised(self, engine):
        fn = getattr(srv.get_app_details, "fn", srv.get_app_details)

        def call(i):
            return json.loads(fn(app_id=f"app-{i % 2}"))

        results = _hammer(call, count=6)
        assert engine.overlaps == [], f"tool bodies overlapped: {engine.overlaps}"
        assert all("error" not in r for r in results)


