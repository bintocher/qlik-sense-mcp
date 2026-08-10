"""Shared fixtures, and the wiring for the end-to-end suite.

The e2e tests talk to a real Qlik Sense server. They are the ones that
matter: this project is a thin translation layer over two Qlik protocols,
so almost every real defect found so far lived in what Qlik actually does
with our frames, not in our own branching. A fake socket cannot tell you
that a WebSocket ping wedges the next request through a virtual proxy, that
a reused session-object id hands back a stale calculation, or that the
per-user session limit answers with a fatal greeting instead of an error.

They are skipped unless the environment names a server:

    QLIK_E2E_URL         https://host/<vp-prefix>   (JWT) or https://host (certificate)
    QLIK_E2E_APP         app GUID to run against (small app, reloaded, with data)

    # JWT mode — either one:
    QLIK_E2E_TOKEN_FILE  path to a file holding the JWT
    QLIK_E2E_TOKEN       the JWT itself

    # certificate mode:
    QLIK_E2E_CERTS_DIR   directory with client.pem / client_key.pem / root.pem
    QLIK_E2E_USER_DIR    Qlik user directory
    QLIK_E2E_USER_ID     Qlik user id

    # optional:
    QLIK_E2E_SHEET_APP   app GUID that has sheets with objects (defaults to QLIK_E2E_APP)
    QLIK_E2E_TIMEOUT     per-call timeout, seconds (default 120)

Run them with `uv run pytest tests -m e2e`; `-m "not e2e"` is the offline
subset. See the `qlik-live` skill for the qlik1 values and for clearing
Engine sessions before a run.
"""

import json
import os
import pathlib

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: talks to a real Qlik Sense server; skipped unless QLIK_E2E_URL is set",
    )
    config.addinivalue_line(
        "markers",
        "slow: heavy queries against a large app; needs QLIK_E2E_BIG_APP",
    )


def _e2e_env():
    """Build the QLIK_* environment for a live run, or return None."""
    url = os.getenv("QLIK_E2E_URL")
    app = os.getenv("QLIK_E2E_APP")
    if not url or not app:
        return None

    env = {
        "QLIK_SERVER_URL": url,
        "QLIK_VERIFY_SSL": os.getenv("QLIK_E2E_VERIFY_SSL", "false"),
        "QLIK_WS_TIMEOUT": os.getenv("QLIK_E2E_TIMEOUT", "120"),
    }

    token = os.getenv("QLIK_E2E_TOKEN")
    token_file = os.getenv("QLIK_E2E_TOKEN_FILE")
    if token_file:
        token = pathlib.Path(token_file).read_text(encoding="utf-8").strip()
    if token:
        env["QLIK_JWT_TOKEN"] = token
        return env

    certs = os.getenv("QLIK_E2E_CERTS_DIR")
    if not certs:
        return None
    certs_dir = pathlib.Path(certs)
    env.update({
        "QLIK_CLIENT_CERT_PATH": str(certs_dir / "client.pem"),
        "QLIK_CLIENT_KEY_PATH": str(certs_dir / "client_key.pem"),
        "QLIK_CA_CERT_PATH": str(certs_dir / "root.pem"),
        "QLIK_USER_DIRECTORY": os.getenv("QLIK_E2E_USER_DIR", ""),
        "QLIK_USER_ID": os.getenv("QLIK_E2E_USER_ID", ""),
    })
    return env


