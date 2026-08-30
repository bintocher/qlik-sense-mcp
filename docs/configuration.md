# Configuration

All configuration is done through environment variables. The server reads
them via [python-dotenv](https://pypi.org/project/python-dotenv/) on
startup, so you can put them in a `.env` file next to where you run the
server, or pass them via your MCP client's `env` block.

## Required variables

Note: `QLIK_USER_DIRECTORY` and `QLIK_USER_ID` are NOT used in JWT mode —
the user identity travels in the JWT payload. See
[JWT authentication](#jwt-authentication) below. In form mode they carry
the login credentials instead of an `X-Qlik-User` header — see
[Form (login/password) authentication](#form-loginpassword-authentication).

| Variable | Description |
|----------|-------------|
| `QLIK_SERVER_URL` | Qlik Sense server URL, including scheme. Example: `https://qlik.company.com`. In JWT mode the virtual proxy prefix is required as URL path, e.g. `https://qlik.company.com/jwt`; in form mode it is optional, since a form-based auth module can sit on the central proxy. The scheme is honoured everywhere, including the Engine WebSocket: `https` connects with `wss://`, `http` with `ws://`. Use `http` only on a trusted network — the token and session cookie are then unencrypted. |
| `QLIK_USER_DIRECTORY` | User directory used for authentication (e.g. `COMPANY`). Cert mode: sent as `X-Qlik-User`. Form mode: the domain part of the login (optional — see below). Unused in JWT mode. |
| `QLIK_USER_ID` | User ID used for authentication. Cert mode: sent as `X-Qlik-User`. Form mode: the username submitted to the login form (required). Unused in JWT mode. |

## Certificate configuration

This section applies to **cert mode only**. For the JWT alternative see
[JWT authentication](#jwt-authentication), for the login/password
alternative see [Form authentication](#form-loginpassword-authentication)
below, and [AUTH_JWT.md](AUTH_JWT.md) for the full QMC virtual proxy setup.

Required for production. `QLIK_CA_CERT_PATH` does not switch TLS
verification on or off - that is `QLIK_VERIFY_SSL` alone (off by default).
With verification on and no CA path set, the system trust store decides,
which a self-signed Qlik certificate will not pass; point
`QLIK_CA_CERT_PATH` at the signing CA to have it trusted.

| Variable | Description |
|----------|-------------|
| `QLIK_CLIENT_CERT_PATH` | Absolute path to the client certificate file (`.pem`) |
| `QLIK_CLIENT_KEY_PATH` | Absolute path to the client private key file (`.pem`) |
| `QLIK_CA_CERT_PATH` | Absolute path to the CA certificate file (`.pem`) |

## JWT authentication

JWT mode is selected automatically when `QLIK_JWT_TOKEN` is set. In this
mode the MCP talks to a Qlik **JWT virtual proxy** over standard HTTPS/WSS
(port 443) — no client certificates needed on the analyst's machine. See
[AUTH_JWT.md](AUTH_JWT.md) for the full setup, including admin-side QMC
virtual proxy configuration and key/token issuance.

| Variable | Default | Description |
|----------|---------|-------------|
| `QLIK_JWT_TOKEN` | unset | Signed JWT bearer token. Setting this switches the server to JWT mode. |

In JWT mode `QLIK_SERVER_URL` MUST include the virtual proxy prefix as
URL path (e.g. `https://qlik.company.com/jwt`). `QLIK_USER_DIRECTORY`
and `QLIK_USER_ID` are ignored — Qlik extracts the identity from the
JWT payload itself.

## Form (login/password) authentication

Form mode is selected automatically when `QLIK_PASSWORD` is set (and
`QLIK_JWT_TOKEN` is not — JWT takes priority). The MCP logs into a virtual
proxy's "Form based" login page the way a browser would — most commonly
the in-box Windows credentials login page — and then reuses the resulting
session cookie exactly like JWT mode does after its own bootstrap. See
[AUTH_FORM.md](AUTH_FORM.md) for how the login flow works and how to
adapt it to a non-default login page.

| Variable | Default | Description |
|----------|---------|-------------|
| `QLIK_PASSWORD` | unset | Password submitted to the login form. Setting this switches the server to form mode. |
| `QLIK_FORM_LOGIN_PATH` | empty (the virtual proxy root) | Where to start the login redirect chain. The login page itself carries a per-visit `targetId` that cannot be guessed, so the default is a page Qlik redirects FROM, not the login URL. Override only if the login flow starts somewhere other than the proxy root. |
| `QLIK_FORM_USERNAME_FIELD` | auto-detected | Force the login form's username field name, if auto-detection picks the wrong `<input>`. |
| `QLIK_FORM_PASSWORD_FIELD` | auto-detected | Force the login form's password field name (auto-detection is normally reliable here — it matches `type="password"`). |

Unlike JWT mode, `QLIK_SERVER_URL` does not need a virtual proxy prefix —
a form-based auth module can be attached to the central proxy as well as
to a named one. The username submitted is
`QLIK_USER_DIRECTORY\QLIK_USER_ID` when a directory is set, otherwise just
`QLIK_USER_ID`.

## Network configuration

Defaults match the standard
[Qlik Sense Enterprise port allocation](https://help.qlik.com/en-US/sense-admin/Subsystems/DeployAdministerQSE/Content/Sense_DeployAdminister/QSEoW/Deploy_QSEoW/Ports.htm):

| Variable | Default | Description |
|----------|---------|-------------|
| `QLIK_REPOSITORY_PORT` | `4242` | Repository (QRS) API port |
| `QLIK_ENGINE_PORT` | `4747` | Engine API WebSocket port |

Both ports apply to certificate mode, which talks to Qlik directly. JWT
and form mode go through the virtual proxy on the port in
`QLIK_SERVER_URL`, so neither variable has any effect there.

## SSL

| Variable | Default | Description |
|----------|---------|-------------|
| `QLIK_VERIFY_SSL` | `false` | Verify TLS certificates. Off by default: Qlik Sense Enterprise serves its own self-signed certificate, so a correct installation fails verification. Set to `true` once Qlik presents a certificate your machine trusts (and point `QLIK_CA_CERT_PATH` at the CA if it is a private one). |

## Which tools are registered

| Variable | Default | Description |
|----------|---------|-------------|
| `QLIK_TASK_TOOLS` | `true` in certificate mode, `false` in JWT/form mode | Register the 14 reload-task tools. They need QRS repository-admin rights — a QMC role, not a property of the authentication method — so they default to on in certificate mode (which normally runs as a trusted admin identity) and off in JWT/form mode (which normally authenticates an ordinary analyst). Set to `false` to leave them out of certificate mode too, for an identity that only reads data. Set to `true` to turn them on in JWT or form mode as well — do this only once you have verified the identity behind that JWT/password does hold QRS admin rights, since every call will otherwise fail with 403. |

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Root log level: one of `DEBUG`, `INFO`, `WARNING`, `ERROR`. `DEBUG` is very verbose - it logs every Engine API frame, which is what you need to troubleshoot hypercube performance. |
| `QLIK_LOG_FILE` | unset | Also write the log to this file. Over stdio there is nowhere else for it to go - stdout carries the protocol and MCP clients generally swallow the server's stderr. The log records every tool call with its arguments, which is what you need to see what a model actually asked for. |
| `QLIK_LOG_REPLIES` | `false` | Set to `true` to log what a tool returned as well as the call it answered, truncated to 4000 characters. This is the tool's own payload, logged before the envelope adds `tool_call_seconds`, `tool` and `request`; a call that raised is reported by the exception log instead and writes no reply line. Off by default: replies carry data, and data does not belong in a log unless someone asked for it. |

## Timeouts

| Variable | Default | Description |
|----------|---------|-------------|
| `QLIK_WS_TIMEOUT` | `180.0` | WebSocket timeout in seconds. Applied to both the WS handshake and every Engine API call (`OpenDoc`, hypercube creation, `GetLayout`, field statistics). Increase this value if hypercube operations on large apps time out with `WebSocket recv() timed out`. |

There are no retry-count or HTTP-timeout variables: the connection cache
decides on its own when to re-check a socket (it trusts one that answered
less than 30 seconds ago, and probes an older one with a bounded
`EngineVersion` request), and those thresholds are constants rather than
configuration.

## Transport

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_PORT` | `8000` | Port the Streamable HTTP transport listens on. The bind address is always `127.0.0.1`, so the server is reachable from the local machine only. Ignored in stdio mode (`--stdio`). |

## Sample `.env`

See [`.env.example`](../.env.example) at the repository root for a copy-pasteable template.

## MCP client configuration

The blocks below are complete examples of registering the server in an
MCP client config. They use the `command` form, in which the client
spawns the server itself and talks to it over stdio - hence the `--stdio`
argument. Without it the process starts in Streamable HTTP mode and the
client never sees a reply.

To use the default Streamable HTTP transport instead, start the server
yourself (`uvx qlik-sense-mcp-server`, with the `QLIK_*` variables in the
environment or in a `.env` file beside it) and point the client at the
running process rather than spawning one:

```jsonc
{
  "mcpServers": {
    "qlik-sense": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

The exact key for an HTTP server varies by client (`type`, `transport`,
or a bare `url`); check your client's documentation.

### Cert mode

```jsonc
{
  "mcpServers": {
    "qlik-sense": {
      "command": "uvx",
      "args": ["qlik-sense-mcp-server", "--stdio"],
      "env": {
        "QLIK_SERVER_URL": "https://qlik.company.com",
        "QLIK_USER_DIRECTORY": "COMPANY",
        "QLIK_USER_ID": "your-username",
        "QLIK_CLIENT_CERT_PATH": "/etc/qlik/certs/client.pem",
        "QLIK_CLIENT_KEY_PATH": "/etc/qlik/certs/client_key.pem",
        "QLIK_CA_CERT_PATH": "/etc/qlik/certs/root.pem",
        "QLIK_REPOSITORY_PORT": "4242",
        "QLIK_ENGINE_PORT": "4747",
        "QLIK_VERIFY_SSL": "false",
        "QLIK_WS_TIMEOUT": "180.0",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

A copy is also kept in [`mcp.json.example`](../mcp.json.example).

### JWT mode

Minimal config — two variables are enough. See
[AUTH_JWT.md](AUTH_JWT.md) for the full version including optional
overrides.

```jsonc
{
  "mcpServers": {
    "qlik-sense": {
      "command": "uvx",
      "args": ["qlik-sense-mcp-server", "--stdio"],
      "env": {
        "QLIK_SERVER_URL": "https://qlik.company.com/jwt",
        "QLIK_JWT_TOKEN": "eyJhbGciOiJSUzI1NiJ9...."
      }
    }
  }
}
```

### Form (login/password) mode

See [AUTH_FORM.md](AUTH_FORM.md) for how the login flow works and the
overrides available for non-default login pages.

```jsonc
{
  "mcpServers": {
    "qlik-sense": {
      "command": "uvx",
      "args": ["qlik-sense-mcp-server", "--stdio"],
      "env": {
        "QLIK_SERVER_URL": "https://qlik.company.com/forms",
        "QLIK_USER_DIRECTORY": "COMPANY",
        "QLIK_USER_ID": "your-username",
        "QLIK_PASSWORD": "your-password"
      }
    }
  }
}
```
