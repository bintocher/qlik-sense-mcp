"""Private single-token worker; gateway owns admission, expiry and HTTP auth."""

import asyncio
import json
import os
import sys
import threading
import time


def _report(category):
    print(json.dumps({"status": category}), flush=True)


def main():
    if len(sys.argv) != 2 or os.name != "posix":
        _report("configuration")
        return
    socket_path = sys.argv[1]
    token = sys.stdin.readline(16386).rstrip("\n")
    from .jwt_validity import InvalidToken, token_expiration
    try:
        token_expiration(token)
    except InvalidToken:
        _report("authentication")
        return
    os.environ.update({
        "QLIK_MCP_MODE": "legacy", "QLIK_JWT_TOKEN": token,
        "QLIK_PASSWORD": "", "QLIK_SESSION_CACHE": "false",
        "QLIK_PROXY_LOGOUT": "false", "QLIK_TASK_TOOLS": "false",
        "QLIK_LOG_FILE": "", "QLIK_LOG_REPLIES": "false",
        "LOG_LEVEL": "CRITICAL",
    })
    parent = os.getppid()

    def monitor_parent():
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(1)

    threading.Thread(target=monitor_parent, daemon=True).start()
    guard = None
    try:
        from .config import QlikSenseConfig
        config = QlikSenseConfig.from_env()
        config.validate_runtime()
        from .tools import context, helpers
        from .worker_session import WorkerSessionGuard
        from .worker_tools import install_tool_guard
        guard = WorkerSessionGuard(context, config)
        guard.initialize()
        install_tool_guard(helpers, guard)
        from . import server
        import uvicorn
        app = server.mcp.streamable_http_app()
        instance = uvicorn.Server(uvicorn.Config(
            app, uds=socket_path, log_level="critical", access_log=False,
            timeout_graceful_shutdown=5,
        ))

        async def serve():
            task = asyncio.create_task(instance.serve())
            while not instance.started:
                if task.done():
                    await task
                    raise RuntimeError("Worker startup failed")
                await asyncio.sleep(0.01)
            _report("ready")
            await task
        asyncio.run(serve())
    except Exception as exc:
        # Imports may themselves fail (e.g. a broken installation); classify
        # that without re-importing a dependency that already failed.
        session_module = sys.modules.get("qlik_sense_mcp_server.jwt_session")
        bootstrap_type = getattr(session_module, "JwtBootstrapError", ())
        if isinstance(exc, InvalidToken):
            _report("authentication")
        elif bootstrap_type and isinstance(exc, bootstrap_type):
            text = str(exc)
            if "returned 401" in text or "returned 400" in text:
                _report("authentication")
            else:
                _report("upstream")
        elif isinstance(exc, (ValueError, FileNotFoundError)):
            _report("configuration")
        else:
            _report("startup")
    finally:
        if guard is not None:
            guard.close()


if __name__ == "__main__":
    main()
