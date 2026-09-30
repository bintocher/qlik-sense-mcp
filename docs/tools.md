# Tools

The server exposes up to **28** MCP tools, grouped into three areas:

- **Repository API** — fast metadata via Qlik Repository (HTTP/QRS).
- **Engine API** — data and load script via Qlik Engine (WebSocket).
- **Task management** — reload tasks, schedules, executions, script logs.

**Availability depends on the authentication mode.** Task management
calls QRS endpoints that require repository-admin rights — a QMC role, not
a property of the authentication method — so these 14 tools default to on
in certificate mode (normally a trusted admin identity) and off in JWT or
form mode (normally an ordinary analyst identity). `QLIK_TASK_TOOLS`
overrides the default in either direction; see
[configuration.md](configuration.md#which-tools-are-registered):

| Mode | Tools registered |
|------|------------------|
| certificate | 28 — everything below |
| certificate, `QLIK_TASK_TOOLS=false` | 14 — analysis only |
| JWT (virtual proxy) | 14 — analysis only |
| JWT, `QLIK_TASK_TOOLS=true` | 28 — only if this identity has QRS admin rights, otherwise task calls 403 |
| form (login/password via virtual proxy) | 14 — analysis only |
| form, `QLIK_TASK_TOOLS=true` | 28 — same caveat as JWT above |

A tool the caller cannot use is not free: its name and description sit
in the model's context, and a model that reads about task administration
tries it.

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
| `get_app_details` | The data model and what the app's author defined, within about 20k tokens. `app` (name, description, stream, reload time); `tables` with a `role` read from the model's shape (fact, dimension table, calendar), `rows`, and their `fields` - `distinct`, `type`, `key`, `hidden`, `comment` from `COMMENT FIELD`, `values` when there are 25 or fewer, `min`/`max` on date fields; a calendar states its rows against its distinct dates, so a calendar where one date stands for several rows (as-of, cumulative, shifted) shows as such, and carries `plain_layer`: the set that keeps one row per date, the date itself, found by counting the calendar's rows per value of its few-valued fields; the field that picks it is marked `picks_plain_layer` and listed with its values even when hidden. `links` spell the joins (`Facts -Key-> Table`), `islands` list unlinked tables in one line each. `dimensions` are master dimensions as `name = field`; `measures` are master measures with their fields, the expression cut at 150 characters (`id` leads to the whole one through `get_app_object`) and `variants` such as `Y-1` or `YoY, %` folded under their base. `usage` sums up the author's sheets (approved sheets of a published app, all sheets otherwise; definitions only): each field's objects, sheets, role (`dimension`, `aggregated`, `condition` in a set modifier, `Aggr dimension`) and neighbouring fields and measures, unused fields, titled objects with their dimensions and measures, measure formulas written in objects, and objects in an alternate state or under a whole-cube set. `report_filters` lists how a user filters the report: filter panes, date pickers, variable inputs with their values, buttons, tab conditions. `bookmarks` are the approved ones only. `measures[].filter_limits` says what keeps a filter from reaching a measure (`fields` it sets itself, `own_set`, `computed_first` for `$(=...)`). `warnings` open with each such calendar and its condition, then volume, and a description whose dates or span ("2 months") match none of the date fields' ranges. `size_budget` gives the estimated tokens and, when the reply had to be shortened, what was `trimmed`. Always call this before building a hypercube. |

## Engine API

| Tool | Purpose |
|------|---------|
| `get_app_script` | The load script (`SET`/`LET`, `LOAD ... FROM ...`) a page of lines at a time: `offset`/`limit` (default the first 400 lines), with `total_lines` and `next_offset`; pages joined with a newline give the script back exactly. `search` returns only the lines containing a word, each with 5 lines around, as `matches` blocks. Read this to understand how calendar fields and named variables are built. |
| `get_app_variables` | User variables split by source (script vs UI), with wildcard search and pagination. |
| `get_app_sheets` | List of sheets in the app, with title and description. |
| `get_app_sheet_objects` | The objects on a sheet in reading order, each as one `summary` line - dimensions (`[label]=field`, `[label]==expression`, `master <id> "<name>" = field`), measures with their expressions, roles the expression does not show, alternative measures, show and calculation conditions, top N, state, `$(variables)`, and what filters, variable inputs and buttons do - plus `object_id`, `object_type`, `title` and `fields_used`. Containers list their tabs in `children` with `tab` and `shown_if`. Read by a rule per type from definitions only (`GetFullPropertyTree`), so nothing is calculated; master visualizations and master items are read once per app. |
| `search_app` | Find which field holds a value, and how it is spelled there. Use before writing a set-analysis filter on a value you have not seen: Qlik answers a filter on a missing value with zeros, not an error. Omit `fields` to search every field with up to 100000 distinct values (larger ones are listed in `fields_not_searched`), or name them for an instant answer; a name the app does not have is refused as `field_not_found`. |
| `get_app_object` | One object in full: its definition in structured form (`dimensions`, `measures`, `conditions`, `alt_measures`, `controls`, `children`, the `variables` it uses with their definitions) and `data` computed by the server as one flat hypercube from the object's own columns, under the limits of `engine_create_hypercube`. A pivot or stacked chart comes back as rows, not as the tree its layout would be; the row whose dimension is empty (`-`) is kept; a column whose show condition is false now is left out and named in `data.notes`. The layout itself is not returned. |
| `get_app_field` | Distinct values of one field with pagination and wildcard search, both applied by Engine over the whole field rather than over a prefetched prefix. Falls back to a single-dimension hypercube if the underlying `ListObject` returns nothing. Adds `field_comment` when the load script commented that field. |
| `engine_get_field_range` | Lightning-fast bounds for one field: count distinct, min, max. Implemented as a measures-only hypercube — runs in seconds on any table size. Prefer this over `get_app_field_statistics`. |
| `get_app_field_statistics` | Field statistics via a measures-only hypercube. Defaults to **light** mode (count distinct, non-null count, null count, total count, min, max, null %, completeness — the null share comes from `NullCount()`, not from subtracting one non-null count from another). Pass `full=true` to also compute avg / sum / median / mode / stdev — slow on big fact tables and meaningless for date/text fields. |
| `engine_query` | **The main data-analysis tool.** A query stated rather than written: `group_by` names the grouping fields, `metrics` the aggregations (`{"field": "Amount", "agg": "sum"}`), `filters` the period or values to narrow to. The server writes the Qlik expressions, proves the filters select something, and reports the period each one actually covered. `queries` takes a list of independent questions — different groupings, different measures, different filters — and runs the whole list over three round-trips rather than three per question. A master measure is named with `{"master": "<name>"}`; with `filters` it takes them as an outer set in front of it, and `warnings` says when the measure keeps a filter out. Past the cap of 200 expressions, a batch that differs only in one filter comes back with `one_query`, the grouped query that answers all of it. |
| `engine_create_hypercube` | The same shape, with the expressions written by the caller: for a nested aggregation, `Aggr()`, `FirstSortedValue`, `P()`/`E()` set analysis, or a calculated dimension. Ranking is `sort_by` + `sort_order` + `limit`. Rows with a NULL dimension value (Qlik's `"-"`) are kept unless `exclude_null_dimensions` says otherwise. Hard limits: `limit <= 5000`, `columns * limit <= 9900`. `filters` works here too, applied wherever a measure carries the `{filter}` marker: in front of the whole expression (`{filter} [Revenue] / [Orders]`) it is an outer set that reaches every aggregation inside, master measures included; inside one aggregation (`Sum({filter} Amount)`) it narrows that one; next to a set of the expression's own (`Sum({filter} {<Flag={1}>} Amount)`) it is merged into that set, since Qlik reads one of two sets side by side. With no marker in any measure, every measure takes the filters in front of it. `warnings` names an expression that sets a filtered field itself, counts over a set with an identifier, holds `$(=...)` or picks values with P()/E() over sets the filter does not reach; `period_check` and `fan_out_check` are the same as in `engine_query`. |

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
  "count": 18,                  // ...then the tool's own payload
  "apps": []
}
```

`tool_call_seconds` is always the first key. Use it to find the slow
calls in a session at a glance.

`engine_query` answers with `results`, one entry per query, each
carrying `id`, `columns`, `rows`, `total_rows`, `grand_total`,
`sorted_by`, `filters_applied` and `period_check`.
`engine_create_hypercube` returns the same `columns` + `rows` shape for
a single query, plus `timings`; pass `include_raw_layout=true` for the
untouched Qlik `qHyperCube` with per-cell `qElemNumber`/`qState`.

Values come back as data: numbers stay numbers, and a date reads as the
text Qlik displays for it — the same writing the sample values, the
field values and the field range use for that field. One value has one
writing everywhere in the reply.

### Narrowing: what a filter can say

A filter names one field and one condition. Several conditions on one
field are several filters, not one filter with several keys — stating
two at once is refused rather than answered by the first of them.

| Written as | Means |
|:---|:---|
| `{"field": "Region", "values": ["North"]}` | keep these values |
| `{"field": "Region", "exclude": ["North"]}` | keep everything else |
| `{"field": "Region", "add": ["South"]}` | add to what is selected |
| `{"field": "Region", "intersect": ["North"]}` | keep what is in both |
| `{"field": "Amount", "greater_than": 400}` | above a bound |
| `{"field": "OrderDate", "period": "2024-03"}` | a period: a year, a month or a day |
| `{"field": "Client", "contains": "ltd"}` | text search, case-insensitive |
| `{"field": "Client", "matching": {...}}` | values of this field that satisfy a condition on another |
| `{"field": "Client", "match_expression": "Sum(Amount) > 1000"}` | values an expression holds for |

`matching` and `not_matching` take filters of their own, and answer the
question "which clients bought in 2023" without a second query:

```jsonc
{"field": "Client",
 "matching": {"filters": [{"field": "Year", "values": ["2023"]}]},
 "not_matching": {"filters": [{"field": "Year", "values": ["2024"]}]}}
```

Both together read as "matched the first and not the second".
`of_field` reads the values from a different field than the one being
narrowed.

### Scope: what the query counts over

`scope` says what the numbers are counted over, before any filter
narrows them:

| Written as | Means |
|:---|:---|
| `{"ignore_selections": true}` | the whole model, whatever is selected |
| `{"bookmark": "BM01"}` | what that bookmark selects |
| `{"state": "Compare"}` | what that alternate state selects |
| `{"selection_back": 1}` | the selections as they were one step ago |
| `{"current_selection": true}` | what is selected right now, stated plainly |

A scope stated on its own, with no filters beside it, applies as it
reads. Stated on the query it reaches every measure; stated on a metric
or on one part of an arithmetic metric, it reaches only that one.

### Combining sets

Two sets joined by one operation answer what no modifier on a single field
can: "bought in 2023 **or** lives in the South".

```jsonc
{"combine": "union",
 "of": [{"ignore_selections": true,
         "filters": [{"field": "Year", "values": ["2023"]}]},
        {"ignore_selections": true,
         "filters": [{"field": "Region", "values": ["South"]}]}]}
```

| `combine` | Answers |
|:---|:---|
| `union` | everything in either set |
| `intersect` | only what is in both |
| `exclude` | the first without the second |
| `symmetric_difference` | what belongs to exactly one of them |

Each set is described the way any scope is, with filters of its own. A
filter stated outside the combination is refused: Qlik reads no modifier
written around one.

### Metrics beyond a single aggregation

A metric is `{"field": ..., "agg": ...}`, and four keys extend it:

- `filters` — this metric alone is narrowed, the others are not. The
  numerator and the denominator of a ratio come back in the same row.
- `total` / `total_except` — count across the grouping instead of within
  it: a share of the whole (`total`), or of the group named by
  `total_except`.
- `inner_agg` + `per` — aggregate twice: `{"field": "Days",
  "inner_agg": "sum", "per": "IssueId", "agg": "median"}` reads as "the
  median over issues of the days summed within each issue".
- `op` + `of` — arithmetic over aggregations. Each part can carry its
  own `filters` and its own `scope`; division answers with no value
  rather than an error when the denominator is zero.

The reply lists what each of them was narrowed by under
`measure_filters`, a part of an arithmetic metric under the metric's
label with its position.

### Checking a period from the answer

A query filtered on a period carries `period_check`: the earliest and
latest value of that field as each measure reads it - through the
measure's own sets, so a measure written by hand or taken from the library
is checked as it runs - and `filter_applied`. A value outside the
requested period means Qlik ignored the condition, which is otherwise
invisible — it answers with a plausible number rather than an error; the
check then names the `measure`. `engine_create_hypercube` carries the same
check.

A period that applied can still reach the facts through days outside it.
When the filtered field reaches the largest table through a date key -
a shifted or cumulative calendar - and the key holds values outside the
period under the filter, `period_check.fan_out` gives the `key`, the
number of `values` and their `min` and `max`, and `warnings` says the
figures cover more than the period.

Any filter on a calendar whose dates repeat - more rows than dates, as in
a cumulative, as-of or shifted calendar - is checked the same way, a value
list or a set written by hand in a measure included: when the key values
it reaches belong to other values of its field too, `fan_out_check` gives
the `field`, the fact `table` and `key`, the `key_values` reached,
`per_value` and the `repeating_tables` that cause it, and `warnings` leads with it,
naming the rows that keep one per date where `get_app_details` found them
(its `plain_layer`). The field that picks such rows is not checked
itself. `filters_applied[].form` says how the
period was written: `range` in the field's display format, `numeric`, or
`expression` where neither counts the right values.

Engine's error codes are said in words inside `error`: 7004 is the
calculation time limit, 7009 more than 10000 cells on a page, 30100 a
generated set that is too long, 19, 6 and 7002 lack of memory.

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

### Warnings

A successful `engine_create_hypercube` reply carries a `warnings` list.
It is empty on a clean query and names the things Qlik answers instead
of refusing:

- a measure whose every value came back `0` or `'-'` — the signature of
  an aggregation over no rows;
- a query that returned no rows at all;
- a period filter whose result holds values outside the period asked
  for, which means Qlik dropped the condition;
- a cut result with no sort, where the rows are arbitrary rather than
  the largest.

Treat a warning as "check this before quoting the number", not as a
failure: the rows are real, their meaning may not be.

The categories you can see from `engine_create_hypercube`:

- `invalid_sort` — `sort_by` matched no column, or `sort_order` was not
  `asc`/`desc`. The response lists `available_columns`.
- `limit_exceeded` — `limit > 5000`. Redesign as top-N or
  slice-by-category.
- `cell_cap_exceeded` — `columns * limit > 9900`. Drop columns or
  reduce `limit`. The hint contains an exact suggested value.
- `socket_timeout` — Engine is genuinely computing something slow. Add
  more set-analysis filters, reduce `max_rows`, or switch to top-N.
- `field_not_found` — a name the data model does not have, in a
  dimension, a measure or a set modifier. Qlik would not refuse it: an
  unknown name is evaluated as an expression worth 0, so the cube
  collapses to one row carrying the grand total, or the measure comes
  back as a column of zeros. Either reads as a real answer. The reply
  lists `unknown_fields` and `did_you_mean`.
- `invalid_expression` — Qlik's own parser rejected an expression; its
  message is quoted in `error`.
- `empty_period` — the period asked for holds no value of that field.
  The reply says how to read the loaded period.
- `value_not_found` — the field does not hold the value filtered on.
  `did_you_mean` lists what it holds instead.
- `engine_api_error` — invalid expression / unknown field. The full
  Engine error is in `error`.
- `connection_error` — WebSocket connection problem.

See [troubleshooting.md](troubleshooting.md) for remediation steps for
each error category, including the typical fixes for `socket_timeout`
and `engine_api_error`.
