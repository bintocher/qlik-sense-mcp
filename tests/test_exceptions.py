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








