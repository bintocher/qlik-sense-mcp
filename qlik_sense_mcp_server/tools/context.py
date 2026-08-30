"""Process-wide state every tool shares: the MCP host and the API clients.

One module owns them so the tool modules can be split by topic without
each keeping its own copy — a tool reads `context.repo_api` at call time,
so re-initialising the clients (or a test replacing them) is visible
everywhere at once.

Import-time work lives here too: logging, choosing the MCP host class for
whichever SDK line is installed, and building the clients from the
environment.
"""

import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv

# Ensure UTF-8 encoding on Windows (must be before any I/O)
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

# MCP SDK 2.0 (released 2026-07-28) removed `mcp.server.fastmcp` and
# replaced the FastMCP host with `MCPServer`. The two are compatible where
# it matters to us — same `@tool()` decorator, same `run_stdio_async()` /
# `run_streamable_http_async()` — but 2.x takes the bind address in
# `run_streamable_http_async()` instead of the constructor.
#
# Both SDK lines are supported so that existing installs pinned to 1.x
# keep working and fresh installs get 2.x.
try:
    from mcp.server.mcpserver import MCPServer as _McpHost

    MCP_SDK_MAJOR = 2
except ImportError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as _McpHost

    MCP_SDK_MAJOR = 1

from ..config import QlikSenseConfig, AUTH_MODE_JWT, AUTH_MODE_FORM, AUTH_MODE_CERTIFICATE
from ..repository_api import QlikRepositoryAPI
from ..engine import QlikEngineAPI
from ..jwt_session import JwtSession
from ..form_session import FormSession

# Initialize logging configuration early
load_dotenv()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_logging_level = getattr(logging, LOG_LEVEL, logging.INFO)
_log_formatter = logging.Formatter(
    fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
if not logging.getLogger().handlers:
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_log_formatter)
    logging.getLogger().addHandler(handler)
# Over stdio the log has nowhere to go: stdout is the protocol, and MCP
# clients generally swallow the server's stderr. QLIK_LOG_FILE gives the
# operator somewhere to look — which tools were called, with which
# arguments, and how long each took.
_log_file = os.getenv("QLIK_LOG_FILE")
if _log_file:
    try:
        file_handler = logging.FileHandler(_log_file, encoding="utf-8")
        file_handler.setFormatter(_log_formatter)
        logging.getLogger().addHandler(file_handler)
    except OSError as exc:  # a bad path must not stop the server
        logging.getLogger().warning("QLIK_LOG_FILE %r unusable: %s", _log_file, exc)
logging.getLogger().setLevel(_logging_level)
logger = logging.getLogger("qlik_sense_mcp_server.server")

# ─── Globals initialised once ───────────────────────────────────────────────

config: Optional[QlikSenseConfig] = None
repo_api: Optional[QlikRepositoryAPI] = None
engine_api: Optional[QlikEngineAPI] = None
jwt_session: Optional[JwtSession] = None
form_session: Optional[FormSession] = None


def _init_clients():
    global config, repo_api, engine_api, jwt_session, form_session
    try:
        config = QlikSenseConfig.from_env()
        config.validate_runtime()

        if config.auth_mode == AUTH_MODE_JWT:
            jwt_session = JwtSession(config)
            form_session = None
            repo_api = QlikRepositoryAPI(config, jwt_session=jwt_session)
            engine_api = QlikEngineAPI(config, jwt_session=jwt_session)
            logger.info(
                "Qlik Sense API clients initialised (JWT mode via virtual proxy '/%s')",
                config.virtual_proxy_prefix,
            )
        elif config.auth_mode == AUTH_MODE_FORM:
            jwt_session = None
            form_session = FormSession(config)
            repo_api = QlikRepositoryAPI(config, form_session=form_session)
            engine_api = QlikEngineAPI(config, form_session=form_session)
            logger.info(
                "Qlik Sense API clients initialised (form mode via virtual proxy '/%s', user=%s\\%s)",
                config.virtual_proxy_prefix, config.user_directory, config.user_id,
            )
        else:
            jwt_session = None
            form_session = None
            repo_api = QlikRepositoryAPI(config)
            engine_api = QlikEngineAPI(config)
            logger.info(
                "Qlik Sense API clients initialised (certificate mode, user=%s\\%s)",
                config.user_directory, config.user_id,
            )
    except Exception as e:
        logger.warning("Failed to init Qlik API clients: %s", e)


_init_clients()

# ─── MCP server host ────────────────────────────────────────────────────────

_mcp_host = "127.0.0.1"
_mcp_port = int(os.getenv("MCP_PORT", "8000"))
if MCP_SDK_MAJOR >= 2:
    # 2.x takes the bind address in run_streamable_http_async(), not here.
    mcp = _McpHost("qlik-sense-mcp-server")
else:
    mcp = _McpHost("qlik-sense-mcp-server", host=_mcp_host, port=_mcp_port)

# The server registers the analysis tools and nothing else by default.
# Reload-task administration is a separate job, done by a separate person,
# against QRS endpoints (/qrs/reloadtask, /qrs/executionresult) that need
# repository-admin rights. Whether those rights are available depends on
# the QMC role granted to whatever identity ends up authenticated — QRS
# does not care whether that happened via a client certificate, a JWT, or
# a form login, only what role it maps to. Certificate mode almost always
# runs as a trusted admin/service identity, so task tools default to ON
# there; a JWT or form identity is normally an ordinary analyst without
# those rights, so they default to OFF — but an operator who has verified
# their identity IS privileged (see docs/AUTH_FORM.md) can opt in
# explicitly with QLIK_TASK_TOOLS=true.
#
# Every tool the caller cannot use costs it something even unused: the
# names and descriptions sit in its context, and a model that reads about
# task administration tries it — which is exactly what the default-off
# behaviour outside certificate mode avoids for the common case.
_TASK_TOOLS_ENV = os.getenv("QLIK_TASK_TOOLS", "").strip().lower()
_TASK_TOOLS_EXPLICITLY_OFF = _TASK_TOOLS_ENV in ("false", "0", "no")
_TASK_TOOLS_EXPLICITLY_ON = _TASK_TOOLS_ENV in ("true", "1", "yes")
_CERT_MODE = config is None or config.auth_mode == AUTH_MODE_CERTIFICATE

if _TASK_TOOLS_EXPLICITLY_OFF:
    _TASK_TOOLS_ENABLED = False
elif _CERT_MODE:
    _TASK_TOOLS_ENABLED = True
else:
    _TASK_TOOLS_ENABLED = _TASK_TOOLS_EXPLICITLY_ON

if not _TASK_TOOLS_ENABLED:
    logger.info(
        "Reload-task tools are not registered (%s).",
        "QLIK_TASK_TOOLS is off" if _TASK_TOOLS_EXPLICITLY_OFF else
        "set QLIK_TASK_TOOLS=true if this identity has QRS admin rights",
    )
elif not _CERT_MODE:
    logger.warning(
        "Reload-task tools are registered outside certificate mode "
        "(QLIK_TASK_TOOLS=true). Every call will fail with 403 unless the "
        "%s identity in use has QRS admin rights (RootAdmin/ContentAdmin "
        "or an equivalent custom role) — QRS checks the QMC role, not how "
        "the session was authenticated.",
        config.auth_mode,
    )


def _task_admin_tool():
    """Register a tool only when task administration was asked for and
    the current mode allows it — see `_TASK_TOOLS_ENABLED` above."""
    def decorator(fn):
        return mcp.tool()(fn) if _TASK_TOOLS_ENABLED else fn
    return decorator
