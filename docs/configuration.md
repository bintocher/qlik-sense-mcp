# Configuration

All configuration is done through environment variables. The server reads
them via [python-dotenv](https://pypi.org/project/python-dotenv/) on
startup, so you can put them in a `.env` file next to where you run the
server, or pass them via your MCP client's `env` block.

## Required variables

| Variable | Description |
|----------|-------------|
| `QLIK_SERVER_URL` | Qlik Sense server URL, including scheme. Example: `https://qlik.company.com` |
| `QLIK_USER_DIRECTORY` | User directory used for authentication (e.g. `COMPANY`) |
| `QLIK_USER_ID` | User ID used for authentication |

## Certificate configuration

Required for production. If `QLIK_CA_CERT_PATH` is not set, SSL
verification is disabled automatically.

| Variable | Description |
|----------|-------------|
| `QLIK_CLIENT_CERT_PATH` | Absolute path to the client certificate file (`.pem`) |
| `QLIK_CLIENT_KEY_PATH` | Absolute path to the client private key file (`.pem`) |
| `QLIK_CA_CERT_PATH` | Absolute path to the CA certificate file (`.pem`) |

## Network configuration

Defaults match the standard
[Qlik Sense Enterprise port allocation](https://help.qlik.com/en-US/sense-admin/Subsystems/DeployAdministerQSE/Content/Sense_DeployAdminister/QSEoW/Deploy_QSEoW/Ports.htm):

| Variable | Default | Description |
|----------|---------|-------------|
| `QLIK_REPOSITORY_PORT` | `4242` | Repository (QRS) API port |
| `QLIK_PROXY_PORT` | `4243` | Proxy (QPS) API port — used for ticket auth |
| `QLIK_ENGINE_PORT` | `4747` | Engine API WebSocket port |
| `QLIK_HTTP_PORT` | unset | Optional HTTP port for the metadata endpoint `/api/v1/apps/{id}/data/metadata` |

## SSL

| Variable | Default | Description |
|----------|---------|-------------|
| `QLIK_VERIFY_SSL` | `true` | Verify SSL certificates. Set to `false` for self-signed dev clusters. |

## Timeouts and retries

| Variable | Default | Description |
|----------|---------|-------------|
| `QLIK_HTTP_TIMEOUT` | `10.0` | HTTP request timeout in seconds (Repository API). |
| `QLIK_WS_TIMEOUT` | `180.0` | WebSocket timeout in seconds. Applied to BOTH the WS handshake AND every Engine API call (`OpenDoc`, hypercube creation, `GetLayout`, field statistics). Increase this value if hypercube operations on large apps time out with `WebSocket recv() timed out`. |
| `QLIK_WS_RETRIES` | `2` | Number of WebSocket connection endpoints to try when connecting. |

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. `DEBUG` is very verbose — it logs every Engine API frame, useful for troubleshooting hypercube performance. |

## Sample `.env`

See [`.env.example`](../.env.example) at the repository root for a copy-pasteable template.

## MCP client configuration

The block below is a complete example of registering the server in an
MCP client config. The example uses Streamable HTTP (the default), so the
client connects to the long-lived server process you start manually.

```jsonc
{
  "mcpServers": {
    "qlik-sense": {
      "command": "uvx",
      "args": ["qlik-sense-mcp-server"],
      "env": {
        "QLIK_SERVER_URL": "https://qlik.company.com",
        "QLIK_USER_DIRECTORY": "COMPANY",
        "QLIK_USER_ID": "your-username",
        "QLIK_CLIENT_CERT_PATH": "/etc/qlik/certs/client.pem",
        "QLIK_CLIENT_KEY_PATH": "/etc/qlik/certs/client_key.pem",
        "QLIK_CA_CERT_PATH": "/etc/qlik/certs/root.pem",
        "QLIK_REPOSITORY_PORT": "4242",
        "QLIK_PROXY_PORT": "4243",
        "QLIK_ENGINE_PORT": "4747",
        "QLIK_VERIFY_SSL": "false",
        "QLIK_HTTP_TIMEOUT": "10.0",
        "QLIK_WS_TIMEOUT": "180.0",
        "QLIK_WS_RETRIES": "2",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

A copy is also kept in [`mcp.json.example`](../mcp.json.example).
