"""The first call must survive a proxy that has been idle.

The bootstrap shares its httpx client with ordinary QRS calls, and that
client carries the ordinary deadline. A virtual proxy that has been quiet
answers this first request slowly — measured, 15 to 21 seconds cold
against 0.45 warm — so under the ordinary budget the first call after a
quiet period failed every time, with a message that reads like a broken
configuration rather than a server waking up.
"""

import httpx
import pytest

from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.jwt_session import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    JwtBootstrapError,
    JwtSession,
)


class _Client:
    """Records the timeout each request was given."""

    def __init__(self, delay=0.0, budget=10.0):
        self.delay = delay
        self.budget = budget
        self.timeouts = []
        self.cookies = httpx.Cookies()

    def get(self, url, headers=None, timeout=None):
        self.timeouts.append(timeout)
        if self.delay > (timeout if timeout is not None else self.budget):
            raise httpx.ReadTimeout("The read operation timed out")
        response = httpx.Response(
            204, headers=[("qlik-csrf-token", "csrf-value"),
                          ("set-cookie", "X-Qlik-Session-jwt=abc; Path=/")],
            request=httpx.Request("GET", url))
        return response


def _session():
    return JwtSession(QlikSenseConfig(
        server_url="https://qlik.example.com/jwt",
        jwt_token="header.payload.signature"))


class TestBootstrapDeadline:
    def test_the_bootstrap_sets_its_own_timeout(self):
        client = _Client()
        _session().ensure(client)
        assert client.timeouts == [BOOTSTRAP_TIMEOUT_SECONDS]



