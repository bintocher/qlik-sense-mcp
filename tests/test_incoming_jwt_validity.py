"""Tests for the local incoming JWT time checks, not signature validation."""

import base64
import json

import pytest

from qlik_sense_mcp_server.jwt_validity import InvalidToken, token_expiration


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def test_valid_expiration():
    assert token_expiration(jwt({"exp": 2000, "nbf": 999}), now=1000) == 2000


@pytest.mark.parametrize("claims", [
    {}, {"exp": 1000}, {"exp": "2000"}, {"exp": True},
    {"exp": 2000, "nbf": 1001}, {"exp": 2000, "nbf": True},
])
def test_invalid_window(claims):
    with pytest.raises(InvalidToken):
        token_expiration(jwt(claims), now=1000)


@pytest.mark.parametrize("token", ["", "one.two", "a.%%.c", "a.e30.", "x" * 16385])
def test_invalid_shape(token):
    with pytest.raises(InvalidToken):
        token_expiration(token, now=1000)


def test_exception_does_not_expose_token():
    token = jwt({"exp": 1, "secret": "sensitive"})
    with pytest.raises(InvalidToken) as exc:
        token_expiration(token, now=1000)
    assert token not in str(exc.value)
    assert "sensitive" not in str(exc.value)
