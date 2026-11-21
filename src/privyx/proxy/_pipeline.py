"""The exchange flow: mask a request, send it upstream, restore the reply.

One flow serves both proxy modes; only where a request goes differs, the mode's
routing rule (:mod:`privyx.proxy._upstream`).  The transparent proxy joins the
request path to its upstream and relays the client's own headers, its key or
login included; the gateway serves its routes only, and posts each to the
provider's endpoint for it with the provider's headers alone.

The flow of one exchange (:meth:`Pipeline.handle`):

1. match the path against the configured routes: a masking route has a
   dialect, and any other path is relayed unmasked or refused;
2. on a masking route, parse the body, and find the session;
3. mask the body, the headers that describe the client's machine, and the
   query, then check that no masked value is left: direct calls, so privacy
   never depends on what else is registered;
4. send the request upstream with the provider's credential;
5. restore the reply, structurally for JSON, event by event for a stream, and
   relay anything else as it arrives.

Sessions are resolved from the ``x-privyx-session`` header when present and are
otherwise **ephemeral per request**: a request is pseudonymized and its reply
restored within one exchange, and the client never sees a pseudonym, so a fresh
session per request is correct and — unlike a single shared session — cannot
collide two callers' pseudonym maps.  It stays in memory for that one exchange
and never touches the vault.

:class:`Pipeline` takes an :class:`~privyx.core.Exchange` and returns a
:class:`Reply`, without web framework types: the ASGI adapter
(:mod:`privyx.proxy._asgi`) turns one into the other.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from privyx.config.schema import ProxyConfig
from privyx.core import Exchange, LeakError, Session, VaultError
from privyx.core import request_id as new_request_id
from privyx.engine import PrivacyEngine
from privyx.observability import tracing
from privyx.observability.audit import AuditLogger
from privyx.observability.metrics import AuditStats
from privyx.observability.usage import UsageMeter
from privyx.proxy._headers import filter_request_headers, filter_response_headers
from privyx.proxy._session import resolve_session_id
from privyx.proxy._streaming import AuditedStream
from privyx.proxy._tap import Tap
from privyx.proxy._upstream import (
    header,
    joined_path,
    named_upstream,
    served_routes,
    upstream_client,
    upstream_failure,
    wants_stream,
)
from privyx.proxy.schemas import (
    AUTH_QUERY_PARAMS,
    MASKED_HEADERS,
    detect_schema,
    has_token_ids,
    restore_response,
    transform_request,
)
from privyx.proxy.stream_router import (
    LineRouter,
    StreamRouter,
    resolver_for,
    restore_chunks,
    select_processor_factory,
)
from privyx.streaming.adapters.registry import build_stream_adapter

if TYPE_CHECKING:
    from privyx.providers import Provider

_JSON = "application/json"
_SSE = "text/event-stream"
_NDJSON = "application/x-ndjson"  # Ollama's stream: one JSON object per line
_log = logging.getLogger("privyx.proxy")

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
class Reply:
    """What the pipeline answers an exchange with, for the adapter to send.

    Exactly one of ``body`` (a restored batch response) or ``stream`` (restored
    SSE text, or the bytes of a response with nothing to restore) is set.
    """

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str | None = None
    body: bytes | None = None
    stream: AsyncIterator[str] | AsyncIterator[bytes] | None = None


def _error(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> Reply:
    """A JSON error reply."""
    return Reply(status, headers or {}, _JSON, json.dumps(body).encode())


def _refusal(status: int, message: str) -> Reply:
    """A request Privyx refuses itself, with ``message`` saying why."""
    return _error(status, {"error": {"message": message}})


class Pipeline:
    """The privacy pipeline of one proxy: each exchange masked, sent, and restored.

    Args:
        engine: Configured privacy engine.
        provider: The upstream's provider, owned by the caller.  Its
            :meth:`~privyx.providers.Provider.auth` is the credential sent; the
            transparent proxy writes a key in the header the request path's
            format takes, and adds the configured ``headers``.  ``None`` sends
            no credential of Privyx's.
        gateway: Route as the gateway: only the routes it serves
            (:func:`~privyx.proxy._upstream.served_routes`), each posted to
            ``provider.url()`` with ``provider.headers()``, and none of the
            client's headers or query.  Needs ``provider``.
        origin: The transparent proxy's upstream origin (``scheme://host[:port]``);
            the request path is appended to it.  See
            :func:`privyx.providers.resolve_origin`.  With ``strip_prefix``, the
            upstream URL including its path.
        strip_prefix: When set (``""`` included), the request path must lie
            under this prefix, which is removed before the rest is appended to
            ``origin``.  Paths outside it, or that could leave it, are refused.
            ``None`` appends the whole path (``proxy.path_mapping.mode: origin``).
        routes: Path → dialect map (defaults to ``proxy.routes``' default:
            OpenAI Chat and Responses, Anthropic Messages and token counting).
        passthrough_unknown: Forward paths missing from ``routes`` verbatim.  When
            false they are refused with 403, so nothing reaches the upstream
            unmasked.
        forward_client_auth: Forward the client's ``Authorization`` / ``x-api-key``.
        client: Optional httpx client (owned by the caller when supplied); used by
            tests to inject a mock transport.
        timeout, connect_timeout, max_connections: For the client built when
            ``client`` is not given; see
            :func:`~privyx.proxy._upstream.upstream_client`.
        audit: Optional audit logger for the PII-safe ``proxy.request`` event.
            Defaults to a disabled no-op logger.
        session_strategy: How to identify a session when the client sends no
            ``x-privyx-session`` header — ``ephemeral`` (default), ``client``, or
            ``conversation``.  See :func:`privyx.proxy._session.resolve_session_id`.
        tap: Optional developer tap that records each masked exchange before
            and after masking (``audit.tap``; see :mod:`privyx.proxy._tap`).
        run_token: Set by ``privyx run pi``, whose requests each name their
            upstreams in :data:`~privyx.proxy._headers.UPSTREAMS_HEADER`, with
            this secret in :data:`~privyx.proxy._headers.RUN_TOKEN_HEADER`.  The
            request's route picks the base URL, and the path is appended to it;
            ``origin`` and ``strip_prefix`` are not used.  A request without a
            matching token, or with no base URL for its route, is refused.
    """

    def __init__(
        self,
        engine: PrivacyEngine,
        provider: Provider | None = None,
        *,
        gateway: bool = False,
        origin: str = "",
        strip_prefix: str | None = None,
        routes: dict[str, str] | None = None,
        passthrough_unknown: bool = True,
        forward_client_auth: bool = True,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
        connect_timeout: float = 10.0,
        max_connections: int | None = None,
        audit: AuditLogger | None = None,
        session_strategy: str = "ephemeral",
        tap: Tap | None = None,
        run_token: str | None = None,
    ) -> None:
        if gateway and provider is None:
            raise ValueError("the gateway needs a provider")
        self._engine = engine
        self._provider = provider
        self._gateway = gateway
        self._origin = origin.rstrip("/")
        self._strip_prefix = strip_prefix
        self._run_token = run_token
        self._tap = tap
        self._audit = audit or AuditLogger(None)
        self._session_strategy = session_strategy
        routes = routes if routes is not None else ProxyConfig().routes
        if gateway:
            from privyx.providers import GoogleProvider

            routes = served_routes(routes, google=isinstance(provider, GoogleProvider))
        self._routes = routes
        self._passthrough_unknown = passthrough_unknown and not gateway
        self._forward_client_auth = forward_client_auth and not gateway
        self._owns_client = client is None
        self._client = client or upstream_client(
            timeout=timeout, connect_timeout=connect_timeout, max_connections=max_connections
        )

    @property
    def engine(self) -> PrivacyEngine:
        return self._engine

    @property
    def gateway(self) -> bool:
        """Whether this pipeline routes as the gateway."""
        return self._gateway

    @property
    def routes(self) -> dict[str, str]:
        """The masking routes, path → dialect: for the gateway, the ones it serves."""
        return dict(self._routes)

    @property
    def stats(self) -> AuditStats:
        """Totals of the audit events emitted so far, served at ``GET /metrics``."""
        return self._audit.stats

    async def close(self) -> None:
        """Release the httpx client if this pipeline created it."""
        if self._owns_client:
            await self._client.aclose()

    async def handle(self, exchange: Exchange) -> Reply:
        """Run the exchange through the pipeline and return the :class:`Reply`.

        ``exchange.body`` may be the chunks as they arrive: a routed body is read
        whole to be masked, any other is streamed upstream as it comes.
        """
        exchange.path = "/" + exchange.path.lstrip("/")
        span = tracing.start(
            "privyx.request", headers=exchange.headers, **{"http.request.method": exchange.method}
        )
        try:
            result = await self._handle(exchange, span)
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

    async def _handle(self, exchange: Exchange, span: Any) -> Reply:
        start = time.perf_counter()
        req_id = new_request_id()
        # Correlate every event of this exchange and aggregate its per-leaf
        # transform/restore into one line each; closed in the finally below (the
        # streaming generator carries req_id explicitly, so it needs no scope).
        token = self._audit.begin_request(req_id)
        method, path, headers = exchange.method, exchange.path, exchange.headers
        sid: str | None = None
        ephemeral: Session | None = None
        defer_cleanup = False
        try:
            if header(headers, "upgrade").lower() == "websocket":
                # A WebSocket cannot be relayed here, so its frames could not be
                # masked.  426 makes Codex fall back to HTTP at once; on another
                # status it retries for seconds first, and gives up on a 400.
                return _refusal(426, "privyx: WebSocket is not supported; use HTTP")
            base = self._base(exchange)
            if isinstance(base, Reply):
                return base
            exchange.dialect = dialect = detect_schema(path, self._routes)
            if dialect is not None:
                tracing.annotate(span, **{"http.route": path, "privyx.schema": dialect})
            else:
                self._audit.route_decision(allowed=self._passthrough_unknown, method=method)
                if not self._passthrough_unknown:
                    return _refusal(
                        403,
                        f"privyx: {path} is not in proxy.routes "
                        "and proxy.passthrough_unknown is false",
                    )
                if header(headers, "content-length") not in ("", "0") or header(
                    headers, "transfer-encoding"
                ):
                    self._audit.unmasked_bodies += 1

            body, payload = exchange.body, None
            if dialect is not None:
                if not isinstance(body, bytes):
                    body = b"".join([chunk async for chunk in body])
                # Parse the body once, up front: the session strategy may
                # fingerprint it (e.g. the first user message), and the transform
                # reuses the parse.  Whatever its content type says (`curl -d`
                # sends a form type): a routed body that is not a JSON object
                # would go upstream unmasked.  The gateway's routes all take one.
                payload = _loads(body) if body else None
                if (body or self._gateway) and not isinstance(payload, dict):
                    return _error(400, INVALID_BODY)
                if isinstance(payload, dict) and has_token_ids(payload):
                    return _error(400, TOKEN_IDS_BODY)
                exchange.body = payload
            elif not isinstance(body, bytes) and not (
                header(headers, "content-length") or header(headers, "transfer-encoding")
            ):
                body = b""  # none was sent; an empty stream would go out chunked

            resolved_id, source = resolve_session_id(
                self._session_strategy,
                header_value=header(headers, "x-privyx-session") or None,
                headers=headers,
                payload=payload,
                schema=dialect,
            )
            if source == "ephemeral":
                # It lives for this request only, so it never touches the vault.
                session = ephemeral = self._engine.ephemeral_session()
            else:
                session = await self._engine.get_or_create_session(resolved_id, source=source)
            exchange.session_id = sid = session.session_id
            self._audit.set_session(sid)

            stream = self._gateway and wants_stream(dialect, path, payload, exchange.query)
            out_headers, params, auth = self._outgoing(exchange, stream)
            out_body: Any = body
            transform_ms: float | None = None
            # A masked header comes on body-less requests too, and on unrouted
            # paths.
            if (
                isinstance(payload, dict)
                or not MASKED_HEADERS.isdisjoint(out_headers)
                or params is not None
            ):
                transform_start = time.perf_counter()
                masked = await self._mask(
                    exchange, payload, out_headers, params, ephemeral, span, start, req_id
                )
                if isinstance(masked, Reply):
                    return masked
                transform_ms = _elapsed_ms(transform_start)
                if isinstance(payload, dict):
                    out_body = json.dumps(masked, ensure_ascii=False).encode("utf-8")
                    if self._tap is not None:
                        self._tap.record(
                            sid,
                            req_id,
                            "request",
                            path=path,
                            schema=dialect,
                            before=payload,
                            after=masked,
                        )
            self._audit.flush_transform()

            url = base if base is not None else self._provider_url(path, stream)
            length = header(headers, "content-length")
            if not isinstance(out_body, bytes) and length:
                # Streamed unchanged, so its length still holds; without it
                # httpx would send the body chunked.
                out_headers["content-length"] = length
            request = self._client.build_request(
                method, url, headers=out_headers, content=out_body, params=params
            )
            upstream_span = tracing.start("privyx.upstream", span)
            if dialect is not None:
                self._audit.routed += 1
            try:
                response = await self._client.send(
                    request, stream=True, auth=auth or httpx.USE_CLIENT_DEFAULT
                )
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
                return _error(status, error, error_headers)

            tracing.end(upstream_span)
            resp_content_type = response.headers.get("content-type", "")
            resp_headers = filter_response_headers(response.headers)
            resp_headers["x-privyx-session"] = sid

            # Restored as it streams: SSE, and the JSON lines of an Ollama route.
            lines = dialect == "ollama" and _NDJSON in resp_content_type
            is_stream = (_SSE in resp_content_type or lines) and method.upper() != "HEAD"
            tracing.annotate(span, **{"privyx.stream": is_stream})
            self._audit.request(
                method=method,
                # An unmatched URL may itself contain PII. The route-decision
                # event exposes the gap without copying a client-supplied path.
                path=path if dialect is not None else "[unmatched]",
                schema=dialect,
                status=response.status_code,
                session_id=sid,
                stream=is_stream,
                duration_ms=_elapsed_ms(start),
                upstream=urlsplit(url).netloc,
                request_id=req_id,
                transform_ms=transform_ms,
            )

            if method.upper() == "HEAD":
                await response.aclose()
                if "content-length" in response.headers:
                    resp_headers["content-length"] = response.headers["content-length"]
                return Reply(response.status_code, resp_headers, resp_content_type or None, b"")
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
                mapping = stored.mapping if stored is not None else {}
                return self._stream_reply(
                    response, resp_headers, dialect, sid, mapping, req_id, start, ephemeral, span,
                    lines=lines,
                )  # fmt: skip
            if dialect and _JSON in resp_content_type:
                return await self._batch_reply(
                    response, resp_headers, resp_content_type, sid, ephemeral, req_id, start,
                    dialect=dialect, span=span,
                )  # fmt: skip
            defer_cleanup = True  # as for a stream
            return self._relay_reply(
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
            return _error(503, VAULT_UNAVAILABLE_BODY)
        finally:
            if not defer_cleanup:
                self._end_session(ephemeral, req_id)
            self._audit.end_request(token)

    # -- routing -------------------------------------------------------------

    def _base(self, exchange: Exchange) -> str | Reply | None:
        """The transparent proxy's upstream URL for ``exchange``, or the refusal.

        ``None`` for the gateway, which asks the provider once it knows
        whether the request streams (:meth:`_provider_url`).
        """
        if self._gateway:
            return None
        origin, prefix = self._origin, self._strip_prefix
        if self._run_token is not None:
            named = named_upstream(exchange.headers, exchange.path, self._routes, self._run_token)
            if named is None:
                self._audit.route_decision(
                    allowed=False, method=exchange.method, reason="no_upstream"
                )
                return _refusal(403, "privyx: the request names no upstream for its route")
            origin, prefix = named, ""
        joined = joined_path(exchange.path, exchange.raw_path, prefix)
        if joined is None:
            self._audit.route_decision(
                allowed=False, method=exchange.method, reason="outside_prefix"
            )
            return _refusal(
                404,
                f"privyx: the path is outside proxy.path_mapping.strip_prefix "
                f"{prefix or '/'!r} or could leave it",
            )
        return origin + joined

    def _provider_url(self, path: str, stream: bool) -> str:
        """The gateway's upstream URL: the provider's endpoint for ``path``."""
        assert self._provider is not None  # a gateway has one
        return self._provider.url(path, stream=stream)

    def _outgoing(
        self, exchange: Exchange, stream: bool
    ) -> tuple[dict[str, str], list[tuple[str, Any]] | None, httpx.Auth | None]:
        """The headers, query, and credential to send upstream, before masking.

        The transparent proxy relays the client's headers and query, with
        Privyx's key, when it has one, in the header the request's format takes
        (:func:`~privyx.proxy._headers.filter_request_headers`).  The gateway
        sends the provider's headers and credential only.
        """
        from privyx.providers import ApiKey

        auth = self._provider.auth() if self._provider is not None else None
        if self._gateway:
            assert self._provider is not None
            own = {key.lower(): value for key, value in self._provider.headers().items()}
            own.setdefault("content-type", _JSON)
            if stream:
                own["accept"] = _SSE
            return own, None, auth
        key = auth.key if isinstance(auth, ApiKey) else None
        extra = self._provider.options.headers if self._provider is not None else None
        out_headers = filter_request_headers(
            exchange.headers,
            schema=exchange.dialect,
            forward_client_auth=self._forward_client_auth,
            api_key=key,
            extra=extra,
        )
        params: list[tuple[str, Any]] | None = list(exchange.query) or None
        if params and (auth is not None or not self._forward_client_auth):
            # The client's key in the query (Gemini's `?key=`) goes with its
            # headers: Privyx's key replaces it, or none is relayed.
            params = [(k, v) for k, v in params if k.lower() not in AUTH_QUERY_PARAMS]
        # A key is written into the headers above; another credential is applied
        # as the request goes out, over the body actually sent.
        return out_headers, params, None if key is not None else auth

    async def _mask(
        self,
        exchange: Exchange,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        params: list[tuple[str, Any]] | None,
        ephemeral: Session | None,
        span: Any,
        start: float,
        request_id: str,
    ) -> dict[str, Any] | Reply:
        """The request masked and checked, or the reply refusing it when that fails.

        ``headers`` and ``params``, the ones about to be sent, are masked in place.
        """
        sid = exchange.session_id
        assert sid is not None
        transform_span = tracing.start("privyx.transform", span)
        try:
            transformed = await transform_request(
                payload or {},
                self._engine,
                sid,
                session=ephemeral,
                headers=headers,
                params=params,
                schema=exchange.dialect,
                path=exchange.path,
            )
        except VaultError as exc:
            tracing.end(transform_span, error=exc)
            raise  # not a scan failure: answered by the caller
        except Exception as exc:
            tracing.end(transform_span, error=exc)
            self._audit.error(
                phase="transform",
                error_type=type(exc).__name__,
                session_id=sid,
                request_id=request_id,
                duration_ms=_elapsed_ms(start),
            )
            log_scan_failure(exc)
            status, error = scan_failure(exc)
            return _error(status, error)
        tracing.end(transform_span)
        return transformed

    # -- replies -------------------------------------------------------------

    def _stream_reply(
        self,
        response: httpx.Response,
        headers: dict[str, str],
        dialect: str | None,
        session_id: str,
        mapping: dict[str, str],
        request_id: str,
        start: float,
        ephemeral: Session | None,
        span: Any = None,
        *,
        lines: bool = False,
    ) -> Reply:
        headers.pop("content-type", None)  # media_type carries it, avoid duplicate
        headers.setdefault("cache-control", "no-cache")
        stream_iter = self._stream(
            response, dialect, session_id, mapping, request_id, start, ephemeral, span, lines=lines
        )
        return Reply(
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
        dialect: str | None,
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
        tapped: tuple[list[bytes], list[str]] | None = ([], []) if self._tap and dialect else None
        completed = aborted = False
        try:
            adapter = build_stream_adapter(dialect or "generic")
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

    def _relay_reply(
        self,
        response: httpx.Response,
        headers: dict[str, str],
        content_type: str,
        session_id: str,
        request_id: str,
        start: float,
        ephemeral: Session | None,
        span: Any = None,
    ) -> Reply:
        """Forward a response with nothing to restore as it arrives.

        Read whole first, like a restored one, a download of any size sat in
        memory and reached the client only after its last byte.
        """
        return Reply(
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

    async def _batch_reply(
        self,
        response: httpx.Response,
        headers: dict[str, str],
        content_type: str,
        session_id: str,
        ephemeral: Session | None,
        request_id: str,
        start: float,
        dialect: str | None = None,
        span: Any = None,
    ) -> Reply:
        data = await response.aread()
        await response.aclose()
        restore_span = tracing.start("privyx.restore", span)
        meter = UsageMeter()
        upstream_data = data
        payload = _loads(data)
        meter.feed_json(payload)  # embeddings report usage too
        chunks = dialect == "gemini" and isinstance(payload, list)
        if dialect != "embeddings" and (isinstance(payload, dict) or chunks):
            try:
                restored: Any
                if chunks:
                    restored = await self._restore_chunks(payload, session_id, ephemeral)
                else:
                    restored = await restore_response(
                        payload, self._engine, session_id, session=ephemeral, schema=dialect
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
        return Reply(response.status_code, headers, content_type or None, data)

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


def _tapped_body(data: bytes) -> Any:
    """A body as the tap keeps it: parsed when it is JSON, else as text."""
    parsed = _loads(data)
    return parsed if parsed is not None else data.decode("utf-8", "replace")


def _loads(data: bytes | str) -> Any:
    try:
        return json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return None
