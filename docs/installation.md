# Installation

## System requirements

- Python 3.12 (the version pinned in [`pyproject.toml`](../pyproject.toml))
- Qlik Sense Enterprise with Repository API on port 4242 and Engine API on port 4747 (the [standard Qlik Sense Enterprise port allocation](https://help.qlik.com/en-US/sense-admin/Subsystems/DeployAdministerQSE/Content/Sense_DeployAdminister/QSEoW/Deploy_QSEoW/Ports.htm))
- Network access from the host running the MCP server to those Qlik ports
- Client certificate (`.pem`) and matching private key issued by the Qlik Sense node, plus the root CA certificate

The MCP client that talks to this server must be able to handle large JSON
responses — keep `limit` and `max_rows` small while testing.

## Install from PyPI via uvx (recommended)

[`uvx`](https://docs.astral.sh/uv/guides/tools/) is part of [uv](https://docs.astral.sh/uv/)
and runs a published Python tool in an isolated, throw-away environment.
No global install required.

```bash
uvx qlik-sense-mcp-server
```

To pin a specific version:

```bash
uvx qlik-sense-mcp-server@1.4.0
```

## Install from PyPI via pip

```bash
pip install qlik-sense-mcp-server
qlik-sense-mcp-server
```

## Install from source

```bash
git clone https://github.com/bintocher/qlik-sense-mcp.git
cd qlik-sense-mcp
make dev
```

`make dev` creates a virtual environment via `uv`, installs the package
in editable mode together with the optional `dev` extras
(`build`, `twine`, `bump2version`, `pytest`, `pytest-asyncio`).

## Setup

1. Place certificates somewhere outside the repository:
   ```
   /etc/qlik/certs/client.pem
   /etc/qlik/certs/client_key.pem
   /etc/qlik/certs/root.pem
   ```
   (or any path you like — pass absolute paths in env vars)

2. Copy the env template and fill it in:
   ```bash
   cp .env.example .env
   ```
   Then edit `.env`. See [configuration.md](configuration.md) for the
   full list of variables and meanings.

3. Start the server. By default it listens for MCP over Streamable HTTP
   on `http://127.0.0.1:8000/mcp`. See [usage.md](usage.md).
