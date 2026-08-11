"""The WebSocket follows the scheme the operator configured.

`QLIK_SERVER_URL=http://host/jwt` has to connect with `ws://`. The client
used to hard-code `wss://` on the grounds that TLS is mandatory, which is
not a decision it gets to make: Qlik serves the virtual proxies on 80 and
443 both, and which one an operator points this server at is their call —
a lab node, a deployment that terminates TLS in front of Qlik, or a client
whose TLS stack cannot talk to that particular proxy.
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

    def test_every_endpoint_tried_uses_the_configured_scheme(self):
        """Not just the first one: if the first URL fails, the retries must
        not silently switch to TLS against a server that has none."""
        client = _client("http://qlik.example.com/jwt")
        client.ws_retries = 5
        tried = []

        def always_refused(url, **kwargs):
            tried.append(url)
            raise OSError("connection refused")

        with patch("websocket.create_connection", side_effect=always_refused):
            with pytest.raises(Exception):
                client.connect(app_id="app-1")

        assert len(tried) > 1, "only one endpoint was tried; the test proves nothing"
        assert all(url.startswith("ws://") for url in tried), tried


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


class TestCertificateMode:
    """The other authentication mode obeys the same contract.

    Certificate mode connects straight to the Engine port instead of going
    through a virtual proxy, but `QLIK_SERVER_URL` is still where the
    operator states the scheme. It used to list both `wss://` endpoints
    first and only then `ws://`, and since the default retry budget is 2,
    the `ws://` entries were unreachable — on a node whose Engine TLS is
    unhappy, the client failed with no way to ask for plain WebSocket.
    """

    @staticmethod
    def _cert_client(server_url):
        config = QlikSenseConfig(server_url=server_url,
                                 user_directory="QLIK1", user_id="svc")
        return QlikEngineAPI(config)

    def test_http_url_tries_ws_first(self):
        tried = _connect_and_capture(self._cert_client("http://qlik.example.com"))
        assert tried[0][0].startswith("ws://"), tried[0][0]

    def test_https_url_tries_wss_first(self):
        tried = _connect_and_capture(self._cert_client("https://qlik.example.com"))
        assert tried[0][0].startswith("wss://"), tried[0][0]

    def test_the_engine_port_is_used(self):
        tried = _connect_and_capture(self._cert_client("https://qlik.example.com"))
        assert ":4747/" in tried[0][0], tried[0][0]

    def test_the_requested_scheme_is_exhausted_before_the_other(self):
        client = self._cert_client("http://qlik.example.com")
        client.ws_retries = 2  # the default budget
        tried = []

        def always_refused(url, **kwargs):
            tried.append(url)
            raise OSError("connection refused")

        with patch("websocket.create_connection", side_effect=always_refused):
            with pytest.raises(Exception):
                client.connect()

        assert tried, "nothing was tried"
        assert all(url.startswith("ws://") for url in tried), tried

    def test_identity_header_is_sent(self):
        tried = _connect_and_capture(self._cert_client("https://qlik.example.com"))
        headers = tried[0][1]["header"]
        assert any("X-Qlik-User: UserDirectory=QLIK1; UserId=svc" == h for h in headers)
