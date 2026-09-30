"""A tool call ends within QLIK_TOOL_TIMEOUT, however slow Engine is.

An MCP client drops the whole session after a few minutes of silence, not
just the call, and the model is left without tools. A call the server
stops itself costs one retry instead: the reply says the limit was hit,
Engine is asked to cancel, and the next call finds a working connection.
"""

import json
import threading
import time

import anyio
import pytest
import websocket

from qlik_sense_mcp_server import deadline
from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.exceptions import ToolTimeLimitError
from qlik_sense_mcp_server.tools import context, helpers


class _SlowSocket:
    """Answers every request after `delay` seconds, honouring the socket timeout."""

    def __init__(self, delay):
        self.delay = delay
        self.timeout = None
        self.sent = []
        self.connected = True
        self._pending = []
        self._shut = threading.Event()
        outer = self

        class _Sock:
            def settimeout(self, value):
                outer.timeout = value

        self.sock = _Sock()

    def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        if frame["method"] != "CancelRequest":
            self._pending.append(frame["id"])

    def recv(self):
        wait = self.delay if self.timeout is None else min(self.delay, self.timeout)
        if self._shut.wait(wait):
            raise ConnectionResetError("socket shut down")
        if self.timeout is not None and self.delay > self.timeout:
            # What websocket-client raises for a socket timeout: not a
            # TimeoutError, which is what the transport used to catch.
            raise websocket.WebSocketTimeoutException("Connection timed out")
        return json.dumps({"jsonrpc": "2.0", "id": self._pending.pop(0), "result": {}})

    def shutdown(self):
        self._shut.set()

    def close(self):
        self.connected = False


def _engine(delay):
    engine = QlikEngineAPI.__new__(QlikEngineAPI)
    engine.ws = _SlowSocket(delay)
    engine.request_id = 0
    engine.ws_timeout_seconds = 180.0
    return engine


class TestSendRequestUnderALimit:
    def test_a_slow_request_stops_at_the_limit_and_is_cancelled(self):
        engine = _engine(delay=200)
        ws = engine.ws
        started = time.monotonic()
        with deadline.call_deadline(0.3) as bound:
            with pytest.raises(ToolTimeLimitError, match="QLIK_TOOL_TIMEOUT"):
                engine.send_request("GetLayout", [], handle=5)
        assert time.monotonic() - started < 2
        assert bound.expired
        cancels = [f for f in ws.sent if f["method"] == "CancelRequest"]
        assert cancels and cancels[0]["params"] == {"qRequestId": ws.sent[0]["id"]}
        assert engine.ws is None  # dropped, so the next call reconnects clean

    def test_a_pipelined_batch_stops_at_the_limit(self):
        engine = _engine(delay=200)
        with deadline.call_deadline(0.3):
            with pytest.raises(ToolTimeLimitError):
                engine.send_requests_pipelined(
                    [{"method": "GetProperties", "params": [], "handle": h}
                     for h in (1, 2)])

    def test_no_time_left_fails_before_sending(self):
        engine = _engine(delay=0)
        with deadline.call_deadline(0.01):
            time.sleep(0.05)
            with pytest.raises(ToolTimeLimitError):
                engine.send_request("GetLayout", [], handle=5)
        assert engine.ws.sent == []

    def test_outside_a_tool_call_nothing_changes(self):
        engine = _engine(delay=0)
        engine.send_request("GetLayout", [], handle=5)
        assert engine.ws.timeout is None


@pytest.fixture
def fast_limit(monkeypatch):
    monkeypatch.setenv("QLIK_TOOL_TIMEOUT", "0.3")
    monkeypatch.setattr(helpers, "_BACKSTOP_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(helpers, "_request_context", lambda: None)


@pytest.fixture
def engine_slot(monkeypatch):
    def install(engine):
        monkeypatch.setattr(context, "engine_api", engine)
        return engine
    return install


def _engine_tool():
    @helpers._timed
    @helpers._engine_serialised
    def some_engine_tool(app_id: str) -> str:
        try:
            context.engine_api.send_request("GetLayout", [], handle=1)
            return helpers._ok({"app_id": app_id})
        except Exception as ex:
            return helpers._err(str(ex), error_category="engine_api_error")
    return helpers._bounded(some_engine_tool)


class TestToolReply:
    def test_the_reply_is_a_time_limit_error_and_the_next_call_works(
            self, fast_limit, engine_slot):
        tool = _engine_tool()
        engine_slot(_engine(delay=200))

        started = time.monotonic()
        reply = json.loads(anyio.run(lambda: tool(app_id="app-1")))
        assert time.monotonic() - started < 2
        assert reply["error_category"] == "time_limit"
        assert reply["request"] == {"app_id": "app-1"}

        engine_slot(_engine(delay=0))
        assert "error" not in json.loads(anyio.run(lambda: tool(app_id="app-1")))

    def test_a_swallowed_timeout_is_still_reported_as_one(self, fast_limit, engine_slot):
        engine_slot(_engine(delay=200))

        @helpers._timed
        def swallowing_tool() -> str:
            try:
                context.engine_api.send_request("GetLayout", [], handle=1)
            except Exception:
                pass  # a step that tolerates failure, as sheet reading does
            return helpers._err("Not connected to Engine API")

        reply = json.loads(anyio.run(helpers._bounded(swallowing_tool)))
        assert reply["error_category"] == "time_limit"

    def test_work_that_cannot_be_stopped_is_abandoned(self, fast_limit, engine_slot):
        engine = engine_slot(_engine(delay=0))

        @helpers._timed
        @helpers._engine_serialised
        def stuck_tool() -> str:
            engine.ws._shut.wait(5)  # stands for a blocked connect
            return helpers._ok({})

        started = time.monotonic()
        reply = json.loads(anyio.run(helpers._bounded(stuck_tool)))
        assert time.monotonic() - started < 2
        assert reply["error_category"] == "time_limit"
        assert engine.ws._shut.is_set()  # the socket was broken to free the thread

    def test_a_limit_of_zero_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("QLIK_TOOL_TIMEOUT", "0")
        reply = anyio.run(helpers._bounded(lambda: helpers._ok({"done": True})))
        assert json.loads(reply) == {"done": True}

    def test_the_default_leaves_room_for_a_large_app(self, monkeypatch):
        """Six calls on a 1.16-billion-row app hit 120 s; the default is
        240 s, still under the 300 s of silence a client drops a session
        after."""
        from qlik_sense_mcp_server import deadline
        monkeypatch.delenv("QLIK_TOOL_TIMEOUT", raising=False)
        assert deadline.tool_timeout_seconds() == 240.0 < 300


class TestProgress:
    def test_a_long_call_reports_progress(self, monkeypatch):
        sent = []

        class _Ctx:
            async def report_progress(self, progress, total=None, message=None):
                sent.append((progress, total, message))

        monkeypatch.setenv("QLIK_TOOL_TIMEOUT", "5")
        monkeypatch.setattr(helpers, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(helpers, "_request_context", lambda: _Ctx())

        def slow() -> str:
            time.sleep(0.3)
            return helpers._ok({})

        anyio.run(helpers._bounded(slow))
        assert len(sent) >= 2
        assert all(total == 5.0 for _progress, total, _message in sent)


class TestRegistration:
    def test_the_host_runs_the_async_form_and_python_callers_the_plain_one(self):
        from qlik_sense_mcp_server import server as srv
        registered = srv.mcp._tool_manager._tools["get_app_sheet_objects"]
        assert registered.is_async
        assert not callable(getattr(srv.get_app_sheet_objects, "__await__", None))
        assert list(registered.parameters["properties"]) == ["app_id", "sheet_id"]
