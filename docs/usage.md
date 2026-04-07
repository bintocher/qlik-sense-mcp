# Usage

## Transports

The server speaks the two transports defined by the
[Model Context Protocol specification 2025-03-26](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports):

- **Streamable HTTP** (default since v1.4.0). The server listens on
  `http://127.0.0.1:8000/mcp`. Use this when running the server as a
  long-lived process — for example a service started by systemd or a
  background terminal — and pointing one or more MCP clients at it.
- **stdio** (legacy). The server reads JSON-RPC frames from stdin and
  writes them to stdout. Use this when your MCP client wants to spawn the
  server as a subprocess on demand.

## Starting the server

```bash
# Streamable HTTP transport (default)
uvx qlik-sense-mcp-server

# Same, when installed via pip
qlik-sense-mcp-server

# stdio transport (legacy)
qlik-sense-mcp-server --stdio

# From source / development
python -m qlik_sense_mcp_server.server
```

## Connection caching

In streamable-http mode the server process stays alive between MCP
requests. The Engine API client keeps a single long-lived WebSocket and
caches the currently opened app handle. As long as the same `app_id` is
reused, every tool call piggybacks on that connection — no per-call
`OpenDoc` round-trip, no per-call WebSocket handshake.

When the client switches to a different `app_id`, the cached document is
closed and the new one is opened on the same socket. If the socket dies
(network blip, idle timeout, server-side `OnSessionTimedOut` notification),
the next call transparently reconnects.

See [architecture.md](architecture.md) for the details and rationale.

## What to call first

The intended call order for any analysis session is:

1. **`get_about`** — verify connectivity and Qlik Sense version. Optional.
2. **`get_apps`** — discover apps. Use the `name` filter to narrow down.
3. **`get_app_details`** — open a specific app and read its data model.
   This is the only tool that returns `distinct_values` for every field
   and `rows` for every table — your hypercube planning depends on those
   numbers. Pay attention to the `warnings` array — it flags huge fact
   tables and high-cardinality fields.
4. **`engine_get_field_range`** — bounds (count distinct, min, max) for
   one field, fast on any size.
5. **`engine_create_hypercube`** — the main analysis tool. Read its full
   tool docstring before calling — it explains the set-analysis rules,
   the dimension-expression antipattern, and the hard 5000-row /
   9900-cell limits the server enforces.

## Hard limits enforced by this server

- `engine_create_hypercube`: `max_rows` is capped at **5000**, and the
  total `columns * max_rows` is capped at **9900** (Qlik Engine itself
  refuses pages over 10000 cells per `GetHyperCubeData` call —
  [error 7009 `calc-pages-too-large`](https://help.qlik.com/en-US/sense-developer/November2025/Subsystems/EngineJSONAPI/Content/service-genericobject-gethypercubedata.htm)).
  Requests over the limits are rejected immediately with a
  `limit_exceeded` / `cell_cap_exceeded` error and a hint pointing at
  set-analysis filters or top-N patterns.
- `get_apps`: `limit` is capped at **50**.
- `get_app_field`: `limit` is capped at **100**.

These limits are deliberately strict to push the LLM toward narrow,
focused queries. To pull more data, design more queries — not bigger
ones. See the `engine_create_hypercube` docstring for the
SLICE-BY-CATEGORY pattern.

## Tool list

See [tools.md](tools.md) for the full inventory of all 24 MCP tools, the
transport (Repository / Engine / Tasks) each one uses, and a short
description of when to call it. Detailed parameter docs live in the
Python docstrings — every tool returns its full docstring via the
standard MCP `tools/list` response.

## Diagnostics

Every tool response now starts with a `tool_call_seconds` field —
wall-clock time of the call rounded to milliseconds. Use it to spot the
slow tools in a session.

For deeper diagnosis, set `LOG_LEVEL=DEBUG` in your env block. Each
hypercube call then logs `CreateSessionObject`, `GetLayout` and any
follow-up Engine method with their durations.
