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







class TestHeaders:
    def test_origin_matches_the_configured_scheme(self):
        """Qlik compares Origin against the VP host allow list."""
        tried = _connect_and_capture(_client("http://qlik.example.com/jwt"))
        headers = tried[0][1]["header"]
        assert "Origin: http://qlik.example.com" in headers




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




