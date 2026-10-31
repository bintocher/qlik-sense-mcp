# Incoming Qlik JWT mode

## Unified startup

Install this fork, then always use the same command:

```sh
qlik-sense-mcp-server
```

`QLIK_MCP_MODE` selects how that command starts:

| Value | Behavior |
| --- | --- |
| `legacy` (default) | Existing certificate, environment-JWT or form authentication, chosen as before. `--stdio` remains supported. |
| `incoming-jwt` | Streamable HTTP gateway; every request supplies a Qlik-compatible bearer JWT. No service-account fallback. POSIX Unix sockets are required. |

The selector is read after `.env` loading, before importing the original
server, so incoming mode does not initialize a global service identity.
Unknown modes and `incoming-jwt` with `--stdio` fail closed.
`python -m qlik_sense_mcp_server.cli` is the equivalent module invocation.
The original `python -m qlik_sense_mcp_server.server` remains a legacy entry
point for backward compatibility and does not select the gateway.

Minimal incoming-mode `.env`:

```dotenv
QLIK_MCP_MODE=incoming-jwt
QLIK_SERVER_URL=https://qlik.example.com/jwt
MCP_PORT=8000
```

Send `Authorization: Bearer <Qlik JWT>` on every MCP GET, POST and DELETE.
Never place tokens in prompts or tool arguments. The JWT must be accepted
by the configured Qlik virtual proxy and contain a finite numeric `exp`.
The server does not issue tokens and does not require the JWT signing key.

For Docker and an external reverse proxy, use [DOCKER.md](DOCKER.md).
The image sets `MCP_HOST=0.0.0.0` inside the container; bare-host startup
continues to default to `127.0.0.1`.

## Authentication and isolation

- Each exact JWT selects a private worker process. A cache miss validates
  that exact JWT with Qlik before exposing the worker. Disk-cookie restore
  is disabled; different JWTs never share clients, Engine sockets or caches.
- Cache hits reuse Qlik's session cookie/CSRF material, without calling Qlik
  merely to revalidate the JWT. Local JWT expiry is checked for every
  request and at tool execution. Streams stop when the JWT expires.
- Qlik session invalidation retires the old session and clears all dependent
  clients, Engine state and the app-details cache. A failed call is not
  automatically replayed. The next tool call authenticates afresh before
  it can read data or a cache. Failed reauthentication leaves clients absent.
- Tool calls for the same JWT are serialized through a worker-level guard,
  including Repository calls and cached reads. Same-token conversations
  intentionally share Qlik selection state; conversation isolation is not
  provided in this version.
- Reload-task administration, proxy-wide logout, disk session persistence
  and shared reply log files are disabled in incoming workers. Closing a
  worker does not log out another client of that Qlik user. Qlik proxy
  sessions may remain until their inactivity timeout.

## Limits and HTTP behavior

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `MCP_HOST` | `127.0.0.1` | Public gateway bind address; Docker sets `0.0.0.0`. Does not change private worker sockets or TLS settings. |
| `MCP_PORT` | `8000` | Gateway HTTP port. |
| `QLIK_INCOMING_MAX_CONTEXTS` | `128` | Maximum cached workers and reserved startups. A new token cannot evict a healthy authenticated worker; capacity returns 503. |
| `QLIK_INCOMING_IDLE_SECONDS` | `900` | Idle worker lifetime. Active streams are not idle. Cleanup runs every 30 seconds. |
| `QLIK_INCOMING_MAX_AGE_SECONDS` | `0` | Optional absolute cache age; zero disables forced age reauthentication. |
| `QLIK_INCOMING_START_TIMEOUT` | `120` | Worker startup deadline. |
| `QLIK_INCOMING_MAX_STARTS` | `4` | Maximum concurrent unauthenticated worker startups. |
| `QLIK_INCOMING_STARTS_PER_MINUTE` | `30` | Global startup-rate limit; affects new contexts, not cache hits. |
| `QLIK_INCOMING_MAX_BODY_BYTES` | `4194304` | Maximum buffered request body, checked incrementally including chunked uploads. |
| `QLIK_INCOMING_BODY_TIMEOUT` | `30` | Upload deadline, additionally bounded by JWT expiry. |

Rejected startup outcomes have a bounded 10-second negative cache. It stores
only keyed token fingerprints and fixed failure categories. Such a cache
entry never authorizes a request. Rate limits are per gateway process;
additional network-level connection/rate controls belong at the reverse proxy.

Credentials rejected by Qlik return 401. Startup/configuration/upstream
availability failures return 503, startup admission returns 429, oversized
bodies return 413, and slow uploads return 408. Upstream request transport
failures return 502. Streams that fail after headers have been sent are
closed, their worker is invalidated, and resources are released exactly once.
Gateway logs record fixed diagnostic categories, never worker exception
bodies, JWTs, cookies or CSRF tokens.

Proxying removes hop-by-hop headers and Connection-nominated headers.
Nominating protected MCP/auth headers is rejected. MCP session/protocol
headers are retained, external credentials/cookies are not forwarded to the
private worker, and HTTP framing is regenerated. Responses are `no-store`.

## Deployment and compatibility

The gateway defaults to `127.0.0.1`; expose it through an appropriately secured
reverse proxy when remote access is needed. Containers need `MCP_HOST=0.0.0.0`
inside their network namespace, with published ports restricted separately.
Preserve MCP headers, disable stream buffering, and do not log authorization
values. Each worker listens on a random Unix socket in a private directory,
not a public TCP port. Forwarded headers are not trusted as user identity.

**TLS verification toward Qlik is unchanged and optional.** Existing
`QLIK_VERIFY_SSL` and `QLIK_CA_CERT_PATH` behavior and defaults are preserved.
Gateway TLS termination is the reverse proxy's responsibility.

Eviction, process restart, token rotation or a worker crash loses that
worker's MCP sessions. The client must reinitialize its MCP session; an old
session ID is not transferable to another token/worker. Multiple replicas
have separate caches; deploy a single process or route connections consistently.
Qlik per-user session limits still apply, especially when rotating tokens.
Revocation/permission changes are detected when Qlik rejects a session or
on the next bootstrap, not necessarily immediately on a cache hit.

## Verification status

Policy, startup-selection and coordinated-invalidation tests can run with
`python -m unittest` and mocked Qlik clients. HTTP streaming tests require
Starlette; SDK integration tests require the installed MCP SDK. Both SDK
lines, real subprocess startup, reverse-proxy disconnects and two live Qlik
identities with different Section Access must pass before production rollout.
Do not treat passing isolated tests as a live-Qlik certification.
