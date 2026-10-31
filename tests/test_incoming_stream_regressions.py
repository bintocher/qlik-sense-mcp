"""Gateway cleanup and expiry regressions without a live Qlik server."""

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest

from qlik_sense_mcp_server import incoming_gateway as gateway
from qlik_sense_mcp_server.token_context_cache import TokenContextCache


def token(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


class Request:
    method = "GET"

    def __init__(self, value):
        self.headers = SimpleNamespace(
            raw=[(b"authorization", f"Bearer {value}".encode())],
            getlist=lambda name: [f"Bearer {value}"] if name == "authorization" else [],
        )

    async def body(self):
        return b""


class Worker:
    socket = "/fake"
    process = SimpleNamespace(returncode=None)

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Upstream:
    status_code = 200
    headers = SimpleNamespace(raw=[(b"content-type", b"text/event-stream")])

    def __init__(self):
        self.closed = False

    async def aiter_raw(self):
        yield b"event: ready\n\n"
        await asyncio.sleep(10)
        yield b"event: too-late\n\n"

    async def aclose(self):
        self.closed = True


class Client:
    def __init__(self, *args, **kwargs):
        self.upstream = Upstream()
        self.closed = False

    def build_request(self, *args, **kwargs):
        return None

    async def send(self, request, stream=False):
        return self.upstream

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_closes_at_expiry_and_releases_worker(monkeypatch):
    worker = Worker()
    clients = []
    cache = TokenContextCache(lambda _: worker)
    monkeypatch.setattr(gateway, "_cache", cache)
    monkeypatch.setattr(gateway.httpx, "AsyncHTTPTransport", lambda **kwargs: None)

    def make_client(*args, **kwargs):
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr(gateway.httpx, "AsyncClient", make_client)
    jwt = token(time.time() + 0.3)
    response = await gateway.endpoint(Request(jwt))
    started = time.monotonic()
    chunks = [part async for part in response.body_iterator]
    assert chunks == [b"event: ready\n\n"]
    assert time.monotonic() - started < 3
    assert clients[0].closed and clients[0].upstream.closed
    assert worker.closed  # expiry removes the context once the lease ends
    cache.close()


@pytest.mark.asyncio
async def test_missing_authorization_returns_401():
    request = SimpleNamespace(headers=SimpleNamespace(getlist=lambda _: []))
    response = await gateway.endpoint(request)
    assert response.status_code == 401
