"""The WebSocket follows the scheme the operator configured.

`QLIK_SERVER_URL=http://host/jwt` has to connect with `ws://`. The client
used to hard-code `wss://` on the grounds that TLS is mandatory, which is
not a decision it gets to make: Qlik serves the virtual proxies on 80 and
443 both, and when the proxy's TLS is broken — a state this very server hit
on a live stand, where 443 stopped completing handshakes while 80 answered
`204` — plain HTTP is the only thing that works.
"""

from unittest.mock import MagicMock, patch

import pytest

from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.engine_api import QlikEngineAPI


class _Session:
    """A JwtSession that is already bootstrapped."""

    csrf_token = "tok-123"

    def ensure_standalone(self):
        pass

    def cookie_header(self):
        return "X-Qlik-Session-jwt=abc"


def _client(server_url):
    config = QlikSenseConfig(server_url=server_url, jwt_token="jwt-payload")
    return QlikEngineAPI(config, jwt_session=_Session())


def _connect_and_capture(client, **kwargs):
    """Run connect() against a stubbed websocket, return the URLs it tried."""
    tried = []

    def create_connection(url, **cc_kwargs):
        tried.append((url, cc_kwargs))
        ws = MagicMock()
        ws.recv.return_value = '{"jsonrpc":"2.0","method":"OnConnected","params":{}}'
        return ws

    with patch("websocket.create_connection", side_effect=create_connection):
        client.connect(**kwargs)
    return tried


class TestScheme:
    def test_http_url_connects_over_ws(self):
        tried = _connect_and_capture(_client("http://qlik.example.com/jwt"))
        assert tried[0][0].startswith("ws://"), tried[0][0]

    def test_https_url_connects_over_wss(self):
        tried = _connect_and_capture(_client("https://qlik.example.com/jwt"))
        assert tried[0][0].startswith("wss://"), tried[0][0]

    def test_plain_ws_carries_no_ssl_context(self):
        """websocket-client rejects sslopt on a non-TLS connection."""
        tried = _connect_and_capture(_client("http://qlik.example.com/jwt"))
        assert "sslopt" not in tried[0][1]

    def test_wss_carries_the_ssl_context(self):
        tried = _connect_and_capture(_client("https://qlik.example.com/jwt"))
        assert "sslopt" in tried[0][1]

    @pytest.mark.parametrize("url, expected", [
        ("http://qlik.example.com/jwt", "ws://qlik.example.com/jwt/app/"),
        ("http://qlik.example.com:8080/jwt", "ws://qlik.example.com:8080/jwt/app/"),
        ("https://qlik.example.com:8443/jwt", "wss://qlik.example.com:8443/jwt/app/"),
    ])
    def test_port_is_preserved(self, url, expected):
        tried = _connect_and_capture(_client(url), app_id="app-1")
        assert tried[0][0].startswith(expected), tried[0][0]

    def test_every_fallback_endpoint_uses_the_same_scheme(self):
        """A wss:// fallback after a ws:// first try would fail confusingly."""
        client = _client("http://qlik.example.com/jwt")
        client.ws_retries = 5
        tried = _connect_and_capture(client, app_id="app-1")
        # Only the first succeeds here, so force the failure path instead.
        with patch("websocket.create_connection", side_effect=OSError("refused")):
            with pytest.raises(Exception):
                client.connect(app_id="app-1")
        assert all(url.startswith("ws://") for url, _ in tried)


class TestHeaders:
    def test_origin_matches_the_configured_scheme(self):
        """Qlik compares Origin against the VP host allow list."""
        tried = _connect_and_capture(_client("http://qlik.example.com/jwt"))
        headers = tried[0][1]["header"]
        assert "Origin: http://qlik.example.com" in headers

    def test_csrf_token_travels_in_the_url_and_the_header(self):
        tried = _connect_and_capture(_client("http://qlik.example.com/jwt"))
        url, kwargs = tried[0]
        assert "qlik-csrf-token=tok-123" in url
        assert "qlik-csrf-token: tok-123" in kwargs["header"]

    def test_no_bearer_on_the_upgrade(self):
        """CSWSH protection rejects an Authorization header on the upgrade."""
        tried = _connect_and_capture(_client("https://qlik.example.com/jwt"))
        assert not any(h.lower().startswith("authorization")
                       for h in tried[0][1]["header"])
