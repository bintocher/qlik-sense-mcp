"""Configuration for Qlik Sense MCP Server."""

import os
from typing import Optional
from pydantic import BaseModel, Field


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
    repository_port: int = Field(4242, description="Repository API port")
    proxy_port: int = Field(4243, description="Proxy API port")
    engine_port: int = Field(4747, description="Engine API port")
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
            repository_port=int(os.getenv("QLIK_REPOSITORY_PORT", "4242")),
            proxy_port=int(os.getenv("QLIK_PROXY_PORT", "4243")),
            engine_port=int(os.getenv("QLIK_ENGINE_PORT", "4747")),
            verify_ssl=os.getenv("QLIK_VERIFY_SSL", "true").lower() == "true"
        )
