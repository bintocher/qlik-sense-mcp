"""Build isolated Qlik clients from an incoming JWT.

This is not an MCP authentication layer. The transport must acquire a
TokenContextCache lease only after reading the current request's bearer token,
and must keep that lease alive until the tool's worker actually exits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .config import QlikSenseConfig
from .engine import QlikEngineAPI
from .jwt_session import JwtSession
from .repository_api import QlikRepositoryAPI


@dataclass
class RequestQlikContext:
    """Qlik resources and user-sensitive caches belonging to one JWT."""

    config: QlikSenseConfig
    jwt_session: JwtSession
    repo_api: QlikRepositoryAPI
    engine_api: QlikEngineAPI
    details_cache: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        """Release local resources without proxy-wide Qlik logout."""
        try:
            self.engine_api.disconnect()
        finally:
            self.repo_api.client.close()
            self.details_cache.clear()


def build_request_context(token: str) -> RequestQlikContext:
    """Authenticate the exact token against Qlik before creating a context.

    A fresh JwtSession must never restore a prior disk cookie. Otherwise a
    token arriving on a cache miss could inherit a session without Qlik
    validating that token. Certificate verification uses the existing
    QLIK_VERIFY_SSL and QLIK_CA_CERT_PATH configuration unchanged.
    """
    if os.getenv("QLIK_PROXY_LOGOUT", "true").strip().lower() not in (
        "false", "0", "no", "off"
    ):
        raise ValueError("Incoming JWT mode requires QLIK_PROXY_LOGOUT=false")

    config = QlikSenseConfig.from_env().model_copy(
        update={"jwt_token": token, "password": None}
    )
    config.validate_runtime()
    session = JwtSession(config)
    # JwtSession supports a None cache key: restore/store/forget become no-ops.
    # Set this before any ensure call so the first authentication reaches Qlik.
    session._cache_key = None
    session._cache_checked = True
    session.ensure_standalone()

    repo = QlikRepositoryAPI(config, jwt_session=session)
    try:
        engine = QlikEngineAPI(config, jwt_session=session)
    except BaseException:
        repo.client.close()
        raise
    return RequestQlikContext(config, session, repo, engine)
