"""Login/password (form) session bootstrap.

FormSession drives a Qlik virtual proxy's "Form based" login page the way a
browser would: GET the page, parse out the login form, POST credentials,
and pick up the session cookie the same way JwtSession does after its own
bootstrap. The exact login endpoint and field names are deployment-specific
(there is no single documented wire format, unlike JWT's /qps/csrftoken),
so these tests exercise the auto-detection and the escape hatches
(QLIK_FORM_LOGIN_PATH / QLIK_FORM_USERNAME_FIELD / QLIK_FORM_PASSWORD_FIELD)
rather than one hardcoded page shape.
"""

import httpx
import pytest

from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.form_session import FormBootstrapError, FormSession

LOGIN_HTML = """
<html><body>
<form action="/jwt/internal_forms_authentication/login" method="post">
<input type="hidden" name="__token" value="tok-123">
<input type="text" name="username">
<input type="password" name="pwd">
<input type="submit" value="Log in">
</form>
</body></html>
"""

NO_FORM_HTML = "<html><body>Welcome. No login form on this page.</body></html>"

# Field names that reveal nothing via their "name" attribute — only the
# `type="password"` attribute marks the login field. Exercises detection
# that does not depend on English field-naming conventions.
OBSCURE_FIELD_NAMES_HTML = """
<html><body>
<form action="login">
<input type="hidden" name="csrf" value="xyz">
<input type="text" name="f1">
<input type="password" name="f2">
</form>
</body></html>
"""

# Two non-password text fields before the real login field — auto-detection
# without a "user"-like name would grab the wrong one (the search box).
AMBIGUOUS_USERNAME_HTML = """
<html><body>
<form action="login">
<input type="text" name="search">
<input type="text" name="account_login">
<input type="password" name="secret">
</form>
</body></html>
"""


class _FakeClient:
    """Stand-in for httpx.Client covering exactly what FormSession calls."""

    def __init__(self, login_html=LOGIN_HTML, post_sets_cookie=True):
        self.cookies = httpx.Cookies()
        self.calls = []
        self._login_html = login_html
        self._post_sets_cookie = post_sets_cookie

    def get(self, url, headers=None, timeout=None, follow_redirects=None):
        self.calls.append(("GET", url))
        if url.endswith("qps/csrftoken"):
            return httpx.Response(
                200, headers=[("qlik-csrf-token", "csrf-value")],
                request=httpx.Request("GET", url))
        return httpx.Response(200, text=self._login_html, request=httpx.Request("GET", url))

    def post(self, url, data=None, timeout=None, follow_redirects=None, headers=None):
        self.calls.append(("POST", url, data))
        if self._post_sets_cookie:
            self.cookies.set("X-Qlik-Session-jwt", "abc123")
        return httpx.Response(200, request=httpx.Request("POST", url))


def _config(**overrides):
    defaults = dict(
        server_url="https://qlik.example.com/jwt",
        user_directory="COMPANY",
        user_id="ivanov",
        password="s3cret",
    )
    defaults.update(overrides)
    return QlikSenseConfig(**defaults)


