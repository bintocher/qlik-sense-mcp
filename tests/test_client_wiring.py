"""The clients must build from a real config object, not a stand-in.

Removing an unused config field passed all 500 tests and still broke every
live call: two places still read `config.jwt_session_cookie_override`, and
every test that touches JwtSession hands it a hand-made double that happily
answers any attribute. The failure only showed up against Qlik.

So these tests construct the genuine article — QlikSenseConfig.from_env —
and walk the same wiring the server uses at startup.
"""

import pytest

from qlik_sense_mcp_server.config import QlikSenseConfig
from qlik_sense_mcp_server.engine_api import QlikEngineAPI
from qlik_sense_mcp_server.jwt_session import JwtSession
from qlik_sense_mcp_server.repository_api import QlikRepositoryAPI


@pytest.fixture
def jwt_env(monkeypatch):
    monkeypatch.setenv("QLIK_SERVER_URL", "https://qlik.example/jwt")
    monkeypatch.setenv("QLIK_JWT_TOKEN", "header.payload.signature")
    for leftover in ("QLIK_CLIENT_CERT_PATH", "QLIK_CLIENT_KEY_PATH",
                     "QLIK_CA_CERT_PATH"):
        monkeypatch.delenv(leftover, raising=False)
    return QlikSenseConfig.from_env()


class TestJwtWiring:
    def test_a_session_builds_from_the_real_config(self, jwt_env):
        """No attribute the session reads may be missing from the config."""
        session = JwtSession(jwt_env)
        assert session.cookie_name is None




class TestStartupWiring:
    def test_the_server_initialises_its_clients(self, jwt_env, monkeypatch):
        """What `_init_clients` does on every server start, in one call."""
        from qlik_sense_mcp_server.tools import context

        monkeypatch.setattr(context, "config", None)
        monkeypatch.setattr(context, "engine_api", None)
        context._init_clients()
        assert context.engine_api is not None, (
            "клиенты не поднялись — смотри предупреждение в журнале")
        assert context.config.auth_mode == "jwt"
