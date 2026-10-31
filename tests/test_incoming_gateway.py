"""Gateway behavior with a fake Qlik-authenticated worker."""

import base64
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from qlik_sense_mcp_server import incoming_gateway as gateway
from qlik_sense_mcp_server.token_context_cache import TokenContextCache


def token(user, expires=None):
    claims = {"sub": user, "exp": expires if expires is not None else time.time() + 60}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class FakeProcess:
    returncode = None


class FakeWorker:
    def __init__(self, identity):
        self.identity = identity
        self.socket = "/unused"
        self.process = FakeProcess()
        self.closed = False

    def close(self):
        self.closed = True


@asynccontextmanager
async def client_for(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://gateway") as client:
        yield client


@pytest.mark.asyncio
async def test_rejects_missing_and_expired_tokens():
    async with client_for(gateway.app) as client:
        assert (await client.post("/mcp")).status_code == 401
        expired = token("a", time.time() - 1)
        assert (await client.post("/mcp", headers={"Authorization": f"Bearer {expired}"})).status_code == 401


@pytest.mark.asyncio
async def test_authentication_failure_is_not_cached(monkeypatch):
    calls = []

    def fail(raw):
        calls.append(raw)
        raise ValueError("Qlik rejected token")

    cache = TokenContextCache(fail)
    monkeypatch.setattr(gateway, "_cache", cache)
    bearer = token("a")
    async with client_for(gateway.app) as client:
        for _ in range(2):
            response = await client.post("/mcp", headers={"Authorization": f"Bearer {bearer}"})
            assert response.status_code == 401
            assert bearer not in response.text
    assert len(calls) == 2
    cache.close()


@pytest.mark.asyncio
async def test_routes_each_token_to_its_own_worker(monkeypatch):
    workers = {}

    def create(raw):
        worker = FakeWorker(raw)
        workers[raw] = worker
        return worker

    cache = TokenContextCache(create)
    monkeypatch.setattr(gateway, "_cache", cache)
    received = []

    async def backend(request):
        received.append(dict(request.headers))
        # Match MCP SDK 1.x's default localhost:* host rule. A host without
        # a port is rejected with 421 before MCP initialization.
        if request.headers.get("host") != "localhost:8000":
            return JSONResponse({"error": "invalid host"}, status_code=421)
        return JSONResponse({"ok": True}, headers={"mcp-session-id": "session"})

    fake_app = Starlette(routes=[Route("/mcp", backend, methods=["POST"])])
    monkeypatch.setattr(gateway.httpx, "AsyncHTTPTransport", lambda **kw: httpx.ASGITransport(app=fake_app))
    a, b = token("a"), token("b")
    async with client_for(gateway.app) as client:
        for bearer in (a, b, a):
            response = await client.post("/mcp", headers={"Authorization": f"Bearer {bearer}"}, json={})
            assert response.status_code == 200
            assert response.headers["mcp-session-id"] == "session"
    assert len(workers) == 2
    assert all("authorization" not in headers for headers in received)
    assert all(headers["host"] == "localhost:8000" for headers in received)
    cache.close()
