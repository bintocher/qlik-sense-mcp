"""Probe local gateway availability without credentials or a Qlik session."""

import os
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def main():
    port = int(os.getenv("MCP_PORT", "8000"))
    # Explicitly disable environment proxies for this loopback-only probe.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(Request(f"http://127.0.0.1:{port}/mcp", method="GET"), timeout=3):
            return 1
    except HTTPError as error:
        # An unauthenticated request should be rejected before worker creation.
        return 0 if error.code == 401 else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
