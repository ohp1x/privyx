"""Gateway server — FastAPI app wiring engine, proxy, and vault together."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

from privyx.config.schema import Settings
from privyx.core.engine import PrivacyEngine
from privyx.observability.audit import AuditLogger
from privyx.proxy.http import HTTPProxy

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError:  # pragma: no cover - optional extra
    FastAPI = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]
    StreamingResponse = None  # type: ignore[assignment,misc]


class Gateway:
    """HTTP gateway exposing the privacy pipeline.

    Args:
        engine: Configured privacy engine.
        proxy: HTTP proxy that processes requests/responses.
        settings: Validated settings (for metadata).
        audit: Optional audit logger for the PII-safe ``proxy.request`` event.
    """

    def __init__(
        self,
        engine: PrivacyEngine,
        proxy: HTTPProxy,
        settings: Settings | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        if FastAPI is None:
            raise ImportError(
                "Gateway requires FastAPI. Install with `pip install privyx[server]`."
            )
        self._engine = engine
        self._proxy = proxy
        self._settings = settings
        self._audit = audit or AuditLogger(None)
        self._upstream_host = _upstream_host(settings)
        self._app = FastAPI(title="Privyx", version="0.1.0")
        self._register_routes()

    @property
    def app(self) -> Any:
        return self._app

    def _register_routes(self) -> None:
        # FastAPI is guaranteed non-None here: __init__ raises ImportError if missing.

        @self._app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @self._app.post("/v1/chat/completions")
        async def chat_completions(request: Request) -> Any:
            from fastapi import HTTPException
            from fastapi.responses import JSONResponse as _JSONResponse
            from fastapi.responses import StreamingResponse as _StreamingResponse

            start = time.perf_counter()
            payload = await request.json()
            session_id = request.headers.get("x-privyx-session")
            is_stream = bool(payload.get("stream", False))

            transformed, session_id = await self._proxy.process_request(
                payload, session_id=session_id
            )

            if session_id is None:
                raise HTTPException(status_code=500, detail="session creation failed")

            self._audit.request(
                method="POST",
                path="/v1/chat/completions",
                schema="openai",
                status=200,
                session_id=session_id,
                stream=is_stream,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
                upstream=self._upstream_host,
            )

            if is_stream:
                return _StreamingResponse(
                    self._proxy.process_stream(transformed, session_id),
                    media_type="text/event-stream",
                    headers={"X-Privyx-Session": session_id},
                )

            response = await self._proxy.send_batch(transformed)
            deanonymized = await self._proxy.process_response(response, session_id)
            return _JSONResponse(deanonymized, headers={"X-Privyx-Session": session_id})


def _upstream_host(settings: Settings | None) -> str:
    """Host of the resolved upstream, for the ``proxy.request`` audit field."""
    if settings is None:
        return ""
    from privyx.providers.registry import resolve_base_url

    return urlsplit(resolve_base_url(settings)).netloc or ""