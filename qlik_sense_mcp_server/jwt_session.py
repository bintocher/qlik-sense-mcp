"""
Bootstrap a Qlik Sense Enterprise session from a pre-signed JWT.

On Qlik Sense November 2024+ (and everything later) Engine WebSocket
connections with just ``Authorization: Bearer <jwt>`` on the upgrade request
fail with 403 Forbidden. This is Qlik's intentional Cross-Site WebSocket
Hijacking protection (CSWSH): the virtual proxy now requires a real session
cookie and an anti-CSRF header that is only issued through a dedicated
bootstrap endpoint.

The supported two-phase flow, used by this module for ALL Qlik versions
(the extra header is harmless on pre-Nov-2024 releases):

    Phase 1 — GET {server}/{vp_prefix}/qps/csrftoken
              Headers: Authorization: Bearer <jwt>
              Response: Set-Cookie: X-Qlik-Session-<prefix>=<value>
                        HTTP header: qlik-csrf-token: <value>

    Phase 2 — Everything else (QRS HTTP requests, Engine WebSocket):
              Use the session cookie + the csrf token. Do NOT repeat
              the Authorization header — that is exactly what CSWSH
              protection rejects on a WebSocket upgrade.

The bootstrap result is cached for a conservative TTL (default 25 min —
Qlik session idle timeout is 30 min, leaving a 5 min buffer) and
transparently re-fetched when stale or on an explicit invalidate().

Sources:
    https://community.qlik.com/t5/Integration-Extension-APIs/Qlik-Sense-Nov-2025-WebSocket-connections-via-JWT-fail-with-403/td-p/2539417
    https://help.qlik.com/en-US/sense-developer/November2025/Subsystems/EngineAPI/Content/Sense_EngineAPI/GettingStarted/connecting-to-engine-api.htm
"""

from __future__ import annotations

import logging
import ssl
import threading
import time
from typing import Optional

import httpx

from .config import QlikSenseConfig

logger = logging.getLogger(__name__)


# Qlik's default session idle timeout is 30 minutes. Refreshing at 25 leaves
# margin so a borderline request never races the server-side eviction.
DEFAULT_JWT_SESSION_TTL_SECONDS = 25 * 60

# Name Qlik gives the virtual proxy session cookie. The suffix varies with
# the proxy, so the cookie is matched by this prefix rather than in full.
_QLIK_SESSION_COOKIE_PREFIX = "X-Qlik-Session"


class JwtBootstrapError(RuntimeError):
    """Raised when /qps/csrftoken bootstrap fails irrecoverably."""


