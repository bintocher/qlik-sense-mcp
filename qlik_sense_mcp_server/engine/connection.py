"""WebSocket transport for the Engine API: connect, keep, speak JSON-RPC.

Everything that owns the socket lives here — the handshake and its
greeting frames, the liveness rule that must not use a ping, the single
cached document, and the two ways to send requests (one at a time, or a
pipelined batch). Higher layers only ever call send_request /
send_requests_pipelined and trust that a live socket is behind them.
"""

from ..config import (
    DEFAULT_WS_IDLE_PROBE_AFTER,
    DEFAULT_WS_PROBE_TIMEOUT,
    DEFAULT_WS_GREETING_TIMEOUT,
    AUTH_MODE_JWT,
)
from ..exceptions import QlikConnectionError, QlikEngineError, QlikSessionLimitError
from ..jwt_session import JwtBootstrapError
from contextlib import contextmanager
from typing import Dict, List, Any, Optional
import json
import logging
import ssl
import threading
import time
import uuid
import websocket

logger = logging.getLogger(__name__)

# Guards creation of a per-instance transaction lock, nothing else.
_LOCK_CREATION_GUARD = threading.Lock()


class EngineConnectionMixin:
    def _transaction_lock(self) -> threading.RLock:
        """This instance's transaction lock, created on first use.

        Not a class attribute: a shared default would silently couple every
        partially-constructed client in the process — two unrelated objects
        would serialise against each other, and a future change could have
        them sharing lock state outright. Instances built without __init__
        (test doubles) get their own here rather than inheriting one.
        """
        lock = self.__dict__.get("_lock")
        if lock is None:
            with _LOCK_CREATION_GUARD:
                lock = self.__dict__.get("_lock")
                if lock is None:
                    lock = threading.RLock()
                    self.__dict__["_lock"] = lock
        return lock

    @contextmanager
    def transaction(self):
        """Hold the Engine session for one logical operation.

        The server keeps a single WebSocket and a single open document, but
        the Streamable HTTP transport happily serves several MCP clients at
        once. Without this, two concurrent tool calls interleave on the same
        socket: strict id-matching means each discards the other's reply as
        stale, `ensure_app` can switch documents between one call's
        `CreateSessionObject` and its `GetLayout`, and a reconnect in one
        thread leaves the other holding a closed socket.

        Locking a single `send_request` is not enough — the unit that has
        to be atomic is the whole chain from `ensure_app` to the last
        request of the tool. Reentrant, so nested use inside the client is
        free.

        The cost is deliberate: heavy calls serialise. Qlik gives this
        session one document anyway, so the parallelism was never real —
        it only corrupted results.
        """
        with self._transaction_lock():
            yield self

    @contextmanager
    def session_object(self, app_handle: int, definition: Dict[str, Any],
                       timeout: Optional[float] = None):
        """Create a session object and always destroy it again.

        Engine keeps a session object alive for the rest of the session,
        holding its result set in memory, until it is explicitly
        destroyed. Cleanup written after the read — rather than in a
        `finally` — leaks the object on every early return and every
        exception, which is most of the interesting paths.

        The id is generated per call: reusing one that was destroyed
        moments ago can hand back the previous calculation instead of
        evaluating this one.

        Yields the object's handle, or raises whatever Engine said.
        """
        info = definition.setdefault("qInfo", {})
        info["qId"] = f"{info.get('qId', 'obj')}-{uuid.uuid4().hex[:12]}"
        object_id = info["qId"]

        result = self.send_request("CreateSessionObject", [definition],
                                   handle=app_handle, timeout=timeout)
        handle = (result.get("qReturn") or {}).get("qHandle")
        if handle is None:
            raise QlikEngineError(
                f"Engine did not return a handle for session object "
                f"{object_id!r}: {result}")
        try:
            yield handle
        finally:
            # A dead socket has nobody to tell; anything else must not
            # mask the original failure.
            if self.ws is not None:
                try:
                    self.send_request("DestroySessionObject", [object_id],
                                      handle=app_handle)
                except Exception as cleanup_error:
                    logger.warning("DestroySessionObject(%s) failed: %s",
                                   object_id, cleanup_error)

    def _get_next_request_id(self) -> int:
        """Get next request ID."""
        self.request_id += 1
        return self.request_id

    def connect(self, app_id: Optional[str] = None) -> None:
        """
        Connect to Engine API via WebSocket.

        In certificate mode, connects directly to the Engine port (4747) with
        an X-Qlik-User impersonation header and a loaded client cert chain.

        In JWT mode, goes through the virtual proxy on 443:
        ``wss://<host>/<vp_prefix>/app/<app_guid>``. A bootstrap call to
        ``/qps/csrftoken`` happens first (via ``JwtSession.ensure_standalone``)
        so the WS upgrade can carry the resulting session cookie, the
        ``qlik-csrf-token`` anti-CSWSH header, and a valid ``Origin`` — which
        is what Qlik November 2024+ explicitly requires. We do NOT send
        ``Authorization: Bearer`` on the upgrade request because CSWSH
        protection rejects exactly that.

        If `app_id` is provided, the per-app endpoint `/app/<app_id>` is tried
        first — this is the Qlik-recommended way that binds the session to a
        specific document immediately and avoids an extra OpenDoc round-trip.
        The global `/app/engineData` endpoint is still tried as a fallback so
        that global calls (GetDocList, etc.) keep working.
        """
        from urllib.parse import quote, urlparse
        is_jwt = self.config.auth_mode == AUTH_MODE_JWT
        # For JWT mode we preserve the full netloc (host + optional port) so
        # deployments on non-standard ports like 8443 keep working.
        # Certificate mode builds its own host:port below since it always
        # appends the Engine port explicitly.
        parsed_url = urlparse(self.config.server_url)
        server_netloc = parsed_url.netloc or self.config.qlik_hostname
        server_scheme = parsed_url.scheme or "https"
        server_host = self.config.qlik_hostname  # bare hostname, used for cert mode

        # In JWT mode the CSRF token must be appended to the WS URL as a query
        # parameter — Qlik November 2024+ rejects the upgrade with 403 if the
        # anti-CSWSH token is only sent as an HTTP header. Bootstrap the
        # session up-front so we know the token value when building URLs.
        jwt_csrf_qs = ""
        if is_jwt:
            if self.jwt_session is None:
                raise QlikConnectionError(
                    "JWT mode requires a JwtSession — check server._init_clients wiring."
                )
            try:
                self.jwt_session.ensure_standalone()
            except JwtBootstrapError as exc:
                raise QlikConnectionError(f"JWT session bootstrap failed: {exc}") from exc
            if self.jwt_session.csrf_token:
                jwt_csrf_qs = f"?qlik-csrf-token={quote(self.jwt_session.csrf_token, safe='')}"

        # Build endpoint list — per-app first if app_id is given.
        endpoints_all: List[str] = []
        if is_jwt:
            # Via the virtual proxy, following the scheme the operator
            # configured: `https://host/jwt` connects with wss://,
            # `http://host/jwt` with ws://. Forcing wss:// regardless — which
            # this used to do — makes the server unusable on a deployment
            # whose proxy serves plain HTTP, and that is a real configuration:
            # Qlik listens on 80 and 443 both, and 443 is the one that breaks
            # when the proxy certificate is unhappy.
            ws_scheme = "ws" if server_scheme == "http" else "wss"
            prefix = self.config.virtual_proxy_prefix
            if app_id:
                enc = quote(app_id, safe="")
                endpoints_all.append(
                    f"{ws_scheme}://{server_netloc}/{prefix}/app/{enc}{jwt_csrf_qs}"
                )
            endpoints_all.extend([
                f"{ws_scheme}://{server_netloc}/{prefix}/app/engineData{jwt_csrf_qs}",
                f"{ws_scheme}://{server_netloc}/{prefix}/app{jwt_csrf_qs}",
            ])
        else:
            if app_id:
                enc = quote(app_id, safe="")
                endpoints_all.append(
                    f"wss://{server_host}:{self.config.engine_port}/app/{enc}"
                )
            endpoints_all.extend([
                f"wss://{server_host}:{self.config.engine_port}/app/engineData",
                f"wss://{server_host}:{self.config.engine_port}/app",
                f"ws://{server_host}:{self.config.engine_port}/app/engineData",
                f"ws://{server_host}:{self.config.engine_port}/app",
            ])
        # ws_retries controls how many fallback endpoints to try; always at
        # least 1, and if app_id is given we add +1 to include the per-app URL
        # without starving the fallback list.
        retry_budget = max(1, self.ws_retries + (1 if app_id else 0))
        endpoints_to_try = endpoints_all[: min(retry_budget, len(endpoints_all))]

        # Setup SSL context
        ssl_context = ssl.create_default_context()
        if not self.config.verify_ssl:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        # In JWT mode we intentionally do NOT load a client certificate —
        # auth is handled by the VP via the bearer token + session cookie.
        if (not is_jwt
                and self.config.client_cert_path
                and self.config.client_key_path):
            ssl_context.load_cert_chain(
                self.config.client_cert_path, self.config.client_key_path
            )

        if self.config.ca_cert_path:
            ssl_context.load_verify_locations(self.config.ca_cert_path)

        # Headers for authentication
        if is_jwt:
            # jwt_session is already bootstrapped above — we needed the CSRF
            # token at URL-build time.
            headers = [
                f"Cookie: {self.jwt_session.cookie_header()}",
                # Send qlik-csrf-token both as a header and as a query
                # parameter (appended to the URL above). Qlik November 2024+
                # requires the query-parameter form for WebSocket upgrades —
                # the header alone still 403s under CSWSH protection.
                *([f"qlik-csrf-token: {self.jwt_session.csrf_token}"]
                  if self.jwt_session.csrf_token else []),
                # Origin must match an entry in the VP Host allow list from
                # QMC. Qlik accepts the bare hostname entry and compares
                # case-insensitively against the Origin hostname. We reuse
                # the scheme/netloc from the configured server_url so
                # non-standard ports are preserved and http vs https is not
                # hardcoded.
                f"Origin: {server_scheme}://{server_netloc}",
            ]
        else:
            headers = [
                f"X-Qlik-User: UserDirectory={self.config.user_directory}; UserId={self.config.user_id}"
            ]

        last_error = None
        jwt_retried = False  # one re-bootstrap per connect() call
        i = 0
        while i < len(endpoints_to_try):
            url = endpoints_to_try[i]
            try:
                if url.startswith("wss://"):
                    self.ws = websocket.create_connection(
                        url, sslopt={"context": ssl_context}, header=headers, timeout=self.ws_timeout_seconds
                    )
                else:
                    self.ws = websocket.create_connection(
                        url, header=headers, timeout=self.ws_timeout_seconds
                    )

                # Read the greeting notifications. Engine answers a fresh
                # socket with OnAuthenticationInformation + OnConnected, but
                # it can also answer with a fatal one — most importantly
                # OnMaxParallelSessionsExceeded, sent when the user already
                # holds Qlik's per-user session limit (5 by default). That
                # frame is followed by an immediate close, so treating the
                # first frame as "session established" turns a plain quota
                # error into "Failed to parse WebSocket frame" on the next
                # call, which says nothing about the real cause.
                self._consume_greeting()
                return  # Success
            except QlikSessionLimitError:
                # Quota, not a bad endpoint — every fallback URL would be
                # refused the same way. Surface it as-is.
                self._kill_socket()
                raise
            except websocket.WebSocketBadStatusException as e:
                last_error = e
                if self.ws:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
                # On a stale JWT session we see 401 (cookie expired) or 403
                # (CSRF stale under CSWSH). Re-bootstrap once and retry the
                # same URL — symmetric to the QRS 401-retry path. If we are
                # not in JWT mode or we already retried, fall through to the
                # next fallback endpoint.
                if (is_jwt
                        and not jwt_retried
                        and self.jwt_session is not None
                        and getattr(e, "status_code", None) in (401, 403)):
                    jwt_retried = True
                    self.jwt_session.invalidate()
                    try:
                        self.jwt_session.ensure_standalone()
                    except JwtBootstrapError as boot_exc:
                        raise QlikConnectionError(
                            f"JWT session re-bootstrap after {e.status_code} failed: {boot_exc}"
                        ) from boot_exc
                    # Refresh csrf query-param, cookie header, rebuild URL list.
                    new_csrf = self.jwt_session.csrf_token
                    new_qs = (f"?qlik-csrf-token={quote(new_csrf, safe='')}"
                              if new_csrf else "")
                    endpoints_to_try = [
                        u.split("?", 1)[0] + new_qs for u in endpoints_to_try
                    ]
                    headers = [
                        f"Cookie: {self.jwt_session.cookie_header()}",
                        *([f"qlik-csrf-token: {new_csrf}"] if new_csrf else []),
                        f"Origin: {server_scheme}://{server_netloc}",
                    ]
                    continue  # retry same i
                i += 1
            except Exception as e:
                last_error = e
                if self.ws:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
                i += 1

        raise QlikConnectionError(
            f"Failed to connect to Engine API. Last error: {str(last_error)}"
        )

    # Engine notifications that mean the socket is dead on arrival. The
    # session-limit one is by far the most common in practice: every
    # reconnect leaves the previous Engine session alive until the virtual
    # proxy's inactivity timeout, so a handful of retries is enough to hit
    # the per-user cap.
    _FATAL_GREETINGS = {
        "OnMaxParallelSessionsExceeded": (
            "Qlik refused the Engine session: the per-user limit of concurrent "
            "sessions is exhausted. Existing sessions stay alive until the "
            "virtual proxy inactivity timeout expires. Close them (QPS "
            "delete-user-sessions) or wait for the timeout, and avoid running "
            "several MCP servers or parallel tool calls against one account."
        ),
        "OnSessionClosed": "Qlik closed the Engine session immediately after connect.",
        "OnSessionTimedOut": "Qlik reported the Engine session as timed out on connect.",
        "OnLicenseAccessDenied": "Qlik denied the Engine session: no license access for this user.",
    }

    def _consume_greeting(self) -> None:
        """Read Engine's greeting frames and fail loudly on a fatal one.

        Engine answers a fresh socket with notifications (no `id`): usually
        OnAuthenticationInformation then OnConnected. Reading until
        OnConnected both confirms the session and leaves the recv buffer
        empty for the first real request. A fatal greeting is turned into a
        typed error here, because the socket is closed right after it and
        every later call would otherwise fail with an unrelated parse error.
        """
        import socket as _socket

        # Greeting frames arrive immediately; a long wait here would only
        # delay the fallback to the next endpoint.
        greeting_timeout = min(self.ws_greeting_timeout, self.ws_timeout_seconds)
        self._set_socket_timeout(greeting_timeout)
        try:
            for _ in range(10):
                try:
                    data = self.ws.recv()
                except (_socket.timeout, TimeoutError):
                    # No OnConnected within the window: keep the socket and
                    # let the first real request decide. Pre-1.7.2 behaviour.
                    logger.debug("No OnConnected within %.1fs, proceeding anyway",
                                 greeting_timeout)
                    return

                if not data:
                    raise QlikConnectionError(
                        "Engine closed the WebSocket immediately after connect "
                        "without sending a greeting."
                    )

                try:
                    frame = json.loads(data)
                except Exception:
                    logger.debug("Non-JSON greeting frame, ignoring: %r", data[:120])
                    continue

                method = frame.get("method")
                if method in self._FATAL_GREETINGS:
                    params = frame.get("params") or {}
                    message = (
                        f"{self._FATAL_GREETINGS[method]} "
                        f"(Engine notification {method}, "
                        f"severity={params.get('severity', 'unknown')})"
                    )
                    if method == "OnMaxParallelSessionsExceeded":
                        raise QlikSessionLimitError(message)
                    raise QlikConnectionError(message)
                if method == "OnConnected":
                    return
                # OnAuthenticationInformation and friends: keep reading.
        finally:
            self._set_socket_timeout(self.ws_timeout_seconds)

    def disconnect(self) -> None:
        """Disconnect from Engine API."""
        self._invalidate_cache()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def _invalidate_cache(self) -> None:
        """Reset cached app state."""
        self._cached_app_id = None
        self._cached_app_handle = -1
        self._cached_has_data = False

    # Class-level defaults so partially constructed instances (test doubles,
    # subclasses that skip __init__) still answer the liveness question.
    # Instances override these from the environment in __init__.
    ws_idle_probe_after: float = DEFAULT_WS_IDLE_PROBE_AFTER
    ws_probe_timeout: float = DEFAULT_WS_PROBE_TIMEOUT
    ws_greeting_timeout: float = DEFAULT_WS_GREETING_TIMEOUT
    _last_successful_io: float = 0.0

    def _is_connected(self) -> bool:
        """Check whether the cached WebSocket is still usable.

        Deliberately does NOT send a WebSocket ping. Qlik's Proxy Service
        does not relay ping/pong to the Engine: through a virtual proxy the
        first request after a ping never gets an answer and the call blocks
        for the whole QLIK_WS_TIMEOUT, then reconnects — which is how a
        working JWT session degraded into "every second tool call hangs for
        three minutes" and piled up Engine sessions until the per-user limit
        refused new ones. Verified on Qlik 31.60: ping is harmless on a
        direct Engine socket (port 4747, certificate mode) and fatal through
        the proxy (443, JWT mode).

        Instead: a socket that answered recently is trusted as-is, and an
        idle one is probed with a cheap real request, which is both more
        honest than a ping (it proves the Engine answers, not just that the
        TCP socket is open) and safe on both transports.
        """
        if not self.ws or not self.ws.connected:
            return False
        if (time.monotonic() - self._last_successful_io) <= self.ws_idle_probe_after:
            return True
        try:
            # send_request kills the socket itself on failure, so a False
            # here always leaves a clean state for the caller to reconnect.
            self.send_request("EngineVersion", [], handle=-1,
                              timeout=min(self.ws_probe_timeout, self.ws_timeout_seconds))
            return True
        except Exception as exc:
            logger.debug("Idle connection probe failed, reconnecting: %s", exc)
            return False

    def ensure_app(self, app_id: str, no_data: bool = False) -> int:
        """
        Get app handle, reusing cached connection when possible.

        Returns app_handle. Reconnects automatically on stale connections.
        If cached connection was opened without data but data is now needed,
        reconnects with data.
        """
        needs_data = not no_data

        # Check if we can reuse the cached connection
        if (self._cached_app_id == app_id
                and self._cached_app_handle != -1
                and (not needs_data or self._cached_has_data)
                and self._is_connected()):
            logger.debug("Reusing cached connection for app %s (handle=%d)",
                         app_id, self._cached_app_handle)
            return self._cached_app_handle

        # Need to (re)connect — close old connection first
        logger.info("Opening new Engine connection for app %s (no_data=%s)", app_id, no_data)
        self.disconnect()
        # Pass app_id so connect() uses the per-app WebSocket endpoint first
        self.connect(app_id=app_id)

        handle, has_data = self._open_doc_verified(app_id, no_data=no_data)

        # If we need data but Qlik joined us to (or kept us on) a no-data
        # session — e.g. session sharing attached to an existing no-data
        # session for this user+app — retry once against a fresh connection
        # before giving up. Trusting the requested `no_data` flag here is
        # exactly the bug: GetAppLayout/GetAppProperties succeed regardless,
        # so a stale no-data handle silently yields an empty data model
        # later (GetTablesAndKeys returns qtr: []) instead of an error.
        if needs_data and not has_data:
            logger.warning(
                "App %s opened without data despite no_data=False; "
                "retrying with a fresh connection", app_id,
            )
            self.disconnect()
            self.connect(app_id=app_id)
            handle, has_data = self._open_doc_verified(app_id, no_data=no_data)
            if not has_data:
                self.disconnect()
                raise QlikEngineError(
                    f"App {app_id} remains opened without data (qIsOpenedWithoutData=True) "
                    f"after retry — the data model will appear empty. This usually means "
                    f"the Engine session was shared/attached to an existing no-data session "
                    f"for this user+app."
                )

        self._cached_app_id = app_id
        self._cached_app_handle = handle
        self._cached_has_data = has_data
        return handle

    def _open_doc_verified(self, app_id: str, no_data: bool) -> "tuple[int, bool]":
        """Open the doc and verify the real data state via GetAppLayout.

        Returns (handle, has_data) where has_data reflects Engine's own
        `qIsOpenedWithoutData` flag rather than the requested `no_data` —
        the two can disagree when the session is shared/attached.
        """
        app_result = self.open_doc(app_id, no_data=no_data)
        handle = app_result.get("qReturn", {}).get("qHandle", -1)
        if handle == -1:
            self.disconnect()
            raise Exception(f"Failed to open app {app_id}: {app_result}")

        try:
            layout = self.send_request("GetAppLayout", [], handle=handle)
            opened_without_data = bool(
                layout.get("qLayout", {}).get("qIsOpenedWithoutData", no_data)
            )
        except Exception:
            # If we can't even verify, fall back to trusting the request —
            # better than blocking on an unrelated GetAppLayout hiccup.
            opened_without_data = no_data

        return handle, not opened_without_data

    def _set_socket_timeout(self, timeout: float) -> None:
        """Set timeout on the underlying WebSocket socket."""
        if self.ws and self.ws.sock:
            self.ws.sock.settimeout(timeout)

    def _kill_socket(self) -> None:
        """Force-close the WebSocket and invalidate the cached app handle.

        Called whenever the socket is in an unrecoverable state — after a
        timeout, after a stray-frame parse error, etc. The next ensure_app()
        will open a fresh connection. Without this, a single timed-out
        request leaves stale data sitting in the recv buffer that the next
        send_request would mistake for its own response.
        """
        self._invalidate_cache()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def send_request(
        self, method: str, params: List[Any] = None, handle: int = -1,
        timeout: float = None,
    ) -> Dict[str, Any]:
        """
        Send JSON-RPC 2.0 request to Qlik Engine API and return response.

        Strict id-matching: every received frame is parsed and only the
        frame whose `id` matches our `req_id` is treated as the answer.
        Stray frames (notifications, late replies to a previous timed-out
        call, OnConnected events) are logged and skipped.

        On any timeout or socket error the WebSocket is force-closed via
        `_kill_socket()` so the next call gets a fresh connection — this
        prevents the "WebSocket recv() failed" cascade after a single slow
        hypercube.

        Args:
            method: Engine API method name
            params: Method parameters list
            handle: Object handle for scoped operations (-1 for global)
            timeout: Override socket timeout for this request (seconds)
        """
        import socket as _socket
        if not self.ws:
            raise ConnectionError("Not connected to Engine API")

        effective_timeout = timeout if timeout is not None else self.ws_timeout_seconds
        if timeout is not None:
            self._set_socket_timeout(timeout)

        req_id = self._get_next_request_id()
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "handle": handle,
            "method": method,
            "params": params or [],
        }

        response: Optional[Dict[str, Any]] = None
        try:
            try:
                self.ws.send(json.dumps(request))
            except (_socket.timeout, TimeoutError) as e:
                self._kill_socket()
                raise TimeoutError(
                    f"WebSocket send() timed out after {effective_timeout:.1f}s "
                    f"for Engine method '{method}' (handle={handle}, req_id={req_id})"
                ) from e
            except Exception as e:
                self._kill_socket()
                raise ConnectionError(
                    f"WebSocket send() failed for Engine method '{method}' "
                    f"(handle={handle}, req_id={req_id}): {type(e).__name__}: {e}"
                ) from e

            # Strict id-matching loop. Skip notifications and stale replies
            # from previous timed-out requests until we see our own id.
            stray_frames = 0
            try:
                while True:
                    data = self.ws.recv()
                    try:
                        frame = json.loads(data)
                    except Exception as parse_err:
                        # Unparseable frame — socket state is suspect.
                        self._kill_socket()
                        raise ConnectionError(
                            f"Failed to parse WebSocket frame for method "
                            f"'{method}' (req_id={req_id}): {parse_err}"
                        ) from parse_err

                    frame_id = frame.get("id")
                    if frame_id == req_id:
                        response = frame
                        # Engine answered: the socket is provably alive, so
                        # _is_connected can skip its probe for a while.
                        self._last_successful_io = time.monotonic()
                        break

                    # Stray frame: notification (no id), or a late reply to
                    # an earlier request whose recv() timed out. Log and
                    # keep reading.
                    if frame_id is None:
                        # Engine notifications: OnConnected, OnAuthenticated,
                        # OnSessionTimedOut, change events. Harmless.
                        logger.debug(
                            "send_request[%s req_id=%d]: skipping notification: %s",
                            method, req_id, frame.get("method", "<no-method>"),
                        )
                    else:
                        logger.warning(
                            "send_request[%s req_id=%d]: discarding stale frame "
                            "with id=%s (likely a late reply to a previously "
                            "timed-out request)",
                            method, req_id, frame_id,
                        )
                    stray_frames += 1
                    if stray_frames > 100:
                        # Sanity cap. Something is very wrong; bail out.
                        self._kill_socket()
                        raise ConnectionError(
                            f"send_request[{method} req_id={req_id}]: "
                            f"received {stray_frames} stray frames without a "
                            f"matching id, killing connection"
                        )
            except (_socket.timeout, TimeoutError) as e:
                self._kill_socket()
                raise TimeoutError(
                    f"WebSocket recv() timed out after {effective_timeout:.1f}s "
                    f"waiting for response to Engine method '{method}' "
                    f"(handle={handle}, req_id={req_id}). "
                    f"Increase QLIK_WS_TIMEOUT if the operation is legitimately heavy."
                ) from e
            except ConnectionError:
                raise
            except Exception as e:
                self._kill_socket()
                raise ConnectionError(
                    f"WebSocket recv() failed for Engine method '{method}' "
                    f"(handle={handle}, req_id={req_id}): {type(e).__name__}: {e}"
                ) from e
        finally:
            if timeout is not None and self.ws is not None:
                try:
                    self._set_socket_timeout(self.ws_timeout_seconds)
                except Exception:
                    pass

        if response is None:
            # Should be unreachable — the loop only exits via break or raise.
            raise ConnectionError(
                f"send_request[{method} req_id={req_id}]: no response received"
            )

        if "error" in response:
            raise Exception(
                f"Engine API error for method '{method}' (handle={handle}): {response['error']}"
            )

        return response.get("result", {})

    def send_requests_pipelined(
        self, requests: List[Dict[str, Any]], timeout: float = None,
        raise_on_error: bool = True,
    ) -> List[Any]:
        """
        Send multiple independent JSON-RPC requests back-to-back, without
        waiting for each response before sending the next, then collect all
        responses.

        Why this is safe: the Qlik Engine JSON API requires every request to
        carry its own numeric `id` specifically so the client can match
        responses to requests out of order — see "Let's Dissect the Qlik
        Engine API - Part 1: RPC Basics" (Qlik Community). `send_request()`
        sends one request and blocks for its matching response before
        sending the next; for a batch of independent requests (e.g. N
        sibling objects on a sheet, each needing its own GetObject/GetLayout)
        that stacks N network round-trips even though nothing in the
        protocol requires it.

        What this does NOT claim: it does not change whether the Engine
        computes/queues the N requests concurrently server-side — it only
        removes client-side round-trip stacking. Whether that yields a real
        wall-clock win depends on the workload (network RTT vs. per-request
        server cost) and should be measured for the specific call site
        before being relied on, not assumed.

        Args:
            requests: list of {"method": str, "params": list|dict, "handle": int}.
            timeout: shared socket timeout applied to the whole batch
                (default: `self.ws_timeout_seconds`, same as send_request).
            raise_on_error: if True (default), raise a single Exception
                aggregating every per-request Engine error, matching
                send_request()'s raise-on-error behavior. If False, failed
                requests are returned as `Exception` instances in place of
                their result (mirrors
                `asyncio.gather(..., return_exceptions=True)`), so the
                caller can keep the successful items from a batch instead
                of losing all of them to one bad request — used by
                `_get_sheet_objects_detailed()` so one broken sheet object
                doesn't drop every other object on the same sheet.

        Returns:
            List of `result` dicts (or Exception instances when
            raise_on_error=False), in the same order as `requests`. Every
            response for this batch is drained from the socket before
            returning or raising, even on error, so the connection is never
            left holding unread frames that a later call could mistake for
            its own.
        """
        import socket as _socket
        if not self.ws:
            raise ConnectionError("Not connected to Engine API")
        if not requests:
            return []

        effective_timeout = timeout if timeout is not None else self.ws_timeout_seconds
        if timeout is not None:
            self._set_socket_timeout(timeout)

        # req_id -> index in `requests`, so responses (possibly out of
        # order) land back in the caller's original order.
        pending: Dict[int, int] = {}
        outcomes: List[Any] = [None] * len(requests)

        try:
            for idx, req in enumerate(requests):
                req_id = self._get_next_request_id()
                pending[req_id] = idx
                payload = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "handle": req.get("handle", -1),
                    "method": req["method"],
                    "params": req.get("params") or [],
                }
                try:
                    self.ws.send(json.dumps(payload))
                except (_socket.timeout, TimeoutError) as e:
                    self._kill_socket()
                    raise TimeoutError(
                        f"WebSocket send() timed out after {effective_timeout:.1f}s "
                        f"mid-batch at item {idx} ('{req['method']}')"
                    ) from e
                except Exception as e:
                    self._kill_socket()
                    raise ConnectionError(
                        f"WebSocket send() failed mid-batch at item {idx} "
                        f"('{req['method']}'): {type(e).__name__}: {e}"
                    ) from e

            stray_frames = 0
            stray_cap = max(100, 20 * len(requests))
            while pending:
                try:
                    data = self.ws.recv()
                except (_socket.timeout, TimeoutError) as e:
                    self._kill_socket()
                    raise TimeoutError(
                        f"WebSocket recv() timed out after {effective_timeout:.1f}s "
                        f"waiting for {len(pending)}/{len(requests)} outstanding "
                        f"pipelined responses. Increase QLIK_WS_TIMEOUT if the "
                        f"batch is legitimately heavy."
                    ) from e
                except Exception as e:
                    self._kill_socket()
                    raise ConnectionError(
                        f"WebSocket recv() failed mid-batch "
                        f"({len(pending)}/{len(requests)} still outstanding): "
                        f"{type(e).__name__}: {e}"
                    ) from e

                try:
                    frame = json.loads(data)
                except Exception as parse_err:
                    self._kill_socket()
                    raise ConnectionError(
                        f"Failed to parse WebSocket frame mid-batch "
                        f"({len(pending)}/{len(requests)} still outstanding): "
                        f"{parse_err}"
                    ) from parse_err

                frame_id = frame.get("id")
                idx = pending.pop(frame_id, None)
                if idx is None:
                    # Notification, or a late reply to an earlier
                    # timed-out request — same stray-frame handling as
                    # send_request().
                    if frame_id is None:
                        logger.debug(
                            "send_requests_pipelined: skipping notification: %s",
                            frame.get("method", "<no-method>"),
                        )
                    else:
                        logger.warning(
                            "send_requests_pipelined: discarding stale frame "
                            "with id=%s (not part of this batch)", frame_id,
                        )
                    stray_frames += 1
                    if stray_frames > stray_cap:
                        self._kill_socket()
                        raise ConnectionError(
                            f"send_requests_pipelined: received {stray_frames} "
                            f"stray frames without matching all {len(requests)} "
                            f"batch ids, killing connection"
                        )
                    continue

                if "error" in frame:
                    outcomes[idx] = Exception(
                        f"Engine API error for method "
                        f"'{requests[idx]['method']}' "
                        f"(handle={requests[idx].get('handle', -1)}): {frame['error']}"
                    )
                else:
                    outcomes[idx] = frame.get("result", {})
                # Engine answered: the socket is provably alive.
                self._last_successful_io = time.monotonic()
        finally:
            if timeout is not None and self.ws is not None:
                try:
                    self._set_socket_timeout(self.ws_timeout_seconds)
                except Exception:
                    pass

        if raise_on_error:
            errors = [o for o in outcomes if isinstance(o, Exception)]
            if errors:
                raise Exception(
                    f"send_requests_pipelined: {len(errors)}/{len(requests)} "
                    f"requests failed: {'; '.join(str(e) for e in errors)}"
                )

        return outcomes

    def get_doc_list(self) -> List[Dict[str, Any]]:
        """Get list of available documents."""
        try:
            # Connect to global engine first
            result = self.send_request("GetDocList")
            doc_list = result.get("qDocList", [])

            # Ensure we return a list even if empty
            if isinstance(doc_list, list):
                return doc_list
            else:
                return []

        except Exception as e:
            # Return empty list on error for compatibility
            return []

    def open_doc(self, app_id: str, no_data: bool = True) -> Dict[str, Any]:
        """
        Open Qlik Sense application document.

        Args:
            app_id: Application ID to open
            no_data: If True, open without loading data (faster for metadata operations)

        Returns:
            Response with document handle
        """
        try:
            if no_data:
                return self.send_request("OpenDoc", [app_id, "", "", "", True],
                                         timeout=self.ws_operation_timeout)
            else:
                return self.send_request("OpenDoc", [app_id],
                                         timeout=self.ws_operation_timeout)
        except Exception as e:
            # If app is already open, try to get existing handle
            if "already open" in str(e).lower():
                try:
                    # Try to get the already open document
                    doc_list = self.get_doc_list()
                    for doc in doc_list:
                        if doc.get("qDocId") == app_id:
                            # Return mock response with existing handle
                            return {
                                "qReturn": {
                                    "qHandle": doc.get("qHandle", -1),
                                    "qGenericId": app_id
                                }
                            }
                except Exception:
                    pass
            raise e

