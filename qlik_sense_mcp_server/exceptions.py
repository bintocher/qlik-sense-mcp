"""Custom exceptions for Qlik Sense MCP Server."""


class QlikError(Exception):
    """Base exception for Qlik Sense MCP Server."""


class QlikConnectionError(QlikError):
    """Raised when connection to Qlik Sense fails."""


class QlikAuthError(QlikError):
    """Raised when authentication fails."""


class QlikSessionLimitError(QlikConnectionError):
    """Raised when Engine refuses the socket with OnMaxParallelSessionsExceeded.

    Qlik allows a limited number of concurrent Engine sessions per user
    (5 by default). Past that, the greeting on a fresh WebSocket is a fatal
    notification followed by an immediate close, and no request will ever
    be answered on that socket.
    """


class QlikEngineError(QlikError):
    """Raised when Engine API returns an error."""


class QlikRepositoryError(QlikError):
    """Raised when Repository API returns an error."""


class QlikAppNotFoundError(QlikError):
    """Raised when application is not found."""


class QlikConfigError(QlikError):
    """Raised when configuration is invalid or missing."""
