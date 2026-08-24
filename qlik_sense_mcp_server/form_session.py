"""
Bootstrap a Qlik Sense Enterprise session from a login/password form.

Some Qlik Sense Enterprise deployments configure a virtual proxy with a
"Form based" authentication method — most commonly the in-box Windows
credentials login page, served under
``internal_forms_authentication/``. Unlike JWT (see ``jwt_session.py``)
there is no single documented wire format for this: the login page is
ordinary HTML with a `<form>`, and the exact URL and field names are
whatever the module renders.

Critically, the login URL is not a fixed path: Qlik appends a `targetId`
query parameter generated per visit (`internal_forms_authentication/
?targetId=<guid>`), and POSTing without it fails with `400 Virtual proxy
not possible to determine, since neither TargetId nor Virtual Proxy were
specified` — verified against a live deployment. A browser never sees this
because it never requests that URL directly: it starts at the hub or the
virtual proxy root, and Qlik's own redirect chain lands it on the login
page with `targetId` already attached. So this module reproduces that
same starting point rather than guessing the login URL:

    1. GET the entry point — the virtual proxy root by default — and
       follow every redirect. Whatever page Qlik lands on is treated as
       the login page, `targetId` and all. Cookies picked up along the
       way are kept.
    2. Parse the returned HTML for the first `<form>` that contains a
       `<input type="password">` — that is the login form. Hidden fields
       (anti-forgery tokens, return URLs, ...) are captured and resent
       unchanged. The username field is the first `<input type="text">` (or
       "email") whose name suggests an identity field ("user", "login",
       "email", "account"), falling back to the first such field found.
    3. POST the credentials — hidden fields plus username/password — to the
       form's `action` URL (or the landed-on URL itself, `targetId`
       included, when the form has no `action`), again following
       redirects.
    4. Whatever Qlik session cookie shows up in the client's cookie jar
       after that is the bootstrapped session.
    5. Fetch `qlik-csrf-token` via `GET {vp}/qps/csrftoken`, the same
       anti-CSWSH endpoint JWT mode's phase 1 hits — now authenticated by
       the cookie instead of a bearer token.

From that point on a form session behaves exactly like a JWT session after
its own bootstrap: cookie + csrf token, no further credentials sent. Both
`repository_api.py` and `engine/connection.py` treat them interchangeably.

Because the login endpoint and field names are deployment-specific, every
part of the flow can be overridden:

    QLIK_FORM_LOGIN_PATH      — path under the virtual proxy to start
                                 from, before any redirect (default: the
                                 virtual proxy root)
    QLIK_FORM_USERNAME_FIELD  — override the detected username field name
    QLIK_FORM_PASSWORD_FIELD  — override the detected password field name

The username value submitted is ``{QLIK_USER_DIRECTORY}\\{QLIK_USER_ID}``
when a directory is set, otherwise just ``QLIK_USER_ID`` — the conventional
Windows login format. If a deployment expects something else (a UPN, for
example) leave QLIK_USER_DIRECTORY empty and put the full string in
QLIK_USER_ID.
"""

from __future__ import annotations

import logging
import os
import ssl
import threading
import time
from html.parser import HTMLParser
from typing import Dict, Optional
from urllib.parse import urljoin

import httpx

from .config import QlikSenseConfig
from .utils import pick_qlik_session_cookie

logger = logging.getLogger(__name__)


# Same idle-timeout margin reasoning as JwtSession — see its module docstring.
DEFAULT_FORM_SESSION_TTL_SECONDS = 25 * 60

# Empty — the virtual proxy root. See module docstring: the actual login
# URL carries a per-visit `targetId` that only Qlik's own redirect chain
# produces, so the entry point has to be something Qlik redirects FROM,
# not a guess at the login page itself.
DEFAULT_LOGIN_PATH = ""

# Same cold-start reasoning as jwt_session.BOOTSTRAP_TIMEOUT_SECONDS: a proxy
# that has been idle answers its first request slowly.
BOOTSTRAP_TIMEOUT_SECONDS = 60.0


class FormBootstrapError(RuntimeError):
    """Raised when the login/password bootstrap fails irrecoverably."""


# Substrings that suggest a text/email input is the identity field, not
# some unrelated box on the same page (a site search, a promo signup).
# Checked against the field's `name` attribute, lowercased.
_USERNAME_FIELD_HINTS = ("user", "login", "email", "account")


class _LoginForm:
    """One `<form>` tag's fields, as found by `_LoginFormParser`."""

    def __init__(self) -> None:
        self.action: Optional[str] = None
        self.hidden_fields: Dict[str, str] = {}
        self.username_field: Optional[str] = None
        self.password_field: Optional[str] = None
        self._first_text_field: Optional[str] = None

    def finalize(self) -> bool:
        """True if this form has a password field, i.e. looks like a login form."""
        if not self.password_field:
            return False
        if not self.username_field:
            self.username_field = self._first_text_field
        return True


