"""Real SDK initialize/list/call through the gateway; Qlik itself is mocked."""

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from qlik_sense_mcp_server import server
from qlik_sense_mcp_server import incoming_gateway as gateway
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.token_context_cache import TokenContextCache


def token():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time()+60}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class FakeWorker:
    socket = "/unused"
    process = SimpleNamespace(returncode=None)
    def close(self): pass


@pytest.mark.asyncio
async def test_sdk_initialize_list_and_call_through_gateway(monkeypatch):
    worker_app = server.mcp.streamable_http_app()
    cache = TokenContextCache(lambda _: FakeWorker())
    monkeypatch.setattr(gateway, "_cache", cache)
    monkeypatch.setattr(context, "repo_api", SimpleNamespace(get_about=lambda: {"buildVersion": "gateway-test"}))
    monkeypatch.setattr(gateway.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.ASGITransport(app=worker_app))
    headers = {"Authorization": "Bearer "+token(), "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "gateway-test", "version": "1"}}}
    async with worker_app.router.lifespan_context(worker_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway") as client:
            result = await client.post("/mcp", headers=headers, json=init)
            assert result.status_code == 200, result.text
            session_id = result.headers.get("mcp-session-id")
            assert session_id
            session_headers = dict(headers, **{"Mcp-Session-Id": session_id, "Mcp-Protocol-Version": "2025-03-26"})
            result = await client.post("/mcp", headers=session_headers,
                json={"jsonrpc":"2.0", "method":"notifications/initialized"})
            assert result.status_code == 202, result.text
            result = await client.post("/mcp", headers=session_headers,
                json={"jsonrpc":"2.0", "id":2, "method":"tools/list"})
            assert result.status_code == 200 and "get_about" in result.text, result.text
            result = await client.post("/mcp", headers=session_headers,
                json={"jsonrpc":"2.0", "id":3, "method":"tools/call", "params":{"name":"get_about", "arguments":{}}})
            assert result.status_code == 200 and "gateway-test" in result.text, result.text
    cache.close()
