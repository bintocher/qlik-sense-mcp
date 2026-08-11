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

from ..config import QlikSenseConfig, AUTH_MODE_JWT
from ..repository_api import QlikRepositoryAPI
from ..engine_api import QlikEngineAPI
from ..jwt_session import JwtSession

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


def _init_clients():
    global config, repo_api, engine_api, jwt_session
    try:
        config = QlikSenseConfig.from_env()
        config.validate_runtime()

        if config.auth_mode == AUTH_MODE_JWT:
            jwt_session = JwtSession(config)
            repo_api = QlikRepositoryAPI(config, jwt_session=jwt_session)
            engine_api = QlikEngineAPI(config, jwt_session=jwt_session)
            logger.info(
                "Qlik Sense API clients initialised (JWT mode via virtual proxy '/%s')",
                config.virtual_proxy_prefix,
            )
        else:
            jwt_session = None
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

# Reload-task management talks to QRS endpoints (/qrs/reloadtask,
# /qrs/executionresult, /qrs/reloadtask/{id}/scriptlog) that require
# repository-admin rights. A JWT analyst reaches QRS through the virtual
# proxy as an ordinary user and gets 403s there, so advertising those
# tools in JWT mode only invites the LLM to burn turns on calls that
# cannot succeed. They are therefore registered in certificate mode only.
#
# When the configuration failed to load at all (`config is None`) every
# tool stays registered — that path is used by `--help` and the tests,
# and hiding tools there would just be confusing.
_CERT_ONLY_TOOLS_ENABLED = config is None or config.auth_mode != AUTH_MODE_JWT
if not _CERT_ONLY_TOOLS_ENABLED:
    logger.info(
        "JWT mode: reload-task tools are not registered (QRS task "
        "administration requires certificate mode)."
    )


def _cert_only_tool():
    """Register a tool only when QRS admin rights are actually available."""
    def decorator(fn):
        return mcp.tool()(fn) if _CERT_ONLY_TOOLS_ENABLED else fn
    return decorator
