"""Incoming Qlik JWT HTTP gateway. TLS settings toward Qlik are unchanged."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import pathlib
import secrets
import shutil
import sys
import tempfile
import time
from contextlib import asynccontextmanager

import anyio
import httpx
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from starlette.routing import Route

from .gateway_policy import (
    AuthenticationRejected, ConfigurationFailure, GatewayFailure,
    StartupAdmission, StartupFailure, proxy_headers, read_body,
)
from .gateway_response import OwnedProxyResponse as _ProxyResponse
from .jwt_validity import InvalidToken, token_expiration
from .token_context_cache import CacheBusy, TokenContextCache

load_dotenv()
logger = logging.getLogger(__name__)


def _positive(name, default, *, integer=False, zero=False):
    try:
        value = int(os.getenv(name, str(default))) if integer else float(os.getenv(name, str(default)))
        finite = math.isfinite(value)
    except (ValueError, OverflowError):
        raise ValueError(f"Invalid {name}") from None
    if not finite or value < 0 or (not zero and value == 0):
        raise ValueError(f"Invalid {name}")
    return value


_TIMEOUT = _positive("QLIK_INCOMING_START_TIMEOUT", 120)
_MAX_BODY = _positive("QLIK_INCOMING_MAX_BODY_BYTES", 4194304, integer=True)
_BODY_TIMEOUT = _positive("QLIK_INCOMING_BODY_TIMEOUT", 30)
_ROOT = None
_loop = None
_reaper_task = None
_workers = set()
_pending_acquisitions = set()
_admission = StartupAdmission(
    concurrent=_positive("QLIK_INCOMING_MAX_STARTS", 4, integer=True),
    per_minute=_positive("QLIK_INCOMING_STARTS_PER_MINUTE", 30, integer=True),
)


class _Worker:
    @classmethod
    async def start(cls, token):
        if _ROOT is None:
            raise StartupFailure()
        worker = object.__new__(cls)
        worker.socket = str(_ROOT / (secrets.token_hex(16) + ".sock"))
        worker.loop = asyncio.get_running_loop()
        worker.process = None
        worker.stopping = asyncio.Lock()
        env = os.environ.copy()
        env.pop("QLIK_JWT_TOKEN", None)
        env.pop("QLIK_PASSWORD", None)
        _workers.add(worker)
        try:
            worker.process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "qlik_sense_mcp_server.incoming_worker", worker.socket,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env,
            )
            worker.process.stdin.write((token + "\n").encode("ascii"))
            await worker.process.stdin.drain()
            worker.process.stdin.close()
            line = await asyncio.wait_for(worker.process.stdout.readline(), _TIMEOUT)
            try:
                status = json.loads(line).get("status")
            except (ValueError, AttributeError):
                raise StartupFailure() from None
            if status == "authentication":
                raise AuthenticationRejected()
            if status == "configuration":
                raise ConfigurationFailure()
            if status == "upstream":
                raise GatewayFailure()
            if status != "ready" or worker.process.returncode is not None:
                raise StartupFailure()
            return worker
        except BaseException:
            await worker.stop()
            raise

    async def stop(self):
        async with self.stopping:
            if self.process is not None and self.process.returncode is None:
                try:
                    self.process.terminate()
                    await asyncio.wait_for(self.process.wait(), 6)
                except asyncio.TimeoutError:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    await self.process.wait()
                except ProcessLookupError:
                    pass
            try:
                os.unlink(self.socket)
            except FileNotFoundError:
                pass
            _workers.discard(self)

    def close(self):
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.stop(), self.loop).result(timeout=15)


def _factory(token):
    with _admission.start(token):
        if _loop is None:
            raise StartupFailure()
        future = asyncio.run_coroutine_threadsafe(_Worker.start(token), _loop)
        try:
            return future.result(timeout=_TIMEOUT + 15)
        except GatewayFailure:
            raise
        except BaseException as exc:
            future.cancel()
            if not isinstance(exc, Exception):
                raise
            raise StartupFailure() from None


_cache = TokenContextCache(
    _factory,
    max_entries=_positive("QLIK_INCOMING_MAX_CONTEXTS", 128, integer=True),
    idle_seconds=_positive("QLIK_INCOMING_IDLE_SECONDS", 900),
    max_age_seconds=_positive("QLIK_INCOMING_MAX_AGE_SECONDS", 0, zero=True),
    evict_idle=False,
)


def _error(status, category):
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if status in (429, 503):
        headers["Retry-After"] = "10"
    return JSONResponse({"error": category}, status_code=status, headers=headers)


class _Resources:
    def __init__(self, lease, token):
        self.lease = lease
        self.token = token
        self.client = None
        self.upstream = None
        self.closed = False
        self.lock = asyncio.Lock()

    async def close(self, *, invalidate=False):
        with anyio.CancelScope(shield=True):
            async with self.lock:
                if self.closed:
                    return
                self.closed = True
                try:
                    if invalidate:
                        await anyio.to_thread.run_sync(_cache.invalidate, self.token)
                    if self.upstream is not None:
                        await self.upstream.aclose()
                finally:
                    try:
                        if self.client is not None:
                            await self.client.aclose()
                    finally:
                        await anyio.to_thread.run_sync(self.lease.__exit__, None, None, None)


async def _acquire(lease):
    task = asyncio.create_task(asyncio.to_thread(lease.__enter__))
    try:
        with anyio.CancelScope(shield=True):
            return await asyncio.shield(task)
    except asyncio.CancelledError:
        async def release_later():
            try:
                await task
            except Exception:
                return
            await asyncio.to_thread(lease.__exit__, None, None, None)
        cleanup = asyncio.create_task(release_later())
        _pending_acquisitions.add(cleanup)
        cleanup.add_done_callback(_pending_acquisitions.discard)
        raise


async def endpoint(request):
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return _error(401, "bearer_required")
    scheme, sep, token = values[0].partition(" ")
    if not sep or scheme.lower() != "bearer":
        return _error(401, "invalid_token")
    try:
        expires_at = token_expiration(token)
        headers = proxy_headers(request.headers.raw, request=True)
        body = await read_body(request, expires_at=expires_at, max_bytes=_MAX_BODY, timeout=_BODY_TIMEOUT)
    except InvalidToken:
        return _error(401, "invalid_or_expired_token")
    except GatewayFailure as exc:
        return _error(exc.status, exc.category)
    except ValueError:
        return _error(400, "invalid_headers")
    except ClientDisconnect:
        return _error(400, "client_disconnected")
    try:
        lease = _cache.lease(token)
        worker = await _acquire(lease)
    except InvalidToken:
        return _error(401, "invalid_or_expired_token")
    except CacheBusy:
        return _error(503, "session_capacity")
    except GatewayFailure as exc:
        logger.warning("Gateway worker start failed: %s", exc.category)
        return _error(exc.status, exc.category)
    except Exception:
        logger.error("Gateway worker start failed: internal")
        return _error(503, "startup")

    resources = _Resources(lease, token)
    handed_off = False
    invalidate = False
    try:
        token_expiration(token)
        if worker.process.returncode is not None:
            invalidate = True
            return _error(503, "worker_unavailable_reinitialize")
        resources.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=worker.socket), timeout=None)
        req = resources.client.build_request(
            request.method, "http://localhost:8000/mcp", headers=headers, content=body)
        with anyio.move_on_after(max(0, expires_at - time.time())) as deadline:
            resources.upstream = await resources.client.send(req, stream=True)
        if deadline.cancel_called:
            return _error(401, "expired")
        response_headers = [(k, v) for k, v in proxy_headers(resources.upstream.headers.raw) if k != b"cache-control"]
        response = _ProxyResponse(resources, expires_at, response_headers)
        handed_off = True
        return response
    except InvalidToken:
        return _error(401, "expired")
    except httpx.HTTPError:
        invalidate = True
        logger.warning("Gateway request failed: upstream_transport")
        return _error(502, "upstream_transport")
    except ValueError:
        invalidate = True
        return _error(502, "invalid_upstream_headers")
    finally:
        if not handed_off:
            await resources.close(invalidate=invalidate)


async def _reap_idle_contexts():
    while True:
        await asyncio.sleep(30)
        await asyncio.to_thread(_cache.reap)


async def startup():
    global _loop, _ROOT, _reaper_task
    if os.name != "posix":
        raise ValueError("incoming-jwt mode requires POSIX Unix sockets")
    from urllib.parse import urlparse
    parsed = urlparse(os.getenv("QLIK_SERVER_URL", ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("QLIK_SERVER_URL must name a Qlik JWT virtual proxy")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("QLIK_SERVER_URL must not contain credentials, query or fragment")
    _loop = asyncio.get_running_loop()
    _ROOT = pathlib.Path(tempfile.mkdtemp(prefix="qlik-mcp-"))
    os.chmod(_ROOT, 0o700)
    _reaper_task = asyncio.create_task(_reap_idle_contexts())


async def shutdown():
    global _ROOT, _reaper_task
    if _reaper_task is not None:
        _reaper_task.cancel()
        try:
            await _reaper_task
        except asyncio.CancelledError:
            pass
        _reaper_task = None
    await asyncio.to_thread(_cache.close)
    await asyncio.gather(*(worker.stop() for worker in list(_workers)), return_exceptions=True)
    if _pending_acquisitions:
        await asyncio.gather(*list(_pending_acquisitions), return_exceptions=True)
    if _ROOT is not None:
        shutil.rmtree(_ROOT, ignore_errors=True)
        _ROOT = None


@asynccontextmanager
async def lifespan(app):
    await startup()
    try:
        yield
    finally:
        await shutdown()


app = Starlette(routes=[Route("/mcp", endpoint, methods=["GET", "POST", "DELETE"])], lifespan=lifespan)


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("MCP_PORT", "8000")), access_log=False)


if __name__ == "__main__":
    main()
