"""Regression tests for the unwired token context cache."""

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from qlik_sense_mcp_server.jwt_validity import InvalidToken
from qlik_sense_mcp_server.token_context_cache import CacheBusy, TokenContextCache


def jwt(identity, exp=None):
    payload = {"sub": identity, "exp": exp if exp is not None else time.time() + 60}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.signature"


class FakeContext:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_reuse_and_isolation():
    built = []

    def factory(_token):
        context = FakeContext()
        built.append(context)
        return context

    cache = TokenContextCache(factory)
    a, b = jwt("A"), jwt("B")
    with cache.lease(a) as first:
        with cache.lease(a) as same, cache.lease(b) as other:
            assert same is first
            assert other is not first
    assert len(built) == 2
    cache.close()
    assert all(c.closed for c in built)


def test_same_token_concurrent_single_factory():
    gate = threading.Barrier(8)
    count_lock = threading.Lock()
    count = 0

    def factory(_token):
        nonlocal count
        with count_lock:
            count += 1
        time.sleep(0.05)
        return FakeContext()

    cache = TokenContextCache(factory)
    token = jwt("A")

    def use():
        gate.wait()
        with cache.lease(token) as context:
            return context

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: use(), range(8)))
    assert count == 1
    assert all(c is results[0] for c in results)
    cache.close()


def test_expired_never_reuses_session():
    calls = []
    cache = TokenContextCache(lambda token: calls.append(token) or FakeContext())
    with pytest.raises(InvalidToken):
        with cache.lease(jwt("A", time.time() - 1)):
            pass
    assert calls == []


def test_active_context_is_not_evicted():
    cache = TokenContextCache(lambda _: FakeContext(), max_entries=1)
    with cache.lease(jwt("A")) as active:
        with pytest.raises(CacheBusy):
            with cache.lease(jwt("B")):
                pass
        assert not active.closed
    cache.close()
    assert active.closed


def test_factory_failure_is_never_cached():
    attempts = []

    def reject(_):
        attempts.append(1)
        raise ValueError("not authenticated")

    cache = TokenContextCache(reject)
    token = jwt("A")
    for _ in range(2):
        with pytest.raises(ValueError):
            with cache.lease(token):
                pass
    assert len(attempts) == 2
