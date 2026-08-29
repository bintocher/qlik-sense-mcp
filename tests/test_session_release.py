"""Two things that decide whether the server can connect at all.

The ttl segment is what stops sessions piling up: without it Qlik holds a
session for its own inactivity timeout, and the sixth start of this server
on one login is refused. Measured: — five starts passed, the sixth
did not, and adding the segment fixed it.

Picking the session cookie is the other one. The conventional name is
X-Qlik-Session, but QMC lets an admin rename it per virtual proxy and a load
balancer adds cookies of its own, so the choice cannot assume the name.
"""

import httpx
import pytest

from qlik_sense_mcp_server.config import QlikSenseConfig, WS_SESSION_TTL_SECONDS
from qlik_sense_mcp_server.jwt_session import JwtSession


def _response(cookies):
    """A csrftoken response carrying the given cookies."""
    headers = [("set-cookie", f"{name}={value}; Path=/")
               for name, value in cookies.items()]
    return httpx.Response(204, headers=headers,
                          request=httpx.Request("GET", "https://qlik/jwt/qps/csrftoken"))


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("QLIK_SERVER_URL", "https://qlik.example/jwt")
    monkeypatch.setenv("QLIK_JWT_TOKEN", "a.b.c")
    return JwtSession(QlikSenseConfig.from_env())


class TestSessionCookieChoice:
    def test_the_conventional_name_wins(self, session):
        name, value = session._pick_session_cookie(
            _response({"BIGipServerQlik": "lb", "X-Qlik-Session-jwt": "sess"}))
        assert (name, value) == ("X-Qlik-Session-jwt", "sess")





class TestTtlOnTheSocketUrl:
    """Without this segment the sessions of finished processes pile up."""

    @staticmethod
    def _endpoints(monkeypatch, **env):
        import ssl

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from qlik_sense_mcp_server.engine_api import QlikEngineAPI

        api = QlikEngineAPI(QlikSenseConfig.from_env())
        captured = {}

        def fake_connect(url, **kwargs):
            captured.setdefault("urls", []).append(url)
            raise OSError("не подключаемся — нужен только адрес")

        # The placeholder certificate files are not real PEM, and loading
        # them raises before a single URL is built.
        monkeypatch.setattr(ssl.SSLContext, "load_cert_chain",
                            lambda self, *a, **kw: None)
        monkeypatch.setattr(ssl.SSLContext, "load_verify_locations",
                            lambda self, *a, **kw: None)
        monkeypatch.setattr("websocket.create_connection", fake_connect)
        with pytest.raises(Exception):
            api.connect("app-guid")
        return captured.get("urls", [])

    def test_certificate_mode_asks_for_a_ttl(self, monkeypatch, tmp_path):
        for name in ("client.pem", "client_key.pem", "root.pem"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        urls = self._endpoints(
            monkeypatch,
            QLIK_SERVER_URL="https://qlik.example",
            QLIK_CLIENT_CERT_PATH=str(tmp_path / "client.pem"),
            QLIK_CLIENT_KEY_PATH=str(tmp_path / "client_key.pem"),
            QLIK_CA_CERT_PATH=str(tmp_path / "root.pem"),
            QLIK_USER_DIRECTORY="DIR", QLIK_USER_ID="user")
        assert urls, "ни одного адреса не построено"
        assert all(f"/ttl/{WS_SESSION_TTL_SECONDS}" in url for url in urls)

