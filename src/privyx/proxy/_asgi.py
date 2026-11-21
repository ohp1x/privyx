"""The ASGI adapter: the proxy's routes, and each HTTP request as an exchange.

The one place the pipeline meets a web framework.  :func:`create_app` serves
``GET /health`` and ``GET /metrics``, then the routes of plugins, then the
proxy: every method and path for the transparent proxy, a ``POST`` of each
route it serves for the gateway.  A request there becomes an
:class:`~privyx.core.Exchange` for the :class:`~privyx.proxy._pipeline.Pipeline`
at ``app.state.pipeline``, and its :class:`~privyx.proxy._pipeline.Reply` the
HTTP response.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from privyx.config.schema import ProxyConfig, Settings
from privyx.core import Exchange
from privyx.proxy._plugin_routes import include_router

if TYPE_CHECKING:
    from privyx.proxy._pipeline import Pipeline

_CATCH_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def create_app(
    pipeline: Pipeline | None = None,
    settings: Settings | None = None,
    router: APIRouter | None = None,
    *,
    served: dict[str, str] | None = None,
    lifespan: Callable[[Any], Any] | None = None,
) -> FastAPI:
    """The FastAPI app serving ``pipeline``.

    Args:
        pipeline: The pipeline each request goes through, at
            ``app.state.pipeline``, its engine at ``app.state.engine``.
            ``None`` when ``lifespan`` sets them as the app starts.
        settings: Validated settings, at ``app.state.settings``;
            ``proxy.routes`` are the masking routes plugin routes must stay off.
        router: Plugin routes, served before the proxy's.
        served: The routes the gateway serves, by ``POST``; ``None`` serves
            every method and path, as the transparent proxy.  Taken from a
            gateway ``pipeline`` when not given.
        lifespan: The app's lifespan, as FastAPI takes it.

    Raises:
        ConfigError: If a plugin route overlaps a masking route.
    """
    if served is None and pipeline is not None and pipeline.gateway:
        served = pipeline.routes
    # No Swagger UI or OpenAPI document: a privacy proxy has no API to browse,
    # and the transparent proxy's paths belong to the upstream.
    app = FastAPI(
        title="Privyx", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    if pipeline is not None:
        app.state.pipeline = pipeline
        app.state.engine = pipeline.engine

    # Registered first, so GET /health and /metrics are answered here.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request) -> str:
        return str(request.app.state.pipeline.stats.prometheus())

    routes = settings.proxy.routes if settings is not None else ProxyConfig().routes
    include_router(app, router, dict(routes))
    if served is None:
        app.api_route("/{path:path}", methods=_CATCH_ALL_METHODS)(_proxy)
    else:
        for route in served:
            app.post(_route_path(route))(_proxy)
    return app


async def _proxy(request: Request) -> Any:
    pipeline: Pipeline = request.app.state.pipeline
    reply = await pipeline.handle(
        Exchange(
            method=request.method,
            path=_path(request.scope),
            headers=request.headers,
            body=request.stream(),
            query=list(request.query_params.multi_items()),
            raw_path=(request.scope.get("raw_path") or b"").decode("latin-1") or None,
        )
    )
    if reply.stream is not None:
        return StreamingResponse(
            reply.stream,
            status_code=reply.status_code,
            headers=reply.headers,
            media_type=reply.media_type,
        )
    return Response(
        content=reply.body or b"",
        status_code=reply.status_code,
        headers=reply.headers,
        media_type=reply.media_type,
    )


def _path(scope: MutableMapping[str, Any]) -> str:
    """The request path within this app, without the prefix a parent app mounts it at."""
    path: str = scope["path"]
    root: str = scope.get("root_path", "")
    return path[len(root) :] if root and path.startswith(root) else path


def _route_path(route: str) -> str:
    """``route`` as a FastAPI path: each ``*`` becomes a path parameter."""
    pieces = route.split("*")
    return "".join(f"{{_{i - 1}}}{piece}" if i else piece for i, piece in enumerate(pieces))
