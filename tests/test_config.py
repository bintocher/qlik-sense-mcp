"""Tests for configuration module."""

import os
import pytest
from unittest.mock import patch
from qlik_sense_mcp_server.config import (
    QlikSenseConfig,
    DEFAULT_REPOSITORY_PORT,
    DEFAULT_ENGINE_PORT,
)


class TestConstants:
    def test_default_ports(self):
        assert DEFAULT_REPOSITORY_PORT == 4242
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


class TestAuthModeResolution:
    """auth_mode is derived from which credential is present — jwt takes
    priority over form, which takes priority over certificate."""

    def test_no_credentials_is_certificate_mode(self):
        config = QlikSenseConfig(server_url="https://qlik.example.com",
                                 user_directory="DOMAIN", user_id="admin")
        assert config.auth_mode == "certificate"

    def test_password_alone_is_form_mode(self):
        config = QlikSenseConfig(server_url="https://qlik.example.com/jwt",
                                 user_id="ivanov", password="s3cret")
        assert config.auth_mode == "form"

    def test_jwt_token_wins_over_password(self):
        config = QlikSenseConfig(server_url="https://qlik.example.com/jwt",
                                 user_id="ivanov", password="s3cret",
                                 jwt_token="eyJhbGciOiJSUzI1NiJ9.e30.c2lnbmF0dXJl")
        assert config.auth_mode == "jwt"

    @patch.dict(os.environ, {
        "QLIK_SERVER_URL": "https://qlik.example.com/forms",
        "QLIK_USER_DIRECTORY": "COMPANY",
        "QLIK_USER_ID": "ivanov",
        "QLIK_PASSWORD": "s3cret",
    }, clear=True)
    def test_from_env_password_selects_form_mode(self):
        config = QlikSenseConfig.from_env()
        assert config.auth_mode == "form"
        assert config.password == "s3cret"


class TestFormModeValidation:
    def test_requires_user_id(self):
        config = QlikSenseConfig(server_url="https://qlik.example.com/forms",
                                 password="s3cret")
        with pytest.raises(ValueError, match="QLIK_USER_ID"):
            config.validate_runtime()

    def test_without_a_password_it_falls_back_to_certificate_validation(self):
        """No QLIK_PASSWORD means auth_mode is certificate, not form — so a
        user_id-only config is validated against cert-mode's rules (which
        also require a directory), not form-mode's."""
        config = QlikSenseConfig(server_url="https://qlik.example.com/forms",
                                 user_id="ivanov")
        assert config.auth_mode == "certificate"
        with pytest.raises(ValueError, match="QLIK_USER_DIRECTORY"):
            config.validate_runtime()

    def test_does_not_require_a_virtual_proxy_prefix(self):
        """Unlike JWT, a form auth module can sit on the central proxy."""
        config = QlikSenseConfig(server_url="https://qlik.example.com",
                                 user_id="ivanov", password="s3cret")
        config.validate_runtime()  # must not raise

    def test_valid_form_config_passes(self):
        config = QlikSenseConfig(server_url="https://qlik.example.com/forms",
                                 user_directory="COMPANY", user_id="ivanov",
                                 password="s3cret")
        config.validate_runtime()  # must not raise


class TestVirtualProxyPathSegment:
    def test_empty_prefix_yields_empty_segment(self):
        config = QlikSenseConfig(server_url="https://qlik.example.com",
                                 user_id="ivanov", password="s3cret")
        assert config.virtual_proxy_path_segment == ""

    def test_named_prefix_yields_trailing_slash(self):
        config = QlikSenseConfig(server_url="https://qlik.example.com/forms",
                                 user_id="ivanov", password="s3cret")
        assert config.virtual_proxy_path_segment == "forms/"
