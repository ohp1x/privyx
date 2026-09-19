"""Gateway server — FastAPI app wiring engine, provider, and vault together.

Unlike the transparent proxy, this app exposes *its own* endpoints and forwards
every one of them to the single upstream endpoint named by ``provider.base_url``
(:func:`~privyx.providers.registry.resolve_base_url`), used verbatim.  That is
the reason to pick this mode: the incoming path is not echoed at the upstream,
so an upstream carrying a base path (``https://host/anthropic/v1/messages``,
an Azure deployment, an OpenRouter prefix) works here and cannot be expressed in
transparent mode, which appends the client's path to the bare origin.

Which paths are served — and the wire schema each speaks — comes from
``proxy.routes``, the same map the transparent proxy classifies against.  Note
that nothing translates between schemas: a client speaking Anthropic still needs
an upstream that speaks Anthropic.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

from privyx.config.schema import ProxyConfig, Settings
from privyx.core.engine import PrivacyEngine
from privyx.core.errors import ProviderError
from privyx.observability.audit import AuditLogger
from privyx.providers.base import Provider
from privyx.proxy.http import HTTPProxy
from privyx.proxy.session import resolve_session_id
from privyx.proxy.streaming import AuditedStream
from privyx.streaming.adapters.registry import build_stream_adapter
from privyx.utils.ids import request_id as new_request_id

try:
    from fastapi import FastAPI, Request
except ImportError:  # pragma: no cover - optional extra
    FastAPI = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment,misc]


#: Token-counting and compaction endpoints.  The gateway posts every path to
#: one chat endpoint, which would turn a token count into a billed completion,
#: so these stay unserved (404) and clients fall back as they would upstream.
_AUXILIARY_ROUTES = frozenset(
    {"/v1/messages/count_tokens", "/v1/responses/input_tokens", "/v1/responses/compact"}
)


def _elapsed_ms(start: float) -> float:
    """Milliseconds since a ``time.perf_counter()`` mark, rounded for the trail."""
    return round((time.perf_counter() - start) * 1000, 2)


class Gateway:
    """HTTP gateway exposing the privacy pipeline.

    Args:
        engine: Configured privacy engine.
        provider: Upstream provider (owned by the caller, which closes it).
        settings: Validated settings; ``proxy.routes`` selects the served paths.
        audit: Optional audit logger for the PII-safe ``proxy.request`` event.
    """

    def __init__(
        self,
        engine: PrivacyEngine,
        provider: Provider,
        settings: Settings | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        if FastAPI is None:
            raise ImportError(
                "Gateway requires FastAPI. Install with `pip install privyx[server]`."
            )
        self._engine = engine
        self._settings = settings
        self._audit = audit or AuditLogger(None)
        self._upstream_host = _upstream_host(settings)
        self._session_strategy = settings.session.strategy if settings else "ephemeral"
        routes = dict(settings.proxy.routes) if settings else ProxyConfig().routes
        self._routes = {path: s for path, s in routes.items() if path not in _AUXILIARY_ROUTES}
        # One proxy per schema: the stream adapter (and the schema used to
        # restore a batch response) is per-instance, while the provider — hence
        # the upstream endpoint and its connection pool — is shared.
        self._proxies = {
            schema: HTTPProxy(
                engine=engine, provider=provider, stream_adapter=build_stream_adapter(schema)
            )
            for schema in set(self._routes.values())
        }
        self._app = FastAPI(title="Privyx", version="0.1.0")
        self._register_routes()

    @property
    def app(self) -> Any:
        return self._app

    @property
    def routes(self) -> dict[str, str]:
        """The served path → wire-schema map."""
        return dict(self._routes)

    def _register_routes(self) -> None:
        # FastAPI is guaranteed non-None here: __init__ raises ImportError if missing.

        @self._app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        for path, schema in self._routes.items():
            self._app.post(path)(self._make_handler(path, schema))

    def _make_handler(self, path: str, schema: str) -> Any:
        proxy = self._proxies[schema]

        async def handler(request: Request) -> Any:
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse, StreamingResponse

            start = time.perf_counter()
            req_id = new_request_id()
            token = self._audit.begin_request(req_id)
            session_id: str | None = None
            ephemeral = False
            defer_cleanup = False
            try:
                payload = await request.json()
                session_id, source = resolve_session_id(
                    self._session_strategy,
                    header_value=request.headers.get("x-privyx-session"),
                    headers=request.headers,
                    payload=payload,
                    schema=schema,
                )
                ephemeral = source == "ephemeral"
                is_stream = bool(payload.get("stream", False))

                transformed, session_id = await proxy.process_request(
                    payload, session_id=session_id, source=source
                )

                if session_id is None:
                    raise HTTPException(status_code=500, detail="session creation failed")

                self._audit.set_session(session_id)
                self._audit.flush_transform()
                self._audit.request(
                    method="POST",
                    path=path,
                    schema=schema,
                    status=200,
                    session_id=session_id,
                    stream=is_stream,
                    duration_ms=_elapsed_ms(start),
                    upstream=self._upstream_host,
                    request_id=req_id,
                )

                if is_stream:
                    frames = proxy.process_stream(transformed, session_id)
                    # Pull the first frame before committing to a 200, so an
                    # upstream error status reaches the client as itself.
                    try:
                        first = await anext(frames, None)
                    except ProviderError as exc:
                        self._audit.error(
                            phase="upstream",
                            error_type=type(exc).__name__,
                            session_id=session_id,
                            request_id=req_id,
                            duration_ms=_elapsed_ms(start),
                        )
                        return _relay_upstream_error(exc)
                    defer_cleanup = True
                    stream_iter = self._audited_stream(
                        _prepend(first, frames),
                        session_id=session_id,
                        request_id=req_id,
                        start=start,
                        ephemeral=ephemeral,
                    )
                    cleanup = (
                        (lambda: self._cleanup_session(session_id, req_id, start))
                        if ephemeral
                        else None
                    )
                    return StreamingResponse(
                        AuditedStream(stream_iter, cleanup=cleanup),
                        media_type="text/event-stream",
                        headers={"X-Privyx-Session": session_id},
                    )

                try:
                    response = await proxy.send_batch(transformed)
                    deanonymized = await proxy.process_response(response, session_id)
                except Exception as exc:
                    self._audit.error(
                        phase="upstream",
                        error_type=type(exc).__name__,
                        session_id=session_id,
                        request_id=req_id,
                        duration_ms=_elapsed_ms(start),
                    )
                    if isinstance(exc, ProviderError):
                        return _relay_upstream_error(exc)
                    raise
                result = JSONResponse(deanonymized, headers={"X-Privyx-Session": session_id})
                restored_n = self._audit.flush_restore()
                self._audit.response(
                    status=200,
                    stream=False,
                    size=len(result.body),
                    restored=restored_n,
                    session_id=session_id,
                    request_id=req_id,
                    duration_ms=_elapsed_ms(start),
                )
                return result
            finally:
                if not defer_cleanup and ephemeral and session_id is not None:
                    await self._cleanup_session(session_id, req_id, start)
                self._audit.end_request(token)

        return handler

    async def _audited_stream(
        self,
        source: Any,
        *,
        session_id: str,
        request_id: str,
        start: float,
        ephemeral: bool,
    ) -> Any:
        """Forward a gateway SSE stream, recording ``proxy.response`` at the end.

        Gateway mode is the narrow back-compat path: ``HTTPProxy.process_stream``
        owns the resolver, so the restored count is not observable here (it is on
        the transparent proxy).  ``proxy.error`` is still recorded if the stream
        breaks mid-flight.
        """
        frames = 0
        completed = False
        try:
            async for frame in source:
                frames += 1
                yield frame
            completed = True
        except Exception as exc:
            self._audit.error(
                phase="stream",
                error_type=type(exc).__name__,
                session_id=session_id,
                request_id=request_id,
                duration_ms=_elapsed_ms(start),
            )
            raise
        finally:
            if completed:
                self._audit.response(
                    status=200,
                    stream=True,
                    frames=frames,
                    restored=0,
                    session_id=session_id,
                    request_id=request_id,
                    duration_ms=_elapsed_ms(start),
                )
            if ephemeral:
                await self._cleanup_session(session_id, request_id, start)

    async def _cleanup_session(self, session_id: str, request_id: str, start: float) -> None:
        """Best-effort deletion for a request-scoped ephemeral session."""
        try:
            await self._engine.delete_session(
                session_id,
                reason="ephemeral_request_complete",
                request_id=request_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Cleanup must never mask the response or an upstream/stream error.
            self._audit.error(
                phase="cleanup",
                error_type=type(exc).__name__,
                session_id=session_id,
                request_id=request_id,
                duration_ms=_elapsed_ms(start),
            )


def _relay_upstream_error(exc: ProviderError) -> Any:
    """The upstream's own error response, or a 502 when it never answered."""
    from fastapi.responses import JSONResponse, Response

    if exc.status_code is None:
        return JSONResponse({"error": {"message": str(exc)}}, status_code=502)
    return Response(exc.body, status_code=exc.status_code, media_type=exc.content_type or None)


async def _prepend(first: Any, rest: Any) -> Any:
    """Yield ``first`` (unless ``None``), then everything left in ``rest``."""
    if first is not None:
        yield first
    async for item in rest:
        yield item


def _upstream_host(settings: Settings | None) -> str:
    """Host of the resolved upstream, for the ``proxy.request`` audit field."""
    if settings is None:
        return ""
    from privyx.providers.registry import resolve_base_url

    return urlsplit(resolve_base_url(settings)).netloc or ""
