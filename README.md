# Qlik Sense MCP Server

[![PyPI version](https://badge.fury.io/py/qlik-sense-mcp-server.svg)](https://pypi.org/project/qlik-sense-mcp-server/)
[![PyPI downloads](https://img.shields.io/pypi/dm/qlik-sense-mcp-server)](https://pypi.org/project/qlik-sense-mcp-server/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python versions](https://img.shields.io/pypi/pyversions/qlik-sense-mcp-server)](https://pypi.org/project/qlik-sense-mcp-server/)

[Model Context Protocol](https://modelcontextprotocol.io/) server for
Qlik Sense Enterprise. Exposes Qlik's Repository (HTTP) and Engine
(WebSocket) APIs as **28 MCP tools** so an LLM client can discover apps,
inspect data models, query data, and manage reload tasks through a
single uniform interface. In JWT mode the 14 reload-task tools are
hidden, since QRS task administration needs certificate auth.

## What's in the box

| Area | Tools | Used for |
|------|-------|----------|
| Repository (apps & metadata) | `get_about`, `get_apps`, `get_app_details` | Discover apps, list tables and fields with cardinalities |
| Engine (data & script)       | `engine_query`, `engine_create_hypercube`, `get_app_script`, `get_app_variables`, `get_app_sheets`, `get_app_sheet_objects`, `get_app_object`, `search_app`, `get_app_field`, `engine_get_field_range`, `get_app_field_statistics` | Query data, read load script, list visualizations, inspect field values |
| Reload tasks *(certificate mode only)* | `get_tasks`, `get_task_details`, `get_task_dependencies`, `get_task_schedule`, `get_task_executions`, `get_task_script_log`, `get_failed_tasks_with_logs`, `start_task`, `create_task`, `update_task`, `delete_task`, `create_task_schedule`, `update_task_schedule`, `delete_task_schedule` | Inspect, trigger and manage reload tasks |

Full list with descriptions: [`docs/tools.md`](docs/tools.md).

The main analysis call takes the question, not the Qlik syntax for it:

```jsonc
// engine_query — "revenue by region for 2024, biggest first"
{
  "app_id": "<app guid>",
  "group_by": ["region_name"],
  "metrics":  [{"field": "amount", "agg": "sum", "label": "Revenue"}],
  "filters":  [{"field": "order_date", "period": "2024"}],
  "sort_by": "Revenue",
  "limit": 10
}
```

The server writes the set analysis, checks that the filter selects
something, and answers with `period_check` — the earliest and latest
date actually in the result — so a filter that failed to apply is
visible instead of hiding behind a plausible number. Independent
questions go in one call as `queries` and share three round-trips.

Harder questions stay in the same form. A share of the whole, with the
numerator narrowed and the denominator not:

```jsonc
{
  "group_by": ["region_name"],
  "metrics": [{"label": "Share", "op": "divide", "of": [
    {"field": "amount", "agg": "sum",
     "filters": [{"field": "category", "values": ["Alpha"]}]},
    {"field": "amount", "agg": "sum", "total": true}]}]
}
```

The same form states an aggregation over an aggregation
(`"inner_agg": "sum", "per": "order_id", "agg": "median"`), the clients
who bought in one year and not the next (`matching` / `not_matching`),
counting over a bookmark or ignoring selections (`scope`), and values
kept, dropped, added or intersected on any field.

`engine_create_hypercube` takes the same shape with the expressions
written by hand, for calculations the typed form cannot state.

## Quick start

```bash
uvx qlik-sense-mcp-server
```

The server starts in [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
mode on `http://127.0.0.1:8000/mcp`. Configure it via environment
variables — see [`docs/configuration.md`](docs/configuration.md).

For stdio mode (legacy MCP transport), pass `--stdio`.

Two authentication modes are supported: client certificate (legacy,
full QRS access) and JWT via virtual proxy (per-analyst, no on-disk
secrets). See [`docs/AUTH_JWT.md`](docs/AUTH_JWT.md) for the JWT setup.

## Documentation

| Document | What's inside |
|----------|---------------|
| [`docs/installation.md`](docs/installation.md) | Requirements, install via `uvx` / `pip` / source, certificate setup |
| [`docs/configuration.md`](docs/configuration.md) | All `QLIK_*` environment variables, sample `.env`, MCP client config snippet |
| [`docs/AUTH_JWT.md`](docs/AUTH_JWT.md) | JWT authentication via virtual proxy: key generation, virtual proxy setup, `QLIK_JWT_TOKEN` usage |
| [`docs/usage.md`](docs/usage.md) | Transports, server start commands, recommended call order, hard limits enforced by this server |
| [`docs/tools.md`](docs/tools.md) | Inventory of all 27 tools, response/error envelope, error categories |
| [`docs/architecture.md`](docs/architecture.md) | Project layout, components, connection caching, strict id-matching, two-tier timeout |
| [`docs/development.md`](docs/development.md) | `make` targets, tests, versioning, how to add a new tool |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Common errors, hypercube planning failures, verbose logging, configuration self-test |
| [`docs/llm-behaviour.md`](docs/llm-behaviour.md) | What models actually do with this server, measured: calls per question, where they go wrong, session limits, how to benchmark honestly |
| [`CHANGELOG.md`](CHANGELOG.md) | Release notes |

## Key facts

- **A wrong query is refused, not answered.** Qlik evaluates an unknown
  field name as an expression worth 0, so a hypercube grouped by a typo
  came back as a single row holding the grand total — a plausible number
  with nothing to mark it as wrong. Every query is checked by Engine
  before it runs: `ExpandExpression` resolves variables, `CheckExpression`
  reports syntax and unknown names, `GetFieldsFromExpression` reports the
  fields a set modifier actually filters on. The four checks cost about
  4ms in one batch, against 75ms for the smallest hypercube.
- **A period filter is measured, not assumed.** Comparison inside a set
  modifier runs against the text Qlik displays for a value, so a serial
  number range returns 0 on a field displayed as `01.01.2024` and works
  on one displayed as `45292` — with no error either way. State the
  period as a filter and the server tries the cheap numeric form against
  a reference count, falls back to the form that always works, and
  reports the period the result actually covers.
- **One value, one writing.** A date in a query result reads as the text
  Qlik displays for it, the same as the sample values in
  `get_app_details` and the bounds from `engine_get_field_range`.
- **A field name is always written in brackets.** A bare `Тип ставки` is
  read by Qlik's parser as two tokens — "Garbage after expression:
  'ставки'" — which used to refuse queries Qlik itself runs happily.
- **Aggregating over groups, not rows.** `per` and `inner_agg` state
  "sum per issue, then the 85th percentile across issues", which is a
  different question from a percentile over rows and gives a different
  number.
- **A measure can narrow itself.** Its own `filters` override the
  query's, so a KPI carries its numerator and its denominator in one
  answer.
- **Objects say which fields they use.** `get_app_sheet_objects` returns
  `fields_used`, including fields reached through master measures and the
  ones inside a filter pane's listboxes — so "what does this sheet work
  with" is one call.
- **Paging is done by Qlik, not after it.** App and task listings read
  `/{entity}/table` with `skip`/`take` and take the total from
  `/{entity}/count`, so nothing past the QRS record limit goes missing
  and `total_found` is the real total. Field search and field paging
  likewise happen in Engine — verified on a field with 200,000 distinct
  values, where the old local scan simply could not see a match.
- **A failure is never an empty answer.** A QRS 500, a refused
  connection or an Engine error used to arrive as `[]`, `""` or "no
  schedule", which reads as a tidy, empty Qlik. Every such path now
  returns an `error_category` and the original cause.
- **Column meanings, not just column names.** Fields and tables commented
  in the load script (`COMMENT FIELD` / `COMMENT TABLE`) carry that text
  into `get_app_details` as `comment`, and into `get_app_field` as
  `field_comment`, so the model reads what a column means instead of
  guessing from its name. Added in 1.7.2.

- **Runs on both MCP SDK lines.** SDK 2.0 dropped `FastMCP`; the server
  now picks `MCPServer` (2.x) or `FastMCP` (1.x) at import time, so
  `mcp>=1.1.0,<3.0.0` all work. Both lines are covered by the test
  suite and were verified end to end against a live Qlik app.

- **Ranked queries (top-N) in one call.** `engine_create_hypercube`
  takes `sort_by` (a measure label, a measure expression or a dimension
  field), `sort_order` (`desc` / `asc`) and `limit`, so "the 10 clients
  with the highest GGR" is a single request. Before v1.6.0 sorting by a
  measure silently did nothing — `qInterColumnSortOrder` was hard-coded
  to the dimensions, so the server returned the alphabetically first
  rows instead of the largest ones.
- **NULL groups stay out of rankings.** Facts with no value for the
  grouping field collapse into Qlik's `"-"` row, which often holds a
  large total and would otherwise take first place in a top-N. It is
  dropped by default; pass `exclude_null_dimensions=false` to measure
  how much data is unattributed.
- **Compact, LLM-friendly results.** The hypercube response is
  `columns` + `rows` with real numbers, plus `grand_total` and
  per-step `timings`. Pass `include_raw_layout=true` for the full Qlik
  layout.
- **Failures name the query that failed.** Every error reply, timeouts
  included, echoes `tool` and `request` with the exact arguments sent.
- **Fewer useless tools in JWT mode.** Reload-task administration needs
  QRS admin rights, so those 14 tools are registered only in
  certificate mode: 27 tools with a certificate, 13 with a JWT.
- **One Qlik session per server.** Qlik's per-user limit (5 by default)
  counts proxy sessions, and in JWT mode one is created by the session
  bootstrap itself — before any WebSocket. The server therefore
  bootstraps once and reuses that session for every call; restarting it
  in a loop is what exhausts the quota, not the number of queries.
- **JWT authentication via virtual proxy.** Set `QLIK_JWT_TOKEN`
  instead of certificate paths and the server will authenticate every
  Repository and Engine call as the analyst encoded in the token. No
  certificates or private keys live on the host. The legacy
  certificate mode is unchanged and still required for full QRS access.
  Setup guide: [`docs/AUTH_JWT.md`](docs/AUTH_JWT.md).
- **Cached Engine WebSocket connections.** Once an app is opened, every
  subsequent tool call against the same `app_id` reuses the same
  WebSocket and the same open document. Switching `app_id` closes the
  old document and opens the new one on the same socket. Dropped
  connections are reopened transparently. Implementation:
  [`engine_api.py`](qlik_sense_mcp_server/engine_api.py) and
  [`docs/architecture.md`](docs/architecture.md).
- **Streamable HTTP transport by default.** The server is a long-lived
  process; multiple MCP clients can talk to it in parallel. The legacy
  stdio mode still works behind `--stdio`.
- **`tool_call_seconds`** is injected as the first key of every tool
  response — wall-clock time of the call in milliseconds. Use it to
  spot slow tools.
- **Hard hypercube limits.** `engine_create_hypercube` rejects requests
  with `max_rows > 5000` or `columns * max_rows > 9900` immediately,
  with a structured error and a hint pointing at set-analysis or
  top-N patterns. Qlik Engine itself returns
  [error 7009 `calc-pages-too-large`](https://help.qlik.com/en-US/sense-developer/November2025/Subsystems/EngineJSONAPI/Content/service-genericobject-gethypercubedata.htm)
  for any single page over 10000 cells.
- **Single timeout knob.** `QLIK_WS_TIMEOUT` (default `180.0` seconds)
  controls both the WebSocket handshake and every Engine API call.

## Requirements

- Python 3.12 (the package is built and tested against this version; see [`pyproject.toml`](pyproject.toml))
- Qlik Sense Enterprise (Repository on port 4242, Engine on port 4747 — the
  [standard ports](https://help.qlik.com/en-US/sense-admin/Subsystems/DeployAdministerQSE/Content/Sense_DeployAdminister/QSEoW/Deploy_QSEoW/Ports.htm))
- Client certificate, private key and root CA from the Qlik Sense node
- Network access from the host running this server to Qlik

## Disclaimer

This project is an independent, community-built integration. It is
**NOT affiliated with, endorsed by, sponsored by, or supported by Qlik
Technologies Inc., QlikTech International AB, or any other Qlik
entity**. "Qlik", "Qlik Sense", "QlikView" and all related product
names are trademarks of their respective owners.

All information about Qlik Sense APIs, port allocations, error codes,
protocol behavior and usage patterns used in this project was obtained
exclusively from publicly available sources — the Qlik Developer Portal
([help.qlik.com](https://help.qlik.com), [qlik.dev](https://qlik.dev)),
the [Qlik Community](https://community.qlik.com) forums, and other
public documentation. No proprietary, confidential or reverse-engineered
material is used.

## License

[MIT](LICENSE) © 2025-2026 Stanislav Chernov
