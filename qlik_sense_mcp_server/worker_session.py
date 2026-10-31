"""Transactional session lifecycle for a single incoming-JWT worker.

No tool is replayed after invalidation. The affected call is failed, all
state is dropped, and the next call must authenticate with Qlik again.
"""

import threading

from .jwt_session import JwtSession


class SessionInvalidated(RuntimeError):
    pass


class ManagedJwtSession(JwtSession):
    def __init__(self, config, invalidated):
        super().__init__(config, ttl_seconds=float("inf"))
        self._cache_key = None
        self._cache_checked = True
        self._invalidated = invalidated

    def invalidate(self):
        super().invalidate()
        self._invalidated()
        # Stop lower-level implicit reauthentication inside a logical call.
        # The guard will rebuild *all* dependent state at the next boundary.
        raise SessionInvalidated("Qlik session invalidated; retry the tool call")

    def logout(self):
        raise SessionInvalidated("Proxy-wide logout is disabled for incoming JWTs")


class WorkerSessionGuard:
    def __init__(self, context, config, *, session_factory=ManagedJwtSession,
                 repo_factory=None, engine_factory=None):
        if repo_factory is None:
            from .repository_api import QlikRepositoryAPI
            repo_factory = QlikRepositoryAPI
        if engine_factory is None:
            from .engine import QlikEngineAPI
            engine_factory = QlikEngineAPI
        self.context = context
        self.config = config
        self.session_factory = session_factory
        self.repo_factory = repo_factory
        self.engine_factory = engine_factory
        self.lock = threading.RLock()
        self.dirty = True
        self.generation = 0
        self.closed = False

    def _invalidated(self):
        # Called from the active tool thread, after JwtSession's lock exits.
        self.dirty = True
        engine = self.context.engine_api
        if engine is not None:
            engine.abort_socket()

    def _drop(self):
        engine, repo = self.context.engine_api, self.context.repo_api
        # Make stale clients unreachable before closing them.
        self.context.engine_api = None
        self.context.repo_api = None
        self.context.jwt_session = None
        self.context.form_session = None
        self.dirty = True
        if engine is not None:
            try:
                engine.disconnect()
            except Exception:
                pass
        if repo is not None:
            try:
                repo.client.close()
            except Exception:
                pass
        # App details are the only tool-module data cache; other mutable
        # Qlik caches belong to the Engine instance discarded above.
        import sys
        module = sys.modules.get("qlik_sense_mcp_server.tools.repository")
        if module is not None:
            module.forget_app_details()

    def _build(self):
        self._drop()
        if self.closed:
            raise SessionInvalidated("Qlik worker is closing")
        from .jwt_validity import token_expiration
        token_expiration(self.config.jwt_token)
        session = self.session_factory(self.config, self._invalidated)
        # Clients are published only after authentication completes.
        session.ensure_standalone()
        repo = self.repo_factory(self.config, jwt_session=session)
        try:
            engine = self.engine_factory(self.config, jwt_session=session)
        except BaseException:
            repo.client.close()
            raise
        self.context.jwt_session = session
        self.context.repo_api = repo
        self.context.engine_api = engine
        self.dirty = False
        self.generation += 1

    def initialize(self):
        with self.lock:
            self._build()

    def execute(self, function, *args, **kwargs):
        # Every tool, including cache reads and Repository-only calls, uses
        # this lock. An abandoned worker thread retains it until it exits.
        with self.lock:
            from .jwt_validity import token_expiration
            token_expiration(self.config.jwt_token)
            if self.dirty:
                self._build()
            try:
                result = function(*args, **kwargs)
                if self.dirty:
                    raise SessionInvalidated("Qlik session invalidated; retry the tool call")
                return result
            finally:
                if self.dirty:
                    self._drop()

    def close(self):
        with self.lock:
            self.closed = True
            self._drop()
