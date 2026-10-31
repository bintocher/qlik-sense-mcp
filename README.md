# Qlik Sense MCP Server

Community MCP integration for Qlik Sense Enterprise, exposing Repository
HTTP and Engine WebSocket APIs for app discovery, model inspection, queries
and optional reload-task administration. This fork adds incoming per-request
Qlik JWT authentication with isolated token workers.

## Install this fork

Use Python 3.12 or newer and install the checked-out feature branch, rather
than assuming the upstream PyPI package contains these changes:

```sh
python -m pip install -e .
```

For development tests:

```sh
python -m pip install -e '.[dev]'
python -m pytest tests -m 'not e2e'
```

## Docker behind your reverse proxy

A non-root Docker image and hardened Compose service are included. On your
Docker host, set your Qlik JWT virtual-proxy URL in `.env`, then run:

```sh
docker compose config
docker compose up -d --build
```

Compose starts incoming-JWT mode and publishes port 8000 on host loopback
by default. A proxy on the same host uses `http://127.0.0.1:8000/mcp`.
For a proxy on another machine or in another container, change the network
route as described in [the Docker guide](docs/DOCKER.md). Do not expose an
unrestricted backend port or bake user JWTs into the image.

Docker image build/start has not been verified in the authoring environment.
The container still needs live Qlik and full SDK acceptance testing.

## One startup command, two routes

```sh
qlik-sense-mcp-server
```

Select the route in `.env` or the service environment:

| Setting | Route |
| --- | --- |
| `QLIK_MCP_MODE=legacy` (default) | Existing certificate, environment-JWT or form authentication. |
| `QLIK_MCP_MODE=incoming-jwt` | Each MCP HTTP request supplies its own Qlik JWT in the Authorization header. |

Incoming-mode example:

```dotenv
QLIK_MCP_MODE=incoming-jwt
QLIK_SERVER_URL=https://qlik.example.com/jwt
MCP_PORT=8000
```

Start the same command shown above. The endpoint is
`http://127.0.0.1:8000/mcp`; use an appropriately secured reverse proxy for
remote access. Linux/macOS POSIX Unix sockets are required in incoming mode.
The gateway does not use an environment service token as a fallback.
The Docker image sets `MCP_HOST=0.0.0.0` inside the container; bare-host
incoming-mode startup still defaults to `127.0.0.1`.

Legacy stdio still uses `qlik-sense-mcp-server --stdio`. Incoming mode is
HTTP-only and rejects `--stdio`. `python -m qlik_sense_mcp_server.cli` is an
equivalent unified module entry point. The original server module remains a
legacy entry point for compatibility.

**TLS behavior is unchanged:** Qlik certificate verification remains optional
through `QLIK_VERIFY_SSL` and `QLIK_CA_CERT_PATH`, with the original defaults.

See [.env.incoming.example](.env.incoming.example) and
[the incoming JWT guide](docs/INCOMING_JWT.md) for limits, failure behavior,
client reinitialization, deployment and verification requirements.

## Incoming JWT session semantics

- The first use of an exact JWT authenticates against Qlik's configured JWT
  virtual proxy and obtains a session cookie and CSRF token.
- Cache hits reuse that session without another JWT-authentication request.
  JWT expiration is enforced locally on requests, tool execution and streams.
- Different JWTs have separate worker processes, clients, WebSockets and
  caches. Same-token conversations intentionally share state.
- When Qlik invalidates a session, its dependent clients and caches are
  dropped. The failed tool is not replayed; the next call authenticates
  afresh. Failed reauthentication cannot access the old state.
- Worker startup, request-body size, upload time and cache capacity are
  bounded. Failure logs contain fixed categories, not credentials or raw
  authentication error bodies. Workers disable proxy-wide logout and disk
  session persistence.
- Incoming workers do not expose reload-task administration tools.

This fork's incoming mode needs the full SDK integration suite and live Qlik
acceptance tests before production rollout. Unit tests with mocked Qlik are
not evidence of live authentication or Section Access correctness.

## Tools

| Area | Examples |
| --- | --- |
| Repository | `get_about`, `get_apps`, `get_app_details` |
| Engine | `engine_query`, `engine_create_hypercube`, `get_app_field`, `get_app_field_statistics`, `get_app_sheets`, `get_app_sheet_objects`, `get_app_object`, `get_app_script`, `get_app_variables`, `search_app`, `engine_get_field_range` |
| Reload tasks (legacy, optional) | Task discovery, execution, schedules and administration. Requires the appropriate Qlik role. |

`engine_query` supports typed dimensions, metrics, filters and master
measures. `engine_create_hypercube` supports handwritten Qlik expressions.
Read the model with `get_app_details` before querying unfamiliar fields.

## Documentation

- [Docker and reverse proxies](docs/DOCKER.md)
- [Configuration](docs/configuration.md)
- [Incoming per-request JWT authentication](docs/INCOMING_JWT.md)
- [Legacy JWT virtual-proxy setup](docs/AUTH_JWT.md)
- [Form authentication](docs/AUTH_FORM.md)
- [Installation](docs/installation.md)
- [Tools](docs/tools.md)
- [Usage](docs/usage.md)
- [Architecture](docs/architecture.md)
- [Development](docs/development.md)
- [Troubleshooting](docs/troubleshooting.md)
- [LLM behavior](docs/llm-behaviour.md)
- [Changelog](CHANGELOG.md)

## Requirements and disclaimer

The MCP host needs network access to Qlik. Legacy certificate mode connects
to the Repository/Engine ports; JWT and form modes use the configured
virtual proxy. App access, licensing, security rules and Section Access
remain Qlik responsibilities.

This is an independent community integration, not affiliated with, endorsed
by, sponsored by or supported by Qlik Technologies Inc. or other Qlik
entities. Qlik-related names are trademarks of their respective owners.

## License

[MIT](LICENSE). Original project by Stanislav Chernov:
[bintocher/qlik-sense-mcp](https://github.com/bintocher/qlik-sense-mcp).
