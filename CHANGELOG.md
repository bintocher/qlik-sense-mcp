# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic Versioning.

## [1.8.0] - 2026-08-11

### Added

- **`update_task_schedule` and `delete_task_schedule`** — a task can have
  several triggers, and until now the only way to stop one through this
  server was `update_task(enabled=False)`, which stops the task and every
  other trigger with it. Both verified end to end against a live Qlik:
  disable one trigger, retime it, give it a window, delete it.
- `update_reload_task` refuses fields that must not travel in a PUT
  (`id`, `createdDate`, the operational section) and reports a QRS 409 as
  `error_category: conflict` — someone else changed the task between the
  read and the write, which the caller answers by re-reading, not by
  retrying blindly.
- **`QlikEngineAPI.send_requests_pipelined()`** — sends a batch of
  independent JSON-RPC requests back-to-back and matches responses by
  `id`, which the Engine protocol supports precisely so replies can come
  back out of order. `send_request()` is untouched. `raise_on_error=False`
  returns per-item exceptions so one bad item does not lose the batch.
  (from PR #29)
- `_get_sheet_objects_detailed()` uses two pipelined batches instead of
  one `GetObject`+`GetLayout` round-trip per object: 2 round-trips
  instead of 2N. Measured on a live sheet with 16 objects: 0.135s to
  0.028s, identical results. (from PR #29)
- `QLIK_WS_IDLE_PROBE_AFTER` (default 30s), `QLIK_WS_PROBE_TIMEOUT`
  (15s) and `QLIK_WS_GREETING_TIMEOUT` (15s) — liveness and handshake
  budgets, separate from `QLIK_WS_TIMEOUT`, because a real query may
  legitimately take minutes while a health check never should.
  (adapted from PR #28)
- **End-to-end test suite** (`tests/test_e2e_*.py`, marker `e2e`) that
  runs the tools against a real Qlik and asserts on real values: ranking
  actually ordered by the measure, pagination actually moving, NULL
  groups actually dropped, the connection actually reused. Skipped
  unless `QLIK_E2E_*` names a server. `tests/test_e2e_large_app.py`
  additionally needs an app with 10M+ rows — none of the defects fixed
  in this release could be reproduced by a fake socket.

### Changed

- **`get_app_variables` returns objects, always.** An empty group came
  back as `""` instead of `{}`, so the field's type depended on whether it
  had data. It also dropped script variables unless `created_in_script`
  was passed explicitly, which left `variables_from_script` permanently
  empty on the default call — half the reply was structurally dead. The
  default now returns both sources, and the reply carries `count` and
  `total_found`.
- **`qSuppressMissing` is never set.** Measured on Qlik 31.62: it drops
  exactly one row — the NULL-dimension group — and leaves rows with a NULL
  *measure* untouched. That is what `qNullSuppression` per dimension
  already does, under the caller's control, so the cube-wide flag only
  ever meant an explicit "keep the NULL group" could be overridden.
- **`engine_api.py` and `server.py` are split into packages.** The Engine
  client became `engine/` — transport, hypercubes, fields, sheets and
  app metadata as separate mixins assembled in `engine/api.py` — and the
  tools became `tools/`, grouped by the API they talk to, with shared
  state in `tools/context.py` and the response envelope in
  `tools/helpers.py`. `from .engine_api import QlikEngineAPI` and
  `from .server import get_apps` keep working; the largest module went
  from 3394 lines to 875.

### Removed

- 43 methods that nothing called (~1180 lines): `get_table_data`,
  `create_data_export`, `get_visualization_data`,
  `get_detailed_app_metadata`, `get_sheets_with_objects`, bookmark and
  selection helpers, and the rest of the unreachable surface of
  `QlikEngineAPI` and `QlikRepositoryAPI`. Among them
  `get_pivot_table_data`, which raised `NameError` on every call, and a
  duplicate `get_field_description` shadowed by a later definition —
  deleting the visible one would have silently reactivated the dead one.

### Fixed

- **QRS paging is done by the server now.** `get_apps` fetched
  `app/full` — which ignores skip/take and is itself truncated at the
  QRS MaxRecordLimit (100 by default) — and then sliced the result
  locally. Every app past that cap was invisible, and `total_found`
  reported the cap as if it were the total, so a client paging through
  `has_more` walked off the end of the data without an error. Pages now
  come from `app/table` with skip/take and the total from `app/count`.
  The same applies to reload tasks (`reloadtask/table`, read through to
  the last page) and to execution history, where `top` became a
  server-side `take` instead of a slice of an arbitrary window.
- **`published="both"` returns both.** It was documented as "any other
  value means both" but folded into the default `True`, so the mode was
  unreachable — the tool answered with published apps only. Live check on
  8 published + 10 unpublished apps now returns 18.
- **`create_task_schedule` produced schedules Qlik rejected outright.**
  The body carried an empty `operational` section, which QRS answers with
  400 "invalid property ... operational with EMPTY GuID" — so the tool
  failed on every call. Behind that were three more errors: an
  `incrementOption` numbering that does not exist (there is no
  "minutely", and daily is 2 rather than 3), the interval written into
  the *days* position of `incrementDescription`, and an enum code sent
  where `schemaFilterDescription` expects an 8-position window string.
  Verified end to end: a created trigger now comes back with a real
  `nextExecution` instead of the 1753 sentinel.
- **Failed tasks include aborted and errored runs.** The filter matched
  status 8 (FinishedFail) only, missing 6 (Aborted) and 11 (Error) — the
  status codes were confirmed against the server's own
  `TaskExecutionStatus` enum rather than assumed. `get_tasks` also gained
  the documented-but-missing `"running"` filter, and an unknown
  `status_filter` is now refused instead of quietly returning everything.
- **Hypercube pages are completed.** Engine may trim a page below the
  requested `qInitialDataFetch`; the remainder is now read with
  `GetHyperCubeData` before the reply is assembled, so a request for 4000
  rows no longer returns fewer without saying so.
- **One Engine call at a time.** The Streamable HTTP transport serves
  several MCP clients from one process while the client holds a single
  WebSocket and a single open document. Overlapping calls interleaved on
  that socket: strict id-matching made each discard the other's reply,
  and `ensure_app` could switch documents mid-call. Engine-backed tools
  now hold the client's reentrant lock for the whole call
  (`_engine_serialised`); Repository-only tools are deliberately left
  unsynchronised so a slow hypercube does not block them.
- **Session objects are always destroyed.** `get_sheets` and
  `_get_user_variables` never destroyed theirs at all, and the rest of
  the client cleaned up after the read rather than in a `finally`, so any
  early return or exception leaked the object — and its result set — into
  Engine memory for the rest of the session. Creation now goes through
  `QlikEngineAPI.session_object()`, a context manager that generates a
  unique id and destroys it on every path.
- **Field search and paging happen in Engine.** `get_app_field` read the
  first 500–5000 values, matched them in Python and sliced the result,
  so on a larger field a match simply did not exist: verified on a field
  with 200,000 distinct values, where `search_string="C19999*"` returned
  nothing and `offset=150000` returned an empty page. Both now go to
  Engine — the search as a NULL-suppressed calculated dimension, the
  offset as the page top — and the reply carries `total_matches`.
  Passing `search_string` and `search_number` together is refused
  instead of one silently overwriting the other.
- **Null statistics were meaningless.** `get_app_field_statistics`
  labelled Qlik's `Count()` — which ignores NULLs — as `total_count`, and
  paired it with `Count({$<[field]={'*'}>})`, an aggregation with no
  argument. The derived `null_percentage` therefore compared two counts
  of the same non-null values and came out near zero however much was
  missing. It now uses `NullCount()`, reports `null_count` alongside
  `non_null_count`, and derives `total_count` as their sum. On a field
  built with exactly 5% NULLs the answer is 5.0%; it used to be 0.
- **`get_app_object` no longer raises KeyError** when Engine returns no
  handle for an object (an unknown id, or a type it will not open) —
  that came back as an opaque failure instead of
  `error_category: object_not_available`.
- **A Repository failure is no longer reported as an empty list.**
  `get_tasks`, `get_failed_tasks_with_logs`, `get_task_executions` and
  `get_apps` turned a 500, a 403 or a refused connection into "nothing
  found". They now return `error_category: repository_error` with the
  original cause.
- `/count` replies are accepted in both shapes QRS uses — a bare integer
  and `{"value": N}`. Insisting on the object form would have failed
  every paged read on a server that answers with the number.
- A hypercube page that could not be read is now labelled `INCOMPLETE`
  rather than reusing the "showing the top N" wording, which made a
  failed read look like a deliberate ranking. The cause travels in
  `timings.page_fetch_error`.
- `get_task_schedule` and `get_task_dependencies` no longer answer
  "no schedule" / "no dependencies" when QRS is unreachable; composite
  events are also read page by page instead of from the truncated
  `compositeevent/full`.
- `_read_all` compares what it collected against `/count` and reports a
  short read instead of returning a quietly truncated list. The same
  applies to its safety cap, which used to appear only in the server log.
- Case-sensitive field search keeps wildcard semantics (`Match`, which
  honours `*` and `?`); it previously stripped the wildcards and fell
  back to a substring test, so `C1*9` matched anything containing "C19".
- `daylightSavingTime` is a parameter of `create_schema_trigger` instead
  of a hard-coded 0, and `create_task_schedule` gained `time_window` —
  the 8-position filter that says when a schedule may fire. It existed
  in the Repository layer but no tool could reach it, so a caller could
  only ever set how often, never when.
- Each Engine client gets its own transaction lock. A class-level
  default coupled every partially-constructed client in the process,
  so unrelated instances serialised against each other.
- 12 more dead functions removed: the unreachable `get_app_details`
  branch of the Engine client and its helpers — two of which called
  methods that do not exist anywhere in the package — plus the unused
  ticket/proxy helpers and a no-op loop over `qChildList`.
- **Wildcards are Qlik's, not Python's.** The case-sensitive filter went
  through `fnmatch`, which also reads `[...]` as a character class — so the
  pattern `Order[12]` matched the value `Order[1]`, a match Qlik would
  never make. Patterns are now compiled with only `*` and `?` as
  wildcards and everything else literal.
- **The search's own caveats reach the caller.** `search_truncated`,
  `total_matches_at_least` and `candidates_scanned` were computed and then
  dropped by the tool layer, so a capped scan looked like the complete
  answer.
- **Changing a schedule's repetition now requires the interval too.**
  `incrementOption` and `incrementDescription` describe one schedule
  between them; switching daily→hourly while keeping `"0 0 1 0"` meant
  "hourly, every 1 day". Refused instead of saved.
- **`start_date` no longer defaults to a date in the past.** The hard-coded
  `2026-04-01` had long since passed, and a `once` schedule dated in the
  past never fires. It now defaults to the next midnight, and the docstring
  says what the value actually means: wall-clock time in `time_zone`, not
  UTC.
- **Session objects in `get_field_values` and `get_field_statistics`** are
  destroyed on the exception paths as well — both cleaned up after the read
  rather than in a `finally`.
- **`_read_all`'s safety cap counts rows, not offsets**, so it no longer
  refuses the last page one row early.
- **Case-sensitive field search kept its wildcards.** The previous fix
  reached for `Match()`, which respects case but does not understand `*`
  or `?` — verified on 31.62, `Match('C000001','C00000*')` is 0. Qlik has
  no case-sensitive wildcard function at all, so the search now asks
  Engine for the case-insensitive superset with `WildMatch` and narrows it
  here. That cannot miss anything (the exact answer is a subset), paging
  is applied after the narrowing, and the scan is capped so one search
  cannot walk a 200k-value field end to end. When the cap is hit the reply
  says `search_truncated` and reports `total_matches_at_least` instead of
  a total it did not count.
- **Script-log retrieval works again.** Moving execution history to the
  table endpoint dropped the very fields the log download needs
  (`scriptLogAvailable`, `scriptLogLocation`), so every request fell
  through to the summary fallback. The table endpoint now returns ids and
  each execution is read in full. An error envelope from that call is also
  reported as one — iterating it walked the dict's keys and raised
  `AttributeError` on the first `.get`.
- **`grand_total` no longer contains nulls.** It only guarded against the
  `"NaN"` sentinel, while the rows also handle a missing `qNum`; a total
  Engine returned as text therefore arrived as JSON `null`.
- **An empty extra page marks the result incomplete.** The page loop
  stopped on it, as intended, but said nothing — so a short answer looked
  like a deliberate top-N.
- **`_read_all` no longer reports a phantom truncation.** A result whose
  size is an exact multiple of the page size reached the safety cap having
  already read everything, and that was reported as a partial read. It now
  stops as soon as the collected count reaches what `/count` promised.
- **Session objects in `get_field_range`** are destroyed on the early
  return as well — the cleanup was written after it.
- **An unreadable load script says so.** Reading it needs Professional
  access; with an Analyzer licence Qlik returns an empty string and no
  error, which is indistinguishable from an app that has no script.
  `get_app_script` now attaches a `note` explaining the difference.
- **The cached WebSocket is no longer wedged by its own health check.**
  `_is_connected()` sent a WebSocket ping before reusing the connection.
  Qlik's Proxy Service does not relay ping/pong to the Engine: through a
  virtual proxy the first request after a ping is never answered, so the
  call blocked for the whole `QLIK_WS_TIMEOUT` (180s by default) and only
  then reconnected. In JWT mode — the only mode that goes through a proxy
  — that meant every second tool call hung for minutes, and each forced
  reconnect burned another Engine session until Qlik's per-user limit
  (5) refused new ones and every call failed. Verified on Qlik 31.60: the
  same ping is harmless on a direct Engine socket (port 4747, certificate
  mode), which is why it survived this long. A connection that answered
  within `QLIK_WS_IDLE_PROBE_AFTER` seconds is now reused as-is, and an
  idle one is validated with a real `EngineVersion` request — which also
  proves more than a ping did, since it shows the Engine still answers
  rather than just that the socket accepts writes. Live effect on a
  repeated call: 63s timeout to 0.005s.
- **A refused Engine session is now reported as such.** `connect()` read
  exactly one greeting frame and treated it as "session established".
  When Qlik answers a new socket with `OnMaxParallelSessionsExceeded`
  (the per-user session limit) and closes it immediately, the client
  reported success and the next call failed with `Failed to parse
  WebSocket frame ... Expecting value` — a message that says nothing
  about the cause. Greeting frames are now read up to `OnConnected`, and
  a fatal one raises `QlikSessionLimitError` naming the quota and how to
  clear it.
- **`exclude_null_dimensions=false` now actually keeps the NULL group.**
  Sorting by a measure switched on the cube-wide `qSuppressMissing`, which
  drops the NULL-dimension row as well — so the one request whose whole
  point is "show me how much data is unattributed" came back with that row
  removed. On a 10M-row app the NULL group held 499,918 rows and never
  appeared. The suppression is now off whenever the caller opts into
  keeping NULL groups; the ranking itself is unchanged.
- **A missing field is reported instead of invented.** `get_app_field`
  fell back to a single-dimension hypercube when the `ListObject` path
  returned nothing, and Qlik evaluates an unknown field name as an
  expression worth 0 — so a misspelled field answered
  `{"field_values": ["0"]}`, which reads as data. The field's existence is
  now checked first (one `GetFieldDescription`, whose result is reused for
  `field_comment`), and an unknown name returns `error_category:
  field_not_found`.
- **A Repository failure is no longer reported as a missing app.** Any
  QRS error — 500, 403, a refused connection, a failed JWT bootstrap —
  surfaced from `get_app_details` as `App not found by provided app_id`,
  sending the caller to look for a different id while the real answer was
  that Qlik was unreachable. Found by the e2e suite while the Qlik proxy
  happened to be restarting. Such failures now carry
  `error_category: repository_error`, the original cause and a hint;
  a genuinely absent app keeps `app_not_found`.
- **Session-object ids are unique per call.** `create_hypercube`,
  `get_field_values`, `get_field_range` and `get_field_statistics` built
  `qId` from the request's shape (e.g. `hypercube-1d-1m`), so a call
  issued shortly after an identically-shaped one was destroyed could get
  a stale cached calculation back instead of an evaluation of its own
  `qDef`. Every id now carries a `uuid4` suffix, and the matching
  `DestroySessionObject` uses the generated id. (from PR #27)

## [1.7.2] - 2026-07-31

### Added
- **Field and table comments are now returned.** Qlik keeps the business
  description of a column in `qComment`, set by `COMMENT FIELD x WITH
  '...'` / `COMMENT TABLE t WITH '...'` in the load script, and hands it
  out in `GetTablesAndKeys` for every field and table. The server used to
  drop it — `get_app_details` reported a hard-coded empty comment — so an
  LLM had to infer a column's meaning from its name alone. Now
  `get_app_details` puts a `comment` key on every table and field that
  carries one, and `get_app_field` returns `field_comment` for the field
  it lists. The key is omitted when the script sets no comment, so apps
  without comments pay nothing in context size.
- `QlikEngineAPI.get_field_description()` — thin wrapper over the Engine
  `GetFieldDescription` method: name, comment, source tables, cardinality,
  byte size for a single field, with no data page and no hypercube.
  Returns `{}` for a field the model does not know.
- `tests/test_field_comments.py` — covers comment propagation through
  `get_fields`, `get_field_description` and the `get_app_details` payload,
  including the "no comment set" case that must not emit the key.

## [1.7.1] - 2026-07-29

### Fixed
- **`ensure_app()` could cache a handle as "has data" when Engine actually
  opened it without data.** The cached-connection reuse logic trusted the
  requested `no_data` flag rather than checking what Engine actually did.
  If a WebSocket session got shared/attached to an existing no-data
  session for the same user+app, `GetAppLayout`/`GetAppProperties` still
  succeeded, but `GetTablesAndKeys` silently returned `qtr: []` — an app
  looked fully readable while its data model came back empty, with no
  error surfaced. `ensure_app()` now reads Engine's own
  `qIsOpenedWithoutData` from `GetAppLayout` right after `OpenDoc`,
  retries once on a fresh connection if data was requested but not
  granted, and raises `QlikEngineError` instead of returning a handle
  that would later produce a silently empty data model.
- Corrected misleading inline comments on `GetTablesAndKeys` positional
  arguments (`qCellHeight` / `qSyntheticMode` / `qIncludeSysVars`) in
  three call sites — the values were already correct, only the comments
  describing them were wrong.

## [1.7.0] - 2026-07-29

### Added
- **Support for MCP SDK 2.x.** SDK 2.0 removed `mcp.server.fastmcp` and
  replaced the FastMCP host with `mcp.server.mcpserver.MCPServer`.
  `server.py` now selects the host class at import time and exposes the
  result as `MCP_SDK_MAJOR`, so the same code runs on both SDK lines.
  The only behavioural difference between them is that 2.x takes the
  bind address in `run_streamable_http_async()` rather than in the
  constructor; everything else — the `@tool()` decorator, `stdio`, the
  tool registry used by `--help` — is identical.
- `tests/test_sdk_compat.py` — asserts that the selected host matches the
  installed SDK, that every API the server calls exists on it, and that
  the published `engine_create_hypercube` schema still carries the
  ranking parameters and its docstring. A future SDK change now fails
  here instead of in a user's terminal.

### Changed
- **Dependency is now `mcp>=1.8.0,<3.0.0`.** The `<2.0.0` pin from 1.6.1
  is no longer needed. The floor was raised from the long-standing (and
  incorrect) `1.1.0`: `FastMCP.run_streamable_http_async()` — the default
  transport — first appears in 1.8.0, so 1.2–1.7 would install happily
  and then die with `AttributeError` on startup, and 1.1 has no
  `FastMCP` at all. The upper bound keeps the next breaking SDK major
  from breaking installs again.

### Fixed
*(found by an automated review of this release)*
- **Hypercube session objects leaked on every failure after creation.**
  `DestroySessionObject` ran only on the success path, so a malformed
  layout or an Engine error left the result set pinned in Engine memory
  for the rest of the (deliberately long-lived) session. Cleanup moved
  into a `finally`; it is skipped only when the socket has already been
  force-closed, where there is nothing left to talk to.
- **`limit=0` or a negative limit silently returned one row.** The page
  height was clamped with `max(1, ...)`, so a nonsensical limit produced
  data instead of an error. Non-positive and non-integer limits now
  return a structured `invalid_limit` error before any Engine call.
- **A dimension sort expression given in Qlik's native `{"qv": "..."}`
  form was double-wrapped** into `{"qv": {"qv": "..."}}` and silently
  ignored by the Engine. Both that form and a plain string are now
  accepted.
- Corrected the comment and docs around the automatic `qSuppressMissing`
  applied when ranking by a measure: it is a cube-wide flag that drops
  rows where *any* measure is missing, not only the ranked one. The
  Engine offers no per-measure equivalent.

### Verified
- Both SDK lines were exercised end to end: full test suite on mcp
  1.29.0 and on mcp 2.0.0 (158 tests each), the streamable-HTTP
  transport answering a real `initialize` + `tools/list` handshake on
  both (24 tools published with identical schemas), and a live
  `tools/call` against a 91M-row Qlik app on 2.0.0 returning a correct
  top-5 ranking.

## [1.6.1] - 2026-07-28

### Fixed
- **Pinned `mcp<2.0.0` — the server would not start otherwise.** MCP SDK
  2.0.0 was released on 2026-07-28 and removes `mcp.server.fastmcp`, the
  FastMCP host this server is built on, in favour of a new
  `mcp.server.mcpserver` API. Because the dependency was declared as
  `mcp>=1.1.0`, every fresh install — including 1.6.0 and every earlier
  release — resolved to 2.0.0 and died at import with
  `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. The pin
  restores installability; porting to the 2.x API is a separate piece of
  work.
- **The NULL dimension row no longer hijacks a ranking.** Facts that
  carry no value for the grouping field all collapse into a single row
  that Qlik renders as `"-"`, so that row often holds a very large total
  and takes first place in a top-N, pushing out every real value.
  `engine_create_hypercube` now sets `qNullSuppression` on every
  dimension. Observed on a live app: the `"-"` row held the entire
  measure total and occupied rank 1 of the result.

### Added
- **`exclude_null_dimensions`** on `engine_create_hypercube`, default
  `True`. Pass `False` to keep the `"-"` row — useful precisely when you
  want to measure how much data is unattributed, since a large `"-"`
  total means the grouping field is not linked to those facts in the
  data model.

## [1.6.0] - 2026-07-28

### Added
- **Real top-N support in `engine_create_hypercube`.** New `sort_by` and
  `sort_order` parameters order the result by ANY column — a measure
  label, a measure expression, or a dimension field name (matching
  ignores case and square brackets). `sort_by` is translated into
  `qInterColumnSortOrder` with that column first, plus the matching
  `qSortBy` / `qSortCriterias` direction. Combined with `limit`, this
  finally answers "the 10 clients with the highest GGR" in one call.
- **`limit` parameter**, a clearer name for `max_rows`. `max_rows` still
  works as an alias, so existing callers and saved prompts are
  unaffected.
- **`suppress_zero`** to drop rows whose measure is 0 — mainly useful
  with `sort_order="asc"`, where zero-valued groups would otherwise fill
  the whole result.
- **Request echo on every failure.** Any error reply — raised exception
  or an `{"error": ...}` payload, including timeouts — now carries
  `tool` and `request` with the exact arguments that produced it. A
  timeout finally says WHICH query timed out instead of just "timed out
  after 180s". Implemented once in the `_timed` decorator, so it applies
  to all tools.
- **`timings` in the hypercube response**, split into
  `open_app_seconds` (loading the app into Engine memory, paid only by
  the first call against an app) and `get_layout_seconds` (the actual
  computation) — so a slow call can be attributed instead of guessed at.
- **Usage examples in every tool docstring**, with realistic arguments
  and a shortened but structurally correct response.
- **Qlik session-limit warning in the Engine tool docstrings and in
  `docs/AUTH_JWT.md`**: Qlik permits at most 5 concurrent sessions per
  user identity and can lock the account beyond that, so tool calls must
  never be fanned out in parallel. This server deliberately funnels
  everything through one cached Engine session.
- `tests/test_hypercube.py` and `tests/test_tool_registration.py` — 52
  new tests covering sort resolution, the generated `qHyperCubeDef`,
  guard rails, the request echo and per-mode tool visibility.

### Changed
- **Reload-task tools are registered in certificate mode only.** They
  call QRS endpoints that require repository-admin rights, which a JWT
  analyst does not have, so JWT mode now advertises 12 tools instead of
  24 rather than offering 12 that can only return 403.
- **The hypercube response is compact by default.** It now returns
  `columns` plus `rows` (plain values — numbers stay numbers) together
  with `grand_total`, instead of the raw `qHyperCube` with its per-cell
  `qElemNumber`/`qState` metadata. Pass `include_raw_layout=true` to get
  the full Qlik layout back.
- **`engine_create_hypercube`'s docstring was rewritten** around a
  SQL analogy and three worked examples, and no longer recommends
  `qSortByExpression` for ranking. Measured on a 91M-row table: sorting
  by the measure column runs in 0.2s where `qSortByExpression` took
  286s, because the latter makes the Engine compute the same aggregate a
  second time purely to order rows.
- **The `socket_timeout` hint now lists fixes in order of impact** and
  names the correct environment variable (`QLIK_WS_TIMEOUT`; it
  previously pointed at `QLIK_WS_OPERATION_TIMEOUT`, which does not
  exist).
- Truncation warnings distinguish a ranked query (expected: "showing the
  10 highest rows of 1217") from an unsorted one (a real problem: the
  returned rows are arbitrary).

### Fixed
- **Sorting by a measure never worked.** `qInterColumnSortOrder` was
  hard-coded to `list(range(n_cols))`, so the first dimension always won
  and the per-measure `qSortBy` was dead configuration. Any "top 10 by
  revenue" request silently returned 10 alphabetically-first rows —
  plausible-looking data in the wrong order. Verified against a live
  91M-row app: the new ordering matches a reference sort computed
  independently over all 1217 groups.
- **`create_hypercube` mutated its caller's dictionaries**, injecting
  default `sort_by` keys into the passed-in `dimensions` / `measures`
  and then returning them as the "echoed input".
- **Hypercube session objects were never released.** Each call left its
  session object alive, pinning the result set in Engine memory for the
  rest of the session; they are now destroyed after the data is read.
- `DEFAULT_HYPERCUBE_MAX_ROWS` was imported in `engine_api.py` but
  unused, while the default was hard-coded to 1000 in the signature.
- Docstring corrections: `get_apps` documented QRS field names
  (`modifiedDate`, `lastReloadTime`, `published`, `fileSize`) that the
  tool does not return — it returns `modified_dttm` / `reload_dttm`;
  `get_task_executions` documented snake_case keys where QRS returns
  camelCase with `duration` in milliseconds.

## [1.5.1] - 2026-04-27

### Changed
- **Documentation overhaul.** All `.md` files reviewed for accuracy
  against the v1.5.0 source: README highlights renamed to v1.5.0
  with a JWT auth bullet, JWT row added to the documentation index,
  `docs/configuration.md` gained a complete JWT environment-variable
  reference (`QLIK_JWT_TOKEN`, `QLIK_JWT_USER_ID_CLAIM`,
  `QLIK_JWT_USER_DIR_CLAIM`, `QLIK_JWT_SESSION_COOKIE`,
  `QLIK_JWT_SESSION_TTL`), `docs/installation.md` gained a cert/JWT
  branching note, `docs/architecture.md` documents the new
  `JwtSession` component and `tools/qlik_jwt_admin.py` admin CLI,
  `docs/troubleshooting.md` gained a JWT-authentication problems
  section that cross-links into `docs/AUTH_JWT.md`, and
  `docs/AUTH_JWT.md` gained a `Related` index. `COMMANDS.md` was
  reduced to a true one-page cheatsheet pointing at the deeper
  docs in `docs/`.

### Fixed
- **Release hygiene.** `qlik_sense_mcp_server/__init__.py` and
  `.bumpversion.cfg` were not bumped during the 1.5.0 release and
  still reported `1.4.1`. Both are brought back in sync with
  `pyproject.toml` as part of this release.

## [1.5.0] - 2026-04-24

### Added
- **JWT authentication mode** via a Qlik Sense JWT virtual proxy. The
  admin signs a long-lived token per analyst on a private machine; the
  analyst puts `QLIK_SERVER_URL` and `QLIK_JWT_TOKEN` into their
  `mcp.json` and nothing else. No client certificates, no private keys,
  no service account on the analyst side — identity travels in the JWT
  payload and Qlik applies that user's normal security rules, stream
  membership and Section Access. Mode switches automatically when
  `QLIK_JWT_TOKEN` is set in the environment.
- **`qlik_sense_mcp_server/jwt_session.py`** — lazy, thread-safe holder
  of the bootstrapped Qlik session material (session cookie plus
  `qlik-csrf-token`), with a conservative 25-minute TTL (override via
  `QLIK_JWT_SESSION_TTL`) and transparent re-fetching on 401/403.
- **`tools/qlik_jwt_admin.py`** — admin CLI with two commands:
  `init-keys` generates an RSA 2048 keypair plus self-signed X.509
  certificate for pasting into the QMC JWT virtual proxy;
  `issue-token` signs an RS256 JWT for a single analyst. Token
  lifetime defaults to 90 days; bearer JWTs have no individual
  revocation path, so the default deliberately prefers rotation
  discipline over long-lived convenience.
- **`docs/AUTH_JWT.md`** — complete admin + analyst guide covering key
  generation, QMC virtual proxy configuration (with a multi-node
  warning about linking to the Central Proxy), token issuance,
  revocation strategy, operational troubleshooting and the exact
  two-phase bootstrap the MCP performs under the hood.

### Fixed
- **Engine WebSocket works on Qlik November 2024+.** Under CSWSH
  protection the anti-CSRF token must be present as a URL query
  parameter (`?qlik-csrf-token=<value>`) on the WS upgrade, not just
  as an HTTP header. Without this the upgrade is rejected with 403.
  The Engine client now appends the CSRF token to the URL after the
  JWT session bootstrap and additionally sends it as a header for
  forward/backward compatibility.
- **Engine WebSocket self-heals on stale JWT session.** A 401/403 on
  the WS handshake triggers one re-bootstrap of the JwtSession and a
  retry of the same endpoint, symmetric to the existing QRS 401
  retry path.
- **URL parsing preserves non-standard ports.** `engine_api.connect()`
  now builds WSS URLs and the `Origin` header from the full `netloc`
  of `QLIK_SERVER_URL` instead of the bare hostname, so deployments on
  ports like 8443 work without regression. The `Origin` scheme is also
  derived from the configured URL rather than hardcoded to `https`.

### Changed
- **`QlikSenseConfig.validate_runtime()`** is now the single entry
  point for runtime validation. It rejects `QLIK_SERVER_URL` without a
  scheme, rejects schemes other than `http`/`https`, and warns on
  multi-segment virtual proxy prefixes (Qlik VPs are single-segment).
- **`QlikRepositoryAPI.__init__`** raises `QlikConnectionError` up
  front when `auth_mode == jwt` but no `JwtSession` was passed,
  instead of failing with an obscure 401 on the first request.
- **`qlik_jwt_admin.py issue-token`** warns on stderr when invoked on
  an interactive TTY — the token ends up in shell scrollback and
  must be treated as a password. It also warns when
  `--user-id-claim` or `--user-dir-claim` deviate from the
  documented defaults, since silent claim-name mismatches with the
  QMC VP configuration are the number-one cause of rejected tokens.

## [1.4.1] - 2026-04-07

### Added
- **`engine_get_field_range` MCP tool** — lightning-fast bounds query for
  a single field (`Count(DISTINCT)` + `Min` + `Max`) via a measures-only
  hypercube. Runs in seconds on any table size, regardless of row count.
  Prefer this over `get_app_field_statistics` for "what's the loaded
  period" / "what's the cardinality" questions.
- **`light` parameter on `get_app_field_statistics`** (default `True`).
  Light mode skips `Sum`/`Avg`/`Median`/`Mode`/`Stdev` — these are
  meaningless on date/text fields and extremely slow on big fact tables.
  Pass `full=true` only on small numeric fields.
- **`get_app_details.warnings` array** that flags huge fact tables
  (>500M rows / >100M rows), high-cardinality fields (>1M distinct
  values) and date-typed fields, each with concrete instructions about
  the right tool and pattern to use.
- **Hypercube query estimator hints**: rejection responses for
  `engine_create_hypercube` now carry `error_category` (`limit_exceeded`,
  `cell_cap_exceeded`, `socket_timeout`, `engine_api_error`,
  `connection_error`), `failed_step`, `failed_stage`, `elapsed_seconds`
  and a `hint` pointing at set-analysis / top-N / slice-by-category
  patterns.
- **`tool_call_seconds`** is injected as the first key of every MCP tool
  response (millisecond precision wall-clock time of the call). On
  exception the same envelope carries `error_type` and `tool` so the
  caller can attribute failures.
- **`docs/` folder** with seven topical pages: installation,
  configuration, usage, tools, architecture, development,
  troubleshooting. README is now a short landing page that links into
  `docs/`.
- **Disclaimer** in `README.md` and `LICENSE`: this project is an
  independent community integration, not affiliated with Qlik. All
  protocol information used was obtained from publicly available
  sources (help.qlik.com, qlik.dev, Qlik Community).

### Changed
- **Hard hypercube limits enforced server-side, before any RPC**:
  `engine_create_hypercube` now rejects requests with `max_rows > 5000`
  or `columns * max_rows > 9900` (Qlik Engine itself caps a single
  `NxPage` at 10000 cells with error `7009 calc-pages-too-large`). The
  rejection happens in milliseconds with a structured error and a hint
  — there is no auto-pagination. The LLM must design narrower queries
  via set analysis, top-N or slice-by-category.
- **`QLIK_WS_TIMEOUT` default raised from `8.0` s to `180.0` s**, now
  uniformly applied to BOTH the WebSocket handshake AND every Engine
  API call (`OpenDoc`, hypercube creation, `GetLayout`, field
  statistics).
- **Per-app WebSocket endpoint** is tried first when an `app_id` is
  known. `connect(app_id=...)` builds
  `wss://<host>:<engine_port>/app/<url-encoded-app-id>` as the
  preferred connection URL, falling back to the global
  `/app/engineData` endpoint.
- **All MCP tool docstrings rewritten** in English with generic
  placeholders (`<DimA>`, `<MetricX>`, `<val>` etc.). The
  `engine_create_hypercube` docstring documents the two hard rules
  explicitly: ALWAYS use set analysis (never `If()` inside an
  aggregate), and NEVER put expressions in `qFieldDefs` (per-row
  evaluation, not cached, no symbol-table use).
- **`get_app_field` falls back to a one-dimension hypercube** when the
  underlying `ListObject` returns an empty result. The response then
  includes `fallback_used: "hypercube"` and, on total failure, a
  `warning` field describing the next step.

### Fixed
- **Strict id-matching in `send_request`**. Every received WebSocket
  frame is parsed and only the frame whose `id` matches our `req_id` is
  treated as the answer. Notifications (`OnConnected`, `OnAuthenticated`,
  `OnSessionTimedOut`) are skipped at DEBUG. Late replies from a
  previously timed-out request are skipped at WARNING. Without this
  fix, a single timed-out hypercube call would leave stale data in the
  recv buffer that the next call consumed as its own response,
  cascading failures for the rest of the session.
- **`_kill_socket()` on any failure path**. Timeouts, parse errors and
  unexpected exceptions all force-close the WebSocket and invalidate
  the cached app handle. The next call opens a fresh connection
  instead of reusing a zombie socket.
- **`tests/test_server.py`** rewritten for the FastMCP architecture
  (the old `QlikSenseMCPServer` class no longer exists). Covers
  `_err`/`_ok`, version pin, 24-tool registration, core tool presence,
  and `_timed` decorator behaviour including exception handling. The
  full suite now passes again (97 tests).
- **`tests/test_config.py`** updated for the new `DEFAULT_WS_TIMEOUT`
  default value.

### Documentation
- README cut from ~800 to ~100 lines. The full content lives in `docs/`
  with one topic per file.
- All facts re-verified against current upstream sources: MCP spec
  2025-03-26 (Streamable HTTP transport), qlik.dev, help.qlik.com
  November 2025 (Engine error 7009, hypercube cell cap, standard QSE
  ports).
- All approximate numbers (`~`, `+`, "around", "about") removed from
  user-facing text.
- Copyright years updated to `2025-2026`.

## [1.4.0] - 2026-04-06

### Added
- **HTTP streaming transport**: server now runs with `streamable-http` MCP
  transport by default on `http://127.0.0.1:8000/mcp`. Legacy `stdio`
  transport remains available via the `--stdio` flag.
- **Cached Engine API connections**: `QlikEngineAPI` now keeps a single
  long-lived WebSocket and reuses the opened app handle across tool calls
  via the new `ensure_app(app_id)` entry point. Switching to another
  `app_id` closes the old app and opens the new one; dropped connections
  are transparently re-established (ping-based liveness check). This
  dramatically reduces load on the Qlik engine — no more
  connect/open/close on every single tool call.
- **`QLIK_WS_TIMEOUT` default raised to `180.0s`** and now uniformly
  applied to BOTH the WebSocket handshake AND every Engine API call
  (`OpenDoc`, hypercube creation, `GetLayout`, field statistics). A
  single knob is enough for the vast majority of setups; increase it
  further for very heavy hypercubes on large apps.

### Changed
- Major refactor of `server.py`: all Engine-based tools now use
  `engine_api.ensure_app(...)` instead of the previous
  `connect()` / `open_doc()` / ... / `disconnect()` boilerplate. Each tool
  is now a single-entry call that benefits from connection caching.
- `QlikEngineAPI.send_request()` accepts an optional per-request
  `timeout` argument and restores the previous socket timeout in a
  `finally` block.
- `open_doc` / `open_doc_safe` / `create_hypercube` /
  `get_field_statistics` now use `ws_operation_timeout` for their
  underlying `recv()` calls.

### Fixed
- `Connection timed out` errors on hypercube creation for large apps:
  the hypercube timeout was previously bound to the short
  `QLIK_WS_TIMEOUT` connection timeout. It is now controlled
  independently via `QLIK_WS_OPERATION_TIMEOUT`.

### Documentation
- Updated `README.md`: added "HTTP streaming mode" note, described
  connection caching and the two-timeouts model in the Architecture
  section, documented `QLIK_WS_OPERATION_TIMEOUT` in the environment
  variables reference.
- Updated `.env.example` and MCP configuration snippet with
  `QLIK_WS_OPERATION_TIMEOUT`.

## [1.3.4] - 2025-10-10

### Added
- Enhanced hypercube creation with explicit sorting options for dimensions and measures
- Support for custom sorting expressions in dimensions
- Option to create hypercubes without dimensions (measures-only)
- Improved sorting defaults: dimensions sort by ASCII ascending, measures sort by numeric descending

### Changed
- New configuration parameter `QLIK_HTTP_PORT` for metadata requests to `/api/v1/apps/{id}/data/metadata` endpoint
- Dynamic X-Qlik-Xrfkey generation for enhanced security (16 random alphanumeric characters)
- Utility function `generate_xrfkey()` for secure key generation

### Changed
- Replaced all static "0123456789abcdef" XSRF keys with dynamic generation
- Updated help output to use stderr instead of print to maintain MCP protocol compatibility
- Enhanced logging system throughout the codebase - replaced print statements with proper logging

### Removed
- Removed `size_bytes` parameter from `get_app_details` tool output (non-functional parameter)
- Eliminated all print() statements in favor of logging for MCP server compliance

### Documentation
- Updated README.md with new QLIK_HTTP_PORT configuration parameter
- Updated .env.example and mcp.json.example with QLIK_HTTP_PORT settings
- Enhanced configuration documentation with detailed parameter descriptions

## [1.3.2] - 2025-10-06

### Fixed
- Fixed published filter in get_apps function to properly handle filtering logic
- Removed numeric_value field from user variables and switched to text_value for more accurate data representation

### Changed
- Improved code readability by removing verbose output of user variable lists
- Enhanced user variable handling with better filtering for script-created variables
- Optimized variable data processing for improved performance and accuracy

## [1.3.1] - 2025-09-08

### Fixed
- Proxy API metadata request now respects `verify_ssl` configuration. Replaced conditional CA path logic with `self.config.verify_ssl` in `server.py` to ensure proper TLS verification behavior.

## [1.3.0] - 2025-09-08

### Added
- get_app_sheets: list sheets with titles and descriptions (Engine API)
- get_app_sheet_objects: list objects on a specific sheet with id, type, description (Engine API)
- get_app_object: retrieve specific object layout via GetObject + GetLayout (Engine API)

### Changed
- Upgraded MCP dependency to `mcp>=1.1.0`
- Improved logging configuration with LOG_LEVEL and structured stderr output
- Tunable Engine WebSocket behavior via environment variables: `QLIK_WS_TIMEOUT`, `QLIK_WS_RETRIES`
- Enhanced field statistics calculation and debug information in server responses
- README updated to include new tools and examples; MCP configuration extended

### Fixed
- More robust app open logic (`open_doc_safe`) and better error messages for Engine operations
- Safer cleanup for temporary session objects during Engine operations

### Documentation
- Updated `README.md` with API Reference for new tools and optional environment variables
- Updated `mcp.json.example` autoApprove list to include new tools

[1.4.1]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.4...v1.4.0
[1.3.4]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.3...v1.3.4
[1.3.2]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.2.0...v1.3.0
