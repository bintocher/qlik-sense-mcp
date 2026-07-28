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
│   ├── jwt_session.py    # JWT session bootstrap + cache (since v1.5.0)
│   └── utils.py          # XSRF key generation, helpers
├── tools/
│   └── qlik_jwt_admin.py # Admin CLI: RSA keypair + JWT issuance (since v1.5.0)
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

### `JwtSession` ([jwt_session.py](../qlik_sense_mcp_server/jwt_session.py))

Lazy, thread-safe holder of the bootstrapped Qlik session material for
JWT mode. On first use it calls `GET /{vp_prefix}/qps/csrftoken` with
`Authorization: Bearer <jwt>`, captures the resulting `X-Qlik-Session*`
cookie and `qlik-csrf-token` header, and caches them for 25 minutes
(below the 30-minute Qlik idle timeout). On a 401 / 403 from a later
QRS or Engine call, both API clients invalidate the cache and trigger a
transparent re-bootstrap. See [AUTH_JWT.md](AUTH_JWT.md) for the
protocol-level details and the CSWSH rationale.

### `QlikRepositoryAPI` ([repository_api.py](../qlik_sense_mcp_server/repository_api.py))

HTTP client for the Repository (QRS) API. Implements certificate auth,
dynamic XSRF key generation, and the metadata, app, task and schedule
endpoints used by the Repository / task tools. Accepts an optional
`JwtSession` — when present, it injects the bootstrapped session cookie
plus `qlik-csrf-token` header on every request instead of presenting a
client certificate.

### `QlikEngineAPI` ([engine_api.py](../qlik_sense_mcp_server/engine_api.py))

WebSocket client for the Engine API. Speaks JSON-RPC 2.0. Hosts every
data-side tool: hypercubes, fields, sheets, objects, script. Accepts
the same optional `JwtSession`; in JWT mode the WebSocket handshake
sends the cookie + csrf header (no `Authorization: Bearer` on the
upgrade — that triggers Qlik's CSWSH rejection on Nov-2024+ servers).

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

#### Hypercube sorting (since v1.6.0)

`create_hypercube` accepts `sort_by` (a measure label, a measure
expression, or a dimension field name) plus `sort_order`, and translates
them into two coordinated parts of `qHyperCubeDef`:

1. `qInterColumnSortOrder` is rebuilt with the requested column first.
   Column indices are fixed by the Engine: dimensions occupy `0..D-1`,
   measures `D..D+M-1`. This is the only mechanism that makes the Engine
   order rows by a measure.
2. The direction is written to that column's own criteria — `qSortBy`
   (`qSortByNumeric` ±1) for a measure, `qSortCriterias` with both
   `qSortByNumeric` and `qSortByAscii` for a dimension, so numeric and
   text fields both sort correctly without the caller knowing the type.

Both halves are required: a measure whose `qSortBy` is left at the Engine
default ("ascending alphabetic") produces a nonsense order even when it
leads `qInterColumnSortOrder`.

Before v1.6.0 `qInterColumnSortOrder` was hard-coded to
`list(range(n_cols))`, so the first dimension always won and per-measure
sorting was dead configuration — top-N requests silently returned the
alphabetically first rows.

Ranking this way is also much cheaper than the `qSortByExpression`
alternative, which makes the Engine evaluate the aggregate a second time
purely to order rows. Measured against a live 91M-row app: 0.2s versus
286s.

Unknown `sort_by` / `sort_order` values fail fast with an
`invalid_sort` error listing `available_columns`, before any Engine call
— silently ignoring a sort would return plausible rows in the wrong
order, which is worse than an error.

Since v1.6.1 every dimension also carries `qNullSuppression` (opt out
with `exclude_null_dimensions=False`). Facts that carry no value for the
grouping field collapse into one row that Qlik renders as `"-"`, and
that row frequently holds a large enough total to win the ranking. On a
live app it held the entire measure total and took rank 1, hiding every
real value behind it.

#### Response shape

The tool returns `columns` + `rows` (plain values, numbers preserved),
`grand_total`, `sorted_by` / `sort_order`, and `timings` split into
`open_app_seconds` and `get_layout_seconds`. The raw `qHyperCube` with
per-cell `qElemNumber`/`qState` is opt-in via `include_raw_layout`,
because it costs several times more tokens. The session object is
destroyed once its data has been read, so results are not pinned in
Engine memory for the rest of the session.

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
3. On failure, returns a structured `{tool_call_seconds, error,
   error_type, tool, request}` envelope instead of letting the MCP layer
   turn the traceback into something opaque. `request` is the exact
   argument set the tool was called with, reconstructed from the
   function signature via `inspect.signature().bind_partial()`. It is
   attached both to raised exceptions and to `{"error": ...}` payloads
   that tools return themselves (timeouts, bad field names, limit
   violations) — a bare "timed out after 180s" does not tell the caller
   which query to fix.

#### Per-mode tool registration (since v1.6.0)

Reload-task tools are declared with `@_cert_only_tool()` instead of
`@mcp.tool()`. That decorator registers the function only when
`config.auth_mode != jwt`, because QRS task administration
(`/qrs/reloadtask`, `/qrs/executionresult`, script-log download)
requires repository-admin rights that a JWT analyst identity does not
have. JWT sessions therefore see 12 tools instead of 24, rather than 12
that can only return 403.

When the configuration fails to load entirely (`config is None`) every
tool stays registered — that path serves `--help` and the test suite.

The server runs in
[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
mode by default, listening on `http://127.0.0.1:8000/mcp`. The legacy
`stdio` transport is available behind the `--stdio` CLI flag.
