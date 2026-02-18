"""Tests for custom exceptions."""

import pytest
from qlik_sense_mcp_server.exceptions import (
    QlikError,
    QlikConnectionError,
    QlikAuthError,
    QlikEngineError,
    QlikRepositoryError,
    QlikAppNotFoundError,
    QlikConfigError,
)


class TestExceptionHierarchy:
    def test_base_exception(self):
        with pytest.raises(QlikError):
            raise QlikError("base error")

    def test_connection_error_is_qlik_error(self):
        with pytest.raises(QlikError):
            raise QlikConnectionError("connection failed")

    def test_auth_error_is_qlik_error(self):
        with pytest.raises(QlikError):
            raise QlikAuthError("auth failed")

    def test_engine_error_is_qlik_error(self):
        with pytest.raises(QlikError):
            raise QlikEngineError("engine error")

    def test_repository_error_is_qlik_error(self):
        with pytest.raises(QlikError):
            raise QlikRepositoryError("repo error")

    def test_app_not_found_is_qlik_error(self):
        with pytest.raises(QlikError):
            raise QlikAppNotFoundError("app not found")

    def test_config_error_is_qlik_error(self):
        with pytest.raises(QlikError):
            raise QlikConfigError("config error")

    def test_error_message(self):
        try:
            raise QlikConnectionError("Failed to connect to Engine API")
        except QlikConnectionError as e:
            assert str(e) == "Failed to connect to Engine API"

    def test_all_are_exception(self):
        """All custom exceptions should be catchable as Exception."""
        for exc_class in [QlikError, QlikConnectionError, QlikAuthError,
                          QlikEngineError, QlikRepositoryError,
                          QlikAppNotFoundError, QlikConfigError]:
            with pytest.raises(Exception):
                raise exc_class("test")
