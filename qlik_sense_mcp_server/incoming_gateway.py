"""Experimental bearer-JWT MCP gateway with process-isolated Qlik state.

Run separately: ``python -m qlik_sense_mcp_server.incoming_gateway``.
Requires POSIX Unix sockets. Test with the target MCP SDK and Qlik before use.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import secrets
import shutil
import sys
import tempfile
import time

import anyio
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .jwt_validity import InvalidToken, token_expiration
from .token_context_cache import CacheBusy, TokenContextCache

_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="qlik-mcp-"))
os.chmod(_ROOT, 0o700)
_TIMEOUT = float(os.getenv("QLIK_INCOMING_START_TIMEOUT", "120"))
_loop = None


class _Worker:
    @classmethod
    async def start(cls, token: str) -> "_Worker":
        worker = object.__new__(cls)
        worker.socket = str(_ROOT / (secrets.token_hex(16) + ".sock"))
        worker.loop = asyncio.get_running_loop()
        worker.process = None
        env = os.environ.copy()
        env.pop("QLIK_JWT_TOKEN", None)
        env.pop("QLIK_PASSWORD", None)
        try:
            worker.process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "qlik_sense_mcp_server.incoming_worker", worker.socket,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env,
            )
            worker.process.stdin.write((token + "\n").encode("ascii"))
            await worker.process.stdin.drain()
            worker.process.stdin.close()
            line = await asyncio.wait_for(worker.process.stdout.readline(), timeout=_TIMEOUT)
            if line != b"READY\n" or worker.process.returncode is not None:
                raise RuntimeError("Qlik authentication or worker startup failed")
            return worker
        except BaseException:
            await worker.stop()
            raise

    async def stop(self) -> None:
        if self.process is not None and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            except ProcessLookupError:
                pass
        try:
            os.unlink(self.socket)
        except FileNotFoundError:
            pass

    def close(self) -> None:
        if self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.stop(), self.loop)
            future.result(timeout=10)


def _factory(token: str) -> _Worker:
    if _loop is None:
        raise RuntimeError("Gateway has not started")
    future = asyncio.run_coroutine_threadsafe(_Worker.start(token), _loop)
    try:
        return future.result(timeout=_TIMEOUT + 15)
    except BaseException:
        future.cancel()
        raise


_cache = TokenContextCache(
    _factory,
    max_entries=int(os.getenv("QLIK_INCOMING_MAX_CONTEXTS", "128")),
    idle_seconds=float(os.getenv("QLIK_INCOMING_IDLE_SECONDS", "900")),
    max_age_seconds=float(os.getenv("QLIK_INCOMING_MAX_AGE_SECONDS", "0")),
)


def _error(status: int, text: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return JSONResponse({"error": text}, status_code=status, headers=headers)


async def _release(upstream, client, lease):
    # StreamingResponse may cancel the stream on disconnect. Ensure the
    # client and cache lease are released even inside a cancelled task group.
    with anyio.CancelScope(shield=True):
        try:
            if upstream is not None:
                await upstream.aclose()
        finally:
            try:
                if client is not None:
                    await client.aclose()
            finally:
                await asyncio.to_thread(lease.__exit__, None, None, None)


async def endpoint(request):
    authorization = request.headers.getlist("authorization")
    if len(authorization) != 1:
        return _error(401, "Bearer token required")
    scheme, separator, token = authorization[0].partition(" ")
    if not separator or scheme.lower() != "bearer" or not token or token.strip() != token:
        return _error(401, "Invalid bearer token")
    try:
        expires_at = token_expiration(token)
        lease = _cache.lease(token)
        acquisition = asyncio.create_task(asyncio.to_thread(lease.__enter__))
        try:
            worker = await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                try:
                    await acquisition
                except Exception:
                    pass
                else:
                    await asyncio.to_thread(lease.__exit__, None, None, None)
            raise
    except InvalidToken:
        return _error(401, "Invalid or expired bearer token")
    except CacheBusy:
        return _error(503, "Qlik session capacity reached")
    except Exception:
        return _error(401, "Qlik authentication failed or unavailable")

    client = None
    upstream = None
    handed_off = False
    try:
        try:
            token_expiration(token)
        except InvalidToken:
            return _error(401, "Invalid or expired bearer token")
        if worker.process.returncode is not None:
            await asyncio.to_thread(_cache.invalidate, token)
            return _error(503, "Qlik worker unavailable; retry the request")
        headers = [(k, v) for k, v in request.headers.raw
                   if k.lower() not in (b"authorization", b"host", b"connection", b"content-length",
                                        b"origin", b"cookie", b"proxy-authorization")]
        # MCP SDK 1.x permits localhost only with a port in its default host
        # allowlist. The actual worker is reached through the Unix socket;
        # this Host value is never used to select a TCP destination.
        headers.append((b"host", b"localhost:8000"))
        transport = httpx.AsyncHTTPTransport(uds=worker.socket)
        client = httpx.AsyncClient(transport=transport, timeout=None)
        req = client.build_request(request.method, "http://localhost/mcp", headers=headers,
                                   content=await request.body())
        # An upstream that hangs must not keep a request alive past token expiry.
        with anyio.move_on_after(max(0, expires_at - time.time())) as deadline:
            upstream = await client.send(req, stream=True)
        if deadline.cancel_called:
            return _error(401, "Bearer token expired")
        response_headers = [(k, v) for k, v in upstream.headers.raw
                            if k.lower() not in (b"connection", b"transfer-encoding", b"content-length")]

        async def stream():
            try:
                # The user must reconnect with a valid token after expiry.
                with anyio.move_on_after(max(0, expires_at - time.time())):
                    async for chunk in upstream.aiter_raw():
                        if time.time() >= expires_at:
                            break
                        yield chunk
            finally:
                await _release(upstream, client, lease)

        response = StreamingResponse(stream(), status_code=upstream.status_code)
        response.raw_headers = response_headers
        handed_off = True
        return response
    except asyncio.CancelledError:
        raise
    except Exception:
        await asyncio.to_thread(_cache.invalidate, token)
        return _error(502, "Qlik MCP worker failed; retry the request")
    finally:
        if not handed_off:
            await _release(upstream, client, lease)


async def startup():
    global _loop
    _loop = asyncio.get_running_loop()


async def shutdown():
    await asyncio.to_thread(_cache.close)
    shutil.rmtree(_ROOT, ignore_errors=True)


app = Starlette(routes=[Route("/mcp", endpoint, methods=["GET", "POST", "DELETE"])],
                on_startup=[startup], on_shutdown=[shutdown])


def main():
    if os.name != "posix":
        raise SystemExit("Incoming JWT gateway requires POSIX Unix sockets")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("MCP_PORT", "8000")), access_log=False)


if __name__ == "__main__":
    main()
