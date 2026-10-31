"""Local validity checks only; Qlik authenticates the exact token on cache miss."""

import base64
import binascii
import json
import math
import re
import time

_COMPACT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", re.ASCII)


class InvalidToken(ValueError):
    """A sanitized credential error that never contains the token."""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidToken("Invalid bearer token validity window")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise InvalidToken("Invalid bearer token validity window") from None
    if not math.isfinite(number):
        raise InvalidToken("Invalid bearer token validity window")
    return number


def token_expiration(token: str, *, now: float | None = None) -> float:
    """Enforce a mandatory finite exp and optional nbf without authenticating claims.

    Reject whitespace/control characters throughout the compact token, not
    just in its payload. No token is authorized by these checks alone.
    """
    if not isinstance(token, str) or len(token) > 16384 or not _COMPACT.fullmatch(token):
        raise InvalidToken("Invalid bearer token")
    try:
        payload = token.split(".")[1].encode("ascii")
        payload += b"=" * (-len(payload) % 4)
        claims = json.loads(base64.b64decode(payload, altchars=b"-_", validate=True))
    except (UnicodeError, binascii.Error, ValueError, RecursionError):
        raise InvalidToken("Invalid bearer token") from None
    if not isinstance(claims, dict):
        raise InvalidToken("Invalid bearer token")
    now = time.time() if now is None else now
    exp = _number(claims.get("exp"))
    if exp <= now:
        raise InvalidToken("Bearer token has expired")
    if "nbf" in claims and _number(claims["nbf"]) > now:
        raise InvalidToken("Bearer token is not yet valid")
    return exp
