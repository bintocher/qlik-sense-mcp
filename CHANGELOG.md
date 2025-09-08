# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic Versioning.

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

[1.3.1]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/bintocher/qlik-sense-mcp/compare/v1.2.0...v1.3.0
