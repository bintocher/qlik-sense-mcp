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
from qlik_sense_mcp_server.form_session import FormSession
from qlik_sense_mcp_server.repository_api import QlikRepositoryAPI


@pytest.fixture
def jwt_env(monkeypatch):
    monkeypatch.setenv("QLIK_SERVER_URL", "https://qlik.example/jwt")
    monkeypatch.setenv("QLIK_JWT_TOKEN", "header.payload.signature")
    for leftover in ("QLIK_CLIENT_CERT_PATH", "QLIK_CLIENT_KEY_PATH",
                     "QLIK_CA_CERT_PATH", "QLIK_PASSWORD"):
        monkeypatch.delenv(leftover, raising=False)
    return QlikSenseConfig.from_env()


@pytest.fixture
def form_env(monkeypatch):
    monkeypatch.setenv("QLIK_SERVER_URL", "https://qlik.example/forms")
    monkeypatch.setenv("QLIK_USER_DIRECTORY", "COMPANY")
    monkeypatch.setenv("QLIK_USER_ID", "ivanov")
    monkeypatch.setenv("QLIK_PASSWORD", "s3cret")
    for leftover in ("QLIK_CLIENT_CERT_PATH", "QLIK_CLIENT_KEY_PATH",
                     "QLIK_CA_CERT_PATH", "QLIK_JWT_TOKEN"):
        monkeypatch.delenv(leftover, raising=False)
    return QlikSenseConfig.from_env()


class TestJwtWiring:
    def test_a_session_builds_from_the_real_config(self, jwt_env):
        """No attribute the session reads may be missing from the config."""
        session = JwtSession(jwt_env)
        assert session.cookie_name is None




class TestFormWiring:
    def test_a_session_builds_from_the_real_config(self, form_env):
        session = FormSession(form_env)
        assert session.cookie_name is None

    def test_invalidate_survives_the_real_config(self, form_env):
        session = FormSession(form_env)
        session.invalidate()
        assert session.cookie_name is None

    def test_both_clients_accept_the_session(self, form_env):
        session = FormSession(form_env)
        assert QlikRepositoryAPI(form_env, form_session=session) is not None
        assert QlikEngineAPI(form_env, form_session=session) is not None

    def test_repository_api_requires_a_session_in_form_mode(self, form_env):
        from qlik_sense_mcp_server.exceptions import QlikConnectionError
        with pytest.raises(QlikConnectionError):
            QlikRepositoryAPI(form_env)

    def test_qrs_url_uses_the_named_prefix(self, form_env):
        session = FormSession(form_env)
        api = QlikRepositoryAPI(form_env, form_session=session)
        assert api._get_api_url("about") == "https://qlik.example/forms/qrs/about"

    def test_qrs_url_on_the_central_proxy_has_no_double_slash(self):
        config = QlikSenseConfig(server_url="https://qlik.example",
                                 user_id="ivanov", password="s3cret")
        session = FormSession(config)
        api = QlikRepositoryAPI(config, form_session=session)
        assert api._get_api_url("about") == "https://qlik.example/qrs/about"


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

    def test_the_server_initialises_its_clients_in_form_mode(self, form_env, monkeypatch):
        from qlik_sense_mcp_server.tools import context

        monkeypatch.setattr(context, "config", None)
        monkeypatch.setattr(context, "engine_api", None)
        monkeypatch.setattr(context, "form_session", None)
        context._init_clients()
        assert context.engine_api is not None, (
            "клиенты не поднялись — смотри предупреждение в журнале")
        assert context.config.auth_mode == "form"
        assert context.form_session is not None
        assert context.jwt_session is None

    def test_server_module_exposes_the_live_form_session(self, form_env, monkeypatch):
        """`server.form_session` must follow the context like its JWT twin.

        Consumers reach the live session through the server module: the
        test fixture hands the Qlik session back that way after a live run,
        and without the re-export it silently could not, so form logins
        piled up until Qlik refused new sessions.
        """
        import qlik_sense_mcp_server.server as server
        from qlik_sense_mcp_server.tools import context

        monkeypatch.setattr(context, "config", None)
        monkeypatch.setattr(context, "engine_api", None)
        monkeypatch.setattr(context, "form_session", None)
        context._init_clients()
        assert server.form_session is context.form_session
        assert server.form_session is not None
