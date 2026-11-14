"""Transparent (drop-in) reverse proxy.

Point a client's ``OPENAI_BASE_URL`` / ``ANTHROPIC_BASE_URL`` at Privyx and this
proxy forwards every request to the upstream *origin* unchanged — except that
chat requests are pseudonymized on the way out and the assistant's reply is
restored on the way back, batch or streaming, without any client-side change.

The per-request pipeline:

1. classify the path against the configured routes (:func:`detect_schema`);
2. for a chat path with a JSON body, pseudonymize the sensitive text leaves,
   and on any path the headers that describe the client's machine;
3. forward method, path, query, and (filtered) headers to ``origin/path``;
4. restore the response — structurally for JSON, event-by-event for SSE — and
   forward anything else (models, embeddings, non-JSON) verbatim.

Sessions are resolved from the ``x-privyx-session`` header when present and are
otherwise **ephemeral per request**: a request is pseudonymized and its reply
restored within one exchange, and the client never sees a pseudonym, so a fresh
session per request is correct and — unlike a single shared session — cannot
collide two callers' pseudonym maps.  It stays in memory for that one exchange
and never touches the vault.

:class:`TransparentProxy` is framework-agnostic: it returns a :class:`ProxyResponse`
that the gateway turns into an ASGI response, so it can be unit-tested without a
web server.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from privyx.config.schema import ProxyConfig
from privyx.core.engine import PrivacyEngine
from privyx.core.errors import LeakError, VaultError
from privyx.core.session import Session
from privyx.observability import tracing
from privyx.observability.audit import AuditLogger
from privyx.observability.metrics import AuditStats
from privyx.observability.tap import Tap
from privyx.observability.usage import UsageMeter
from privyx.providers.generic import upstream_client
from privyx.proxy.headers import filter_request_headers, filter_response_headers
from privyx.proxy.schemas import (
    AUTH_QUERY_PARAMS,
    MASKED_HEADERS,
    detect_schema,
    has_token_ids,
    restore_response,
    transform_request,
)
from privyx.proxy.session import resolve_session_id
from privyx.proxy.stream_router import (
    LineRouter,
    StreamRouter,
    resolver_for,
    restore_chunks,
    select_processor_factory,
)
from privyx.proxy.streaming import AuditedStream
from privyx.streaming.adapters.registry import build_stream_adapter
from privyx.utils.ids import request_id as new_request_id

_JSON = "application/json"
_SSE = "text/event-stream"
_NDJSON = "application/x-ndjson"  # Ollama's stream: one JSON object per line
_log = logging.getLogger(__name__)

#: Body of the 503 returned when a request cannot be masked (a detector failed
#: or timed out).  Fail-closed: the request is not forwarded.  No exception text:
#: it could quote the request, or an SDK error could.
SCAN_FAILED_BODY = {
    "error": {
        "type": "privyx_scan_failed",
        "message": "privyx could not scan this request for sensitive data, "
        "so it was not forwarded; see the privyx logs",
    }
}

#: Body of the 422 returned when ``proxy.dlp_check`` finds a value the request
#: masked still in it, in the clear.  Not forwarded, and retrying cannot help.
LEAK_BODY = {
    "error": {
        "type": "privyx_leak_blocked",
        "message": "privyx masked a value in this request but found it again unmasked "
        "elsewhere in it, so it was not forwarded (proxy.dlp_check)",
    }
}

#: Body of the 400 returned when a routed request's body is not a JSON object,
#: the only kind Privyx can mask.  It is not forwarded either.
INVALID_BODY = {
    "error": {
        "type": "privyx_invalid_request",
        "message": "the request body is not a JSON object, so privyx could not mask it "
        "and did not forward it",
    }
}

#: Body of the 400 returned when a routed request sends token ids instead of
#: text (embeddings, legacy completions): Privyx cannot scan them.
TOKEN_IDS_BODY = {
    "error": {
        "type": "privyx_invalid_request",
        "message": "the request sends token ids instead of text, so privyx could not "
        "mask it and did not forward it; send the text instead",
    }
}

#: Body of the 503 returned when the session vault fails (Redis down or not
#: answering, a SQLite error).  Fail-closed like a scan failure: without the
#: session, a request cannot be masked and a reply cannot be restored.
VAULT_UNAVAILABLE_BODY = {
    "error": {
        "type": "privyx_vault_unavailable",
        "message": "privyx could not use its session vault, so it did not complete "
        "this request; see the privyx logs",
    }
}


def _elapsed_ms(start: float) -> float:
    """Milliseconds since a ``time.perf_counter()`` mark, rounded for the trail."""
    return round((time.perf_counter() - start) * 1000, 2)


@dataclass(slots=True)
class ProxyResponse:
    """A framework-agnostic response the gateway renders into an ASGI response.

    Exactly one of ``body`` (a restored batch response) or ``stream`` (restored
    SSE text, or the bytes of a response with nothing to restore) is set.
    """

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str | None = None
    body: bytes | None = None
    stream: AsyncIterator[str] | AsyncIterator[bytes] | None = None


class TransparentProxy:
    """Drop-in reverse proxy that pseudonymizes chat traffic per request.

    Args:
        engine: Configured privacy engine.
        origin: Upstream origin (``scheme://host[:port]``); the request path is
            appended to it.  See :func:`privyx.providers.registry.resolve_origin`.
            With ``strip_prefix``, the upstream URL including its path.
        strip_prefix: When set (``""`` included), the request path must lie
            under this prefix, which is removed before the rest is appended to
            ``origin``.  Paths outside it, or that could leave it, are refused.
            ``None`` appends the whole path (``proxy.path_mapping.mode: origin``).
        routes: Path → wire-schema map (defaults to ``proxy.routes``' default:
            OpenAI Chat and Responses, Anthropic Messages and token counting).
        passthrough_unknown: Forward paths missing from ``routes`` verbatim.  When
            false they are refused with 403, so nothing reaches the upstream
            unmasked.
        forward_client_auth: Forward the client's ``Authorization`` / ``x-api-key``.
        api_key: When set, overrides the upstream credential with this key.
        extra_headers: Static headers merged into every upstream request.
        client: Optional httpx client (owned by the caller when supplied); used by
            tests to inject a mock transport.
        timeout, connect_timeout, max_connections: For the client built when
            ``client`` is not given; see
            :func:`~privyx.providers.generic.upstream_client`.
        audit: Optional audit logger for the PII-safe ``proxy.request`` event.
            Defaults to a disabled no-op logger.
        session_strategy: How to identify a session when the client sends no
            ``x-privyx-session`` header — ``ephemeral`` (default), ``client``, or
            ``conversation``.  See :func:`privyx.proxy.session.resolve_session_id`.
        tap: Optional developer tap that records each masked exchange before
            and after masking (``audit.tap``; see :mod:`privyx.observability.tap`).
    """

    def __init__(
        self,
        engine: PrivacyEngine,
        *,
        origin: str,
        routes: dict[str, str] | None = None,
        passthrough_unknown: bool = True,
        forward_client_auth: bool = True,
        api_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
        connect_timeout: float = 10.0,
        max_connections: int | None = None,
        audit: AuditLogger | None = None,
        session_strategy: str = "ephemeral",
        tap: Tap | None = None,
        strip_prefix: str | None = None,
    ) -> None:
        self._engine = engine
        self._strip_prefix = strip_prefix
        self._tap = tap
        self._origin = origin.rstrip("/")
        self._audit = audit or AuditLogger(None)
        self._session_strategy = session_strategy
        self._upstream_host = urlsplit(self._origin).netloc or self._origin
        self._routes = routes if routes is not None else ProxyConfig().routes
        self._passthrough_unknown = passthrough_unknown
        self._forward_client_auth = forward_client_auth
        self._api_key = api_key
        self._extra_headers = dict(extra_headers or {})
        self._owns_client = client is None
        self._client = client or upstream_client(
            timeout=timeout, connect_timeout=connect_timeout, max_connections=max_connections
        )

    @property
    def stats(self) -> AuditStats:
        """Totals of the audit events emitted so far, served at ``GET /metrics``."""
        return self._audit.stats

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
        body: bytes | AsyncIterator[bytes],
        query_params: Any | None = None,
        session_id: str | None = None,
        raw_path: str | None = None,
    ) -> ProxyResponse:
        """Run the full per-request pipeline and return a :class:`ProxyResponse`.

        ``body`` may be the chunks as they arrive: a routed body is read whole to
        be masked, any other is streamed upstream as it comes.  ``raw_path`` is
        the path as sent, before percent-decoding, when the server has it.
        """
        span = tracing.start("privyx.request", headers=headers, **{"http.request.method": method})
        try:
            result = await self._handle(
                span,
                method=method,
                path=path,
                headers=headers,
                body=body,
                query_params=query_params,
                session_id=session_id,
                raw_path=raw_path,
            )
        except BaseException as exc:
            tracing.end(span, error=exc)
            raise
        # The status the client gets, whichever way the exchange went.
        tracing.annotate(span, **{"http.response.status_code": result.status_code})
        if result.status_code >= 500:
            tracing.fail(span)
        if result.stream is None:  # a stream or relayed reply ends the span when it ends
            tracing.end(span)
        return result

    async def _handle(
        self,
        span: Any,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | AsyncIterator[bytes],
        query_params: Any | None,
        session_id: str | None,
        raw_path: str | None = None,
    ) -> ProxyResponse:
        start = time.perf_counter()
        req_id = new_request_id()
        # Correlate every event of this exchange and aggregate its per-leaf
        # transform/restore into one line each; closed in the finally below (the
        # streaming generator carries req_id explicitly, so it needs no scope).
        token = self._audit.begin_request(req_id)
        sid: str | None = None
        ephemeral: Session | None = None
        defer_cleanup = False
        try:
            if _ci_get(headers, "upgrade").lower() == "websocket":
                # A WebSocket cannot be relayed here, so its frames could not be
                # masked.  426 makes Codex fall back to HTTP at once; on another
                # status it retries for seconds first, and gives up on a 400.
                message = "privyx: WebSocket is not supported; use HTTP"
                return ProxyResponse(
                    status_code=426,
                    media_type=_JSON,
                    body=json.dumps({"error": {"message": message}}).encode(),
                )
            upstream_path = _upstream_path(path, raw_path, self._strip_prefix)
            if upstream_path is None:
                self._audit.route_decision(allowed=False, method=method, reason="outside_prefix")
                message = (
                    f"privyx: the path is outside proxy.path_mapping.strip_prefix "
                    f"{self._strip_prefix or '/'!r} or could leave it"
                )
                return ProxyResponse(
                    status_code=404,
                    media_type=_JSON,
                    body=json.dumps({"error": {"message": message}}).encode(),
                )
            schema = detect_schema(path, self._routes)
            if schema is not None:
                tracing.annotate(
                    span, **{"http.route": "/" + path.lstrip("/"), "privyx.schema": schema}
                )
            if schema is None:
                self._audit.route_decision(allowed=self._passthrough_unknown, method=method)
            if schema is None and not self._passthrough_unknown:
                message = (
                    f"privyx: /{path.lstrip('/')} is not in proxy.routes "
                    "and proxy.passthrough_unknown is false"
                )
                return ProxyResponse(
                    status_code=403,
                    media_type=_JSON,
                    body=json.dumps({"error": {"message": message}}).encode(),
                )
            if session_id is None:
                session_id = _ci_get(headers, "x-privyx-session") or None

            payload: Any = None
            if schema is not None:
                if not isinstance(body, bytes):
                    body = b"".join([chunk async for chunk in body])
                # Parse the body once, up front: the session strategy may
                # fingerprint it (e.g. the first user message), and the transform
                # reuses the parse.  Whatever its content type says (`curl -d`
                # sends a form type): a routed body that is not a JSON object
                # would go upstream unmasked.
                payload = _loads(body) if body else None
                if body and not isinstance(payload, dict):
                    return ProxyResponse(
                        status_code=400, media_type=_JSON, body=json.dumps(INVALID_BODY).encode()
                    )
                if isinstance(payload, dict) and has_token_ids(payload):
                    return ProxyResponse(
                        status_code=400, media_type=_JSON, body=json.dumps(TOKEN_IDS_BODY).encode()
                    )
            elif not isinstance(body, bytes) and not (
                _ci_get(headers, "content-length") or _ci_get(headers, "transfer-encoding")
            ):
                body = b""  # none was sent; an empty stream would go out chunked
            resolved_id, source = resolve_session_id(
                self._session_strategy,
                header_value=session_id,
                headers=headers,
                payload=payload,
                schema=schema,
            )
            if source == "ephemeral":
                # It lives for this request only, so it never touches the vault.
                session = ephemeral = self._engine.ephemeral_session()
            else:
                session = await self._engine.get_or_create_session(resolved_id, source=source)
            sid = session.session_id
            self._audit.set_session(sid)

            upstream_headers = filter_request_headers(
                headers,
                schema=schema,
                forward_client_auth=self._forward_client_auth,
                api_key=self._api_key,
                extra=self._extra_headers,
            )
            out_body = body
            parsed_params = httpx.QueryParams(query_params) if query_params is not None else None
            out_params: list[tuple[str, Any]] | None = (
                list(parsed_params.multi_items()) if parsed_params else None
            )
            if out_params and (self._api_key or not self._forward_client_auth):
                # The client's key in the query (Gemini's `?key=`) goes with its
                # headers: Privyx's key replaces it, or none is relayed.
                out_params = [(k, v) for k, v in out_params if k.lower() not in AUTH_QUERY_PARAMS]
            transform_ms: float | None = None
            # A masked header comes on body-less requests too, and on unrouted
            # paths.
            if (
                isinstance(payload, dict)
                or not MASKED_HEADERS.isdisjoint(upstream_headers)
                or out_params is not None
            ):
                transform_start = time.perf_counter()
                transform_span = tracing.start("privyx.transform", span)
                try:
                    transformed = await transform_request(
                        payload or {},
                        self._engine,
                        sid,
                        session=ephemeral,
                        headers=upstream_headers,
                        params=out_params,
                        schema=schema,
                        path="/" + path.lstrip("/"),
                    )
                except VaultError as exc:
                    tracing.end(transform_span, error=exc)
                    raise  # not a scan failure: answered below
                except Exception as exc:
                    tracing.end(transform_span, error=exc)
                    self._audit.error(
                        phase="transform",
                        error_type=type(exc).__name__,
                        session_id=sid,
                        request_id=req_id,
                        duration_ms=_elapsed_ms(start),
                    )
                    log_scan_failure(exc)
                    status, error = scan_failure(exc)
                    return ProxyResponse(
                        status_code=status, media_type=_JSON, body=json.dumps(error).encode()
                    )
                transform_ms = _elapsed_ms(transform_start)
                tracing.end(transform_span)
                if isinstance(payload, dict):
                    out_body = json.dumps(transformed, ensure_ascii=False).encode("utf-8")
                    if self._tap is not None:
                        self._tap.record(
                            sid,
                            req_id,
                            "request",
                            path="/" + path.lstrip("/"),
                            schema=schema,
                            before=payload,
                            after=transformed,
                        )
            self._audit.flush_transform()

            url = self._origin + upstream_path
            length = _ci_get(headers, "content-length")
            if not isinstance(out_body, bytes) and length:
                # Streamed unchanged, so its length still holds; without it
                # httpx would send the body chunked.
                upstream_headers["content-length"] = length
            request = self._client.build_request(
                method,
                url,
                headers=upstream_headers,
                content=out_body,
                params=out_params,
            )
            upstream_span = tracing.start("privyx.upstream", span)
            if schema is not None:
                self._audit.routed += 1
            try:
                response = await self._client.send(request, stream=True)
            except Exception as exc:
                tracing.end(upstream_span, error=exc)
                self._audit.error(
                    phase="upstream",
                    error_type=type(exc).__name__,
                    session_id=sid,
                    request_id=req_id,
                    duration_ms=_elapsed_ms(start),
                )
                if not isinstance(exc, httpx.RequestError):
                    raise
                status, error_headers, error = upstream_failure(exc)
                return ProxyResponse(
                    status_code=status,
                    headers=error_headers,
                    media_type=_JSON,
                    body=json.dumps(error).encode(),
                )

            tracing.end(upstream_span)
            resp_content_type = response.headers.get("content-type", "")
            resp_headers = filter_response_headers(response.headers)
            resp_headers["x-privyx-session"] = sid

            # Restored as it streams: SSE, and the JSON lines of an Ollama route.
            lines = schema == "ollama" and _NDJSON in resp_content_type
            is_stream = (_SSE in resp_content_type or lines) and method.upper() != "HEAD"
            tracing.annotate(span, **{"privyx.stream": is_stream})
            self._audit.request(
                method=method,
                # An unmatched URL may itself contain PII. The route-decision
                # event exposes the gap without copying a client-supplied path.
                path="/" + path.lstrip("/") if schema is not None else "[unmatched]",
                schema=schema,
                status=response.status_code,
                session_id=sid,
                stream=is_stream,
                duration_ms=_elapsed_ms(start),
                upstream=self._upstream_host,
                request_id=req_id,
                transform_ms=transform_ms,
            )

            if method.upper() == "HEAD":
                await response.aclose()
                if "content-length" in response.headers:
                    resp_headers["content-length"] = response.headers["content-length"]
                return ProxyResponse(
                    status_code=response.status_code,
                    headers=resp_headers,
                    media_type=resp_content_type or None,
                    body=b"",
                )

            if is_stream:
                try:
                    # Read before the 200 goes out, so a failing vault is a 503.
                    stored = ephemeral or await self._engine.vault.get(sid)
                except VaultError:
                    await response.aclose()
                    raise
                # The generator owns cleanup: FastAPI/client cancellation closes
                # it even when the caller does not drain the upstream stream.
                defer_cleanup = True
                return self._streaming_response(
                    response,
                    resp_headers,
                    schema,
                    sid,
                    stored.mapping if stored is not None else {},
                    req_id,
                    start,
                    ephemeral,
                    span,
                    lines=lines,
                )
            if schema and _JSON in resp_content_type:
                return await self._batch_response(
                    response,
                    resp_headers,
                    resp_content_type,
                    sid,
                    ephemeral,
                    req_id,
                    start,
                    schema=schema,
                    span=span,
                )
            defer_cleanup = True  # as for a stream
            return self._relayed_response(
                response, resp_headers, resp_content_type, sid, req_id, start, ephemeral, span
            )
        except VaultError as exc:
            tracing.annotate(span, **{"error.type": type(exc).__name__})
            self._audit.error(
                phase="vault",
                error_type=type(exc).__name__,
                session_id=sid,
                request_id=req_id,
                duration_ms=_elapsed_ms(start),
            )
            log_vault_failure(exc)
            return ProxyResponse(
                status_code=503, media_type=_JSON, body=json.dumps(VAULT_UNAVAILABLE_BODY).encode()
            )
        finally:
            if not defer_cleanup:
                self._end_session(ephemeral, req_id)
            self._audit.end_request(token)

    # -- internals ---------------------------------------------------------

    def _streaming_response(
        self,
        response: httpx.Response,
        headers: dict[str, str],
        schema: str | None,
        session_id: str,
        mapping: dict[str, str],
        request_id: str,
        start: float,
        ephemeral: Session | None,
        span: Any = None,
        *,
        lines: bool = False,
    ) -> ProxyResponse:
        headers.pop("content-type", None)  # media_type carries it, avoid duplicate
        headers.setdefault("cache-control", "no-cache")
        stream_iter = self._stream(
            response, schema, session_id, mapping, request_id, start, ephemeral, span, lines=lines
        )
        return ProxyResponse(
            status_code=response.status_code,
            headers=headers,
            media_type=_NDJSON if lines else _SSE,
            stream=AuditedStream(
                stream_iter,
                response=response,
                cleanup=lambda: self._abandon(ephemeral, request_id, span),
            ),
        )

    async def _stream(
        self,
        response: httpx.Response,
        schema: str | None,
        session_id: str,
        mapping: dict[str, str],
        request_id: str,
        start: float,
        ephemeral: Session | None,
        span: Any = None,
        *,
        lines: bool = False,
    ) -> AsyncIterator[str]:
        """Restore a streamed reply: SSE events, or with ``lines`` JSON lines."""
        restore_span = tracing.start("privyx.restore", span)
        failure: BaseException | None = None
        frames = 0
        status = response.status_code
        restored = 0
        meter = UsageMeter()
        # What the tap keeps, when it is on: the upstream's bytes and ours.
        tapped: tuple[list[bytes], list[str]] | None = ([], []) if self._tap and schema else None
        completed = aborted = False
        try:
            adapter = build_stream_adapter(schema or "generic")
            # Count restores by wrapping the resolver: a non-None lookup is one
            # pseudonym reversed.  (Covers the codec path — pseudonym/hash/encrypt;
            # faker's literal trie restore does not consult ``resolve``.)
            base_resolve = resolver_for(self._engine.operator, mapping)

            def resolve(token: str) -> str | None:
                nonlocal restored
                original = base_resolve(token)
                if original is not None:
                    restored += 1
                return original

            make = select_processor_factory(
                self._engine.operator, self._engine.codec, resolve, mapping
            )
            router: StreamRouter | LineRouter = (
                LineRouter(self._engine.codec, resolve, make)
                if lines
                else StreamRouter(self._engine.codec, resolve, adapter, make_processor=make)
            )
            async for raw in response.aiter_bytes():
                if lines:
                    meter.feed_lines(raw)
                else:
                    meter.feed_sse(raw)
                if tapped is not None:
                    tapped[0].append(raw)
                for out in router.feed(raw):
                    frames += 1
                    if tapped is not None:
                        tapped[1].append(out)
                    yield out
            for out in router.flush():
                frames += 1
                if tapped is not None:
                    tapped[1].append(out)
                yield out
            completed = True
        except Exception as exc:
            failure = exc
            self._audit.error(
                phase="stream",
                error_type=type(exc).__name__,
                session_id=session_id,
                request_id=request_id,
                status=status,
                duration_ms=_elapsed_ms(start),
            )
            raise
        except BaseException:
            # The client went away: the task was cancelled, or the stream closed.
            aborted = True
            raise
        finally:
            try:
                await response.aclose()
            finally:
                if completed or aborted:
                    meter.close()
                    self._audit.restore(session_id, transformations=restored, request_id=request_id)
                    self._audit.response(
                        status=status,
                        stream=True,
                        frames=frames,
                        restored=restored,
                        session_id=session_id,
                        request_id=request_id,
                        duration_ms=_elapsed_ms(start),
                        aborted=aborted,
                        usage=meter.usage,
                        model=meter.model,
                    )
                    if self._tap is not None and tapped is not None:
                        self._tap.record(
                            session_id,
                            request_id,
                            "response",
                            status=status,
                            stream=True,
                            aborted=aborted,
                            upstream=b"".join(tapped[0]).decode("utf-8", "replace"),
                            restored="".join(tapped[1]),
                        )
                tracing.annotate(restore_span, **{"privyx.restored": restored})
                tracing.end(restore_span, error=failure)
                tracing.annotate(span, **{"privyx.aborted": aborted or None})
                tracing.end(span, error=failure)
                self._end_session(ephemeral, request_id)

    def _relayed_response(
        self,
        response: httpx.Response,
        headers: dict[str, str],
        content_type: str,
        session_id: str,
        request_id: str,
        start: float,
        ephemeral: Session | None,
        span: Any = None,
    ) -> ProxyResponse:
        """Forward a response with nothing to restore as it arrives.

        Read whole first, like a restored one, a download of any size sat in
        memory and reached the client only after its last byte.
        """
        return ProxyResponse(
            status_code=response.status_code,
            headers=headers,
            media_type=content_type or None,
            stream=AuditedStream(
                self._relay(response, session_id, request_id, start, ephemeral, span),
                response=response,
                cleanup=lambda: self._abandon(ephemeral, request_id, span),
            ),
        )

    async def _relay(
        self,
        response: httpx.Response,
        session_id: str,
        request_id: str,
        start: float,
        ephemeral: Session | None,
        span: Any = None,
    ) -> AsyncIterator[bytes]:
        size = 0
        failure: BaseException | None = None
        completed = aborted = False
        try:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                yield chunk
            completed = True
        except Exception as exc:
            failure = exc
            self._audit.error(
                phase="response",
                error_type=type(exc).__name__,
                session_id=session_id,
                request_id=request_id,
                status=response.status_code,
                duration_ms=_elapsed_ms(start),
            )
            raise
        except BaseException:
            aborted = True  # the client went away
            raise
        finally:
            try:
                await response.aclose()
            finally:
                if completed or aborted:
                    self._audit.response(
                        status=response.status_code,
                        stream=False,
                        size=size,
                        restored=0,
                        session_id=session_id,
                        request_id=request_id,
                        duration_ms=_elapsed_ms(start),
                        aborted=aborted,
                    )
                tracing.annotate(span, **{"privyx.aborted": aborted or None})
                tracing.end(span, error=failure)
                self._end_session(ephemeral, request_id)

    def _abandon(self, ephemeral: Session | None, request_id: str, span: Any) -> None:
        """Clean up a reply the client closed before reading any of it.

        Its generator never started, so its ``finally`` does not run either.
        """
        tracing.annotate(span, **{"privyx.aborted": True})
        tracing.end(span)
        self._end_session(ephemeral, request_id)

    def _end_session(self, ephemeral: Session | None, request_id: str) -> None:
        """End the request's ephemeral session, if it has one.

        No I/O, so a cancelled stream cannot interrupt it, and nothing to fail.
        """
        if ephemeral is not None:
            self._engine.end_ephemeral_session(ephemeral, request_id=request_id)

    async def _batch_response(
        self,
        response: httpx.Response,
        headers: dict[str, str],
        content_type: str,
        session_id: str,
        ephemeral: Session | None,
        request_id: str,
        start: float,
        schema: str | None = None,
        span: Any = None,
    ) -> ProxyResponse:
        data = await response.aread()
        await response.aclose()
        restore_span = tracing.start("privyx.restore", span)
        meter = UsageMeter()
        upstream_data = data
        payload = _loads(data)
        meter.feed_json(payload)  # embeddings report usage too
        chunks = schema == "gemini" and isinstance(payload, list)
        if schema != "embeddings" and (isinstance(payload, dict) or chunks):
            try:
                restored: Any
                if chunks:
                    restored = await self._restore_chunks(payload, session_id, ephemeral)
                else:
                    restored = await restore_response(
                        payload, self._engine, session_id, session=ephemeral, schema=schema
                    )
            except BaseException as exc:  # the vault failed: answered with 503
                tracing.end(restore_span, error=exc)
                raise
            data = json.dumps(restored, ensure_ascii=False).encode("utf-8")
        restored_n = self._audit.flush_restore()
        tracing.annotate(restore_span, **{"privyx.restored": restored_n})
        tracing.end(restore_span)
        self._audit.response(
            status=response.status_code,
            stream=False,
            size=len(data),
            restored=restored_n,
            session_id=session_id,
            request_id=request_id,
            duration_ms=_elapsed_ms(start),
            usage=meter.usage,
            model=meter.model,
        )
        if self._tap is not None:
            self._tap.record(
                session_id,
                request_id,
                "response",
                status=response.status_code,
                stream=False,
                upstream=_tapped_body(upstream_data),
                restored=_tapped_body(data),
            )
        return ProxyResponse(
            status_code=response.status_code,
            headers=headers,
            media_type=content_type or None,
            body=data,
        )

    async def _restore_chunks(
        self, chunks: list[Any], session_id: str, ephemeral: Session | None
    ) -> list[Any]:
        """Restore a Gemini stream sent as one JSON array, counted in the audit.

        The array is read whole first: the client gets it at the end, not as it
        is generated.
        """
        stored = ephemeral or await self._engine.vault.get(session_id)
        if stored is None:
            return chunks
        restored, count = restore_chunks(
            chunks, self._engine.operator, self._engine.codec, stored.mapping
        )
        self._audit.restore(session_id, transformations=count)
        return restored


def upstream_failure(exc: BaseException) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Status, headers, and JSON body for an upstream call that got no response.

    Also logs it: the class at WARNING and the traceback only at DEBUG, since a
    dead or slow upstream is an expected condition, not a bug in Privyx.
    """
    name = type(exc).__name__
    _log.warning("no response from the upstream: %s", name)
    _log.debug("upstream request failed", exc_info=exc)
    headers: dict[str, str] = {}
    if isinstance(exc, httpx.PoolTimeout):
        status, kind, message = 503, "busy", "privyx is at proxy.max_connections; retry later"
        headers["retry-after"] = "10"  # the time it already waited for a free connection
    elif isinstance(exc, httpx.TimeoutException):
        status, kind, message = 504, "timeout", "the upstream did not respond in time"
    else:
        status, kind, message = 502, "unreachable", "privyx could not reach the upstream"
    body = {"error": {"type": f"privyx_upstream_{kind}", "message": f"{message} ({name})"}}
    return status, headers, body


def scan_failure(exc: Exception) -> tuple[int, dict[str, Any]]:
    """The status and body answering a request that could not be masked."""
    return (422, LEAK_BODY) if isinstance(exc, LeakError) else (503, SCAN_FAILED_BODY)


def log_scan_failure(exc: Exception) -> None:
    """Log a masking failure: the class at WARNING, the traceback only at DEBUG.

    The message stays out of the console because it may quote request text;
    ``log_file`` at DEBUG, which is owner-only, keeps the full traceback.
    """
    _log.warning("request not forwarded: %s while masking it", type(exc).__name__)
    _log.debug("masking failed", exc_info=exc)


def log_vault_failure(exc: VaultError) -> None:
    """Log a failed vault call: its cause at WARNING, the traceback only at DEBUG."""
    _log.warning("request failed: %s from the session vault", type(exc.__cause__ or exc).__name__)
    _log.debug("vault call failed", exc_info=exc)


def _upstream_path(path: str, raw_path: str | None, prefix: str | None) -> str | None:
    """The path to append to the upstream URL, or ``None`` to refuse the request.

    Without a prefix, the whole path.  With one, the rest after it, cut at a
    segment boundary (``/v1`` takes ``/v1/messages``, not ``/v10/messages``).
    A dot segment, backslash, or percent sign (once decoded), or an encoded
    ``/``, is refused rather than normalized: the joined URL could then leave
    the upstream's own path.
    """
    path = "/" + path.lstrip("/")
    if prefix is None:
        return path
    if (
        any(c in path for c in "\\%?#")
        or any(segment in (".", "..") for segment in path.split("/"))
        or (raw_path is not None and "%2f" in raw_path.lower())
    ):
        return None
    if path != prefix and not path.startswith(prefix + "/"):
        return None
    return path[len(prefix) :]


def _ci_get(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup that works for dicts and Headers objects."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


def _tapped_body(data: bytes) -> Any:
    """A body as the tap keeps it: parsed when it is JSON, else as text."""
    parsed = _loads(data)
    return parsed if parsed is not None else data.decode("utf-8", "replace")


def _loads(data: bytes | str) -> Any:
    try:
        return json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None
