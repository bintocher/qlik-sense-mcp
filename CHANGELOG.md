# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic Versioning.

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
