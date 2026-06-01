# Qlik Sense MCP Server

[![PyPI version](https://badge.fury.io/py/qlik-sense-mcp-server.svg)](https://pypi.org/project/qlik-sense-mcp-server/)
[![PyPI downloads](https://img.shields.io/pypi/dm/qlik-sense-mcp-server)](https://pypi.org/project/qlik-sense-mcp-server/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python versions](https://img.shields.io/pypi/pyversions/qlik-sense-mcp-server)](https://pypi.org/project/qlik-sense-mcp-server/)

[Model Context Protocol](https://modelcontextprotocol.io/) server for
Qlik Sense Enterprise. Exposes Qlik's Repository (HTTP) and Engine
(WebSocket) APIs as **24 MCP tools** so an LLM client can discover apps,
inspect data models, build hypercubes, and manage reload tasks through
a single uniform interface.

## What's in the box

| Area | Tools | Used for |
|------|-------|----------|
| Repository (apps & metadata) | `get_about`, `get_apps`, `get_app_details` | Discover apps, list tables and fields with cardinalities |
| Engine (data & script)       | `get_app_script`, `get_app_variables`, `get_app_sheets`, `get_app_sheet_objects`, `get_app_object`, `get_app_field`, `engine_get_field_range`, `get_app_field_statistics`, `engine_create_hypercube` | Read load script, list visualizations, query field values, build hypercubes |
| Reload tasks                 | `get_tasks`, `get_task_details`, `get_task_dependencies`, `get_task_schedule`, `get_task_executions`, `get_task_script_log`, `get_failed_tasks_with_logs`, `start_task`, `create_task`, `update_task`, `delete_task`, `create_task_schedule` | Inspect, trigger and manage reload tasks |

Full list with descriptions: [`docs/tools.md`](docs/tools.md).

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
| [`docs/tools.md`](docs/tools.md) | Inventory of all 24 tools, response/error envelope, error categories |
| [`docs/architecture.md`](docs/architecture.md) | Project layout, components, connection caching, strict id-matching, two-tier timeout |
| [`docs/development.md`](docs/development.md) | `make` targets, tests, versioning, how to add a new tool |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Common errors, hypercube planning failures, verbose logging, configuration self-test |
| [`CHANGELOG.md`](CHANGELOG.md) | Release notes |

## Key facts about the v1.5.0 line

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
