# Architecture

## Project layout

```
qlik-sense-mcp/
├── qlik_sense_mcp_server/
│   ├── __init__.py
│   ├── server.py         # Entry points; imports the tools, which registers them
│   ├── tools/            # MCP tools, grouped by the API they talk to
│   │   ├── context.py    #   MCP host + API clients shared by every tool
│   │   ├── helpers.py    #   Response envelope, timing, argument coercion
│   │   ├── repository.py #   get_about, get_apps, get_app_details
│   │   ├── engine.py     #   script, fields, sheets, hypercubes
│   │   └── tasks.py      #   reload tasks (certificate mode only)
│   ├── engine/           # Engine API client, split by responsibility
│   │   ├── api.py        #   QlikEngineAPI: assembles the mixins below
│   │   ├── connection.py #   WebSocket, greeting, liveness, JSON-RPC
│   │   ├── hypercube.py  #   Sorting, limits, page completion
│   │   ├── fields.py     #   Values, ranges, statistics, descriptions
│   │   ├── sheets.py     #   Sheets and their objects
│   │   └── app_model.py  #   Data model, master items, variables
│   ├── engine_api.py     # Back-compat import path for QlikEngineAPI
│   ├── config.py         # QlikSenseConfig + defaults
│   ├── repository_api.py # Repository (HTTP/QRS) client
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

#### Liveness without ping (since 1.8.0)

Before reusing the cached socket, the client has to decide whether it is
still alive. It must not do that with a WebSocket ping.

Qlik's Proxy Service does not relay ping/pong to the Engine. Through a
virtual proxy — that is, in every JWT deployment — the first request after
a ping is never answered: the call blocks for the whole `QLIK_WS_TIMEOUT`
(180s by default), the socket is then force-closed, and the next call
reconnects — so in JWT mode every second tool call hung for minutes. On a
direct Engine socket (port 4747, certificate mode) the same ping is
harmless, which is why the problem only ever appeared in JWT mode.

`_is_connected()` therefore:

1. Trusts a socket that answered a frame less than
   `QLIK_WS_IDLE_PROBE_AFTER` seconds ago (default 30) — no traffic at all
   in the common case of back-to-back tool calls.
2. Probes an idle one with a real `EngineVersion` request bounded by
   `QLIK_WS_PROBE_TIMEOUT` (default 15s). This proves more than a ping
   ever did: that the Engine answers, not merely that the socket accepts
   writes.

#### Greeting frames

A fresh socket is answered with notifications before anything else —
normally `OnAuthenticationInformation` then `OnConnected`. `connect()`
reads them up to `OnConnected` (bounded by `QLIK_WS_GREETING_TIMEOUT`),
which also leaves the receive buffer empty for the first real request.

A greeting can also be fatal. `OnMaxParallelSessionsExceeded` means the
per-user session quota is exhausted, and Qlik closes the socket right
after sending it. Treating that frame as "session established" is what
turned a plain quota error into `Failed to parse WebSocket frame ...
Expecting value` on the following call. Such frames now raise
`QlikSessionLimitError` (a `QlikConnectionError`) naming the quota and how
to clear it, and `connect()` re-raises it immediately instead of trying
the remaining fallback endpoints — every one of them would be refused the
same way.

#### Pipelined batches

`send_requests_pipelined()` sends a batch of independent requests
back-to-back and matches responses by `id`, which is exactly what the
Engine's numeric request ids are for. `send_request()` is unchanged and
still used everywhere else.

`_get_sheet_objects_detailed()` is the one caller: it resolves every child
object's handle in one batch and reads every layout in a second, so a
sheet with N objects costs 2 round-trips instead of 2N. Measured against a
live sheet with 16 objects: 0.135s to 0.028s for identical results.
`raise_on_error=False` keeps per-object isolation — one broken object is
skipped rather than losing the sheet.

#### One call at a time

The Streamable HTTP transport serves several MCP clients from one
process, but there is one WebSocket and one open document behind them.
Overlapping calls interleave frames on that socket: strict id-matching
makes each discard the other's reply, and `ensure_app` can switch
documents between one call's `CreateSessionObject` and its `GetLayout`,
so the second call reads the first app's data believing it is its own.

Engine-backed tools therefore run inside `QlikEngineAPI.transaction()` —
a reentrant lock held for the whole tool body, applied by the
`_engine_serialised` decorator in `tools/helpers.py`. The unit that has
to be atomic is the chain from `ensure_app` to the last request, not an
individual `send_request`.

Repository-only tools (`get_about`, `get_apps`, the task tools) are
deliberately left out of it: they never touch the socket, and making
them wait behind a slow hypercube would cost responsiveness for nothing.

#### Paging: server-side, or not at all

QRS `/{entity}/full` ignores `skip`/`take` and is itself truncated at the
server's MaxRecordLimit (100 for most types). Slicing that locally — what
this server used to do — both loses everything past the cap and reports
the cap as the total, so a client following `has_more` walks off the end
of the data believing it has seen everything.

Reads that can exceed the cap therefore go through `/{entity}/table`,
which takes `skip`, `take`, `sortColumn` and `filter`, with the real
total from `/{entity}/count` under the same filter. `get_apps` fetches
exactly one page; task listings read through to the last page
(`_read_all`, with a logged hard cap rather than a silent truncation).

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

The host object comes from the
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) and
is chosen at import time, because SDK 2.0 (2026-07-28) removed
`mcp.server.fastmcp`:

| Installed SDK | Host class | `MCP_SDK_MAJOR` |
|---|---|---|
| 1.x | `mcp.server.fastmcp.FastMCP` | `1` |
| 2.x | `mcp.server.mcpserver.MCPServer` | `2` |

Both expose the same `@tool()` decorator, `run_stdio_async()`,
`run_streamable_http_async()` and `_tool_manager._tools` registry. The
only difference the server has to care about is the bind address: 1.x
takes `host`/`port` in the constructor, 2.x in
`run_streamable_http_async()`. `tests/test_sdk_compat.py` pins this
contract down so the next SDK change fails in CI rather than at a user's
first tool call.

The host registers every `@mcp.tool()`-decorated function as an MCP tool. Each
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

#### Why failures are not flagged as protocol-level errors

Tool failures come back as an ordinary MCP result whose payload contains
`error`, `error_category`, `hint` and `request` — `isError` stays false.
This is deliberate. The consumer here is a language model deciding what
to do next, and the structured payload is what lets it act: the category
tells it whether to narrow the query or fix a field name, and `request`
tells it exactly what it sent. Clients that surface `isError` tend to
collapse the result into a generic failure message and drop that
payload, which would leave the model with nothing to correct.

The trade-off is that a client keying retries off the protocol-level
error status will treat a timeout as a success. Such a client should
branch on the `error` key instead.

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
