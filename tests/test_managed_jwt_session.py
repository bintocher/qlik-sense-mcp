"""Bootstrap state and retirement with real JwtSession and mocked Qlik HTTP."""

import base64
import json
import time
import unittest
from unittest.mock import patch

import httpx

from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.worker_session import ManagedJwtSession, SessionInvalidated


class ManagedSessionTests(unittest.TestCase):
    def test_cached_session_uses_one_bootstrap_and_retired_session_cannot_reauth(self):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time()+60}).encode()).rstrip(b"=").decode()
        config = QlikSenseConfig(server_url="https://qlik.example/jwt", jwt_token=f"header.{payload}.signature")
        calls, retired = [], []
        def qlik(request):
            calls.append(request)
            return httpx.Response(200, headers={
                "Set-Cookie": "X-Qlik-Session-jwt=opaque; Path=/", "qlik-csrf-token": "csrf"})
        def client(): return httpx.Client(transport=httpx.MockTransport(qlik))
        session = ManagedJwtSession(config, lambda: retired.append(True))
        with patch.object(session, "_build_bootstrap_client", client):
            session.ensure_standalone()
            session.ensure_standalone()
            with httpx.Client() as repo_client:
                session.ensure(repo_client)
                self.assertEqual(repo_client.cookies.get("X-Qlik-Session-jwt"), "opaque")
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].headers["authorization"].startswith("Bearer "))
            with self.assertRaises(SessionInvalidated): session.invalidate()
            with self.assertRaises(SessionInvalidated): session.ensure_standalone()
            self.assertEqual(len(calls), 1)
            self.assertEqual(retired, [True])
            self.assertIsNone(session.cookie_value)


if __name__ == "__main__": unittest.main()
