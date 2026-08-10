"""Liveness of the cached Engine WebSocket.

The client keeps one long-lived socket and reuses it across tool calls, so
the "is it still alive?" check runs before almost every request. It used to
send a WebSocket ping. Qlik's Proxy Service does not relay ping/pong to the
Engine: through a virtual proxy the next request after a ping is never
answered, so every second tool call blocked for the whole QLIK_WS_TIMEOUT
and then reconnected — piling up Engine sessions until Qlik's per-user limit
refused new ones. Verified on Qlik 31.60: harmless on a direct Engine socket,
fatal through the proxy.
"""

import socket

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _FakeWs:
    def __init__(self, connected=True):
        self.connected = connected
        self.pings = 0
        self.sent = []
        self.closed = False

    def ping(self, *args, **kwargs):
        self.pings += 1

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        raise socket.timeout("nothing to read")

    def close(self):
        self.closed = True


class _Engine(QlikEngineAPI):
    def __init__(self, ws=None, last_io=0.0):
        self.ws = ws
        self.ws_timeout_seconds = 180.0
        self._last_successful_io = last_io
        self._cached_app_id = None
        self._cached_app_handle = -1
        self._cached_has_data = False
        self.probes = []

    # Stand-in for the probe request so no socket work happens in the test.
    def send_request(self, method, params=None, handle=-1, timeout=None):
        self.probes.append((method, timeout))
        if getattr(self, "probe_fails", False):
            self._kill_socket()
            raise TimeoutError("probe timed out")
        return {"result": {"qVersion": "31.60.0.0"}}


@pytest.fixture
def now(monkeypatch):
    """Freeze the monotonic clock the liveness check reads."""
    clock = {"t": 1000.0}
    monkeypatch.setattr("qlik_sense_mcp_server.engine.connection.time.monotonic",
                        lambda: clock["t"])
    return clock


class TestNoPing:
    def test_recently_used_socket_is_trusted_without_any_traffic(self, now):
        ws = _FakeWs()
        eng = _Engine(ws, last_io=now["t"] - 5)
        assert eng._is_connected() is True
        assert ws.pings == 0, "ping breaks the next request through a virtual proxy"
        assert eng.probes == [], "a socket that just answered needs no probe"

    def test_idle_socket_is_probed_with_a_real_request_not_a_ping(self, now):
        ws = _FakeWs()
        eng = _Engine(ws, last_io=now["t"] - 3600)
        assert eng._is_connected() is True
        assert ws.pings == 0
        assert eng.probes and eng.probes[0][0] == "EngineVersion"

    def test_probe_uses_a_short_timeout(self, now):
        """A dead idle socket must not burn the full 180s operation budget."""
        eng = _Engine(_FakeWs(), last_io=now["t"] - 3600)
        eng._is_connected()
        assert eng.probes[0][1] == eng.ws_probe_timeout
        assert eng.probes[0][1] < eng.ws_timeout_seconds

    def test_idle_window_is_configurable(self, now, monkeypatch):
        """QLIK_WS_IDLE_PROBE_AFTER trades a probe request against staleness."""
        eng = _Engine(_FakeWs(), last_io=now["t"] - 60)
        eng.ws_idle_probe_after = 120.0
        assert eng._is_connected() is True
        assert eng.probes == [], "inside the configured window nothing is sent"

    def test_probe_failure_reports_dead_and_leaves_no_socket(self, now):
        eng = _Engine(_FakeWs(), last_io=now["t"] - 3600)
        eng.probe_fails = True
        assert eng._is_connected() is False
        assert eng.ws is None, "caller must find a clean state to reconnect from"


class TestObviouslyDead:
    def test_no_socket(self):
        assert _Engine(None)._is_connected() is False

    def test_socket_marked_disconnected(self):
        assert _Engine(_FakeWs(connected=False))._is_connected() is False

    def test_dead_socket_is_not_probed(self, now):
        eng = _Engine(_FakeWs(connected=False), last_io=now["t"] - 3600)
        eng._is_connected()
        assert eng.probes == []


class TestSuccessRefreshesTheClock:
    def test_answered_frame_marks_the_socket_alive(self, now):
        """Without this, every call past the idle window pays for a probe."""
        class _Answering(QlikEngineAPI):
            def __init__(self):
                self.ws = _AnswerOnce()
                self.ws_timeout_seconds = 30.0
                self.request_id = 0

        class _AnswerOnce(_FakeWs):
            def recv(self):
                return '{"jsonrpc":"2.0","id":1,"result":{}}'

        eng = _Answering()
        eng.send_request("GetScript", [], handle=1)
        assert eng._last_successful_io == now["t"]
