"""Validated HTTP binding for the public gateway, independent of TLS to Qlik."""

import os


def gateway_binding():
    host = os.getenv("MCP_HOST", "127.0.0.1").strip()
    if not host or any(char.isspace() for char in host) or "://" in host or "/" in host:
        raise ValueError("MCP_HOST must be a bind address or hostname")
    try:
        port = int(os.getenv("MCP_PORT", "8000"))
    except ValueError:
        raise ValueError("MCP_PORT must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("MCP_PORT must be an integer from 1 to 65535")
    return host, port


def serve_gateway(app):
    import uvicorn
    host, port = gateway_binding()
    # Do not trust arbitrary X-Forwarded-* values. Authentication is the
    # actual Authorization bearer token, not proxy-provided identity headers.
    uvicorn.run(app, host=host, port=port, access_log=False, proxy_headers=False)
