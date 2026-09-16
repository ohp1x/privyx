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

import time
from typing import Any
from urllib.parse import urlsplit

from privyx.config.schema import ProxyConfig, Settings
from privyx.core.engine import PrivacyEngine
from privyx.observability.audit import AuditLogger
from privyx.providers.base import Provider
from privyx.proxy.http import HTTPProxy
from privyx.proxy.session import resolve_session_id
from privyx.streaming.adapters.registry import build_stream_adapter

try:
    from fastapi import FastAPI, Request
except ImportError:  # pragma: no cover - optional extra
    FastAPI = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment,misc]


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
        self._routes = dict(settings.proxy.routes) if settings else ProxyConfig().routes
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
            payload = await request.json()
            session_id, source = resolve_session_id(
                self._session_strategy,
                header_value=request.headers.get("x-privyx-session"),
                headers=request.headers,
                payload=payload,
                schema=schema,
            )
            is_stream = bool(payload.get("stream", False))

            transformed, session_id = await proxy.process_request(
                payload, session_id=session_id, source=source
            )

            if session_id is None:
                raise HTTPException(status_code=500, detail="session creation failed")

            self._audit.request(
                method="POST",
                path=path,
                schema=schema,
                status=200,
                session_id=session_id,
                stream=is_stream,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
                upstream=self._upstream_host,
            )

            if is_stream:
                return StreamingResponse(
                    proxy.process_stream(transformed, session_id),
                    media_type="text/event-stream",
                    headers={"X-Privyx-Session": session_id},
                )

            response = await proxy.send_batch(transformed)
            deanonymized = await proxy.process_response(response, session_id)
            return JSONResponse(deanonymized, headers={"X-Privyx-Session": session_id})

        return handler


def _upstream_host(settings: Settings | None) -> str:
    """Host of the resolved upstream, for the ``proxy.request`` audit field."""
    if settings is None:
        return ""
    from privyx.providers.registry import resolve_base_url

    return urlsplit(resolve_base_url(settings)).netloc or ""
