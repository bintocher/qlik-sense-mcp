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

    def test_a_call_always_reads_its_own_app(self):
        """Two apps, many threads: nobody may read the other one's document."""
        engine = _SharedSocketEngine()
        mismatches = []

        def call(i):
            wanted = f"app-{i % 2}"
            with engine.transaction():
                engine.ensure_app(wanted)
                seen = engine.read(wanted)
            if seen != wanted:
                mismatches.append((wanted, seen))
            return seen

        _hammer(call, count=10)
        assert mismatches == [], f"read another app's document: {mismatches}"

    def test_without_the_transaction_the_test_would_catch_it(self):
        """Guards the guard: unsynchronised access must be detectable."""
        engine = _SharedSocketEngine()

        def call(i):
            engine.ensure_app(f"app-{i % 2}")
            return engine.read(f"app-{i % 2}")

        _hammer(call, count=10)
        assert engine.overlaps, (
            "the detector never fired, so the passing tests above prove nothing")

    def test_transaction_is_reentrant(self):
        """Client internals nest transactions; that must not deadlock."""
        engine = _SharedSocketEngine()
        with engine.transaction():
            with engine.transaction():
                assert engine.ensure_app("app-0") == 1

    def test_each_client_locks_only_itself(self):
        """Two clients must not queue behind one another.

        A class-level default lock would make them share one, so a
        partially-constructed client — a test double, or a second client
        in the same process — would serialise against every other.
        """
        first = QlikEngineAPI.__new__(QlikEngineAPI)
        second = QlikEngineAPI.__new__(QlikEngineAPI)
        entered = threading.Event()

        def hold():
            with first.transaction():
                entered.set()
                time.sleep(0.3)

        holder = threading.Thread(target=hold)
        holder.start()
        assert entered.wait(2), "first client never entered its transaction"

        started = time.monotonic()
        with second.transaction():
            waited = time.monotonic() - started
        holder.join()
        assert waited < 0.2, (
            f"second client waited {waited:.2f}s for an unrelated client's lock")


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

    def test_engine_tools_are_wrapped(self):
        """A new Engine tool that forgets the decorator loses the guarantee."""
        engine_tools = [
            "get_app_details", "get_app_script", "get_app_field_statistics",
            "engine_get_field_range", "engine_create_hypercube", "get_app_field",
            "get_app_variables", "get_app_sheets", "get_app_sheet_objects",
            "get_app_object",
        ]
        for name in engine_tools:
            tool = getattr(srv, name)
            fn = getattr(tool, "fn", tool)
            serialised = False
            while fn is not None:
                serialised = serialised or getattr(fn, "__engine_serialised__", False)
                fn = getattr(fn, "__wrapped__", None)
            assert serialised, (
                f"{name} is not serialised against the shared Engine socket")

    def test_qrs_only_tools_are_not_blocked_by_engine_work(self):
        """A slow hypercube must not hold up a Repository call."""
        for name in ("get_about", "get_apps"):
            tool = getattr(srv, name)
            fn = getattr(tool, "fn", tool)
            serialised = False
            while fn is not None:
                serialised = serialised or getattr(fn, "__engine_serialised__", False)
                fn = getattr(fn, "__wrapped__", None)
            assert not serialised, f"{name} does not touch Engine and must not wait for it"
