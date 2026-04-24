"""Configuration for Qlik Sense MCP Server."""

import logging
import os
from typing import Optional
from urllib.parse import urlparse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Default ports
DEFAULT_REPOSITORY_PORT = 4242
DEFAULT_PROXY_PORT = 4243
DEFAULT_ENGINE_PORT = 4747

# Default timeouts (seconds)
DEFAULT_HTTP_TIMEOUT = 10.0
DEFAULT_WS_TIMEOUT = 180.0
DEFAULT_TICKET_TIMEOUT = 30.0

# Default retry settings
DEFAULT_WS_RETRIES = 2

# Pagination defaults
DEFAULT_APPS_LIMIT = 25
MAX_APPS_LIMIT = 50
DEFAULT_FIELD_LIMIT = 10
MAX_FIELD_LIMIT = 100
DEFAULT_HYPERCUBE_MAX_ROWS = 1000

# Fetch sizes
DEFAULT_FIELD_FETCH_SIZE = 500
MAX_FIELD_FETCH_SIZE = 5000

# Data model limits
MAX_TABLES_AND_KEYS_DIM = 1000
MAX_TABLES = 50

# JWT defaults (mirror Qlik's own JWT virtual proxy examples and docs/AUTH_JWT.md)
DEFAULT_JWT_USER_ID_CLAIM = "userId"
DEFAULT_JWT_USER_DIR_CLAIM = "userDirectory"

# Authentication modes
AUTH_MODE_CERTIFICATE = "certificate"
AUTH_MODE_JWT = "jwt"