@pytest.fixture(scope="session")
def live(request):
    """The server module wired to a real Qlik, or a skip.

    Session-scoped on purpose. Qlik caps concurrent Engine sessions per
    user (5 by default) and a closed WebSocket does not free its session
    immediately, so every test re-importing the module under its own
    connection would exhaust the quota mid-suite — the exact failure this
    suite exists to catch.
    """
    env = _e2e_env()
    if env is None:
        pytest.skip("live Qlik not configured (see tests/conftest.py for QLIK_E2E_*)")

    saved = {k: os.environ.get(k) for k in list(env) + [
        "QLIK_JWT_TOKEN", "QLIK_CLIENT_CERT_PATH", "QLIK_CLIENT_KEY_PATH",
        "QLIK_CA_CERT_PATH", "QLIK_USER_DIRECTORY", "QLIK_USER_ID",
    ]}
    for key in saved:
        os.environ.pop(key, None)
    os.environ.update(env)

    module = _reimport_server()

    yield module

    # Hand the Qlik session back instead of leaving it to the proxy
    # timeout. Without this, consecutive runs pile up sessions until Qlik
    # refuses new ones and the suite fails for reasons that have nothing
    # to do with the code under test.
    try:
        if module.engine_api is not None:
            module.engine_api.disconnect()
    except Exception:
        pass
    try:
        if module.jwt_session is not None:
            module.jwt_session.logout()
    except Exception:
        pass
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _reimport_server()


def _reimport_server():
    """Rebuild the server against the current environment.

    Clients and the MCP host are built at import time in `tools.context`,
    and each tool module registers itself against that host, so the
    reload has to start at the context and walk outwards. Reloading only
    `server` would leave the previous clients in place — every live test
    would then talk to whatever the last run configured, or to nothing.
    """
    import importlib

    from qlik_sense_mcp_server import server as srv
    from qlik_sense_mcp_server.tools import context
    from qlik_sense_mcp_server.tools import engine as engine_tools
    from qlik_sense_mcp_server.tools import repository as repository_tools
    from qlik_sense_mcp_server.tools import tasks as task_tools

    importlib.reload(context)
    for tool_module in (repository_tools, engine_tools, task_tools):
        importlib.reload(tool_module)
    return importlib.reload(srv)


@pytest.fixture(scope="session")
def call(live):
    """Call a tool by name and return its parsed payload.

    Fails the test on an error envelope: in an e2e test a structured error
    is still a failure unless the test is specifically about that error,
    in which case it uses `raw_call`.
    """
    def _call(name, **kwargs):
        payload = _raw(live, name, **kwargs)
        assert "error" not in payload, f"{name} failed: {payload.get('error')}"
        # get_app_details reports Engine failures in `engine_error` while
        # still looking like a success, so a bare "no error key" check would
        # read a failed call as an empty data model.
        assert not payload.get("engine_error"), (
            f"{name} returned a partial reply: {payload['engine_error']}")
        return payload
    return _call


@pytest.fixture(scope="session")
def raw_call(live):
    """Call a tool and return its payload as-is, errors included."""
    def _call(name, **kwargs):
        return _raw(live, name, **kwargs)
    return _call


def _raw(module, name, **kwargs):
    tool = getattr(module, name)
    fn = getattr(tool, "fn", tool)
    return json.loads(fn(**kwargs))


@pytest.fixture(scope="session")
def app_id():
    return os.getenv("QLIK_E2E_APP")


@pytest.fixture(scope="session")
def sheet_app_id():
    return os.getenv("QLIK_E2E_SHEET_APP") or os.getenv("QLIK_E2E_APP")


@pytest.fixture(scope="session")
def can_read_script(call, app_id):
    """Whether this identity may read a load script at all.

    Reading it needs Professional access; an Analyzer licence gets an empty
    string back with no error. Tests that depend on script content skip
    rather than fail, because the licence is not what they are checking.
    """
    return bool(call("get_app_script", app_id=app_id)["script_length"])


@pytest.fixture(scope="session")
def data_model(call, app_id):
    """The app's real tables and fields — tests assert against these."""
    return call("get_app_details", app_id=app_id)


@pytest.fixture(scope="session")
def text_field(data_model):
    """A real non-system field of the test app."""
    for field in data_model.get("fields") or []:
        name = field.get("name", "")
        if name and not name.startswith("$"):
            return field
    pytest.skip("test app has no usable field")
