"""A restarted server takes up its proxy session instead of opening another.

Measured: eight restarts within ten minutes - each one logged in to the
virtual proxy and none logged out - exhausted the five sessions Qlik allows
a user, and every call was refused with OnMaxParallelSessionsExceeded for
the next quarter of an hour. The refusal came back from get_app_details as
a success with no tables, and the server log said only "close status 1001".
"""

import json
import logging

import httpx

from qlik_sense_mcp_server import session_cache
from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.engine import connection
from qlik_sense_mcp_server.jwt_session import JwtSession
from qlik_sense_mcp_server.tools import context, helpers, repository

BASE = "https://qlik.example.com"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.e30.c2lnbmF0dXJl"


class _Proxy:
    def __init__(self):
        self.logins = 0
        self.logouts = 0

    def __call__(self, request):
        if request.url.path == "/jwt/qps/csrftoken":
            self.logins += 1
            return httpx.Response(204, headers=[
                ("qlik-csrf-token", "csrf"),
                ("set-cookie", f"X-Qlik-Session-jwt=s{self.logins}; Path=/")])
        if request.method == "DELETE" and request.url.path == "/jwt/qps/user":
            self.logouts += 1
            return httpx.Response(204)
        return httpx.Response(200, json={})


def _start(proxy, monkeypatch):
    """One start of the server: a new session holder, as a new process has."""
    session = JwtSession(QlikSenseConfig(server_url=f"{BASE}/jwt", jwt_token=TOKEN))
    transport = httpx.MockTransport(proxy)
    monkeypatch.setattr(session, "_build_bootstrap_client",
                        lambda: httpx.Client(transport=transport))
    return session


def _with_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("QLIK_SESSION_CACHE", "true")
    monkeypatch.setenv("QLIK_SESSION_CACHE_DIR", str(tmp_path))


def test_a_second_start_takes_up_the_first_ones_session(monkeypatch, tmp_path):
    _with_cache(monkeypatch, tmp_path)
    proxy = _Proxy()
    first = _start(proxy, monkeypatch)
    first.ensure_standalone()
    second = _start(proxy, monkeypatch)
    second.ensure_standalone()
    assert proxy.logins == 1
    assert second.cookie_value == first.cookie_value == "s1"


def test_an_old_or_refused_session_is_not_taken_up(monkeypatch, tmp_path):
    _with_cache(monkeypatch, tmp_path)
    proxy = _Proxy()
    first = _start(proxy, monkeypatch)
    first.ensure_standalone()
    first.invalidate()           # the proxy refused it
    _start(proxy, monkeypatch).ensure_standalone()
    assert proxy.logins == 2

    stale = _start(proxy, monkeypatch)
    stale._ttl = 0               # older than the TTL
    stale.ensure_standalone()
    assert proxy.logins == 3


def test_the_file_names_no_identity(monkeypatch, tmp_path):
    _with_cache(monkeypatch, tmp_path)
    _start(_Proxy(), monkeypatch).ensure_standalone()
    [kept] = list(tmp_path.iterdir())
    assert TOKEN not in kept.name and "qlik" not in kept.name
    assert json.loads(kept.read_text())["cookie_value"] == "s1"


def test_shutdown_closes_the_socket_and_logs_out(monkeypatch):
    proxy = _Proxy()
    session = _start(proxy, monkeypatch)
    session.ensure_standalone()

    class _Engine:
        closed = False

        def disconnect(self):
            _Engine.closed = True

    monkeypatch.setattr(context, "engine_api", _Engine())
    monkeypatch.setattr(context, "jwt_session", session)
    monkeypatch.setattr(context, "form_session", None)
    monkeypatch.setattr(context, "_shut_down", False)
    context.shutdown()
    context.shutdown()           # once only
    assert _Engine.closed and proxy.logouts == 1
    assert session.cookie_value is None


def test_the_session_limit_is_an_error_with_its_category(monkeypatch):
    refusal = ("Qlik refused the Engine session: this user already holds as many "
               "Qlik sessions as Qlik allows (Engine notification "
               "OnMaxParallelSessionsExceeded, severity=fatal)")
    reply = json.loads(helpers._err(refusal))
    assert reply["error_category"] == "session_limit"


def test_get_app_details_without_a_model_is_an_error(monkeypatch):
    class _Engine:
        def transaction(self):
            from contextlib import nullcontext
            return nullcontext()

        def ensure_app(self, app_id, **kwargs):
            raise ConnectionError("Qlik refused the Engine session "
                                  "(Engine notification OnMaxParallelSessionsExceeded)")

    class _Repo:
        def get_app_by_id(self, app_id):
            return {"id": app_id, "name": "App", "lastReloadTime": "x"}

    monkeypatch.setattr(repository.context, "engine_api", _Engine())
    monkeypatch.setattr(repository.context, "repo_api", _Repo())
    repository._DETAILS_CACHE.clear()
    reply = json.loads(repository.get_app_details("app-1"))
    assert reply["error_category"] == "session_limit"
    assert "tables" not in reply


def test_the_refusal_is_in_the_server_log(caplog):
    with caplog.at_level(logging.WARNING):
        connection._log_notification({"method": "OnMaxParallelSessionsExceeded",
                                      "params": {"severity": "fatal"}})
    assert "OnMaxParallelSessionsExceeded" in caplog.text
