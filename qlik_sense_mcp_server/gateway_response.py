"""Full ASGI response ownership and grouped upstream error detection."""

import time

import anyio
import httpx
from starlette.responses import StreamingResponse


def upstream_failed(error):
    """Starlette/AnyIO may wrap streaming failures in an ExceptionGroup."""
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
        try:
            with anyio.move_on_after(max(0, self.expires_at - time.time())):
                await super().__call__(scope, receive, send)
        except BaseException as error:
            broken = upstream_failed(error)
            # Do not propagate upstream exception messages into ASGI logs.
            if broken:
                import logging
                logging.getLogger(__name__).warning("Gateway stream failed: upstream_transport")
                raise RuntimeError("Upstream stream failed") from None
            raise
        finally:
            await self.resources.close(invalidate=broken)
