"""Private per-token MCP worker, started only by incoming_gateway.

The bearer token is read once from stdin, never from argv or the parent
process's environment. Qlik bootstrap must succeed before readiness is
reported. The MCP server listens only on a private Unix-domain socket.
"""

import asyncio
import os
import sys
import threading
import time


def main() -> None:
    if len(sys.argv) != 2 or os.name != "posix":
        raise SystemExit(2)
    socket_path = sys.argv[1]
    token = sys.stdin.readline(16386).rstrip("\n")
    if not token or len(token) > 16384:
        raise SystemExit(2)
    os.environ["QLIK_JWT_TOKEN"] = token
    os.environ.pop("QLIK_PASSWORD", None)
    os.environ["QLIK_SESSION_CACHE"] = "false"
    os.environ["QLIK_PROXY_LOGOUT"] = "false"
    os.environ["QLIK_TASK_TOOLS"] = "false"
    # Do not duplicate tool output into a shared file in a multi-user process.
    os.environ.pop("QLIK_LOG_FILE", None)
    os.environ["QLIK_LOG_REPLIES"] = "false"

    parent = os.getppid()

    def monitor_parent() -> None:
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(1)

    threading.Thread(target=monitor_parent, daemon=True).start()

    from .tools import context

    if context.jwt_session is None or context.repo_api is None or context.engine_api is None:
        raise SystemExit(2)
    # With disk persistence disabled, this always validates this exact token
    # with the configured Qlik JWT virtual proxy on first creation.
    context.jwt_session.ensure_standalone()
    from . import server  # registers the tools after the clients exist
    import uvicorn

    app = server.mcp.streamable_http_app()
    config = uvicorn.Config(app, uds=socket_path, log_level="critical", access_log=False)
    instance = uvicorn.Server(config)

    async def serve() -> None:
        task = asyncio.create_task(instance.serve())
        while not instance.started:
            if task.done():
                await task
                raise RuntimeError("MCP worker failed to start")
            await asyncio.sleep(0.01)
        print("READY", flush=True)
        await task

    try:
        asyncio.run(serve())
    finally:
        context.shutdown()


if __name__ == "__main__":
    main()
