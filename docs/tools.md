# Tools

The server exposes up to **24** MCP tools, grouped into three areas:

- **Repository API** — fast metadata via Qlik Repository (HTTP/QRS).
- **Engine API** — data and load script via Qlik Engine (WebSocket).
- **Task management** — reload tasks, schedules, executions, script logs.

**Availability depends on the authentication mode.** Task management
calls QRS endpoints that require repository-admin rights, which a JWT
analyst identity does not have, so those 12 tools are registered in
certificate mode only:

| Mode | Tools registered |
|------|------------------|
| certificate | 24 — everything below |
| JWT (virtual proxy) | 12 — Repository metadata + Engine only |

Every tool returns its full parameter documentation via the standard MCP
`tools/list` request. Use that as the authoritative reference — the
docstrings inside [`server.py`](../qlik_sense_mcp_server/server.py)
include parameter types, defaults, set-analysis rules and concrete error
categories. The lists below are a quick map only.

## Repository API

| Tool | Purpose |
|------|---------|
| `get_about` | Qlik Sense server info: version, build, node type. Use to verify connectivity. |
| `get_apps` | List apps with filters (`name`, `stream`, `published`) and pagination. `limit` capped at 50. |
| `get_app_details` | App overview: metadata, full table list with row counts, full field list with `distinct_values`, plus a `warnings` array that flags huge fact tables and high-cardinality fields. Tables and fields that carry a `COMMENT TABLE` / `COMMENT FIELD` text from the load script also get a `comment` key — the business meaning of the column, absent when the script sets none. Always call this before building a hypercube. |

## Engine API

| Tool | Purpose |
|------|---------|
| `get_app_script` | Full load script (`SET`/`LET`, `LOAD ... FROM ...`). Read this to understand how calendar fields and named variables are built. |
| `get_app_variables` | User variables split by source (script vs UI), with wildcard search and pagination. |
| `get_app_sheets` | List of sheets in the app, with title and description. |
| `get_app_sheet_objects` | List of objects on a specific sheet, with `object_id`, `object_type`, `object_description`. |
| `get_app_object` | Full layout of one specific object via `GetObject` + `GetLayout`. Reverse-engineers an existing chart. |
| `get_app_field` | Distinct values of one field with pagination and wildcard search, both applied by Engine over the whole field rather than over a prefetched prefix. Falls back to a single-dimension hypercube if the underlying `ListObject` returns nothing. Adds `field_comment` when the load script commented that field. |
| `engine_get_field_range` | Lightning-fast bounds for one field: count distinct, min, max. Implemented as a measures-only hypercube — runs in seconds on any table size. Prefer this over `get_app_field_statistics`. |
| `get_app_field_statistics` | Field statistics via a measures-only hypercube. Defaults to **light** mode (count distinct, non-null count, null count, total count, min, max, null %, completeness — the null share comes from `NullCount()`, not from subtracting one non-null count from another). Pass `full=true` to also compute avg / sum / median / mode / stdev — slow on big fact tables and meaningless for date/text fields. |
| `engine_create_hypercube` | Build an arbitrary `GROUP BY` hypercube — the main data-analysis tool. Supports ranking directly: `sort_by` (measure label / measure expression / dimension field) + `sort_order` (`desc` / `asc`) + `limit`. Rows with a NULL dimension value (Qlik's `"-"`) are dropped by default — pass `exclude_null_dimensions=false` to keep them. Hard limits: `limit <= 5000`, `columns * limit <= 9900`. Read the full docstring — it covers set-analysis patterns, the no-expression-in-dimension rule and the session limit. |

## Task management (Repository API)

| Tool | Purpose |
|------|---------|
| `get_tasks` | List reload tasks with filters (`status_filter`, `name_filter`, `app_filter`). |
| `get_task_details` | Full QRS object for one reload task. |
| `get_task_dependencies` | Transitive dependency chain via composite events. `direction="downstream"` or `"upstream"`. |
| `get_task_schedule` | Schema triggers (cron-like rules) attached to a task. |
| `get_task_executions` | Execution history of a task, newest first. |
| `get_task_script_log` | Full script log of the latest run of a task. |
| `get_failed_tasks_with_logs` | Every currently-failed task plus the tail of each script log, in one call. Best entry point for "what's broken on the server right now". |
| `start_task` | Trigger a reload task to run now. **Write operation.** |
| `create_task` | Create a new reload task for an application. **Write operation.** |
| `update_task` | Update task properties (`name`, `enabled`). **Write operation.** |
| `delete_task` | Permanently delete a reload task. **Destructive write operation.** |
| `create_task_schedule` | Attach a new schedule trigger to a task: `repeat` (`once`/`hourly`/`daily`/`weekly`/`monthly`) plus `interval_minutes` say how often, the optional 8-position `time_window` says when firing is allowed. **Write operation.** |
| `update_task_schedule` | Change one schedule trigger — retime it or turn just that trigger off, without disabling the whole task. **Write operation.** |
| `delete_task_schedule` | Remove one schedule trigger, leaving the task and its other triggers alone. **Write operation.** |

Write operations are clearly flagged in the docstrings; ask for explicit
user confirmation in the calling client before invoking them.

## Response envelope

Every tool wraps its result in:

```jsonc
{
  "tool_call_seconds": 0.234,   // wall-clock time, milliseconds precision
  // ...the tool's own JSON payload follows...
}
```

`tool_call_seconds` is always the first key. Use it to find the slow
calls in a session at a glance.

`engine_create_hypercube` returns its data as `columns` + `rows` (plain
values; numbers stay numbers) plus `grand_total`, `sorted_by`,
`sort_order` and `timings`. Pass `include_raw_layout=true` if you need
the untouched Qlik `qHyperCube` with per-cell `qElemNumber`/`qState`.

## Error envelope

On failure, tools return:

```jsonc
{
  "tool_call_seconds": 12.345,
  "error": "human-readable message",
  "error_type": "TimeoutError",      // or ConnectionError, Exception, ...
  "error_category": "socket_timeout", // see below
  "failed_step": "GetLayout",         // or CreateSessionObject, plan, ensure_app
  "hint": "what to do next",
  "tool": "engine_create_hypercube",
  "request": { "app_id": "...", "limit": 5000 },  // the exact arguments sent
  "traceback": "..."
}
```

`tool` and `request` are present on every failure, timeouts included —
so a failed call always tells you *which* query failed, not just that
something timed out.

The categories you can see from `engine_create_hypercube`:

- `invalid_sort` — `sort_by` matched no column, or `sort_order` was not
  `asc`/`desc`. The response lists `available_columns`.
- `limit_exceeded` — `limit > 5000`. Redesign as top-N or
  slice-by-category.
- `cell_cap_exceeded` — `columns * limit > 9900`. Drop columns or
  reduce `limit`. The hint contains an exact suggested value.
- `socket_timeout` — Engine is genuinely computing something slow. Add
  more set-analysis filters, reduce `max_rows`, or switch to top-N.
- `engine_api_error` — invalid expression / unknown field. The full
  Engine error is in `error`.
- `connection_error` — WebSocket connection problem.

See [troubleshooting.md](troubleshooting.md) for remediation steps for
each error category, including the typical fixes for `socket_timeout`
and `engine_api_error`.
