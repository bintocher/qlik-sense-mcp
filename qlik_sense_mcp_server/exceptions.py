"""Custom exceptions for Qlik Sense MCP Server."""


class QlikError(Exception):
    """Base exception for Qlik Sense MCP Server."""


class QlikConnectionError(QlikError):
    """Raised when connection to Qlik Sense fails."""


class QlikSessionLimitError(QlikConnectionError):
    """Raised when Engine refuses the socket with OnMaxParallelSessionsExceeded.

    Qlik allows a limited number of concurrent Engine sessions per user
    (5 by default). Past that, the greeting on a fresh WebSocket is a fatal
    notification followed by an immediate close, and no request will ever
    be answered on that socket.
    """


class QlikLicenseError(QlikConnectionError):
    """Raised when Engine refuses the socket with OnLicenseAccessDenied.

    The identity authenticated fine, it simply has no license to use Qlik.
    Logging in again cannot change that, so this is kept apart from the
    session errors that a fresh login does fix.
    """


class QlikEngineError(QlikError):
    """Raised when Engine API returns an error."""


class QlikProbeUnavailable(Exception):
    """A check this server relies on could not be run at all.

    Told apart from Qlik answering "no": a question that never arrived says
    nothing about the data, and treating silence as an answer is how a
    period becomes a numeric range and a wrong number looks plausible.
    """
