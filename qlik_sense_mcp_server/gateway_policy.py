"""Credential-free gateway policies shared by HTTP routing and workers."""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager

import anyio


class GatewayFailure(RuntimeError):
    status = 503
    category = "upstream"

    def __init__(self):
        super().__init__(self.category)


class AuthenticationRejected(GatewayFailure):
    status = 401
    category = "authentication"


class ConfigurationFailure(GatewayFailure):
    category = "configuration"


class StartupFailure(GatewayFailure):
    category = "startup"


class AdmissionRejected(GatewayFailure):
    status = 429
    category = "startup_capacity"


class BodyTooLarge(GatewayFailure):
    status = 413
    category = "body_too_large"


class BodyTimeout(GatewayFailure):
    status = 408
    category = "body_timeout"


class CredentialExpired(GatewayFailure):
    status = 401
    category = "expired"


_HOP = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
        b"te", b"trailer", b"transfer-encoding", b"upgrade", b"content-length"}
# Never permit Connection options to remove MCP security/protocol headers.
_MCP = {b"mcp-session-id", b"mcp-protocol-version", b"last-event-id", b"authorization"}


def proxy_headers(raw, *, request=False):
    """Remove hop-by-hop fields, including fields nominated by Connection.

    Hop-by-hop nomination of MCP headers is rejected, not silently ignored.
    Framing is recreated by HTTPX/ASGI for the bytes actually forwarded.
    """
    raw = [(key.lower(), value) for key, value in raw]
    nominated = set()
    for key, value in raw:
        if key == b"connection":
            nominated.update(part.strip().lower() for part in value.split(b",") if part.strip())
    if nominated & _MCP:
        raise ValueError("Connection nominates a protected header")
    blocked = _HOP | nominated
    if request:
        blocked |= {b"authorization", b"host", b"origin", b"cookie", b"forwarded",
                    b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto"}
    else:
        blocked |= {b"set-cookie", b"authorization"}
    clean = [(key, value) for key, value in raw if key not in blocked]
    if request:
        clean.append((b"host", b"localhost:8000"))
    return clean


async def read_body(request, *, expires_at, max_bytes, timeout):
    """Bound upload time and memory before allocating a Qlik worker lease."""
    import time
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            raise ValueError("Invalid Content-Length") from None
        if declared < 0:
            raise ValueError("Invalid Content-Length")
        if declared > max_bytes:
            raise BodyTooLarge()
    remaining = expires_at - time.time()
    if remaining <= 0:
        raise CredentialExpired()
    data = bytearray()
    with anyio.move_on_after(min(timeout, remaining)) as scope:
        async for chunk in request.stream():
            if len(data) + len(chunk) > max_bytes:
                raise BodyTooLarge()
            data.extend(chunk)
    if scope.cancel_called:
        if time.time() >= expires_at:
            raise CredentialExpired()
        raise BodyTimeout()
    if time.time() >= expires_at:
        raise CredentialExpired()
    return bytes(data)


class StartupAdmission:
    """Bound unauthenticated starts; cache only short-lived negative outcomes.

    The registry stores HMAC fingerprints, never JWTs. A rejected start never
    authorizes a request and never evicts an authenticated worker.
    """

    def __init__(self, *, concurrent=4, per_minute=30, failure_seconds=10, max_failures=1024):
        if concurrent < 1 or per_minute < 1 or failure_seconds <= 0 or max_failures < 1:
            raise ValueError("Invalid startup admission limits")
        self.concurrent = concurrent
        self.per_minute = per_minute
        self.failure_seconds = failure_seconds
        self.max_failures = max_failures
        self._lock = threading.Lock()
        self._active = 0
        self._starts = deque()
        self._failures = OrderedDict()
        self._key = os.urandom(32)

    @contextmanager
    def start(self, token):
        key = hmac.new(self._key, token.encode("ascii"), hashlib.sha256).digest()
        with self._lock:
            now = time.monotonic()
            while self._starts and self._starts[0] <= now - 60:
                self._starts.popleft()
            for failed, (until, _) in list(self._failures.items()):
                if until <= now:
                    self._failures.pop(failed)
            failure = self._failures.get(key)
            if failure:
                raise failure[1]()
            if self._active >= self.concurrent or len(self._starts) >= self.per_minute:
                raise AdmissionRejected()
            self._active += 1
            self._starts.append(now)
        try:
            yield
        except GatewayFailure as exc:
            if not isinstance(exc, AdmissionRejected):
                with self._lock:
                    self._failures[key] = (time.monotonic() + self.failure_seconds, type(exc))
                    while len(self._failures) > self.max_failures:
                        self._failures.popitem(last=False)
            raise
        finally:
            with self._lock:
                self._active -= 1
