"""The exception hierarchy the server raises and catches."""

import pytest

from qlik_sense_mcp_server.exceptions import (
    QlikConnectionError,
    QlikEngineError,
    QlikError,
    QlikLicenseError,
    QlikProbeUnavailable,
    QlikSessionLimitError,
)


class TestExceptionHierarchy:
    """Every Qlik failure is catchable as QlikError, and the two fatal
    greetings stay catchable as connection errors: a caller retrying a
    dropped socket must not swallow "no license" as if it were one."""

    def test_engine_and_connection_errors_are_qlik_errors(self):
        assert issubclass(QlikConnectionError, QlikError)
        assert issubclass(QlikEngineError, QlikError)

    def test_fatal_greetings_are_connection_errors(self):
        assert issubclass(QlikSessionLimitError, QlikConnectionError)
        assert issubclass(QlikLicenseError, QlikConnectionError)

    def test_session_limit_is_caught_as_a_connection_error(self):
        with pytest.raises(QlikConnectionError):
            raise QlikSessionLimitError("OnMaxParallelSessionsExceeded")

    def test_license_error_is_not_a_session_limit(self):
        assert not issubclass(QlikLicenseError, QlikSessionLimitError)

    def test_probe_unavailable_stands_apart_from_qlik_errors(self):
        # A check that never ran says nothing about the data, so it must not
        # be caught by handlers that treat a QlikError as Qlik's answer.
        assert not issubclass(QlikProbeUnavailable, QlikError)
