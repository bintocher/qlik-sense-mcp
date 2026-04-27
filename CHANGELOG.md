# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic Versioning.

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
