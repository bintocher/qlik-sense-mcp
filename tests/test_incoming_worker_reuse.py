"""Worker state is rebuilt atomically, never replaying a failed tool."""

import base64
import json
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from qlik_sense_mcp_server.worker_session import SessionInvalidated, WorkerSessionGuard


def token():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time()+60}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class Session:
    def __init__(self, config, invalidate):
        self.invalidate_callback = invalidate
    def ensure_standalone(self): pass
    def invalidate(self):
        self.invalidate_callback()
        raise SessionInvalidated()


class Repo:
    def __init__(self, *args, **kwargs):
        self.closed = False
        self.client = SimpleNamespace(close=self.close)
    def close(self): self.closed = True


class Engine:
    def __init__(self, *args, **kwargs): self.closed = False
    def abort_socket(self): self.closed = True
    def disconnect(self): self.closed = True


class GuardTests(unittest.TestCase):
    def make_guard(self):
        ctx = SimpleNamespace(engine_api=None, repo_api=None, jwt_session=None, form_session=None)
        guard = WorkerSessionGuard(ctx, SimpleNamespace(jwt_token=token()),
            session_factory=Session, repo_factory=Repo, engine_factory=Engine)
        return ctx, guard

    def test_invalidation_drops_all_state_and_never_replays_call(self):
        ctx, guard = self.make_guard()
        cache = {"app": {"sample": "secret"}}
        module = SimpleNamespace(forget_app_details=cache.clear)
        with patch.dict(sys.modules, {"qlik_sense_mcp_server.tools.repository": module}):
            guard.initialize()
            cache["app"] = "sample"
            repo, engine = ctx.repo_api, ctx.engine_api
            calls = []
            def tool():
                calls.append(1)
                # Even if a lower layer swallows this exception, the guard
                # must refuse to return a stale successful payload.
                try: ctx.jwt_session.invalidate()
                except SessionInvalidated: pass
                return "stale"
            with self.assertRaises(SessionInvalidated): guard.execute(tool)
            self.assertEqual(calls, [1])
            self.assertTrue(repo.closed and engine.closed)
            self.assertEqual(cache, {})
            self.assertIsNone(ctx.engine_api)
            self.assertEqual(guard.execute(lambda: "fresh"), "fresh")
            self.assertEqual(guard.generation, 2)
            guard.close()

    def test_failed_reauthentication_cannot_access_stale_clients_or_cache(self):
        ctx, guard = self.make_guard()
        guard.initialize()
        old_engine = ctx.engine_api
        with self.assertRaises(SessionInvalidated):
            guard.execute(lambda: ctx.jwt_session.invalidate())
        class Rejected(Session):
            def ensure_standalone(self): raise RuntimeError("rejected")
        guard.session_factory = Rejected
        calls = []
        with self.assertRaises(RuntimeError): guard.execute(lambda: calls.append(1))
        self.assertEqual(calls, [])
        self.assertIsNone(ctx.engine_api)
        self.assertIsNone(ctx.repo_api)
        self.assertTrue(old_engine.closed)
        guard.close()

    def test_cached_calls_do_not_reauthenticate(self):
        ctx, guard = self.make_guard()
        guard.initialize()
        for _ in range(5): guard.execute(lambda: None)
        self.assertEqual(guard.generation, 1)
        guard.close()


if __name__ == "__main__": unittest.main()
