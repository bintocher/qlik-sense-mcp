"""Greeting frames on a fresh Engine socket.

Engine answers a new WebSocket with notifications before anything else.
Most are harmless, but some mean the socket is dead on arrival — above all
`OnMaxParallelSessionsExceeded`, sent once the user holds Qlik's per-user
limit of concurrent sessions. Swallowing that frame as "session established"
is what turned a plain quota error into "Failed to parse WebSocket frame" on
the next call, several minutes and one wrong diagnosis later.
"""

import json
import socket

import pytest

from qlik_sense_mcp_server.engine_api import QlikEngineAPI
from qlik_sense_mcp_server.exceptions import QlikConnectionError, QlikSessionLimitError


def _notification(method, **params):
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params})


class _FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class _FakeWs:
    """Hands out queued frames; raises socket.timeout once they run out."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sock = _FakeSocket()
        self.connected = True
        self.closed = False

    def recv(self):
        if not self.frames:
            raise socket.timeout("no more frames")
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return frame

    def close(self):
        self.closed = True


class _Engine(QlikEngineAPI):
    """Bare instance: no config, no network."""

    def __init__(self, frames):
        self.ws = _FakeWs(frames)
        self.ws_timeout_seconds = 180.0
        self._cached_app_id = None
        self._cached_app_handle = -1
        self._cached_has_data = False


class TestFatalGreetings:
    def test_session_limit_raises_typed_error(self):
        eng = _Engine([_notification("OnMaxParallelSessionsExceeded",
                                     severity="fatal", timestamp="2026-08-10T14:00:15")])
        with pytest.raises(QlikSessionLimitError) as excinfo:
            eng._consume_greeting()
        message = str(excinfo.value)
        assert "OnMaxParallelSessionsExceeded" in message
        assert "severity=fatal" in message
        # The caller has to know what to do about it, not just that it broke.
        assert "delete-user-sessions" in message

    def test_session_limit_is_a_connection_error_too(self):
        """Callers catching QlikConnectionError must not miss the quota case."""
        eng = _Engine([_notification("OnMaxParallelSessionsExceeded", severity="fatal")])
        with pytest.raises(QlikConnectionError):
            eng._consume_greeting()

    @pytest.mark.parametrize("method", [
        "OnSessionClosed", "OnSessionTimedOut", "OnLicenseAccessDenied",
    ])
    def test_other_fatal_greetings(self, method):
        eng = _Engine([_notification(method, severity="fatal")])
        with pytest.raises(QlikConnectionError) as excinfo:
            eng._consume_greeting()
        assert method in str(excinfo.value)

    def test_fatal_frame_after_a_harmless_one_is_still_caught(self):
        eng = _Engine([
            _notification("OnAuthenticationInformation", userId="bintocher"),
            _notification("OnMaxParallelSessionsExceeded", severity="fatal"),
        ])
        with pytest.raises(QlikSessionLimitError):
            eng._consume_greeting()

    def test_immediate_close_is_reported_as_such(self):
        """recv() returning '' is Engine hanging up, not an empty message."""
        eng = _Engine([""])
        with pytest.raises(QlikConnectionError) as excinfo:
            eng._consume_greeting()
        assert "closed the WebSocket" in str(excinfo.value)


class TestNormalGreeting:
    def test_reads_up_to_on_connected_and_stops(self):
        eng = _Engine([
            _notification("OnAuthenticationInformation", userId="bintocher"),
            _notification("OnConnected", qSessionState="SESSION_CREATED"),
            _notification("OnDoNotReadMe"),
        ])
        eng._consume_greeting()
        # The trailing frame must stay queued: reading past OnConnected would
        # consume the reply to the first real request.
        assert len(eng.ws.frames) == 1

    def test_missing_on_connected_does_not_break_the_connection(self):
        """Certificate mode on older Engines may not send OnConnected."""
        eng = _Engine([_notification("OnAuthenticationInformation")])
        eng._consume_greeting()  # must not raise
        assert eng.ws.connected

    def test_greeting_wait_is_bounded_and_timeout_restored(self):
        eng = _Engine([_notification("OnConnected")])
        eng._consume_greeting()
        # Short window while waiting, full timeout restored afterwards.
        assert eng.ws.sock.timeouts[0] == 15.0
        assert eng.ws.sock.timeouts[-1] == 180.0

    def test_non_json_frame_is_skipped(self):
        eng = _Engine(["<html>proxy error</html>", _notification("OnConnected")])
        eng._consume_greeting()
        assert eng.ws.frames == []
