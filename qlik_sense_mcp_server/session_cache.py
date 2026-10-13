"""A proxy session that survives a restart of this server.

Every login to a virtual proxy creates a Qlik session that lives until the
proxy's inactivity timeout, whatever happens to the process that made it.
A server stopped hard - the way a process manager or a script kills it -
cannot log out, and the next start logged in again. Measured: eight
restarts within ten minutes exhausted the five sessions Qlik allows a user,
and every call was refused with OnMaxParallelSessionsExceeded for the next
quarter of an hour.

So the session cookie is kept in a file named after a hash of the identity
it belongs to, with its age, and a new start takes it up again while it is
younger than the session TTL. A cookie the proxy no longer knows fails the
way a stale one always did - 401/403 or a closed socket - and is replaced.

`QLIK_SESSION_CACHE=false` turns this off; `QLIK_SESSION_CACHE_DIR` moves
the files (default: a `qlik-sense-mcp-sessions` folder in the system's
temporary directory). The file holds what the process already holds - a
session cookie for the identity in its own configuration - and is written
readable by its owner only where the system supports it.
"""

import hashlib
import json
import logging
import os
import tempfile
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.getenv("QLIK_SESSION_CACHE", "true").strip().lower() not in (
        "false", "0", "no", "off")


def _directory() -> str:
    return (os.getenv("QLIK_SESSION_CACHE_DIR", "").strip()
            or os.path.join(tempfile.gettempdir(), "qlik-sense-mcp-sessions"))


def key_for(*parts: Any) -> str:
    """A file name that says nothing about the identity it stands for."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8"))
    return digest.hexdigest()[:32]


def _path(key: str) -> str:
    return os.path.join(_directory(), f"{key}.json")


def restore(session: Any, key: Optional[str], ttl: float) -> bool:
    """Take up a cookie a previous process left, if it is young enough."""
    if not key or not enabled():
        return False
    try:
        with open(_path(key), encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return False
    fetched_at = float(saved.get("fetched_at") or 0)
    if not saved.get("cookie_value") or time.time() - fetched_at >= ttl:
        forget(key)
        return False
    session._cookie_name = saved.get("cookie_name")
    session._cookie_value = saved.get("cookie_value")
    session._csrf_token = saved.get("csrf_token") or None
    session._fetched_at = fetched_at
    logger.info("Reusing the proxy session of a previous start (%.0f s old)",
                time.time() - fetched_at)
    return True


def store(session: Any, key: Optional[str]) -> None:
    """Keep the current cookie for the next start."""
    if not key or not enabled() or not session._cookie_value:
        return
    try:
        os.makedirs(_directory(), exist_ok=True)
        path = _path(key)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"cookie_name": session._cookie_name,
                       "cookie_value": session._cookie_value,
                       "csrf_token": session._csrf_token,
                       "fetched_at": session._fetched_at}, handle)
    except OSError as exc:
        logger.debug("Could not keep the proxy session: %s", exc)


def forget(key: Optional[str]) -> None:
    """Drop a kept cookie: it was refused, replaced or logged out."""
    if not key:
        return
    try:
        os.remove(_path(key))
    except OSError:
        pass
