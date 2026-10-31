"""Bounded, in-memory cache for independently authenticated JWT contexts.

The factory MUST authenticate the exact JWT with Qlik before returning.
Time checks alone do not authenticate tokens. A context's close() must not
log out other Qlik sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .jwt_validity import InvalidToken, token_expiration


class CacheBusy(RuntimeError):
    """No safely evictable cache slot is currently available."""


@dataclass
class _Entry:
    context: Any
    expires_at: float
    created: float
    touched: float
    active: int = 0
    invalid: bool = False


class TokenContextCache:
    """Per-token cache; in-flight factories and active leases are protected."""

    def __init__(self, factory: Callable[[str], Any], *, max_entries: int = 128,
                 idle_seconds: float = 900, max_age_seconds: float = 0):
        if max_entries < 1 or idle_seconds <= 0 or max_age_seconds < 0:
            raise ValueError("Invalid cache limits")
        self._factory = factory
        self._max_entries = max_entries
        self._idle_seconds = idle_seconds
        # Zero disables absolute age eviction. JWT expiry is always enforced.
        self._max_age_seconds = max_age_seconds
        self._key = os.urandom(32)
        self._condition = threading.Condition()
        self._entries: dict[bytes, _Entry] = {}
        self._creating: set[bytes] = set()
        self._closed = False

    def _fingerprint(self, token: str) -> bytes:
        try:
            return hmac.new(self._key, token.encode("ascii"), hashlib.sha256).digest()
        except (UnicodeEncodeError, AttributeError):
            raise InvalidToken("Invalid bearer token") from None

    def _stale(self, entry: _Entry, wall: float, mono: float) -> bool:
        # An active streaming request is not idle. Never retire a live entry
        # for age while its leases are held; expiry/invalidation still wins.
        age_expired = self._max_age_seconds > 0 and mono - entry.created >= self._max_age_seconds
        idle_expired = mono - entry.touched >= self._idle_seconds
        return (entry.invalid or wall >= entry.expires_at or
                (entry.active == 0 and (idle_expired or age_expired)))

    @staticmethod
    def _close(context: Any) -> None:
        try:
            context.close()
        except Exception:
            pass

    def invalidate(self, token: str) -> None:
        """Retire one context; an active context is closed after its last lease."""
        key = self._fingerprint(token)
        context = None
        with self._condition:
            entry = self._entries.get(key)
            if entry is not None:
                entry.invalid = True
                if not entry.active:
                    del self._entries[key]
                    context = entry.context
            self._condition.notify_all()
        if context is not None:
            self._close(context)

    @contextmanager
    def lease(self, token: str) -> Iterator[Any]:
        expiration = token_expiration(token)
        fingerprint = self._fingerprint(token)
        entry = None
        while entry is None:
            to_close = []
            create = False
            error = None
            with self._condition:
                if self._closed:
                    raise RuntimeError("Token context cache is closed")
                token_expiration(token)
                wall, mono = time.time(), time.monotonic()
                for key, candidate in list(self._entries.items()):
                    if not candidate.active and self._stale(candidate, wall, mono):
                        del self._entries[key]
                        to_close.append(candidate.context)
                current = self._entries.get(fingerprint)
                if current is not None:
                    if self._stale(current, wall, mono):
                        error = CacheBusy("The previous context is still in use")
                    else:
                        current.active += 1
                        current.touched = mono
                        entry = current
                elif fingerprint in self._creating:
                    self._condition.wait(timeout=1)
                else:
                    occupied = len(self._entries) + len(self._creating)
                    if occupied >= self._max_entries:
                        available = [(k, v) for k, v in self._entries.items() if not v.active]
                        if not available:
                            error = CacheBusy("All token contexts are in use")
                        else:
                            key, victim = min(available, key=lambda pair: pair[1].touched)
                            del self._entries[key]
                            to_close.append(victim.context)
                    if error is None:
                        self._creating.add(fingerprint)
                        create = True
            for context in to_close:
                self._close(context)
            if error is not None:
                raise error
            if not create:
                continue
            try:
                built = self._factory(token)
            except BaseException:
                with self._condition:
                    self._creating.discard(fingerprint)
                    self._condition.notify_all()
                raise
            with self._condition:
                self._creating.discard(fingerprint)
                now_wall, now_mono = time.time(), time.monotonic()
                if not self._closed and now_wall < expiration:
                    entry = _Entry(built, expiration, now_mono, now_mono, active=1)
                    self._entries[fingerprint] = entry
                self._condition.notify_all()
            if entry is None:
                self._close(built)
                if self._closed:
                    raise RuntimeError("Token context cache is closed")
                raise InvalidToken("Bearer token has expired")
        try:
            yield entry.context
        finally:
            to_close = None
            with self._condition:
                entry.active -= 1
                entry.touched = time.monotonic()
                if entry.active == 0 and (self._closed or self._stale(entry, time.time(), entry.touched)):
                    if self._entries.get(fingerprint) is entry:
                        del self._entries[fingerprint]
                        to_close = entry.context
                self._condition.notify_all()
            if to_close is not None:
                self._close(to_close)

    def close(self) -> None:
        """Reject new leases and close idle contexts; active leases close on exit."""
        with self._condition:
            self._closed = True
            idle = [entry.context for entry in self._entries.values() if not entry.active]
            self._entries = {key: entry for key, entry in self._entries.items() if entry.active}
            self._condition.notify_all()
        for context in idle:
            self._close(context)
