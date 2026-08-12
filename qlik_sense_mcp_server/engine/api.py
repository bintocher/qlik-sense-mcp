"""The Engine API client the rest of the server talks to.

`QlikEngineAPI` is assembled from mixins that each own one concern:
transport and the shared socket, hypercubes, fields, sheets, and app-level
metadata. The split is by responsibility, not by size — a change to how
the socket is kept alive should not require reading hypercube code, and
vice versa.

The class itself holds only construction: configuration, timeouts read
from the environment, and the state the transport layer guards.
"""

import logging
import os
import threading
from typing import Optional

from ..config import (
    QlikSenseConfig,
    DEFAULT_WS_TIMEOUT,
    DEFAULT_WS_IDLE_PROBE_AFTER,
    DEFAULT_WS_PROBE_TIMEOUT,
    DEFAULT_WS_GREETING_TIMEOUT,
    DEFAULT_WS_RETRIES,
)
from ..jwt_session import JwtSession
from .app_model import EngineAppModelMixin
from .connection import EngineConnectionMixin
from .fields import EngineFieldsMixin
from .hypercube import EngineHypercubeMixin
from .sheets import EngineSheetsMixin

logger = logging.getLogger(__name__)


class QlikEngineAPI(
    EngineConnectionMixin,
    EngineHypercubeMixin,
    EngineFieldsMixin,
    EngineSheetsMixin,
    EngineAppModelMixin,
):
    """Client for Qlik Sense Engine API using WebSocket."""

    def __init__(self, config: QlikSenseConfig, jwt_session: Optional[JwtSession] = None):
        self.config = config
        self.jwt_session = jwt_session  # required when config.auth_mode == jwt
        self.ws = None
        self.request_id = 0
        # Connection cache
        self._cached_app_id: Optional[str] = None
        self._cached_app_handle: int = -1
        self._cached_has_data: bool = False
        # Monotonic timestamp of the last frame Engine actually answered.
        # Liveness checks are skipped while it is recent — see _is_connected.
        self._last_successful_io: float = 0.0
        # Serialises whole tool calls against the single shared socket —
        # see transaction(). The mixin creates one lazily for instances
        # that skip __init__, so this is the ordinary path, not the only one.
        self._lock = threading.RLock()
        # Timeouts / retries from env
        ws_timeout_env = os.getenv("QLIK_WS_TIMEOUT")
        try:
            self.ws_timeout_seconds = float(ws_timeout_env) if ws_timeout_env else DEFAULT_WS_TIMEOUT
        except ValueError:
            self.ws_timeout_seconds = DEFAULT_WS_TIMEOUT
        # Single unified timeout — used for both connection and all Engine operations
        self.ws_operation_timeout = self.ws_timeout_seconds
        self.ws_retries = DEFAULT_WS_RETRIES
        # Liveness checks keep their own short deadlines: a real query may
        # legitimately take minutes, a health check never should. Fixed —
        # nobody should have to tune this to use the server.
        self.ws_idle_probe_after = DEFAULT_WS_IDLE_PROBE_AFTER
        self.ws_probe_timeout = DEFAULT_WS_PROBE_TIMEOUT
        self.ws_greeting_timeout = DEFAULT_WS_GREETING_TIMEOUT

