"""A token that cannot work must say so before Qlik is asked.

The virtual proxy answers every unusable token with the same bare HTTP 400
- measured: an expired token, a wrong signature and a string that is not a
JWT at all get identical replies. A model reading "csrftoken returned HTTP
400" went looking for a changed proxy prefix, rotated keys and a deleted
app, when the token had simply run out the day before.
"""

import base64
import json

import httpx
import pytest

from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.jwt_session import JwtBootstrapError, JwtSession, token_problem

NOW = 1_790_000_000


def _token(claims):
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.c2lnbmF0dXJl"


class TestTokenProblem:
    def test_a_valid_token_has_no_problem(self):
        assert token_problem(_token({"exp": NOW + 3600}), now=NOW) is None

    def test_a_token_without_exp_is_left_to_the_proxy(self):
        assert token_problem(_token({"userId": "a"}), now=NOW) is None

    def test_an_expired_token_names_the_date_and_the_remedy(self):
        problem = token_problem(_token({"exp": NOW - 60}), now=NOW)
        assert "expired on" in problem
        assert "retrying will not help" in problem
        assert "new token" in problem

    @pytest.mark.parametrize("token", ["abc.def", "not-a-jwt", "a.b.c.d"])
    def test_a_string_that_is_not_a_jwt(self, token):
        assert "not a valid JWT" in token_problem(token, now=NOW)

    def test_an_undecodable_payload(self):
        assert "damaged" in token_problem("eyJhbGciOiJSUzI1NiJ9.!!!.sig", now=NOW)

    def test_a_token_not_yet_valid(self):
        problem = token_problem(_token({"nbf": NOW + 3600}), now=NOW)
        assert "not valid until" in problem

    def test_small_clock_drift_is_tolerated(self):
        assert token_problem(_token({"nbf": NOW + 30}), now=NOW) is None


class _Client:
    def __init__(self, status=204):
        self.status = status
        self.requests = 0
        self.cookies = httpx.Cookies()

    def get(self, url, headers=None, timeout=None):
        self.requests += 1
        return httpx.Response(self.status, text="Bad Request",
                              request=httpx.Request("GET", url))


def _session(token):
    return JwtSession(QlikSenseConfig(server_url="https://qlik.example.com/jwt",
                                      jwt_token=token))


class TestBootstrap:
    def test_an_expired_token_never_reaches_the_proxy(self):
        client = _Client()
        with pytest.raises(JwtBootstrapError, match="expired on"):
            _session(_token({"exp": 1})).ensure(client)
        assert client.requests == 0

    def test_a_400_for_a_live_token_says_the_token_was_refused(self):
        client = _Client(status=400)
        with pytest.raises(JwtBootstrapError, match="did not accept QLIK_JWT_TOKEN"):
            _session(_token({"exp": 4_000_000_000})).ensure(client)
