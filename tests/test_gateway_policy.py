"""Policy regressions runnable with unittest, without Qlik or the MCP SDK."""

import asyncio
import base64
import json
import time
import unittest

from qlik_sense_mcp_server.gateway_policy import (
    AdmissionRejected, AuthenticationRejected, BodyTimeout, BodyTooLarge,
    CredentialExpired, StartupAdmission, proxy_headers, read_body,
)
from qlik_sense_mcp_server.jwt_validity import InvalidToken, token_expiration
from qlik_sense_mcp_server.token_context_cache import CacheBusy, TokenContextCache


def jwt(exp=None):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp if exp is not None else time.time() + 60}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class PolicyTests(unittest.TestCase):
    def test_strict_token_and_numeric_dates(self):
        valid = jwt()
        token_expiration(valid)
        for bad in (valid + "\nignored", valid + "\r", " " + valid,
                    valid.replace("header", "head er"), jwt(10**1000), jwt(float("inf")), jwt(True)):
            with self.subTest(bad=bad[:12]), self.assertRaises(InvalidToken):
                token_expiration(bad)

    def test_proxy_headers_preserve_mcp_and_remove_hop_headers(self):
        raw = [(b"Connection", b"keep-alive, x-private"), (b"X-Private", b"secret"),
               (b"Transfer-Encoding", b"chunked"), (b"Authorization", b"Bearer secret"),
               (b"Mcp-Session-Id", b"session"), (b"Content-Length", b"5")]
        headers = dict(proxy_headers(raw, request=True))
        self.assertEqual(headers[b"host"], b"localhost:8000")
        self.assertEqual(headers[b"mcp-session-id"], b"session")
        for key in (b"authorization", b"x-private", b"transfer-encoding", b"content-length", b"connection"):
            self.assertNotIn(key, headers)
        with self.assertRaises(ValueError):
            proxy_headers([(b"Connection", b"mcp-session-id")])
        self.assertNotIn(b"set-cookie", dict(proxy_headers([(b"set-cookie", b"secret")])))

    def test_negative_start_cache_and_concurrency(self):
        admission = StartupAdmission(concurrent=1)
        with admission.start("one"):
            with self.assertRaises(AdmissionRejected):
                with admission.start("two"):
                    pass
        with self.assertRaises(AuthenticationRejected):
            with admission.start("bad"):
                raise AuthenticationRejected()
        with self.assertRaises(AuthenticationRejected):
            with admission.start("bad"):
                self.fail("Rejected JWT must not start another worker")
        self.assertEqual(admission._active, 0)

    def test_gateway_capacity_never_evicts_authenticated_context(self):
        class Context:
            closed = False
            def close(self): self.closed = True
        built = []
        cache = TokenContextCache(lambda _: built.append(Context()) or built[-1], max_entries=1, evict_idle=False)
        one = jwt()
        with cache.lease(one) as context:
            pass
        # Different exact token, even if it claims the same principal.
        two = one.replace("header", "other")
        with self.assertRaises(CacheBusy):
            with cache.lease(two): pass
        self.assertEqual(len(built), 1)
        self.assertFalse(context.closed)
        cache.close()


class UploadTests(unittest.IsolatedAsyncioTestCase):
    def request(self, chunks, *, declared=None, delay=0):
        class Request:
            headers = {} if declared is None else {"content-length": str(declared)}
            async def stream(self):
                for chunk in chunks:
                    if delay: await asyncio.sleep(delay)
                    yield chunk
        return Request()

    async def test_declared_and_chunked_limits(self):
        for request in (self.request([], declared=100), self.request([b"123", b"456"])):
            with self.assertRaises(BodyTooLarge):
                await read_body(request, expires_at=time.time()+10, max_bytes=5, timeout=1)
        self.assertEqual(await read_body(self.request([b"12", b"3"]), expires_at=time.time()+10, max_bytes=5, timeout=1), b"123")

    async def test_slow_upload_timeout_and_token_expiry(self):
        with self.assertRaises(BodyTimeout):
            await read_body(self.request([b"x"], delay=.1), expires_at=time.time()+10, max_bytes=5, timeout=.01)
        with self.assertRaises(CredentialExpired):
            await read_body(self.request([b"x"], delay=.1), expires_at=time.time()+.01, max_bytes=5, timeout=1)


if __name__ == "__main__":
    unittest.main()