class _LoginFormParser(HTMLParser):
    """Finds the first `<form>` on a page that contains a password input.

    Not a general HTML parser — Qlik's bundled login pages are simple,
    static markup, and the stdlib `html.parser` handles that without
    pulling in a third-party dependency.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.result: Optional[_LoginForm] = None
        self._current: Optional[_LoginForm] = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_d = dict(attrs)
        if tag == "form":
            if self.result is not None:
                return  # already found the login form
            self._current = _LoginForm()
            self._current.action = attrs_d.get("action")
            return

        if tag != "input" or self._current is None:
            return
        name = attrs_d.get("name")
        if not name:
            return
        input_type = (attrs_d.get("type") or "text").lower()
        value = attrs_d.get("value", "")

        if input_type == "hidden":
            self._current.hidden_fields[name] = value
        elif input_type == "password":
            self._current.password_field = name
        elif input_type in ("text", "email"):
            if self._current._first_text_field is None:
                self._current._first_text_field = name
            if (self._current.username_field is None
                    and any(k in name.lower() for k in _USERNAME_FIELD_HINTS)):
                self._current.username_field = name

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            if self._current.finalize():
                self.result = self._current
            self._current = None


class FormSession:
    """
    Lazy, thread-safe holder of a login/password-bootstrapped Qlik session.

    Mirrors `JwtSession`'s public surface (`cookie_name`, `cookie_value`,
    `csrf_token`, `cookie_header()`, `invalidate()`, `logout()`, `ensure()`,
    `ensure_standalone()`) so `repository_api` and `engine_api` can use
    either interchangeably once a session is bootstrapped — the only
    difference between the two modes is how phase 1 happens.
    """

    def __init__(self, config: QlikSenseConfig, ttl_seconds: Optional[int] = None) -> None:
        self._config = config
        self._ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_FORM_SESSION_TTL_SECONDS
        self._lock = threading.Lock()
        self._cookie_name: Optional[str] = None
        self._cookie_value: Optional[str] = None
        self._csrf_token: Optional[str] = None
        self._fetched_at: float = 0.0

    # ─── public surface ────────────────────────────────────────────────

    @property
    def cookie_name(self) -> Optional[str]:
        with self._lock:
            return self._cookie_name

    @property
    def cookie_value(self) -> Optional[str]:
        with self._lock:
            return self._cookie_value

    @property
    def csrf_token(self) -> Optional[str]:
        with self._lock:
            return self._csrf_token

    def cookie_header(self) -> str:
        with self._lock:
            if not (self._cookie_name and self._cookie_value):
                raise FormBootstrapError("FormSession.cookie_header() called before ensure()")
            return f"{self._cookie_name}={self._cookie_value}"

    def invalidate(self) -> None:
        with self._lock:
            self._cookie_value = None
            self._csrf_token = None
            self._fetched_at = 0.0
            self._cookie_name = None

    def logout(self) -> bool:
        """Ask the proxy to end this user's Qlik session, and forget it.

        Same rationale and same endpoint as `JwtSession.logout()` — see
        there for why this matters (`OnMaxParallelSessionsExceeded`) and
        its scope (per virtual proxy, not global).
        """
        with self._lock:
            if not self._cookie_value:
                return False
            client = self._build_bootstrap_client()
            try:
                client.cookies.set(self._cookie_name, self._cookie_value)
                headers = ({"qlik-csrf-token": self._csrf_token}
                           if self._csrf_token else {})
                response = client.delete(
                    f"{self._config.qlik_base_host}/"
                    f"{self._config.virtual_proxy_path_segment}qps/user",
                    headers=headers,
                )
                accepted = response.status_code in (200, 204)
                logger.info("Form session logout: HTTP %s", response.status_code)
            except Exception as exc:
                logger.warning("Form session logout failed: %s", exc)
                accepted = False
            finally:
                client.close()

        self.invalidate()
        return accepted

    def ensure(self, http_client: httpx.Client) -> None:
        """Guarantee a valid bootstrapped session, using the given client.

        Safe to call on every request — returns fast if still fresh. The
        login GET/POST dance runs on the passed-in client, so the resulting
        session cookie lands in its cookie jar automatically.
        """
        if self._is_fresh():
            return
        with self._lock:
            if self._is_fresh():
                return
            self._bootstrap(http_client)

    def ensure_standalone(self) -> None:
        """Bootstrap without an externally-supplied `httpx.Client`.

        Used by `engine_api.connect()`, which does not own an httpx client.
        """
        if self._is_fresh():
            return
        with self._lock:
            if self._is_fresh():
                return
            client = self._build_bootstrap_client()
            try:
                self._bootstrap(client)
            finally:
                client.close()

    # ─── internals ─────────────────────────────────────────────────────

    def _is_fresh(self) -> bool:
        if not (self._cookie_value and self._csrf_token):
            return False
        return (time.time() - self._fetched_at) < self._ttl

    def _build_bootstrap_client(self) -> httpx.Client:
        if self._config.verify_ssl:
            ctx = ssl.create_default_context()
            if self._config.ca_cert_path:
                ctx.load_verify_locations(self._config.ca_cert_path)
            verify: object = ctx
        else:
            verify = False
        return httpx.Client(verify=verify, timeout=30.0)

    def _bootstrap(self, client: httpx.Client) -> None:
        """Log in via the form and store cookie + csrf token on success.

        Must be called with `self._lock` held.
        """
        cfg = self._config
        if not cfg.user_id:
            raise FormBootstrapError("user_id is empty — cannot bootstrap form login")
        if not cfg.password:
            raise FormBootstrapError("password is empty — cannot bootstrap form login")

        entry_path = os.getenv("QLIK_FORM_LOGIN_PATH", DEFAULT_LOGIN_PATH).strip("/")
        entry_url = f"{cfg.qlik_base_host}/{cfg.virtual_proxy_path_segment}{entry_path}"

        logger.info("Bootstrapping form session, entry point %s", entry_url)
        try:
            page = client.get(entry_url, timeout=BOOTSTRAP_TIMEOUT_SECONDS,
                              follow_redirects=True)
        except httpx.TransportError as exc:
            # One retry for a dropped connection on the very first hop of
            # the redirect chain — observed against a live deployment: the
            # TLS connection was reset with no response before any
            # Qlik-specific request even happened, then succeeded
            # immediately on retry. Not a Qlik failure mode, just the kind
            # of transient network blip repository_api.py already retries
            # once for on a cold QRS (see COLD_START_TIMEOUT).
            logger.warning(
                "Entry point request to %s failed (%s), retrying once: %s",
                entry_url, type(exc).__name__, exc)
            try:
                page = client.get(entry_url, timeout=BOOTSTRAP_TIMEOUT_SECONDS,
                                  follow_redirects=True)
            except httpx.HTTPError as retry_exc:
                raise FormBootstrapError(
                    f"entry point request to {entry_url} failed twice: {retry_exc}"
                ) from retry_exc
        except httpx.HTTPError as exc:
            raise FormBootstrapError(
                f"entry point request to {entry_url} failed: {exc}"
            ) from exc
        if page.status_code >= 400:
            raise FormBootstrapError(
                f"login page (reached via {entry_url}, landed on {page.url}) "
                f"returned HTTP {page.status_code}: {page.text[:300]}"
            )
        logger.info("Landed on login page %s", page.url)

        parser = _LoginFormParser()
        parser.feed(page.text)
        form = parser.result
        if form is None:
            raise FormBootstrapError(
                f"could not find a login form (an <input type=\"password\"> "
                f"field) on {page.url} (reached via entry point {entry_url}). "
                f"Set QLIK_FORM_LOGIN_PATH if the login flow starts elsewhere."
            )

        username_field = os.getenv("QLIK_FORM_USERNAME_FIELD") or form.username_field
        password_field = os.getenv("QLIK_FORM_PASSWORD_FIELD") or form.password_field
        if not username_field or not password_field:
            raise FormBootstrapError(
                "could not detect the login form's username/password field "
                "names. Set QLIK_FORM_USERNAME_FIELD / QLIK_FORM_PASSWORD_FIELD "
                "explicitly (inspect the login page's HTML to find them)."
            )

        username_value = f"{cfg.user_directory}\\{cfg.user_id}" if cfg.user_directory else cfg.user_id
        body = dict(form.hidden_fields)
        body[username_field] = username_value
        body[password_field] = cfg.password

        action_url = urljoin(str(page.url), form.action) if form.action else str(page.url)

        try:
            client.post(
                action_url, data=body, timeout=BOOTSTRAP_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise FormBootstrapError(f"login POST to {action_url} failed: {exc}") from exc

        cookie_name, cookie_value = pick_qlik_session_cookie(
            list(client.cookies.keys()), client.cookies.get)
        if not cookie_value:
            raise FormBootstrapError(
                "login did not produce a Qlik session cookie — check the "
                f"credentials, QLIK_FORM_LOGIN_PATH ({entry_path!r}), and the "
                f"detected field names (username={username_field!r}, "
                f"password={password_field!r}). Cookies received: "
                f"{list(client.cookies.keys()) or 'none'}."
            )

        self._cookie_name = cookie_name
        self._cookie_value = cookie_value
        self._csrf_token = self._fetch_csrf_token(client)
        self._fetched_at = time.time()
        logger.info(
            "Form session bootstrap OK (cookie=%s, csrf_present=%s)",
            cookie_name, bool(self._csrf_token),
        )

    def _fetch_csrf_token(self, client: httpx.Client) -> Optional[str]:
        """GET the anti-CSWSH token now that a session cookie is set.

        Same endpoint JWT mode's phase 1 uses — see `jwt_session.py`. The
        difference is authentication: there it is a bearer header, here it
        is the cookie the login POST just produced.
        """
        cfg = self._config
        url = f"{cfg.qlik_base_host}/{cfg.virtual_proxy_path_segment}qps/csrftoken"
        try:
            resp = client.get(url, headers={"Accept": "application/json"},
                              timeout=BOOTSTRAP_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            logger.warning("csrftoken request after form login failed: %s", exc)
            return None
        csrf = resp.headers.get("qlik-csrf-token")
        if not csrf:
            logger.warning(
                "csrftoken response after form login did not include a "
                "'qlik-csrf-token' header. This is fine on pre-Nov-2024 Qlik "
                "versions but will cause 403 on newer releases."
            )
        return csrf or None
