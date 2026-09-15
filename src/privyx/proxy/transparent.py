"""Transparent (drop-in) reverse proxy.

Point a client's ``OPENAI_BASE_URL`` / ``ANTHROPIC_BASE_URL`` at Privyx and this
proxy forwards every request to the upstream *origin* unchanged — except that
chat requests are pseudonymized on the way out and the assistant's reply is
restored on the way back, batch or streaming, without any client-side change.

The per-request pipeline:

1. classify the path against the configured routes (:func:`detect_schema`);
2. for a chat path with a JSON body, pseudonymize the sensitive text leaves;
3. forward method, path, query, and (filtered) headers to ``origin/path``;
4. restore the response — structurally for JSON, event-by-event for SSE — and
   forward anything else (models, embeddings, non-JSON) verbatim.

Sessions are resolved from the ``x-privyx-session`` header when present and are
otherwise **ephemeral per request**: a request is pseudonymized and its reply
restored within one exchange, and the client never sees a pseudonym, so a fresh
session per request is correct and — unlike a single shared session — cannot
collide two callers' pseudonym maps.

:class:`TransparentProxy` is framework-agnostic: it returns a :class:`ProxyResponse`
that the gateway turns into an ASGI response, so it can be unit-tested without a
web server.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from privyx.core.engine import PrivacyEngine
from privyx.observability.audit import AuditLogger
from privyx.proxy.headers import filter_request_headers, filter_response_headers
from privyx.proxy.schemas import detect_schema, restore_response, transform_request
from privyx.proxy.stream_router import StreamRouter, select_processor_factory
from privyx.streaming.adapters.registry import build_stream_adapter

_JSON = "application/json"
_SSE = "text/event-stream"


@dataclass(slots=True)
class ProxyResponse:
    """A framework-agnostic response the gateway renders into an ASGI response.

    Exactly one of ``body`` (batch) or ``stream`` (SSE) is set.
    """

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str | None = None
    body: bytes | None = None
    stream: AsyncIterator[str] | None = None


class TransparentProxy:
    """Drop-in reverse proxy that pseudonymizes chat traffic per request.

    Args:
        engine: Configured privacy engine.
        origin: Upstream origin (``scheme://host[:port]``); the request path is
            appended to it.  See :func:`privyx.providers.registry.resolve_origin`.
        routes: Path → wire-schema map (defaults to OpenAI + Anthropic chat).
        forward_client_auth: Forward the client's ``Authorization`` / ``x-api-key``.
        api_key: When set, overrides the upstream credential with this key.
        extra_headers: Static headers merged into every upstream request.
        client: Optional httpx client (owned by the caller when supplied); used by
            tests to inject a mock transport.
        timeout: Read/write timeout in seconds (generous, for long streams).
        audit: Optional audit logger for the PII-safe ``proxy.request`` event.
            Defaults to a disabled no-op logger.
    """

    def __init__(
        self,
        engine: PrivacyEngine,
        *,
        origin: str,
        routes: dict[str, str] | None = None,
        forward_client_auth: bool = True,
        api_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
        audit: AuditLogger | None = None,
    ) -> None:
        self._engine = engine
        self._origin = origin.rstrip("/")
        self._audit = audit or AuditLogger(None)
        self._upstream_host = urlsplit(self._origin).netloc or self._origin
        self._routes = routes if routes is not None else {
            "/v1/chat/completions": "openai",
            "/v1/messages": "anthropic",
        }
        self._forward_client_auth = forward_client_auth
        self._api_key = api_key
        self._extra_headers = dict(extra_headers or {})
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=timeout, write=timeout, pool=10.0)
        )

    async def close(self) -> None:
        """Release the httpx client if this proxy created it."""
        if self._owns_client:
            await self._client.aclose()

    async def handle(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        query_params: Any | None = None,
        session_id: str | None = None,
    ) -> ProxyResponse:
        """Run the full per-request pipeline and return a :class:`ProxyResponse`."""
        start = time.perf_counter()
        schema = detect_schema(path, self._routes)
        content_type = _ci_get(headers, "content-type")

        if session_id is None:
            session_id = _ci_get(headers, "x-privyx-session") or None
        session = await self._engine.get_or_create_session(session_id)
        sid = session.session_id

        out_body = body
        if schema and body and _JSON in content_type:
            payload = _loads(body)
            if isinstance(payload, dict):
                transformed = await transform_request(payload, self._engine, sid)
                out_body = json.dumps(transformed, ensure_ascii=False).encode("utf-8")

        url = f"{self._origin}/{path.lstrip('/')}"
        request = self._client.build_request(
            method,
            url,
            headers=filter_request_headers(
                headers,
                schema=schema,
                forward_client_auth=self._forward_client_auth,
                api_key=self._api_key,
                extra=self._extra_headers,
            ),
            content=out_body,
            params=query_params,
        )
        response = await self._client.send(request, stream=True)

        resp_content_type = response.headers.get("content-type", "")
        resp_headers = filter_response_headers(response.headers)
        resp_headers["x-privyx-session"] = sid

        is_stream = _SSE in resp_content_type
        self._audit.request(
            method=method,
            path="/" + path.lstrip("/"),
            schema=schema,
            status=response.status_code,
            session_id=sid,
            stream=is_stream,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            upstream=self._upstream_host,
        )

        if is_stream:
            return self._streaming_response(response, resp_headers, schema, sid)
        return await self._batch_response(response, resp_headers, resp_content_type, schema, sid)

    # -- internals ---------------------------------------------------------

    def _streaming_response(
        self,
        response: httpx.Response,
        headers: dict[str, str],
        schema: str | None,
        session_id: str,
    ) -> ProxyResponse:
        headers.pop("content-type", None)  # media_type carries it, avoid duplicate
        headers.setdefault("cache-control", "no-cache")
        return ProxyResponse(
            status_code=response.status_code,
            headers=headers,
            media_type=_SSE,
            stream=self._stream(response, schema, session_id),
        )

    async def _stream(
        self,
        response: httpx.Response,
        schema: str | None,
        session_id: str,
    ) -> AsyncIterator[str]:
        session = await self._engine.vault.get(session_id)
        mapping = session.mapping if session is not None else {}
        adapter = build_stream_adapter(schema or "generic")
        router = StreamRouter(
            self._engine.codec,
            mapping.get,
            adapter,
            make_processor=select_processor_factory(
                self._engine.operator, self._engine.codec, mapping.get, mapping
            ),
        )
        try:
            async for raw in response.aiter_bytes():
                for out in router.feed(raw):
                    yield out
            for out in router.flush():
                yield out
        finally:
            await response.aclose()

    async def _batch_response(
        self,
        response: httpx.Response,
        headers: dict[str, str],
        content_type: str,
        schema: str | None,
        session_id: str,
    ) -> ProxyResponse:
        data = await response.aread()
        await response.aclose()
        if schema and _JSON in content_type:
            payload = _loads(data)
            if isinstance(payload, dict):
                restored = await restore_response(schema, payload, self._engine, session_id)
                data = json.dumps(restored, ensure_ascii=False).encode("utf-8")
        return ProxyResponse(
            status_code=response.status_code,
            headers=headers,
            media_type=content_type or None,
            body=data,
        )


def _ci_get(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup that works for dicts and Headers objects."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


def _loads(data: bytes | str) -> Any:
    try:
        return json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None
