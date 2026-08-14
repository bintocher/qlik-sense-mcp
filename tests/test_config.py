"""Tests for configuration module."""

import os
import pytest
from unittest.mock import patch
from qlik_sense_mcp_server.config import (
    QlikSenseConfig,
    DEFAULT_REPOSITORY_PORT,
    DEFAULT_PROXY_PORT,
    DEFAULT_ENGINE_PORT,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_WS_TIMEOUT,
    DEFAULT_WS_RETRIES,
    DEFAULT_APPS_LIMIT,
    MAX_APPS_LIMIT,
    DEFAULT_FIELD_LIMIT,
    MAX_FIELD_LIMIT,
    DEFAULT_HYPERCUBE_MAX_ROWS,
    DEFAULT_FIELD_FETCH_SIZE,
    MAX_FIELD_FETCH_SIZE,
    MAX_TABLES_AND_KEYS_DIM,
    MAX_TABLES,
)


class TestConstants:
    def test_default_ports(self):
        assert DEFAULT_REPOSITORY_PORT == 4242
        assert DEFAULT_PROXY_PORT == 4243
        assert DEFAULT_ENGINE_PORT == 4747








class TestQlikSenseConfig:
    def test_required_fields(self):
        config = QlikSenseConfig(
            server_url="https://qlik.example.com",
            user_directory="DOMAIN",
            user_id="admin",
        )
        assert config.server_url == "https://qlik.example.com"
        assert config.user_directory == "DOMAIN"
        assert config.user_id == "admin"








