"""Full ASGI response ownership and grouped upstream error detection."""

import time

import anyio
import httpx
from starlette.responses import StreamingResponse


def upstream_failed(error):
    if isinstance(error, httpx.HTTPError):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(upstream_failed(child) for child in error.exceptions)
    return False


class OwnedProxyResponse(StreamingResponse):
    def __init__(self, resources, expires_at, headers):
        self.resources = resources
        self.expires_at = expires_at
        super().__init__(resources.upstream.aiter_raw(), status_code=resources.upstream.status_code)
        self.raw_headers = headers + [(b"cache-control", b"no-store")]

    async def __call__(self, scope, receive, send):
        broken = False
        started = False
        completed = False

        async def tracked_send(message):
            nonlocal started, completed
            await send(message)
            if message["type"] == "http.response.start":
                started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        try:
            with anyio.move_on_after(max(0, self.expires_at - time.time())) as deadline:
                await super().__call__(scope, receive, tracked_send)
            if deadline.cancel_called and started and not completed:
                # A timed-out SSE response must still finish ASGI framing.
                # No data is emitted after expiry, only the empty end marker.
                with anyio.CancelScope(shield=True):
                    await tracked_send({"type": "http.response.body", "body": b"", "more_body": False})
        except BaseException as error:
            broken = upstream_failed(error)
            if broken:
                import logging
                logging.getLogger(__name__).warning("Gateway stream failed: upstream_transport")
                raise RuntimeError("Upstream stream failed") from None
            raise
        finally:
            await self.resources.close(invalidate=broken)
