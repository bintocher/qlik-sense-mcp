"""A long-lived server logs in again once the session TTL runs out.

The Repository client keeps the session cookie in its jar. A re-login sent
through that client carries the old cookie; the proxy recognises it and
answers 204 without Set-Cookie. A login that insisted on a new cookie failed
there, and every QRS call after the first 25 minutes failed with it.
"""

import httpx
import pytest

from qlik_sense_mcp_server import jwt_session as js
from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.jwt_session import JwtBootstrapError, JwtSession

BASE = "https://qlik.example.com"
COOKIE = "X-Qlik-Session-jwt"


class _Proxy:
    """A virtual proxy: a new session only for a request without one."""

    def __init__(self, set_cookie=True):
        self.issued = 0
        self.set_cookie = set_cookie
        self.qrs_cookies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        sent = request.headers.get("cookie", "")
        if request.url.path == "/jwt/qps/csrftoken":
            headers = [("qlik-csrf-token", "csrf")]
            if COOKIE not in sent and self.set_cookie:
                self.issued += 1
                headers.append(("set-cookie", f"{COOKIE}=s{self.issued}; Path=/"))
            return httpx.Response(204, headers=headers)
        self.qrs_cookies.append(sent)
        return httpx.Response(200, json={})


def _session(proxy, monkeypatch):
    session = JwtSession(QlikSenseConfig(
        server_url=f"{BASE}/jwt",
        jwt_token="eyJhbGciOiJSUzI1NiJ9.e30.c2lnbmF0dXJl"))
    transport = httpx.MockTransport(proxy)
    monkeypatch.setattr(session, "_build_bootstrap_client",
                        lambda: httpx.Client(transport=transport))
    return session, httpx.Client(transport=transport)


def _shift_clock(monkeypatch, seconds):
    real = js.time.time
    monkeypatch.setattr(js.time, "time", lambda: real() + seconds)


class TestReloginAfterTtl:
    def test_second_ensure_after_ttl_gets_a_new_cookie(self, monkeypatch):
        proxy = _Proxy()
        session, shared = _session(proxy, monkeypatch)

        session.ensure(shared)
        shared.get(f"{BASE}/jwt/qrs/about")
        _shift_clock(monkeypatch, js.DEFAULT_JWT_SESSION_TTL_SECONDS + 1)
        session.ensure(shared)
        shared.get(f"{BASE}/jwt/qrs/about")

        assert proxy.issued == 2
        assert proxy.qrs_cookies == [f"{COOKIE}=s1", f"{COOKIE}=s2"]

    def test_load_balancer_cookie_survives_the_relogin(self, monkeypatch):
        proxy = _Proxy()
        session, shared = _session(proxy, monkeypatch)
        shared.cookies.set("lb-affinity", "node2")

        session.ensure(shared)
        _shift_clock(monkeypatch, js.DEFAULT_JWT_SESSION_TTL_SECONDS + 1)
        session.ensure(shared)

        assert shared.cookies.get("lb-affinity") == "node2"
        assert shared.cookies.get(COOKIE) == "s2"


class TestNoCookieInReply:
    def test_204_for_a_sent_cookie_keeps_the_session(self, monkeypatch):
        proxy = _Proxy()
        session, shared = _session(proxy, monkeypatch)
        session.ensure(shared)
        _shift_clock(monkeypatch, js.DEFAULT_JWT_SESSION_TTL_SECONDS + 1)

        with session._lock:
            session._bootstrap(shared)  # the jar still carries s1

        assert session.cookie_value == "s1"
        assert session._is_fresh()

    def test_first_login_without_cookie_points_at_qmc(self, monkeypatch):
        session, shared = _session(_Proxy(set_cookie=False), monkeypatch)
        with pytest.raises(JwtBootstrapError, match="QMC"):
            session.ensure(shared)

    def test_later_login_without_cookie_does_not(self, monkeypatch):
        proxy = _Proxy()
        session, shared = _session(proxy, monkeypatch)
        session.ensure(shared)
        proxy.set_cookie = False
        session.invalidate()

        with pytest.raises(JwtBootstrapError) as raised:
            session.ensure(shared)
        assert "QMC" not in str(raised.value)
        assert "earlier login" in str(raised.value)
