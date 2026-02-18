"""Tests for server module."""

import pytest
from unittest.mock import patch, MagicMock
from qlik_sense_mcp_server.server import _make_error, QlikSenseMCPServer
from qlik_sense_mcp_server import __version__


class TestMakeError:
    def test_basic_error(self):
        result = _make_error("something went wrong")
        assert result == {"error": "something went wrong"}

    def test_error_with_extras(self):
        result = _make_error("failed", app_id="abc-123", details="more info")
        assert result["error"] == "failed"
        assert result["app_id"] == "abc-123"
        assert result["details"] == "more info"

    def test_error_always_has_error_key(self):
        result = _make_error("test")
        assert "error" in result


class TestVersion:
    def test_version_format(self):
        parts = __version__.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit()

    def test_version_is_1_4_0(self):
        assert __version__ == "1.4.0"


class TestQlikSenseMCPServer:
    @patch.dict("os.environ", {
        "QLIK_SERVER_URL": "",
        "QLIK_USER_DIRECTORY": "",
        "QLIK_USER_ID": "",
    }, clear=False)
    def test_invalid_config(self):
        server = QlikSenseMCPServer()
        assert server.config_valid is False
        assert server.repository_api is None
        assert server.engine_api is None

    @patch.dict("os.environ", {
        "QLIK_SERVER_URL": "https://qlik.example.com",
        "QLIK_USER_DIRECTORY": "DOMAIN",
        "QLIK_USER_ID": "admin",
        "QLIK_VERIFY_SSL": "false",
    }, clear=False)
    def test_valid_config_creates_apis(self):
        server = QlikSenseMCPServer()
        assert server.config_valid is True
        assert server.repository_api is not None
        assert server.engine_api is not None

    def test_validate_config_no_config(self):
        server = QlikSenseMCPServer.__new__(QlikSenseMCPServer)
        server.config = None
        assert server._validate_config() is False

    @patch.dict("os.environ", {
        "QLIK_SERVER_URL": "https://qlik.example.com",
        "QLIK_USER_DIRECTORY": "DOMAIN",
        "QLIK_USER_ID": "admin",
        "QLIK_VERIFY_SSL": "false",
    }, clear=False)
    def test_server_has_server_instance(self):
        server = QlikSenseMCPServer()
        assert server.server is not None