class JwtSession:
    """
    Lazy, thread-safe holder of the bootstrapped Qlik session material.

    One instance per MCP process is sufficient because all MCP tools impersonate
    the single analyst identity encoded in the JWT. ``repository_api`` and
    ``engine_api`` share the same instance via ``server._init_clients``.
    """

    def __init__(
        self,
        config: QlikSenseConfig,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        self._config = config
        self._ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_JWT_SESSION_TTL_SECONDS
        self._lock = threading.Lock()
        self._cookie_name: Optional[str] = None
        self._cookie_value: Optional[str] = None
        self._csrf_token: Optional[str] = None
        self._fetched_at: float = 0.0

    # ─── public surface ────────────────────────────────────────────────
    # Reader properties snapshot the three session fields under the lock
    # so a concurrent ``invalidate()`` can't surface a half-updated state
    # (e.g. cookie present but csrf already cleared).

    @property
    def cookie_name(self) -> Optional[str]:
        """Last known session cookie name (None until first bootstrap)."""
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
        """
        Build a ``Cookie:`` HTTP header value for the current session.

        Call only after ``ensure()`` — raises if the session has not been
        bootstrapped yet.
        """
        with self._lock:
            if not (self._cookie_name and self._cookie_value):
                raise JwtBootstrapError("JwtSession.cookie_header() called before ensure()")
            return f"{self._cookie_name}={self._cookie_value}"

    def invalidate(self) -> None:
        """Drop the cached session so the next ensure() refetches."""
        with self._lock:
            self._cookie_value = None
            self._csrf_token = None
            self._fetched_at = 0.0
            # Forget the name too, so a fresh Set-Cookie is picked up.
            self._cookie_name = None

    def logout(self) -> bool:
        """Ask the proxy to end this user's Qlik session, and forget it.

        Every bootstrap creates a session that outlives the process:
        closing the WebSocket does not release it, and the virtual proxy
        keeps it until its inactivity timeout. Restart an MCP server a few
        times in quick succession and Qlik starts refusing new sessions
        with `OnMaxParallelSessionsExceeded`, which looks like an outage
        and lasts until the timeout expires.

        Not called automatically. `DELETE /{vp}/qps/user` is the only
        logout a virtual proxy exposes — the per-session endpoint needs
        admin rights on port 4243 — and it ends every session this user
        holds *on this virtual proxy*. Measured on Qlik 31.62: sessions
        the same user has on other virtual proxies, a browser on the
        default one included, are untouched. Still a group operation, so
        call it where the prefix belongs to this server (a dedicated JWT
        proxy, a test run), not on one people also log in through.

        Returns True when the proxy accepted the logout.
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
                    f"{self._config.virtual_proxy_prefix}/qps/user",
                    headers=headers,
                )
                accepted = response.status_code in (200, 204)
                logger.info("JWT session logout: HTTP %s", response.status_code)
            except Exception as exc:
                # A session we cannot reach will expire on its own; saying
                # so is more useful than raising during shutdown.
                logger.warning("JWT session logout failed: %s", exc)
                accepted = False
            finally:
                client.close()

        self.invalidate()
        return accepted

    def ensure(self, http_client: httpx.Client) -> None:
        """
        Guarantee a valid bootstrapped session, using the given ``httpx.Client``.

        Safe to call on every request — returns fast if the session is still
        fresh (within TTL). The passed-in client keeps the cookie jar so the
        bootstrapped session cookie is reused for subsequent QRS calls
        automatically (httpx persists cookies per-client).
        """
        if self._is_fresh():
            return
        with self._lock:
            if self._is_fresh():  # re-check under lock
                return
            self._bootstrap(http_client)

    def ensure_standalone(self) -> None:
        """
        Bootstrap without an externally-supplied ``httpx.Client``.

        Used by ``engine_api.connect()`` which does not own an httpx client —
        we create a short-lived one just to perform phase 1. The resulting
        cookie + csrf values are stored on this JwtSession for the WebSocket
        handshake; we do NOT need the cookie to persist in an httpx jar here.
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
        """Build a minimal httpx client suitable for the csrftoken call."""
        if self._config.verify_ssl:
            ctx = ssl.create_default_context()
            if self._config.ca_cert_path:
                ctx.load_verify_locations(self._config.ca_cert_path)
            verify: object = ctx
        else:
            verify = False
        return httpx.Client(verify=verify, timeout=30.0)

    def _bootstrap(self, client: httpx.Client) -> None:
        """
        Phase 1 request. Stores cookie + csrf token on success.

        Must be called with ``self._lock`` held.
        """
        cfg = self._config
        if not cfg.jwt_token:
            raise JwtBootstrapError("jwt_token is empty — cannot bootstrap")
        if not cfg.virtual_proxy_prefix:
            raise JwtBootstrapError(
                "virtual_proxy_prefix is empty — set QLIK_SERVER_URL to include "
                "the VP prefix, e.g. https://qlik.company.com/jwt"
            )

        url = f"{cfg.qlik_base_host}/{cfg.virtual_proxy_prefix}/qps/csrftoken"
        headers = {
            "Authorization": f"Bearer {cfg.jwt_token}",
            "Accept": "application/json",
        }
        logger.info("Bootstrapping JWT session via %s", url)
        try:
            resp = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise JwtBootstrapError(f"csrftoken request failed: {exc}") from exc

        if resp.status_code == 401:
            raise JwtBootstrapError(
                "csrftoken returned 401 — JWT rejected by the virtual proxy. "
                "Check that the token has not expired, that the VP JWT "
                "certificate matches the private key you signed with, and "
                "that the JWT claim names match the VP 'JWT attribute for "
                "user ID / user directory' fields."
            )
        if resp.status_code == 403:
            raise JwtBootstrapError(
                "csrftoken returned 403 — VP refused the request. Most "
                "common cause: the client hostname is not in the VP Host "
                "allow list in QMC. Add the exact hostname (no IP) used in "
                "QLIK_SERVER_URL to the VP allow list and retry."
            )
        if resp.status_code >= 400:
            raise JwtBootstrapError(
                f"csrftoken returned HTTP {resp.status_code}: {resp.text[:300]}"
            )

        csrf = resp.headers.get("qlik-csrf-token")
        if not csrf:
            # Some older QSEoW builds return the token only as a query param
            # redirect or in a different header casing — log and continue if
            # we at least have a session cookie, callers will fail loudly on
            # the WS handshake if it turns out CSWSH protection is active.
            logger.warning(
                "csrftoken response did not include a 'qlik-csrf-token' header. "
                "This is fine on pre-Nov-2024 Qlik versions but will cause 403 "
                "on newer releases."
            )

        cookie_name, cookie_value = self._pick_session_cookie(resp)
        if not cookie_value:
            raise JwtBootstrapError(
                "csrftoken response did not set a Qlik session cookie. "
                f"Cookies received: {list(resp.cookies.keys()) or 'none'}. "
                "Check that QLIK_SERVER_URL points at the JWT virtual proxy "
                "rather than the central proxy; if it does, the proxy's "
                "'Session cookie header name' in QMC has been renamed to "
                "something this server cannot recognise — set it back to a "
                "name containing 'Qlik'."
            )

        self._cookie_name = cookie_name
        self._cookie_value = cookie_value
        # Store "missing" as None (not ""); the public csrf_token property
        # stays Optional[str] and downstream code uses truthiness checks.
        self._csrf_token = csrf or None
        self._fetched_at = time.time()
        logger.info(
            "JWT session bootstrap OK (cookie=%s, csrf_present=%s)",
            cookie_name, bool(csrf),
        )

    def _pick_session_cookie(self, resp: httpx.Response) -> tuple[Optional[str], Optional[str]]:
        """
        Extract the Qlik session cookie from a bootstrap response.

        The conventional name is ``X-Qlik-Session*``, but QMC lets an admin
        rename it per virtual proxy, and a load balancer in front of Qlik
        adds cookies of its own. So the name is matched in three widening
        steps rather than assumed.
        """
        names = list(resp.cookies.keys())

        # 1. The conventional name.
        for name in names:
            if name.lower().startswith(_QLIK_SESSION_COOKIE_PREFIX.lower()):
                return name, resp.cookies.get(name)

        # 2. A renamed Qlik cookie still tends to say so.
        for name in names:
            if "qlik" in name.lower():
                return name, resp.cookies.get(name)

        # 3. Exactly one cookie — it can only be the session.
        if len(names) == 1:
            return names[0], resp.cookies.get(names[0])

        return None, None
