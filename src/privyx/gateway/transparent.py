"""Transparent gateway — the FastAPI catch-all app for the drop-in proxy.

Wraps a :class:`~privyx.proxy.transparent.TransparentProxy` in an ASGI app whose
single ``/{path:path}`` route forwards every method and path (``GET /health`` and
``GET /metrics`` aside)
to the upstream origin.  This is the HTTP-framework seam; all privacy and
forwarding logic lives in the proxy, so it stays testable without a server.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from privyx.config.schema import Settings
from privyx.core.engine import PrivacyEngine
from privyx.proxy.transparent import TransparentProxy

_CATCH_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def create_transparent_app(
    engine: PrivacyEngine,
    proxy: TransparentProxy,
    settings: Settings | None = None,
) -> Any:
    """Build the FastAPI app for the transparent proxy.

    Args:
        engine: Configured privacy engine (kept for symmetry / future use).
        proxy: The transparent proxy that runs the per-request pipeline.
        settings: Validated settings (metadata only).
    """
    app = FastAPI(title="Privyx (transparent)", version="0.1.0")

    # Registered before the catch-all so GET /health and /metrics are answered here.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        return proxy.stats.prometheus()

    @app.api_route("/{path:path}", methods=_CATCH_ALL_METHODS)
    async def proxy_all(request: Request, path: str) -> Any:
        result = await proxy.handle(
            method=request.method,
            path=path,
            headers=request.headers,
            body=await request.body(),
            query_params=list(request.query_params.multi_items()),
            session_id=request.headers.get("x-privyx-session"),
        )
        if result.stream is not None:
            return StreamingResponse(
                result.stream,
                status_code=result.status_code,
                headers=result.headers,
                media_type=result.media_type,
            )
        return Response(
            content=result.body or b"",
            status_code=result.status_code,
            headers=result.headers,
            media_type=result.media_type,
        )

    return app
