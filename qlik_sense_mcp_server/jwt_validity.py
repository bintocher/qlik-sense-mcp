"""Local time checks for a Qlik-authenticated incoming bearer token.

These checks do not authenticate a JWT or verify its signature. A token must
be accepted by the configured Qlik virtual proxy before it can be cached or
used for tool calls. This module only enforces the validity window locally.
"""

import base64
import binascii
import json
import math
import time


class InvalidToken(ValueError):
    """The bearer token cannot be used, without including its value."""


def token_expiration(token: str, *, now: float | None = None) -> float:
    """Return a JWT's mandatory expiration, or raise a sanitized error.

    Claims are unverified: calling this function alone never authorizes a
    request. Qlik must authenticate the exact token on the first cache miss.
    There is deliberately no expiration grace period on cache hits.
    """
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise InvalidToken("Invalid bearer token")
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise InvalidToken("Invalid bearer token")
    try:
        payload = parts[1].encode("ascii")
        payload += b"=" * (-len(payload) % 4)
        claims = json.loads(base64.b64decode(payload, altchars=b"-_", validate=True))
    except (UnicodeError, binascii.Error, ValueError, RecursionError):
        raise InvalidToken("Invalid bearer token") from None
    if not isinstance(claims, dict):
        raise InvalidToken("Invalid bearer token")

    now = time.time() if now is None else now
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        raise InvalidToken("Bearer token must have a valid expiration")
    if exp <= now:
        raise InvalidToken("Bearer token has expired")
    nbf = claims.get("nbf")
    if nbf is not None:
        if isinstance(nbf, bool) or not isinstance(nbf, (int, float)) or not math.isfinite(nbf):
            raise InvalidToken("Invalid bearer token validity window")
        if nbf > now:
            raise InvalidToken("Bearer token is not yet valid")
    return float(exp)
