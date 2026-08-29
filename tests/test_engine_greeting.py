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



