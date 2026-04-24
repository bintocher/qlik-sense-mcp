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
import os
import ssl
import threading
import time
from typing import Optional

import httpx

from .config import QlikSenseConfig

logger = logging.getLogger(__name__)


# Qlik default session idle timeout is 30 minutes. We refresh a little earlier
# so a borderline request never races the server-side eviction. Can be
# overridden by the ``QLIK_JWT_SESSION_TTL`` environment variable for
# deployments whose QMC session timeout differs from the default 30 minutes.
DEFAULT_JWT_SESSION_TTL_SECONDS = 25 * 60


def _ttl_from_env(default: int = DEFAULT_JWT_SESSION_TTL_SECONDS) -> int:
    raw = os.getenv("QLIK_JWT_SESSION_TTL")
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning(
            "QLIK_JWT_SESSION_TTL=%r is not an integer, falling back to %d",
            raw, default,
        )
        return default
    if v <= 0:
        logger.warning(
            "QLIK_JWT_SESSION_TTL must be positive, got %d — falling back to %d",
            v, default,
        )
        return default
    return v

# Names of session cookies to recognize when auto-detecting from Set-Cookie.
# Any cookie whose name starts with "X-Qlik-Session" is treated as a session
# cookie — Qlik uses "X-Qlik-Session" on the central proxy and
# "X-Qlik-Session-<prefix>" on virtual proxies.
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
        # Explicit arg wins over env, env wins over default.
        self._ttl = ttl_seconds if ttl_seconds is not None else _ttl_from_env()
        self._lock = threading.Lock()
        self._cookie_name: Optional[str] = config.jwt_session_cookie_override
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
            # Keep cookie_name if it was supplied via env override, reset
            # otherwise so a fresh Set-Cookie is picked up.
            if not self._config.jwt_session_cookie_override:
                self._cookie_name = None

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
                "csrftoken response did not set a Qlik session cookie. Verify "
                "that QLIK_SERVER_URL points at the JWT virtual proxy and not "
                "at the central proxy."
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

        Honors the env override ``QLIK_JWT_SESSION_COOKIE`` first. Otherwise
        scans response cookies for the conventional ``X-Qlik-Session*`` name
        and falls back to the single cookie in the response if Qlik set only
        one.
        """
        override = self._config.jwt_session_cookie_override
        if override:
            value = resp.cookies.get(override)
            if value:
                return override, value
            # Explicit override but not present → treat as misconfiguration.
            raise JwtBootstrapError(
                f"QLIK_JWT_SESSION_COOKIE={override!r} set but no such cookie "
                f"in csrftoken response. Available: {list(resp.cookies.keys())}"
            )

        # Prefer any cookie whose name starts with X-Qlik-Session.
        candidates = [
            (name, resp.cookies.get(name))
            for name in resp.cookies.keys()
            if name.lower().startswith(_QLIK_SESSION_COOKIE_PREFIX.lower())
        ]
        if candidates:
            return candidates[0]

        # Last resort — if Qlik set exactly one cookie, use it.
        if len(resp.cookies) == 1:
            name = next(iter(resp.cookies.keys()))
            return name, resp.cookies.get(name)

        return None, None
