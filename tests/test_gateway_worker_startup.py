"""Admission is applied before spawn and worker failures remain categorized."""

import asyncio
import base64
import json
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from qlik_sense_mcp_server import incoming_gateway as gateway
from qlik_sense_mcp_server.gateway_policy import AuthenticationRejected, ConfigurationFailure, GatewayFailure, StartupFailure


class Pipe:
    def __init__(self, status): self.status = status
    def write(self, data): pass
    async def drain(self): pass
    def close(self): pass
    async def readline(self): return (json.dumps({"status": self.status})+"\n").encode()


class Process:
    def __init__(self, status):
        self.returncode = None
        self.stdin = Pipe(status)
        self.stdout = Pipe(status)
    def terminate(self): self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): return self.returncode


class WorkerStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_start_failure_categories_never_expose_token(self):
        import pathlib
        for status, error in (("authentication", AuthenticationRejected),
                              ("configuration", ConfigurationFailure),
                              ("upstream", GatewayFailure), ("startup", StartupFailure)):
            process = Process(status)
            async def spawn(*args, **kwargs):
                self.assertNotIn("sensitive-token", str(args))
                self.assertNotIn("QLIK_JWT_TOKEN", kwargs["env"])
                return process
            with self.subTest(status=status), patch.object(gateway, "_ROOT", pathlib.Path("/tmp")), patch.object(gateway.asyncio, "create_subprocess_exec", spawn):
                with self.assertRaises(error) as caught:
                    await gateway._Worker.start("sensitive-token")
                self.assertNotIn("sensitive-token", str(caught.exception))
                self.assertEqual(process.returncode, 0)
            self.assertFalse(gateway._workers)

    async def test_cancelled_acquisition_releases_lease_after_thread_finishes(self):
        import threading
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        class Lease:
            def __enter__(self):
                entered.set()
                release.wait(2)
                return object()
            def __exit__(self, *args): completed.set()
        task = asyncio.create_task(gateway._acquire(Lease()))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        release.set()
        await asyncio.wait_for(asyncio.to_thread(completed.wait, 2), 3)
        self.assertTrue(completed.is_set())
        if gateway._pending_acquisitions:
            await asyncio.gather(*list(gateway._pending_acquisitions))


if __name__ == "__main__": unittest.main()
