"""Configuration for Qlik Sense MCP Server."""

import os
from typing import Optional
from pydantic import BaseModel, Field


# Default ports
DEFAULT_REPOSITORY_PORT = 4242
DEFAULT_PROXY_PORT = 4243
DEFAULT_ENGINE_PORT = 4747

# Default timeouts (seconds)
DEFAULT_HTTP_TIMEOUT = 10.0
DEFAULT_WS_TIMEOUT = 8.0
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


class QlikSenseConfig(BaseModel):
    """
    Configuration model for Qlik Sense Enterprise server connection.

    Handles server connection details, authentication credentials,
    certificate paths, and API endpoint configuration.
    """

    server_url: str = Field(..., description="Qlik Sense server URL (e.g., https://qlik.company.com)")
    user_directory: str = Field(..., description="User directory for authentication")
    user_id: str = Field(..., description="User ID for authentication")
    client_cert_path: Optional[str] = Field(None, description="Path to client certificate")
    client_key_path: Optional[str] = Field(None, description="Path to client private key")
    ca_cert_path: Optional[str] = Field(None, description="Path to CA certificate")
    repository_port: int = Field(DEFAULT_REPOSITORY_PORT, description="Repository API port")
    proxy_port: int = Field(DEFAULT_PROXY_PORT, description="Proxy API port")
    engine_port: int = Field(DEFAULT_ENGINE_PORT, description="Engine API port")
    http_port: Optional[int] = Field(None, description="HTTP API port for metadata requests")
    verify_ssl: bool = Field(True, description="Verify SSL certificates")

    @classmethod
    def from_env(cls) -> "QlikSenseConfig":
        """
        Create configuration instance from environment variables.

        Reads all required and optional configuration values from environment
        variables with QLIK_ prefix and validates them.

        Returns:
            Configured QlikSenseConfig instance
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
            verify_ssl=os.getenv("QLIK_VERIFY_SSL", "true").lower() == "true"
        )
