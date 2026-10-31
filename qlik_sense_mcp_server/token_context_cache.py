"""Thread-safe, bounded cache of exact-token authenticated contexts."""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .jwt_validity import InvalidToken, token_expiration

logger = logging.getLogger(__name__)


class CacheBusy(RuntimeError):
    pass


@dataclass
class _Entry:
    context: object
    expires_at: float
    created: float
    touched: float
    active: int = 0
    invalid: bool = False


class TokenContextCache:
    def __init__(self, factory, *, max_entries=128, idle_seconds=900,
                 max_age_seconds=0, evict_idle=True):
        if (not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries < 1
                or not math.isfinite(idle_seconds) or idle_seconds <= 0
                or not math.isfinite(max_age_seconds) or max_age_seconds < 0):
            raise ValueError("Invalid cache limits")
        self._factory = factory
        self._max_entries = max_entries
        self._idle_seconds = idle_seconds
        self._max_age_seconds = max_age_seconds
        self._evict_idle = evict_idle
        self._key = os.urandom(32)
        self._condition = threading.Condition()
        self._entries = {}
        self._creating = set()
        self._closed = False

    def _fingerprint(self, token):
        try:
            return hmac.new(self._key, token.encode("ascii"), hashlib.sha256).digest()
        except (AttributeError, UnicodeEncodeError):
            raise InvalidToken("Invalid bearer token") from None

    def _stale(self, entry, wall, mono):
        age = self._max_age_seconds > 0 and mono - entry.created >= self._max_age_seconds
        idle = mono - entry.touched >= self._idle_seconds
        return entry.invalid or wall >= entry.expires_at or (entry.active == 0 and (age or idle))

    @staticmethod
    def _close(context):
        try:
            context.close()
        except Exception:
            # Never log exception bodies: they may contain upstream secrets.
            logger.error("Token context cleanup failed")

    def reap(self):
        stale = []
        with self._condition:
            wall, mono = time.time(), time.monotonic()
            for key, entry in list(self._entries.items()):
                if not entry.active and self._stale(entry, wall, mono):
                    stale.append(entry.context)
                    del self._entries[key]
            self._condition.notify_all()
        for context in stale:
            self._close(context)
        return len(stale)

    def invalidate(self, token):
        context = None
        with self._condition:
            key = self._fingerprint(token)
            entry = self._entries.get(key)
            if entry is not None:
                entry.invalid = True
                if not entry.active:
                    context = entry.context
                    del self._entries[key]
            self._condition.notify_all()
        if context is not None:
            self._close(context)

    @contextmanager
    def lease(self, token):
        expiration = token_expiration(token)
        key = self._fingerprint(token)
        entry = None
        while entry is None:
            self.reap()
            victim = None
            create = False
            with self._condition:
                if self._closed:
                    raise RuntimeError("Token cache is closed")
                token_expiration(token)
                current = self._entries.get(key)
                if current is not None:
                    if self._stale(current, time.time(), time.monotonic()):
                        raise CacheBusy("Retiring context still in use")
                    current.active += 1
                    current.touched = time.monotonic()
                    entry = current
                elif key in self._creating:
                    self._condition.wait(timeout=1)
                else:
                    if len(self._entries) + len(self._creating) >= self._max_entries:
                        available = [(k, v) for k, v in self._entries.items() if not v.active]
                        if not self._evict_idle or not available:
                            raise CacheBusy("Token context capacity reached")
                        old_key, old = min(available, key=lambda pair: pair[1].touched)
                        del self._entries[old_key]
                        victim = old.context
                    self._creating.add(key)
                    create = True
            if victim is not None:
                self._close(victim)
            if not create:
                continue
            try:
                built = self._factory(token)
            except BaseException:
                with self._condition:
                    self._creating.discard(key)
                    self._condition.notify_all()
                raise
            with self._condition:
                self._creating.discard(key)
                if not self._closed and time.time() < expiration:
                    mono = time.monotonic()
                    entry = _Entry(built, expiration, mono, mono, active=1)
                    self._entries[key] = entry
                self._condition.notify_all()
            if entry is None:
                self._close(built)
                if self._closed:
                    raise RuntimeError("Token cache is closed")
                raise InvalidToken("Bearer token has expired")
        try:
            yield entry.context
        finally:
            context = None
            with self._condition:
                entry.active -= 1
                entry.touched = time.monotonic()
                if not entry.active and (self._closed or self._stale(entry, time.time(), entry.touched)):
                    if self._entries.get(key) is entry:
                        del self._entries[key]
                        context = entry.context
                self._condition.notify_all()
            if context is not None:
                self._close(context)

    def close(self):
        with self._condition:
            self._closed = True
            idle = [entry.context for entry in self._entries.values() if not entry.active]
            self._entries = {key: entry for key, entry in self._entries.items() if entry.active}
            self._condition.notify_all()
        for context in idle:
            self._close(context)
