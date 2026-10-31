"""Unified, lazy startup routing. Existing modes remain the default."""

import os
import sys

from dotenv import load_dotenv


def main():
    load_dotenv()
    mode = os.getenv("QLIK_MCP_MODE", "legacy").strip().lower()
    if mode not in ("legacy", "incoming-jwt"):
        raise SystemExit("QLIK_MCP_MODE must be legacy or incoming-jwt")
    if mode == "incoming-jwt":
        if "--stdio" in sys.argv[1:]:
            raise SystemExit("incoming-jwt mode requires HTTP; --stdio is not supported")
        if any(arg not in ("--help", "-h", "--version", "-v") for arg in sys.argv[1:]):
            raise SystemExit("Unsupported incoming-jwt argument")
        if any(arg in ("--help", "-h") for arg in sys.argv[1:]):
            print("qlik-sense-mcp-server: QLIK_MCP_MODE=incoming-jwt serves /mcp using per-request Qlik JWTs")
            return
        if any(arg in ("--version", "-v") for arg in sys.argv[1:]):
            from . import __version__
            print(__version__)
            return
        from .incoming_gateway import main as start
    else:
        from .server import main as start
    start()


if __name__ == "__main__":
    main()
