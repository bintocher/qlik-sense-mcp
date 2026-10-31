"""Exercise full ASGI response ownership, not the body iterator in isolation."""

import asyncio
import time
import unittest
from unittest.mock import patch

import httpx

from qlik_sense_mcp_server import incoming_gateway as gateway


class Lease:
    def __init__(self): self.released = 0
    def __exit__(self, *args): self.released += 1


class Client:
    def __init__(self): self.closed = False
    async def aclose(self): self.closed = True


class Upstream:
    status_code = 200
    def __init__(self, fail=False): self.closed = False; self.fail = fail
    async def aclose(self): self.closed = True
    async def aiter_raw(self):
        yield b"ready"
        if self.fail: raise httpx.ReadError("do not expose upstream details")
        await asyncio.sleep(5)
        yield b"late"


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def resources(self, fail=False):
        lease = Lease()
        resources = gateway._Resources(lease, "test-token")
        resources.client = Client()
        resources.upstream = Upstream(fail)
        return resources, lease

    def scope(self):
        return {"type": "http", "asgi": {"spec_version": "2.4"},
                "method": "GET", "path": "/mcp", "headers": []}

    async def receive(self):
        await asyncio.sleep(20)
        return {"type": "http.disconnect"}

    async def test_expiry_releases_all_resources_exactly_once(self):
        resources, lease = self.resources()
        response = gateway._ProxyResponse(resources, time.time()+.03, [])
        messages = []
        async def send(message): messages.append(message)
        await response(self.scope(), self.receive, send)
        await resources.close()
        self.assertEqual(lease.released, 1)
        self.assertTrue(resources.client.closed and resources.upstream.closed)
        self.assertFalse(any(message.get("body") == b"late" for message in messages))

    async def test_initial_send_failure_still_releases_lease(self):
        resources, lease = self.resources()
        response = gateway._ProxyResponse(resources, time.time()+10, [])
        async def send(message): raise OSError("downstream closed")
        with self.assertRaises(Exception): await response(self.scope(), self.receive, send)
        self.assertEqual(lease.released, 1)
        self.assertTrue(resources.client.closed and resources.upstream.closed)

    async def test_midstream_upstream_failure_invalidates_worker(self):
        resources, lease = self.resources(fail=True)
        response = gateway._ProxyResponse(resources, time.time()+10, [])
        invalidations = []
        cache = type("Cache", (), {"invalidate": lambda _, token: invalidations.append(token)})()
        async def send(message): pass
        with patch.object(gateway, "_cache", cache):
            with self.assertRaises(Exception): await response(self.scope(), self.receive, send)
        self.assertEqual(invalidations, ["test-token"])
        self.assertEqual(lease.released, 1)


if __name__ == "__main__": unittest.main()