class QlikSenseConfig(BaseModel):
    """
    Configuration model for Qlik Sense Enterprise server connection.

    Supports two authentication modes, selected automatically based on which
    environment variables are provided:

    1. ``certificate`` (default, legacy) — client certificate + X-Qlik-User
       header impersonation directly against ports 4242 (QRS) and 4747
       (Engine). Suitable for admin/service use on the Qlik server host.

    2. ``jwt`` — bearer JWT signed by the admin with a private key whose
       certificate is installed on a Qlik JWT virtual proxy. Each analyst
       receives only a long-lived token and talks to Qlik via the VP
       (standard HTTPS/WSS on 443). Recommended for end users.

    The mode is selected by presence of QLIK_JWT_TOKEN in the environment:

        QLIK_JWT_TOKEN set  → jwt mode
        otherwise           → certificate mode
    """

    # Common
    server_url: str = Field(..., description="Qlik Sense server URL. In JWT mode include the "
                                             "virtual proxy prefix as URL path, e.g. "
                                             "'https://qlik.company.com/jwt'.")
    user_directory: str = Field("", description="User directory for X-Qlik-User (certificate mode)")
    user_id: str = Field("", description="User ID for X-Qlik-User (certificate mode)")
    verify_ssl: bool = Field(True, description="Verify SSL certificates")
    ca_cert_path: Optional[str] = Field(None, description="Path to CA certificate (optional, both modes)")

    # Certificate mode
    client_cert_path: Optional[str] = Field(None, description="Path to client certificate (certificate mode)")
    client_key_path: Optional[str] = Field(None, description="Path to client private key (certificate mode)")
    repository_port: int = Field(DEFAULT_REPOSITORY_PORT, description="Repository API port (certificate mode)")
    proxy_port: int = Field(DEFAULT_PROXY_PORT, description="Proxy API port (certificate mode)")
    engine_port: int = Field(DEFAULT_ENGINE_PORT, description="Engine API port (certificate mode)")
    http_port: Optional[int] = Field(None, description="HTTP API port for metadata requests (certificate mode)")

    # JWT mode
    jwt_token: Optional[str] = Field(None, description="Signed JWT bearer (jwt mode)")
    jwt_user_id_claim: str = Field(DEFAULT_JWT_USER_ID_CLAIM,
                                   description="JWT payload claim holding user id — must match QMC 'JWT "
                                               "attribute for user ID'")
    jwt_user_dir_claim: str = Field(DEFAULT_JWT_USER_DIR_CLAIM,
                                    description="JWT payload claim holding user directory — must match "
                                                "QMC 'JWT attribute for user directory'")
    jwt_session_cookie_override: Optional[str] = Field(None,
                                                       description="Optional override for the VP session cookie "
                                                                   "name; when None the name is auto-detected from "
                                                                   "the bootstrap response Set-Cookie header.")

    @property
    def auth_mode(self) -> str:
        """
        Resolve authentication mode from the presence of a JWT token.

        The presence of ``jwt_token`` is the single source of truth — there
        is no standalone ``QLIK_AUTH_MODE`` switch to keep the user-facing
        surface minimal (one less env var to get wrong).
        """
        return AUTH_MODE_JWT if self.jwt_token else AUTH_MODE_CERTIFICATE

    @property
    def qlik_base_host(self) -> str:
        """
        Return ``scheme://host[:port]`` stripped of any URL path.

        ``server_url`` may contain a virtual proxy prefix as path (JWT mode),
        e.g. ``https://qlik.company.com/jwt``. All legacy URL builders (QRS
        on 4242, Engine WSS on 4747) need the bare host without the prefix.
        """
        parsed = urlparse(self.server_url)
        if not parsed.scheme or not parsed.hostname:
            # Fall back to the raw value if it was given as bare host — the
            # original certificate-mode code accepted this form.
            return self.server_url.rstrip("/")
        host = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            host += f":{parsed.port}"
        return host

    @property
    def qlik_hostname(self) -> str:
        """Return just the hostname (no scheme, no port, no path)."""
        parsed = urlparse(self.server_url)
        if parsed.hostname:
            return parsed.hostname
        # Best-effort fallback for raw 'host' or 'host:port' values.
        return self.server_url.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]

    @property
    def virtual_proxy_prefix(self) -> str:
        """
        Return the virtual proxy prefix embedded in ``server_url`` path.

        ``https://qlik.company.com/jwt``    → ``"jwt"``
        ``https://qlik.company.com``        → ``""``
        ``https://qlik.company.com/a/b``    → ``"a/b"`` (multi-segment is
        unusual for Qlik VPs — ``validate_runtime`` warns when it happens).
        """
        parsed = urlparse(self.server_url)
        return (parsed.path or "").strip("/")

    def validate_runtime(self) -> None:
        """
        Validate the assembled configuration for the chosen auth mode.

        Called once at startup from ``server._init_clients``. Raises
        ``ValueError`` with a human-readable message that the CLI converts
        into a clear log line the user can act on.
        """
        if not self.server_url:
            raise ValueError("QLIK_SERVER_URL is required")

        parsed_server = urlparse(self.server_url)
        if not parsed_server.scheme:
            raise ValueError(
                "QLIK_SERVER_URL must include a scheme (e.g. https://qlik.company.com)"
            )
        if parsed_server.scheme not in ("https", "http"):
            raise ValueError(
                f"QLIK_SERVER_URL scheme must be https or http, got "
                f"{parsed_server.scheme!r}"
            )

        if self.auth_mode == AUTH_MODE_JWT:
            if not self.virtual_proxy_prefix:
                raise ValueError(
                    "JWT mode requires a virtual proxy prefix in QLIK_SERVER_URL. "
                    "Example: QLIK_SERVER_URL=https://qlik.company.com/jwt"
                )
            if "/" in self.virtual_proxy_prefix:
                logger.warning(
                    "QLIK_SERVER_URL has a multi-segment path %r — Qlik virtual "
                    "proxies use a single-segment prefix. The MCP will pass the "
                    "full path as-is; expect connection failures if Qlik does "
                    "not recognize it.",
                    self.virtual_proxy_prefix,
                )
            if not self.jwt_token:
                raise ValueError("JWT mode requires QLIK_JWT_TOKEN")
            # user_directory / user_id are intentionally NOT required in JWT
            # mode — Qlik extracts the identity from the JWT payload itself.
        else:
            if not (self.user_directory and self.user_id):
                raise ValueError(
                    "certificate mode requires QLIK_USER_DIRECTORY and QLIK_USER_ID"
                )
            # client_cert/key are optional at the pydantic level (some
            # deployments pre-configure them system-wide), but _create_httpx_client
            # and engine_api.connect() handle both with/without cert.

    @classmethod
    def from_env(cls) -> "QlikSenseConfig":
        """
        Create configuration instance from environment variables.

        The presence of ``QLIK_JWT_TOKEN`` selects JWT mode automatically —
        no separate ``QLIK_AUTH_MODE`` variable is needed.
        """
        return cls(
            server_url=os.getenv("QLIK_SERVER_URL", ""),
            user_directory=os.getenv("QLIK_USER_DIRECTORY", ""),
            user_id=os.getenv("QLIK_USER_ID", ""),
            client_cert_path=os.getenv("QLIK_CLIENT_CERT_PATH"),
            client_key_path=os.getenv("QLIK_CLIENT_KEY_PATH"),
            ca_cert_path=os.getenv("QLIK_CA_CERT_PATH"),
            repository_port=int(os.getenv("QLIK_REPOSITORY_PORT", str(DEFAULT_REPOSITORY_PORT))),
            proxy_port=int(os.getenv("QLIK_PROXY_PORT", str(DEFAULT_PROXY_PORT))),
            engine_port=int(os.getenv("QLIK_ENGINE_PORT", str(DEFAULT_ENGINE_PORT))),
            http_port=int(os.getenv("QLIK_HTTP_PORT")) if os.getenv("QLIK_HTTP_PORT") else None,
            verify_ssl=os.getenv("QLIK_VERIFY_SSL", "true").lower() == "true",
            jwt_token=os.getenv("QLIK_JWT_TOKEN") or None,
            jwt_user_id_claim=os.getenv("QLIK_JWT_USER_ID_CLAIM", DEFAULT_JWT_USER_ID_CLAIM),
            jwt_user_dir_claim=os.getenv("QLIK_JWT_USER_DIR_CLAIM", DEFAULT_JWT_USER_DIR_CLAIM),
            jwt_session_cookie_override=os.getenv("QLIK_JWT_SESSION_COOKIE") or None,
        )
