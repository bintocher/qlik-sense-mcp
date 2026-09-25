"""QRS signals an expired/missing session two different ways.

Verified against a live form-mode deployment: a request carrying no valid
session cookie does not always come back as 401 — it can come back as a
302 redirect to the virtual proxy's login page, because QRS treats an
unauthenticated request like a browser hit rather than an API call. The
401-only retry silently returned `{"error": "HTTP 302: ..."}` instead of
refreshing the session, which is exactly the failure reported from a
neighboring session using the real MCP tool.
"""

import httpx

from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.repository_api import QlikRepositoryAPI


class _StubCookieSession:
    """Looks bootstrapped from the first call; records invalidate/ensure calls."""

    def __init__(self):
        self.csrf_token = "csrf-1"
        self.ensure_calls = 0
        self.invalidate_calls = 0

    def ensure(self, http_client):
        self.ensure_calls += 1
        self.csrf_token = f"csrf-{self.ensure_calls}"

    def invalidate(self):
        self.invalidate_calls += 1


def _form_config():
    return QlikSenseConfig(
        server_url="https://qlik.example.com/forms",
        user_id="ivanov", password="s3cret",
    )


def _jwt_config():
    return QlikSenseConfig(
        server_url="https://qlik.example.com/jwt",
        jwt_token="eyJhbGciOiJSUzI1NiJ9.e30.c2lnbmF0dXJl",
    )


def _json_response(request, payload):
    return httpx.Response(200, json=payload, request=request)


def _redirect_response(request, location="https://qlik.example.com/forms/internal_forms_authentication/?targetId=x"):
    return httpx.Response(302, headers=[("location", location)], request=request)


def _unauthorized_response(request):
    return httpx.Response(401, request=request)


def _auth_error_500_response(request):
    """Qlik's own error page for a present-but-unrecognized session cookie —
    verified against a live deployment, distinct from an outright missing
    cookie (which gets a 302 instead)."""
    return httpx.Response(
        500,
        text="<html><body><h1>500 - Internal server error - Qlik Sense</h1>"
             "<p>Authentication error: Restart the browser.</p></body></html>",
        request=request,
    )


def _unrelated_500_response(request):
    return httpx.Response(500, text="<html>Something else broke.</html>", request=request)


