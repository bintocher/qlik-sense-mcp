# Architecture

## Project layout

```
qlik-sense-mcp/
├── qlik_sense_mcp_server/
│   ├── __init__.py
│   ├── server.py         # FastMCP server, tool registration, request routing
│   ├── config.py         # QlikSenseConfig + defaults
│   ├── repository_api.py # Repository (HTTP/QRS) client
│   ├── engine_api.py     # Engine API (WebSocket) client
│   └── utils.py          # XSRF key generation, helpers
├── docs/                 # All documentation (this folder)
├── tests/                # pytest suite
├── .env.example          # Configuration template
├── mcp.json.example      # MCP client config template
├── pyproject.toml
└── README.md
```

## Components

### `QlikSenseConfig` ([config.py](../qlik_sense_mcp_server/config.py))

Pydantic model that loads `QLIK_*` environment variables, validates them
and exposes the resulting connection settings. Default ports match the
standard
[Qlik Sense Enterprise port allocation](https://help.qlik.com/en-US/sense-admin/Subsystems/DeployAdministerQSE/Content/Sense_DeployAdminister/QSEoW/Deploy_QSEoW/Ports.htm).

### `QlikRepositoryAPI` ([repository_api.py](../qlik_sense_mcp_server/repository_api.py))

HTTP client for the Repository (QRS) API. Implements certificate auth,
dynamic XSRF key generation, and the metadata, app, task and schedule
endpoints used by the Repository / task tools.

### `QlikEngineAPI` ([engine_api.py](../qlik_sense_mcp_server/engine_api.py))

WebSocket client for the Engine API. Speaks JSON-RPC 2.0. Hosts every
data-side tool: hypercubes, fields, sheets, objects, script.

The two non-obvious parts:

#### Connection caching (since v1.4.0)

`QlikEngineAPI` keeps a single long-lived WebSocket and caches the
currently opened app handle. All tool calls go through the
`ensure_app(app_id)` entry point, which:

1. Reuses the cached connection and app handle if the requested
   `app_id` matches and the socket is still alive.
2. Reconnects and re-opens the app if the socket dropped (ping fails) —
   transient network blips do not fail the request.
3. Closes the old document and opens the new one when switching to a
   different `app_id`, so the Qlik server never holds two parallel open
   documents for this MCP session.

When `app_id` is provided, `connect()` first tries the per-app endpoint
`wss://<host>:4747/app/<url-encoded-app-id>` (the Qlik-recommended path
that binds the session to a specific document immediately), then falls
back to the global `/app/engineData` endpoint.

This eliminates the per-call connect/open/close cycle that the v1.3.x
line did. On a typical analysis session that issues 20 tool calls
against the same app, the savings are significant: one WebSocket
handshake plus one `OpenDoc` instead of twenty of each.

#### Strict id-matching in `send_request`

Every JSON-RPC frame received over the WebSocket is parsed and the
`id` field is matched against the request id we just sent. Frames with
no id (Engine notifications such as `OnConnected`, `OnAuthenticated`,
`OnSessionTimedOut`) are logged at DEBUG and skipped. Frames with a
different id (late replies to a previously timed-out request) are
logged at WARNING and skipped.

Without this, a single timed-out hypercube call would leave stale data
in the recv buffer that the next call would consume as its own response,
cascading the failure for the rest of the session. Now any timeout or
parse error force-closes the socket via `_kill_socket()`, the cache is
invalidated, and the next call opens a fresh connection.

#### Two-tier timeouts

A single `QLIK_WS_TIMEOUT` environment variable (default `180.0s`)
controls both the WebSocket handshake and every Engine API call
(`OpenDoc`, `CreateSessionObject`, `GetLayout`, `GetHyperCubeData`,
field statistics). Heavy operations like building a hypercube against a
500-million-row fact table can legitimately take a minute or more —
raise `QLIK_WS_TIMEOUT` if you see `WebSocket recv() timed out`. The
limit is per-call, not per-session.

### `FastMCP` server ([server.py](../qlik_sense_mcp_server/server.py))

The `mcp` package's
[`FastMCP`](https://github.com/modelcontextprotocol/python-sdk) host
registers every `@mcp.tool()`-decorated function as an MCP tool. Each
tool is also wrapped in the local `_timed` decorator, which:

1. Measures wall-clock time of the call.
2. Injects `tool_call_seconds` as the **first** key of the JSON
   response.
3. On exception, returns a structured `{tool_call_seconds, error,
   error_type, tool}` envelope instead of letting the MCP layer turn
   the traceback into something opaque.

The server runs in
[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
mode by default, listening on `http://127.0.0.1:8000/mcp`. The legacy
`stdio` transport is available behind the `--stdio` CLI flag.
