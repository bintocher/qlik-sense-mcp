"""Engine's counterpart to test_repository_session_retry.py.

QRS signals an expired/unrecognized session with an HTTP status (401, or —
per that other test file — a 302/500 with no clean status at all). The
Engine WebSocket upgrade has no equivalent: verified against a live
form-mode deployment, a session cookie whose local TTL says "fresh" but
that Qlik no longer recognizes server-side (a proxy failover, an
out-of-band revocation) lets the HTTP upgrade succeed and then Engine just
closes the socket without ever sending a greeting — no status code for
`WebSocketBadStatusException` to catch at all. `_consume_greeting` turns
that into a `QlikConnectionError`, and `connect()` must treat it as a
retriable stale session exactly like a 401/403 upgrade rejection.
"""

import json

import pytest
from unittest.mock import MagicMock, patch

from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.exceptions import QlikLicenseError
from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _CountingFormSessionStub:
    """A cookie session whose csrf token changes after every refresh, so a
    test can prove a retry actually re-bootstrapped rather than reusing the
    same (still-bad) material."""

    def __init__(self):
        self.generation = 0
        self.ensure_standalone_calls = 0
        self.invalidate_calls = 0

    def ensure_standalone(self):
        self.ensure_standalone_calls += 1

    def invalidate(self):
        self.invalidate_calls += 1
        self.generation += 1

    @property
    def csrf_token(self):
        return f"csrf-gen{self.generation}"

    def cookie_header(self):
        return f"X-Qlik-Session-forms=gen{self.generation}"


def _client(server_url="https://qlik.example.com/forms"):
    config = QlikSenseConfig(server_url=server_url, user_id="ivanov", password="s3cret")
    return QlikEngineAPI(config, form_session=_CountingFormSessionStub())


def _connected_ws():
    ws = MagicMock()
    ws.recv.return_value = json.dumps({"jsonrpc": "2.0", "method": "OnConnected", "params": {}})
    return ws


def _dead_on_arrival_ws():
    """A socket that accepted the upgrade but Engine hangs up on with no
    greeting at all — recv() returning falsy is exactly the case
    `_consume_greeting` turns into "Engine closed the WebSocket immediately
    after connect without sending a greeting"."""
    ws = MagicMock()
    ws.recv.return_value = ""
    return ws


class TestGreetinglessCloseRetry:
    def test_one_dead_socket_is_retried_with_a_refreshed_session(self):
        client = _client()
        attempts = []

        def create_connection(url, **kwargs):
            attempts.append((url, kwargs["header"]))
            if len(attempts) == 1:
                return _dead_on_arrival_ws()
            return _connected_ws()

        with patch("websocket.create_connection", side_effect=create_connection):
            client.connect(app_id="app-1")

        assert len(attempts) == 2, "expected exactly one retry after the dead socket"
        assert client.form_session.invalidate_calls == 1
        assert client.form_session.ensure_standalone_calls == 2  # up front + after the retry
        # The retried attempt must carry the refreshed cookie/csrf, not the stale one.
        _, first_headers = attempts[0]
        _, second_headers = attempts[1]
        assert any("gen0" in h for h in first_headers)
        assert any("gen1" in h for h in second_headers)

    def test_two_dead_sockets_exhaust_the_retry_and_fail(self):
        client = _client()
        client.ws_retries = 1  # keep the fallback endpoint list short for this test

        def create_connection(url, **kwargs):
            return _dead_on_arrival_ws()

        with patch("websocket.create_connection", side_effect=create_connection):
            try:
                client.connect(app_id="app-1")
                assert False, "expected connect() to raise"
            except Exception as exc:
                assert "Failed to connect to Engine API" in str(exc)

        assert client.form_session.invalidate_calls == 1, "only the one retry, not a loop"

    def test_certificate_mode_does_not_retry_a_dead_socket(self):
        """No cookie session in certificate mode — must fall through to the
        next fallback endpoint (or fail) rather than trying to refresh
        something that doesn't exist."""
        config = QlikSenseConfig(server_url="https://qlik.example.com",
                                 user_directory="DOMAIN", user_id="svc")
        client = QlikEngineAPI(config)
        client.ws_retries = 1

        attempts = []

        def create_connection(url, **kwargs):
            attempts.append(url)
            return _dead_on_arrival_ws()

        with patch("websocket.create_connection", side_effect=create_connection):
            try:
                client.connect(app_id="app-1")
                assert False, "expected connect() to raise"
            except Exception as exc:
                assert "Failed to connect to Engine API" in str(exc)

        # Certificate mode has no session to refresh, so every attempt is a
        # distinct fallback endpoint, not a retry of the same one.
        assert len(set(attempts)) == len(attempts)


def _license_denied_ws():
    """A socket Engine refuses because the identity has no license.

    Authentication succeeded, so the upgrade goes through and the refusal
    arrives as a greeting notification - the same shape as a stale-session
    close, but nothing a fresh login can fix.
    """
    ws = MagicMock()
    ws.recv.return_value = json.dumps({
        "jsonrpc": "2.0",
        "method": "OnLicenseAccessDenied",
        "params": {"severity": "fatal"},
    })
    return ws


class TestLicenseDenied:
    def test_a_missing_license_is_not_retried_with_a_new_session(self):
        """Logging in again cannot grant a license.

        Retrying would spend one more of the five Qlik sessions allowed per
        user on a certain failure, bringing the limit closer for no reason.
        """
        client = _client()
        attempts = []

        def create_connection(url, **kwargs):
            attempts.append(url)
            return _license_denied_ws()

        with patch("websocket.create_connection", side_effect=create_connection):
            with pytest.raises(QlikLicenseError) as excinfo:
                client.connect(app_id="app-1")

        assert "no license access" in str(excinfo.value)
        assert len(attempts) == 1, "no fallback endpoint and no retry"
        assert client.form_session.invalidate_calls == 0
        assert client.form_session.ensure_standalone_calls == 1

    def test_a_stale_session_is_still_retried(self):
        """The neighbouring case must keep working: a greeting-less close is
        exactly what a fresh login does fix."""
        client = _client()
        attempts = []

        def create_connection(url, **kwargs):
            attempts.append(url)
            return _dead_on_arrival_ws() if len(attempts) == 1 else _connected_ws()

        with patch("websocket.create_connection", side_effect=create_connection):
            client.connect(app_id="app-1")

        assert client.form_session.invalidate_calls == 1
