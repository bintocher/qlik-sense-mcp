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

## Authentication mode

The server can connect to Qlik Sense Enterprise in one of two mutually
exclusive modes: **certificate** (default — exported client certificate
+ key on port 4242 / 4747) or **JWT** (single bearer token issued
through a JWT virtual proxy, no client certificate required). The mode
is chosen by the env vars in your MCP client config; everything else —
the tool surface, the Streamable HTTP transport, the connection cache —
behaves identically. See [AUTH_JWT.md](AUTH_JWT.md) for the JWT setup,
admin CLI and security model.

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

## Ranking (top-N / bottom-N)

`engine_create_hypercube` maps onto SQL one-to-one: `dimensions` is the
`GROUP BY`, `measures` are the aggregates, `sort_by` + `sort_order` are
the `ORDER BY`, and `limit` is the `LIMIT`. "The 10 clients with the
highest GGR" is one call:

```jsonc
{
  "app_id": "…",
  "dimensions": [{"field": "clientid"}],
  "measures": [{"expression": "Sum(ggr)", "label": "GGR"}],
  "sort_by": "GGR",          // measure label, measure expression or dimension field
  "sort_order": "desc",      // "asc" for bottom-N
  "limit": 10
}
```

`sort_by` puts that column first in Qlik's `qInterColumnSortOrder`,
which is the only way the Engine orders rows by a measure. Do **not**
hand-roll `qSortByExpression` on the dimension for ranking: it makes the
Engine evaluate the same aggregate a second time just to order rows. On
a 91M-row app, ranking by the measure column returned in 0.2s where the
`qSortByExpression` form took 286s.

Rows whose dimension value is NULL — displayed by Qlik as `"-"` — are
kept by default (`exclude_null_dimensions`). Facts that carry no value
for the grouping field all pile into that one row, so it tends to hold a
large total and win a ranking; dropping it is a statement about the data
and therefore the caller's to make. Pass
`"exclude_null_dimensions": true` to leave it out. A large `"-"` total
means the grouping field
is not linked to those facts in the data model.

## One session at a time

Qlik Sense allows at most **5 concurrent sessions per user identity** —
a platform limit no MCP setting can raise. What counts against it is the
*session*, not the query: measured on 31.62, the JWT bootstrap creates
one on its own before any WebSocket exists, while sockets sharing that
one bootstrapped cookie are free (sixteen at once, seven of them holding
seven different apps open, still one session).

So the cost is in how often a session is started, not in how much you
ask. Keep one server process running and reuse it; restarting it in a
loop is what exhausts the quota. Do not run a second MCP process or a
second editor against the same credentials, and do not fan out tool
calls — they are serialised over one WebSocket anyway.

Sessions also outlive their socket: after they are dropped through the
Proxy API the Engine can still refuse a new one for a few minutes, so
when you hit the limit, wait rather than retry.

## Hard limits enforced by this server

- `engine_create_hypercube`: `limit` (alias `max_rows`) is capped at
  **5000**, and the total `columns * limit` is capped at **9900** (Qlik Engine itself
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

Every tool response starts with a `tool_call_seconds` field —
wall-clock time of the call rounded to milliseconds. Use it to spot the
slow tools in a session.

`engine_create_hypercube` additionally returns `timings`, which splits
that number into `open_app_seconds` (loading the app into Engine memory
— only the first call against an app pays it) and
`get_layout_seconds` (the computation itself). A large
`get_layout_seconds` means the query is genuinely heavy: add set
analysis, drop a dimension, or group by a field that sits closer to the
fact table. A well-formed query answers in seconds — grouping by a field
inside a 91M-row fact table returns in well under a second.

Every failed call — including timeouts — echoes back `tool` and
`request` with the exact arguments that produced it, so you can see
which query failed and retry with a cheaper one instead of guessing.

For deeper diagnosis, set `LOG_LEVEL=DEBUG` in your env block. Each
hypercube call then logs `CreateSessionObject`, `GetLayout` and any
follow-up Engine method with their durations.
