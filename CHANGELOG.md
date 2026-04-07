# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic Versioning.

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

[1.4.0]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.4...v1.4.0
[1.3.4]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.3...v1.3.4
[1.3.2]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.2.0...v1.3.0
