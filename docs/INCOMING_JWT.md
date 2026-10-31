# Incoming Qlik JWT gateway (experimental)

The gateway accepts an `Authorization: Bearer` Qlik JWT on each MCP HTTP
request. A new token starts a dedicated Qlik MCP worker. That worker first
validates the token with Qlik's JWT virtual proxy, then serves MCP over a
private Unix-domain socket. Its Qlik clients, WebSocket, cookies and all
original process-wide caches are isolated from other tokens by the process
boundary. Requests for a cached token reuse its worker.

This implementation is experimental: it has not been validated against the
supported MCP SDK versions or a live Qlik deployment. Do not use it in
production until those tests and a security review pass.

## Start

Requirements: POSIX (Linux/macOS), Python 3.12 and the project's installed
dependencies, a Qlik JWT virtual proxy, and an MCP client capable of sending
the same bearer token on every GET, POST and DELETE request. The gateway
binds `127.0.0.1` only; use an appropriately secured reverse proxy for remote
access. A reverse proxy must preserve MCP headers and disable buffering for
streaming responses. Do not log Authorization headers.

```sh
export QLIK_SERVER_URL=https://qlik.example.com/jwt
export MCP_PORT=8000
python -m qlik_sense_mcp_server.incoming_gateway
```

TLS behavior toward Qlik is unchanged: `QLIK_VERIFY_SSL` remains optional,
with the original default. For production, enable it and configure a trusted
CA where appropriate. TLS to the gateway is the reverse proxy's responsibility.
No Qlik credential should be placed in this command or in a prompt.

## Behavior and limitations

- Every distinct JWT is checked against Qlik on a cache miss. No session
  cookies are restored from disk. Each worker disables proxy-wide logout and
  task-administration tools.
- Each request must contain exactly one bearer token with an `exp` claim.
  Expired tokens are rejected locally even while a cached session exists.
- The session cache is per process; restarting the gateway or rotating a JWT
  creates a new Qlik session. Multiple gateway replicas do not share cache.
  Qlik user session limits and memory consumption must be considered.
- Eviction closes the worker locally, but does not end the proxy-wide Qlik
  sessions. Qlik sessions can remain until the proxy's inactivity timeout.
- Default limits: `QLIK_INCOMING_MAX_CONTEXTS=128`,
  `QLIK_INCOMING_IDLE_SECONDS=900`, `QLIK_INCOMING_MAX_AGE_SECONDS=1500`,
  `QLIK_INCOMING_START_TIMEOUT=120`. Changes in Qlik authorization or token
  revocation may not be detected until a new Qlik bootstrap or session error.
- Requests sharing one JWT share one worker. Multiple MCP conversations with
  the same token can therefore share Qlik Engine selection state. Avoid
  concurrent conversations for the same token until this is tested or
  isolated further.
- The original stdio, certificate, form and environment-JWT server entry
  points are unchanged. The gateway is a separate entry point.
- Do not assume a JWT accepted by Qlik is appropriate for another service:
  review issuer, audience and token use policy with the Qlik administrator.

Before release, test missing/invalid/expired JWTs, two users with different
Section Access, concurrent requests, reconnection, cancellation, token
rotation, same-token conversations, MCP SDK 1.x and 2.x, proxy streaming,
and failover against the real Qlik system.
