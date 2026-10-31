"""Real MCP SDK HTTP initialization through the JWT gateway.

Qlik authentication is mocked, but the upstream MCP app, host validation,
protocol lifecycle and session headers are real. Run against both SDK lines.
"""

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from qlik_sense_mcp_server import server
from qlik_sense_mcp_server import incoming_gateway as gateway
from qlik_sense_mcp_server.token_context_cache import TokenContextCache


def token():
    claims = {"exp": time.time() + 60, "sub": "mock-user"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class FakeWorker:
    socket = "/unused"
    process = SimpleNamespace(returncode=None)

    def close(self):
        pass


@pytest.mark.asyncio
async def test_sdk_initialization_and_tool_listing_through_gateway(monkeypatch):
    worker_app = server.mcp.streamable_http_app()
    cache = TokenContextCache(lambda _: FakeWorker())
    monkeypatch.setattr(gateway, "_cache", cache)
    monkeypatch.setattr(gateway.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.ASGITransport(app=worker_app))
    bearer = token()
    headers = {"Authorization": f"Bearer {bearer}",
               "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "gateway-test", "version": "1"}}}

    async with worker_app.router.lifespan_context(worker_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                     base_url="http://gateway") as client:
            result = await client.post("/mcp", headers=headers, json=init)
            assert result.status_code == 200, result.text
            session_id = result.headers.get("mcp-session-id")
            assert session_id
            session_headers = dict(headers, **{"Mcp-Session-Id": session_id,
                                                "Mcp-Protocol-Version": "2025-03-26"})
            ready = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            response = await client.post("/mcp", headers=session_headers, json=ready)
            assert response.status_code == 202, response.text
            listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            response = await client.post("/mcp", headers=session_headers, json=listing)
            assert response.status_code == 200, response.text
            assert "get_about" in response.text
    cache.close()
