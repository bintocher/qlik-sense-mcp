"""Streamable-HTTP gateway for isolated, Qlik-authenticated JWT workers.

Start separately with ``python -m qlik_sense_mcp_server.incoming_gateway``.
POSIX is required for private Unix-domain sockets. Workers authenticate an
uncached JWT with Qlik and isolate *all* original process-wide Qlik state.
This gateway is experimental until its MCP SDK integration and Qlik behavior
have been verified in the target deployment.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import secrets
import shutil
import sys
import tempfile

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .jwt_validity import InvalidToken, token_expiration
from .token_context_cache import CacheBusy, TokenContextCache


_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="qlik-mcp-"))
os.chmod(_ROOT, 0o700)
_TIMEOUT = float(os.getenv("QLIK_INCOMING_START_TIMEOUT", "120"))


class _Worker:
    def __init__(self, token: str):
        self.socket = str(_ROOT / (secrets.token_hex(16) + ".sock"))
        self.process = None
        self.loop = None
        raise RuntimeError("Workers must be started asynchronously")

    @classmethod
    async def start(cls, token: str) -> "_Worker":
        worker = object.__new__(cls)
        worker.socket = str(_ROOT / (secrets.token_hex(16) + ".sock"))
        worker.loop = asyncio.get_running_loop()
        env = os.environ.copy()
        env.pop("QLIK_JWT_TOKEN", None)
        env.pop("QLIK_PASSWORD", None)
        worker.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "qlik_sense_mcp_server.incoming_worker", worker.socket,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        try:
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
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        try:
            os.unlink(self.socket)
        except FileNotFoundError:
            pass

    def close(self) -> None:
        # TokenContextCache invokes close in a worker thread.
        if self.loop is not None and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.stop(), self.loop)
            future.result(timeout=10)


_loop = None


def _factory(token: str) -> _Worker:
    # Cache factories run in a worker thread; subprocesses belong to the ASGI
    # loop so the transport can stream while startup runs independently.
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
    max_age_seconds=float(os.getenv("QLIK_INCOMING_MAX_AGE_SECONDS", "1500")),
)


async def _error(status: int, text: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return JSONResponse({"error": text}, status_code=status, headers=headers)


async def endpoint(request):
    authorization = request.headers.getlist("authorization")
    if len(authorization) != 1:
        return await _error(401, "Bearer token required")
    scheme, separator, token = authorization[0].partition(" ")
    if not separator or scheme.lower() != "bearer" or not token or token.strip() != token:
        return await _error(401, "Invalid bearer token")
    try:
        token_expiration(token)
        lease = _cache.lease(token)
        worker = await asyncio.to_thread(lease.__enter__)
    except InvalidToken:
        return await _error(401, "Invalid or expired bearer token")
    except CacheBusy:
        return await _error(503, "Qlik session capacity reached")
    except Exception:
        return await _error(401, "Qlik authentication failed or unavailable")

    client = None
    upstream = None
    try:
        if worker.process.returncode is not None:
            return await _error(503, "Qlik worker unavailable")
        headers = [(k, v) for k, v in request.headers.raw
                   if k.lower() not in (b"authorization", b"host", b"connection", b"content-length")]
        headers.append((b"host", b"localhost"))
        transport = httpx.AsyncHTTPTransport(uds=worker.socket)
        client = httpx.AsyncClient(transport=transport, timeout=None)
        req = client.build_request(request.method, "http://localhost/mcp", headers=headers,
                                   content=await request.body())
        upstream = await client.send(req, stream=True)
        response_headers = [(k, v) for k, v in upstream.headers.raw
                            if k.lower() not in (b"connection", b"transfer-encoding", b"content-length")]
        status = upstream.status_code

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()
                await asyncio.to_thread(lease.__exit__, None, None, None)

        response = StreamingResponse(stream(), status_code=status)
        response.raw_headers = response_headers
        return response
    except Exception:
        if upstream is not None:
            await upstream.aclose()
        if client is not None:
            await client.aclose()
        await asyncio.to_thread(lease.__exit__, None, None, None)
        return await _error(502, "Qlik MCP worker failed")
    finally:
        if worker.process.returncode is not None and client is None:
            await asyncio.to_thread(lease.__exit__, None, None, None)


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
