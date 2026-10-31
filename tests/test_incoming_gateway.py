"""Gateway routing through fake authenticated workers and real ASGI HTTP."""

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from qlik_sense_mcp_server import incoming_gateway as gateway
from qlik_sense_mcp_server.gateway_policy import AuthenticationRejected, StartupFailure
from qlik_sense_mcp_server.token_context_cache import TokenContextCache


def token(user, expires=None):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": user, "exp": expires if expires is not None else time.time()+60}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class FakeWorker:
    process = SimpleNamespace(returncode=None)
    def __init__(self, identity): self.identity = identity; self.socket = "/unused"; self.closed = False
    def close(self): self.closed = True


@pytest.mark.asyncio
async def test_rejects_missing_and_expired_tokens():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
        assert (await client.post("/mcp")).status_code == 401
        result = await client.post("/mcp", headers={"Authorization": "Bearer "+token("a", time.time()-1)})
        assert result.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", [(AuthenticationRejected,401),(StartupFailure,503)])
async def test_startup_errors_distinguish_authentication_from_infrastructure(monkeypatch, failure, status):
    calls = []
    def fail(raw): calls.append(raw); raise failure()
    cache = TokenContextCache(fail)
    monkeypatch.setattr(gateway, "_cache", cache)
    bearer = token("a")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
        for _ in range(2):
            response = await client.post("/mcp", headers={"Authorization": "Bearer "+bearer})
            assert response.status_code == status
            assert bearer not in response.text
    assert len(calls) == 2  # factory admission, tested separately, caches failures
    cache.close()


@pytest.mark.asyncio
async def test_routes_each_token_to_its_own_worker(monkeypatch):
    workers, received = {}, []
    def create(raw):
        workers[raw] = FakeWorker(raw)
        return workers[raw]
    cache = TokenContextCache(create)
    monkeypatch.setattr(gateway, "_cache", cache)
    async def backend(request):
        received.append(dict(request.headers))
        if request.headers.get("host") != "localhost:8000":
            return JSONResponse({}, status_code=421)
        return JSONResponse({"ok": True}, headers={"mcp-session-id": "session"})
    fake_app = Starlette(routes=[Route("/mcp", backend, methods=["POST"])])
    monkeypatch.setattr(gateway.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.ASGITransport(app=fake_app))
    a, b = token("a"), token("b")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
        for bearer in (a,b,a):
            response = await client.post("/mcp", headers={"Authorization": "Bearer "+bearer}, json={})
            assert response.status_code == 200
            assert response.headers["mcp-session-id"] == "session"
            assert response.headers["cache-control"] == "no-store"
    assert len(workers) == 2
    assert all("authorization" not in headers for headers in received)
    cache.close()


@pytest.mark.asyncio
async def test_oversized_upload_never_creates_worker(monkeypatch):
    calls = []
    cache = TokenContextCache(lambda raw: calls.append(raw))
    monkeypatch.setattr(gateway, "_cache", cache)
    monkeypatch.setattr(gateway, "_MAX_BODY", 5)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
        result = await client.post("/mcp", headers={"Authorization": "Bearer "+token("a")}, content=b"123456")
        assert result.status_code == 413
    assert calls == []
    cache.close()
