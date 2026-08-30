"""Utility functions for the MCP server."""

import random
import string
from typing import List, Union


def escape_qlik_field_name(field_name: str) -> str:
    """A field name as it is written into a Qlik expression: in brackets.

    Always in brackets, never conditionally. A bare name is read as an
    expression, so `Тип ставки` comes back from Qlik as "Garbage after
    expression: 'ставки'" while `[Тип ставки]` is read as the field it is.
    Wrapping only "when it looks like it needs it" is the same guess with
    a smaller blast radius.

    A name that arrives already bracketed is left alone — the caller wrote
    what it meant, and the server has nothing to add.
    """
    if not field_name:
        return ""
    name = field_name.strip()
    if name.startswith("[") and name.endswith("]") and len(name) > 1:
        return name
    return f"[{name}]"


def bare_field_name(field_name: str) -> str:
    """The name without the brackets a caller may have written around it.

    For comparing against the model's own field list, and for saying which
    name was not found.
    """
    name = (field_name or "").strip()
    if name.startswith("[") and name.endswith("]") and len(name) > 1:
        return name[1:-1]
    return name


def generate_xrfkey() -> str:
    """Generate a random X-Qlik-Xrfkey with 16 alphanumeric characters."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))


# Name Qlik gives the virtual proxy session cookie. The suffix varies with
# the proxy, so the cookie is matched by this prefix rather than in full.
QLIK_SESSION_COOKIE_PREFIX = "X-Qlik-Session"


def looks_like_qlik_session_name(name: str) -> bool:
    """True when a cookie name itself identifies it as Qlik's session cookie.

    Only the two naming rules, without the "a single cookie can only be the
    session" fallback: that fallback is safe for one response's Set-Cookie
    header, but not for a client's jar, where a load balancer's own cookie can
    be the only one present.
    """
    lowered = name.lower()
    return (lowered.startswith(QLIK_SESSION_COOKIE_PREFIX.lower())
            or "qlik" in lowered)


def pick_qlik_session_cookie(cookie_names: List[str], get_value) -> "tuple[Union[str, None], Union[str, None]]":
    """
    Pick the Qlik virtual-proxy session cookie out of a jar's cookie names.

    Shared by every session-bootstrap flow (JWT, form-based login) that has
    to find the cookie Qlik just set without knowing its exact name up
    front — QMC lets an admin rename it per virtual proxy, and a load
    balancer in front of Qlik adds cookies of its own. Matched in three
    widening steps rather than assumed:

    1. The conventional ``X-Qlik-Session*`` prefix.
    2. A renamed Qlik cookie still tends to say so (contains "qlik").
    3. Exactly one cookie — it can only be the session.

    `get_value` resolves a name to its value (e.g. ``resp.cookies.get``).
    Returns ``(None, None)`` when nothing matches.
    """
    names = list(cookie_names)

    for name in names:
        if name.lower().startswith(QLIK_SESSION_COOKIE_PREFIX.lower()):
            return name, get_value(name)

    for name in names:
        if "qlik" in name.lower():
            return name, get_value(name)

    if len(names) == 1:
        return names[0], get_value(names[0])

    return None, None
