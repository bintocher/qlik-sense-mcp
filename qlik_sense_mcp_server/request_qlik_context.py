"""Compatibility exports for the single coordinated worker context.

The abandoned standalone client factory has been removed. Incoming-JWT
workers use WorkerSessionGuard; there is no second authentication path.
"""

from .worker_session import ManagedJwtSession, SessionInvalidated, WorkerSessionGuard

__all__ = ["ManagedJwtSession", "SessionInvalidated", "WorkerSessionGuard"]
