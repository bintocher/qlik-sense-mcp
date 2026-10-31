"""Additional lease lifetime and token isolation regressions."""

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from qlik_sense_mcp_server.jwt_validity import InvalidToken
from qlik_sense_mcp_server.token_context_cache import CacheBusy, TokenContextCache


def token(user, expires=None):
    claims = {"sub": user, "exp": expires if expires is not None else time.time() + 60}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


class Context:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_active_stream_can_share_context_after_idle_timeout():
    calls = []
    cache = TokenContextCache(lambda _: calls.append(Context()) or calls[-1],
                              idle_seconds=0.03, max_age_seconds=0.03)
    jwt = token("a")
    with cache.lease(jwt) as stream:
        time.sleep(0.06)
        with cache.lease(jwt) as request:
            assert request is stream
        assert len(calls) == 1
    assert stream.closed  # configured absolute max age applies after last lease


def test_continuous_requests_do_not_force_reauthentication_by_default():
    calls = []
    cache = TokenContextCache(lambda _: calls.append(Context()) or calls[-1],
                              idle_seconds=0.2)
    jwt = token("a")
    for _ in range(5):
        with cache.lease(jwt) as context:
            assert context is calls[0]
        time.sleep(0.04)
    assert len(calls) == 1
    cache.close()


def test_invalidating_active_context_fails_closed_then_reauthenticates():
    calls = []
    cache = TokenContextCache(lambda _: calls.append(Context()) or calls[-1])
    jwt = token("a")
    with cache.lease(jwt) as first:
        cache.invalidate(jwt)
        with pytest.raises(CacheBusy):
            with cache.lease(jwt):
                pass
        assert not first.closed
    assert first.closed
    with cache.lease(jwt) as second:
        assert second is not first
    assert len(calls) == 2
    cache.close()


def test_expired_token_cannot_acquire_even_if_context_is_live():
    cache = TokenContextCache(lambda _: Context())
    jwt = token("a", time.time() + 0.1)
    with cache.lease(jwt):
        time.sleep(0.15)
        with pytest.raises(InvalidToken):
            with cache.lease(jwt):
                pass
    cache.close()


def test_different_tokens_build_in_parallel():
    barrier = Barrier(2, timeout=2)
    def factory(_):
        barrier.wait()
        return Context()
    cache = TokenContextCache(factory)
    def acquire(jwt):
        with cache.lease(jwt):
            return True
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(acquire, (token("a"), token("b"))))
    cache.close()
