"""Custom exceptions for Qlik Sense MCP Server."""


class QlikError(Exception):
    """Base exception for Qlik Sense MCP Server."""


class QlikConnectionError(QlikError):
    """Raised when connection to Qlik Sense fails."""


class QlikAuthError(QlikError):
    """Raised when authentication fails."""


class QlikEngineError(QlikError):
    """Raised when Engine API returns an error."""


class QlikRepositoryError(QlikError):
    """Raised when Repository API returns an error."""


class QlikAppNotFoundError(QlikError):
    """Raised when application is not found."""


class QlikConfigError(QlikError):
    """Raised when configuration is invalid or missing."""