class TestSessionExpiryRetry:
    def test_302_triggers_a_session_refresh_and_retry(self, monkeypatch):
        session = _StubCookieSession()
        repo = QlikRepositoryAPI(_form_config(), form_session=session)

        calls = []

        def fake_request(method, url, **kwargs):
            req = httpx.Request(method, url)
            calls.append(url)
            if len(calls) == 1:
                return _redirect_response(req)
            return _json_response(req, {"buildVersion": "31.60"})

        monkeypatch.setattr(repo.client, "request", fake_request)

        result = repo.get_about()

        assert result == {"buildVersion": "31.60"}
        assert len(calls) == 2, "expected exactly one retry after the 302"
        assert session.invalidate_calls == 1
        assert session.ensure_calls == 2  # once up front, once after the 302

    def test_401_still_triggers_the_same_retry(self, monkeypatch):
        """Regression guard: adding 302 handling must not disturb 401."""
        session = _StubCookieSession()
        repo = QlikRepositoryAPI(_form_config(), form_session=session)

        calls = []

        def fake_request(method, url, **kwargs):
            req = httpx.Request(method, url)
            calls.append(url)
            if len(calls) == 1:
                return _unauthorized_response(req)
            return _json_response(req, {"buildVersion": "31.60"})

        monkeypatch.setattr(repo.client, "request", fake_request)

        result = repo.get_about()

        assert result == {"buildVersion": "31.60"}
        assert session.invalidate_calls == 1

    def test_a_second_redirect_after_retry_is_reported_as_an_error(self, monkeypatch):
        """Not an infinite loop: exactly one retry, then the failure surfaces."""
        session = _StubCookieSession()
        repo = QlikRepositoryAPI(_form_config(), form_session=session)

        def fake_request(method, url, **kwargs):
            req = httpx.Request(method, url)
            return _redirect_response(req)

        monkeypatch.setattr(repo.client, "request", fake_request)

        result = repo.get_about()

        assert "error" in result
        assert "302" in result["error"]
        assert session.invalidate_calls == 1  # only the one retry, not a loop

    def test_302_is_ignored_in_certificate_mode(self, monkeypatch):
        """No cookie session in certificate mode — a 302 must not crash
        trying to invalidate/refresh something that doesn't exist."""
        config = QlikSenseConfig(server_url="https://qlik.example.com",
                                 user_directory="DOMAIN", user_id="admin")
        repo = QlikRepositoryAPI(config)

        def fake_request(method, url, **kwargs):
            req = httpx.Request(method, url)
            return _redirect_response(req, location="https://qlik.example.com/somewhere")

        monkeypatch.setattr(repo.client, "request", fake_request)

        result = repo.get_about()
        assert "error" in result
        assert "302" in result["error"]

    def test_500_authentication_error_triggers_the_same_retry(self, monkeypatch):
        session = _StubCookieSession()
        repo = QlikRepositoryAPI(_form_config(), form_session=session)

        calls = []

        def fake_request(method, url, **kwargs):
            req = httpx.Request(method, url)
            calls.append(url)
            if len(calls) == 1:
                return _auth_error_500_response(req)
            return _json_response(req, {"buildVersion": "31.60"})

        monkeypatch.setattr(repo.client, "request", fake_request)

        result = repo.get_about()

        assert result == {"buildVersion": "31.60"}
        assert session.invalidate_calls == 1

    def test_unrelated_500_is_not_treated_as_an_expired_session(self, monkeypatch):
        """Must not mask a real server error as a session problem — no
        retry, no extra ensure()/invalidate() calls, just the failure."""
        session = _StubCookieSession()
        repo = QlikRepositoryAPI(_form_config(), form_session=session)

        calls = []

        def fake_request(method, url, **kwargs):
            calls.append(url)
            return _unrelated_500_response(httpx.Request(method, url))

        monkeypatch.setattr(repo.client, "request", fake_request)

        result = repo.get_about()

        assert "error" in result
        assert "500" in result["error"]
        assert len(calls) == 1, "an unrelated 500 must not be retried"
        assert session.invalidate_calls == 0

    def test_302_also_works_for_jwt_mode(self, monkeypatch):
        session = _StubCookieSession()
        repo = QlikRepositoryAPI(_jwt_config(), jwt_session=session)

        calls = []

        def fake_request(method, url, **kwargs):
            req = httpx.Request(method, url)
            calls.append(url)
            if len(calls) == 1:
                return _redirect_response(req, location="https://qlik.example.com/jwt/qps/login")
            return _json_response(req, {"buildVersion": "31.60"})

        monkeypatch.setattr(repo.client, "request", fake_request)

        result = repo.get_about()
        assert result == {"buildVersion": "31.60"}


class _DroppingClient:
    """Drops the connection on the first request, answers the second.

    Qlik closes the connection at the end of the login redirect chain, so the
    first QRS call after a bootstrap can land in a socket the server has
    already dropped.
    """

    def __init__(self):
        self.cookies = httpx.Cookies()
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(
            200, json=[{"id": "app-1"}],
            headers={"content-type": "application/json"},
            request=httpx.Request(method, url),
        )


class TestDroppedConnectionRetry:
    def test_a_dropped_read_is_retried_once(self):
        api = QlikRepositoryAPI(_form_config(), form_session=_StubCookieSession())
        api.client = _DroppingClient()
        assert api._make_request("GET", "app/full") == [{"id": "app-1"}]
        assert api.client.calls == 2

    def test_a_dropped_write_is_reported_not_repeated(self):
        api = QlikRepositoryAPI(_form_config(), form_session=_StubCookieSession())
        api.client = _DroppingClient()
        result = api._make_request("POST", "reloadtask")
        assert "error" in result
        assert api.client.calls == 1