class TestFormBootstrap:
    def test_bootstraps_cookie_and_csrf(self):
        client = _FakeClient()
        session = FormSession(_config())
        session.ensure(client)
        assert session.cookie_name == "X-Qlik-Session-jwt"
        assert session.cookie_value == "abc123"
        assert session.csrf_token == "csrf-value"

    def test_submits_domain_and_username_joined_with_backslash(self):
        client = _FakeClient()
        FormSession(_config()).ensure(client)
        _, _, data = client.calls[1]
        assert data["username"] == "COMPANY\\ivanov"
        assert data["pwd"] == "s3cret"

    def test_carries_hidden_fields_through_unchanged(self):
        client = _FakeClient()
        FormSession(_config()).ensure(client)
        _, _, data = client.calls[1]
        assert data["__token"] == "tok-123"

    def test_no_directory_submits_bare_username(self):
        client = _FakeClient()
        FormSession(_config(user_directory="")).ensure(client)
        _, _, data = client.calls[1]
        assert data["username"] == "ivanov"

    def test_password_field_detected_by_type_regardless_of_name(self):
        """Field names carry no meaning — only type="password" does."""
        client = _FakeClient(login_html=OBSCURE_FIELD_NAMES_HTML)
        FormSession(_config()).ensure(client)
        _, _, data = client.calls[1]
        assert data["f2"] == "s3cret"
        assert data["f1"] == "COMPANY\\ivanov"

    def test_username_field_prefers_name_containing_user(self):
        client = _FakeClient(login_html=AMBIGUOUS_USERNAME_HTML)
        FormSession(_config()).ensure(client)
        _, _, data = client.calls[1]
        assert data["account_login"] == "COMPANY\\ivanov"
        assert "search" not in data or data.get("search") != "COMPANY\\ivanov"

    def test_missing_login_form_raises(self):
        client = _FakeClient(login_html=NO_FORM_HTML)
        with pytest.raises(FormBootstrapError, match="could not find a login form"):
            FormSession(_config()).ensure(client)

    def test_missing_session_cookie_raises(self):
        client = _FakeClient(post_sets_cookie=False)
        with pytest.raises(FormBootstrapError, match="did not produce a Qlik session cookie"):
            FormSession(_config()).ensure(client)

    def test_missing_credentials_raise_before_any_request(self):
        client = _FakeClient()
        with pytest.raises(FormBootstrapError, match="password is empty"):
            FormSession(_config(password=None)).ensure(client)
        assert client.calls == []

    def test_env_overrides_take_precedence_over_detection(self, monkeypatch):
        monkeypatch.setenv("QLIK_FORM_USERNAME_FIELD", "search")
        client = _FakeClient(login_html=AMBIGUOUS_USERNAME_HTML)
        FormSession(_config()).ensure(client)
        _, _, data = client.calls[1]
        assert data["search"] == "COMPANY\\ivanov"

    def test_login_path_override_changes_the_request_url(self, monkeypatch):
        monkeypatch.setenv("QLIK_FORM_LOGIN_PATH", "custom/login")
        client = _FakeClient()
        FormSession(_config()).ensure(client)
        method, url = client.calls[0]
        assert url == "https://qlik.example.com/jwt/custom/login"

    def test_ensure_is_a_noop_once_fresh(self):
        client = _FakeClient()
        session = FormSession(_config())
        session.ensure(client)
        calls_after_first = len(client.calls)
        session.ensure(client)
        assert len(client.calls) == calls_after_first

    def test_invalidate_forces_a_fresh_bootstrap(self):
        client = _FakeClient()
        session = FormSession(_config())
        session.ensure(client)
        session.invalidate()
        assert session.cookie_name is None
        session.ensure(client)
        assert session.cookie_name == "X-Qlik-Session-jwt"

    def test_cookie_header_before_ensure_raises(self):
        with pytest.raises(FormBootstrapError):
            FormSession(_config()).cookie_header()


class _FlakyEntryClient(_FakeClient):
    """Drops the connection on the first entry-point GET, like a live
    deployment observed once: the first request of a fresh session failed
    with RemoteProtocolError before any Qlik-specific request happened,
    and the identical request succeeded immediately on retry."""

    def __init__(self, fail_times=1, **kwargs):
        super().__init__(**kwargs)
        self._fail_times = fail_times
        self._entry_gets = 0

    def get(self, url, headers=None, timeout=None, follow_redirects=None):
        if not url.endswith("qps/csrftoken"):
            self._entry_gets += 1
            if self._entry_gets <= self._fail_times:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return super().get(url, headers=headers, timeout=timeout, follow_redirects=follow_redirects)


class TestEntryPointRetry:
    def test_one_dropped_connection_is_retried_transparently(self):
        client = _FlakyEntryClient(fail_times=1)
        session = FormSession(_config())
        session.ensure(client)  # must not raise
        assert session.cookie_value == "abc123"
        assert client._entry_gets == 2

    def test_two_dropped_connections_raise(self):
        client = _FlakyEntryClient(fail_times=2)
        with pytest.raises(FormBootstrapError, match="failed twice"):
            FormSession(_config()).ensure(client)
