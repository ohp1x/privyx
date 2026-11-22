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
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from privyx.config.schema import ProxyConfig
from privyx.core import (
    ConfigError,
    EventBus,
    Exchange,
    LeakError,
    ListenerError,
    Rejected,
    Responded,
    Stop,
    VaultError,
)
from privyx.core import request_id as new_request_id
from privyx.engine import PrivacyEngine
from privyx.events import Headers, RequestMasked, RequestReceived
from privyx.observability import tracing
from privyx.observability.audit import AuditLogger
from privyx.observability.usage import UsageMeter
from privyx.proxy._headers import filter_request_headers, filter_response_headers
from privyx.proxy._mask import (
    AUTH_QUERY_PARAMS,
    agent_metadata,
    restore_response,
    transform_request,
)
from privyx.proxy._routing import detect_schema
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
)
from privyx.streaming import (
    LineRouter,
    StreamRouter,
    resolver_for,
    restore_chunks,
    select_processor_factory,
)

if TYPE_CHECKING:
    from privyx.dialects import Dialect
    from privyx.providers import Provider

_JSON = "application/json"
_SSE = "text/event-stream"
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

#: Body of the 500 returned when a listener of a request event fails: the
#: exchange is refused, fail-closed.  No exception text: it could quote the request.
LISTENER_FAILED_MESSAGE = (
    "a privyx feature failed on this request, so it was not forwarded; see the privyx logs"
)

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
            ``None`` appends the whole path (``provider.path_mapping.mode: origin``).
        routes: Path → dialect name map (defaults to ``proxy.routes``' default:
            OpenAI Chat and Responses, Anthropic Messages and token counting).
        dialects: The dialect of each name ``routes`` uses; by default, a new
            one of each built-in.
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
            and after masking (``proxy.tap``; see :mod:`privyx.proxy._tap`).
        events: The bus the request events go to, the app's: a listener may
            change a request, refuse it, or answer it.
        metadata_keys, masked_headers: The request keys and headers in which
            clients describe the machine they run on, masked whole: the union
            of every agent type's (:class:`~privyx.agents.Agent`); by
            default, the built-in types'.
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
        metadata_keys: frozenset[str] | None = None,
        masked_headers: frozenset[str] | None = None,
        dialects: Mapping[str, Dialect] | None = None,
        events: EventBus | None = None,
    ) -> None:
        from privyx.dialects import builtins

        if gateway and provider is None:
            raise ValueError("the gateway needs a provider")
        self._engine = engine
        self._provider = provider
        self._gateway = gateway
        self._origin = origin.rstrip("/")
        self._strip_prefix = strip_prefix
        self._run_token = run_token
        self._metadata_keys, self._masked_headers = agent_metadata(metadata_keys, masked_headers)
        self._events = events or EventBus()
        self._tap = tap
        self._audit = audit or AuditLogger()
        self._session_strategy = session_strategy
        routes = routes if routes is not None else ProxyConfig().routes
        self._dialects = dict(dialects) if dialects is not None else builtins()
        unknown = sorted(set(routes.values()) - self._dialects.keys())
        if unknown:
            raise ConfigError(f"proxy.routes names unknown dialect(s): {', '.join(unknown)}")
        # Whose paths may come without their API version.
        self._versionless = frozenset(n for n, d in self._dialects.items() if d.versionless)
        if gateway:
            from privyx.providers import GoogleProvider

            google = isinstance(provider, GoogleProvider)
            routes = served_routes(routes, self._dialects, google=google)
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
    def audit(self) -> AuditLogger:
        """The audit logger the pipeline emits its notifications with."""
        return self._audit

    @property
    def versionless(self) -> frozenset[str]:
        """The dialects whose paths may come without their API version."""
        return self._versionless

    @property
    def routes(self) -> dict[str, str]:
        """The masking routes, path → dialect: for the gateway, the ones it serves."""
        return dict(self._routes)

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
        span = exchange.span = tracing.start(
            "privyx.request", headers=exchange.headers, **{"http.request.method": exchange.method}
        )
        try:
            result = await self._scoped(exchange)
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

    async def _scoped(self, exchange: Exchange) -> Reply:
        """The exchange in its audit scope; its session ends here unless its reply streams."""
        exchange.started = time.perf_counter()
        exchange.request_id = new_request_id()
        # Correlate every event of this exchange and aggregate its per-leaf
        # transform/restore into one line each; closed in the finally below (the
        # streaming generator carries the request id itself, so it needs no scope).
        token = self._audit.begin_request(exchange.request_id)
        reply: Reply | None = None
        try:
            reply = await self._flow(exchange)
            return reply
        except VaultError as exc:
            tracing.annotate(exchange.span, **{"error.type": type(exc).__name__})
            self._audit.error(
                phase="vault",
                error_type=type(exc).__name__,
                session_id=exchange.session_id,
                request_id=exchange.request_id,
                duration_ms=_elapsed_ms(exchange.started),
            )
            log_vault_failure(exc)
            return _error(503, VAULT_UNAVAILABLE_BODY)
        finally:
            # A reply that streams owns the cleanup: FastAPI/client cancellation
            # closes its generator even when nothing drains the upstream stream.
            if reply is None or reply.stream is None:
                self._end_session(exchange)
            self._audit.end_request(token)

    async def _flow(self, exchange: Exchange) -> Reply:
        """Route, mask, send, and restore: the steps in this module's docstring."""
        if header(exchange.headers, "upgrade").lower() == "websocket":
            # A WebSocket cannot be relayed here, so its frames could not be
            # masked.  426 makes Codex fall back to HTTP at once; on another
            # status it retries for seconds first, and gives up on a 400.
            return _refusal(426, "privyx: WebSocket is not supported; use HTTP")
        base = self._base(exchange)
        if isinstance(base, Reply):
            return base
        schema = detect_schema(exchange.path, self._routes, self._versionless)
        refused = self._route(exchange, schema)
        if refused is not None:
            return refused
        read = await self._read_body(exchange)
        if isinstance(read, Reply):
            return read
        body, payload = read
        sid = await self._open_session(exchange, payload)
        dialect = exchange.dialect
        if dialect is not None and isinstance(payload, dict):
            answered = await self._received(exchange, payload)
            if answered is not None:
                return answered
            payload = exchange.body

        stream = (
            self._gateway
            and dialect is not None
            and dialect.wants_stream(exchange.path, payload, exchange.query)
        )
        out_headers, params, auth = self._outgoing(exchange, stream)
        out_body: Any = body
        transform_ms: float | None = None
        # A masked header comes on body-less requests too, and on unrouted
        # paths.
        if (
            isinstance(payload, dict)
            or not self._masked_headers.isdisjoint(out_headers)
            or params is not None
        ):
            transform_start = time.perf_counter()
            masked = await self._mask(exchange, payload, out_headers, params)
            if isinstance(masked, Reply):
                return masked
            transform_ms = _elapsed_ms(transform_start)
            if isinstance(payload, dict):
                out_body = json.dumps(masked, ensure_ascii=False).encode("utf-8")
                if self._tap is not None:
                    self._tap.record(
                        sid,
                        exchange.request_id,
                        "request",
                        path=exchange.path,
                        schema=schema,
                        before=payload,
                        after=masked,
                    )
        self._audit.flush_transform()

        url = base if base is not None else self._provider_url(exchange.path, stream)
        response = await self._send(exchange, url, out_headers, out_body, params, auth)
        if isinstance(response, Reply):
            return response
        return await self._answer(exchange, response, schema, url, transform_ms)

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
            named = named_upstream(
                exchange.headers, exchange.path, self._routes, self._run_token, self._versionless
            )
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
                f"privyx: the path is outside provider.path_mapping.strip_prefix "
                f"{prefix or '/'!r} or could leave it",
            )
        return origin + joined

    def _route(self, exchange: Exchange, schema: str | None) -> Reply | None:
        """Give ``exchange`` the dialect of its route; the refusal of a path without one."""
        if schema is not None:
            exchange.dialect = self._dialects[schema]
            tracing.annotate(
                exchange.span, **{"http.route": exchange.path, "privyx.schema": schema}
            )
            return None
        self._audit.route_decision(allowed=self._passthrough_unknown, method=exchange.method)
        if not self._passthrough_unknown:
            return _refusal(
                403,
                f"privyx: {exchange.path} is not in proxy.routes "
                "and proxy.passthrough_unknown is false",
            )
        if header(exchange.headers, "content-length") not in ("", "0") or header(
            exchange.headers, "transfer-encoding"
        ):
            self._audit.unmasked_bodies += 1
        return None

    async def _read_body(self, exchange: Exchange) -> tuple[Any, Any] | Reply:
        """The body to send and, on a masking route, its JSON; or the refusal of the body."""
        body, dialect = exchange.body, exchange.dialect
        if dialect is None:
            if not isinstance(body, bytes) and not (
                header(exchange.headers, "content-length")
                or header(exchange.headers, "transfer-encoding")
            ):
                body = b""  # none was sent; an empty stream would go out chunked
            return body, None
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
        refused = dialect.refuse(payload) if isinstance(payload, dict) else None
        if refused is not None:
            return _error(400, dialect.error_body(400, "privyx_invalid_request", refused))
        exchange.body = payload
        return body, payload

    async def _open_session(self, exchange: Exchange, payload: Any) -> str:
        """Give ``exchange`` its session, and return the session's id."""
        resolved_id, source = resolve_session_id(
            self._session_strategy,
            header_value=header(exchange.headers, "x-privyx-session") or None,
            headers=exchange.headers,
            payload=payload,
            dialect=exchange.dialect,
        )
        if source == "ephemeral":
            # It lives for this request only, so it never touches the vault.
            session = exchange.ephemeral = self._engine.ephemeral_session()
        else:
            session = await self._engine.get_or_create_session(resolved_id, source=source)
        exchange.session_id = session.session_id
        self._audit.set_session(session.session_id)
        return session.session_id

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
            dialect=exchange.dialect,
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

    async def _received(self, exchange: Exchange, payload: dict[str, Any]) -> Reply | None:
        """Emit :class:`~privyx.events.RequestReceived`; the reply when a listener ends it.

        A listener may change the body and the headers in place; the exchange
        takes them.
        """
        if not self._events.wants(RequestReceived):
            return None
        dialect, sid = exchange.dialect, exchange.session_id
        assert dialect is not None and sid is not None
        headers = {key.lower(): value for key, value in exchange.headers.items()}
        event = RequestReceived(
            exchange.method, exchange.path, Headers(headers), payload, dialect, sid,
            exchange.request_id,
        )  # fmt: skip
        try:
            await self._events.emit(event)
        except Stop as stop:
            return self._stopped(stop, dialect)
        except ListenerError as exc:
            return self._listener_failed(exc, exchange)
        exchange.headers, exchange.body = headers, event.body
        return None

    def _stopped(self, stop: Stop, dialect: Dialect) -> Reply:
        """The reply of a listener that refused the exchange, or answered it as it is."""
        if isinstance(stop, Rejected):
            return _error(stop.status, dialect.error_body(stop.status, stop.code, stop.message))
        assert isinstance(stop, Responded)
        return Reply(stop.status, stop.headers, _JSON, json.dumps(stop.body).encode())

    def _listener_failed(self, exc: ListenerError, exchange: Exchange) -> Reply:
        """Refuse an exchange whose listener failed: fail-closed, the listener named."""
        self._audit.error(
            phase="listener",
            error_type=type(exc.__cause__ or exc).__name__,
            session_id=exchange.session_id,
            request_id=exchange.request_id,
            duration_ms=_elapsed_ms(exchange.started),
            listener=exc.listener,
        )
        _log.warning("request not forwarded: %s failed", exc.listener)
        return _error(
            500, {"error": {"type": "privyx_listener_failed", "message": LISTENER_FAILED_MESSAGE}}
        )

    async def _mask(
        self,
        exchange: Exchange,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        params: list[tuple[str, Any]] | None,
    ) -> dict[str, Any] | Reply:
        """The request masked and checked, or the reply refusing it when that fails.

        ``headers`` and ``params``, the ones about to be sent, are masked in place.
        """
        sid = exchange.session_id
        assert sid is not None
        dialect = exchange.dialect
        hook = None
        if dialect is not None and isinstance(payload, dict) and self._events.wants(RequestMasked):

            async def hook(body: dict[str, Any]) -> None:
                assert dialect is not None and sid is not None
                event = RequestMasked(
                    exchange.method, exchange.path, Headers(headers), body, dialect, sid,
                    exchange.request_id,
                )  # fmt: skip
                await self._events.emit(event)

        transform_span = tracing.start("privyx.transform", exchange.span)
        try:
            transformed = await transform_request(
                payload or {},
                self._engine,
                sid,
                session=exchange.ephemeral,
                headers=headers,
                params=params,
                dialect=exchange.dialect,
                metadata_keys=self._metadata_keys,
                masked_headers=self._masked_headers,
                masked=hook,
            )
        except VaultError as exc:
            tracing.end(transform_span, error=exc)
            raise  # not a scan failure: answered by the caller
        except Stop as stop:
            tracing.end(transform_span)
            assert dialect is not None
            if isinstance(stop, Responded) and isinstance(stop.body, dict):
                # Answered after masking: restored as an upstream reply is.
                stop.body = await restore_response(
                    stop.body, self._engine, sid, session=exchange.ephemeral, dialect=dialect
                )
            return self._stopped(stop, dialect)
        except ListenerError as exc:
            tracing.end(transform_span, error=exc)
            return self._listener_failed(exc, exchange)
        except Exception as exc:
            tracing.end(transform_span, error=exc)
            self._audit.error(
                phase="transform",
                error_type=type(exc).__name__,
                session_id=sid,
                request_id=exchange.request_id,
                duration_ms=_elapsed_ms(exchange.started),
            )
            log_scan_failure(exc)
            status, error = scan_failure(exc)
            return _error(status, error)
        tracing.end(transform_span)
        return transformed

    async def _send(
        self,
        exchange: Exchange,
        url: str,
        headers: dict[str, str],
        body: Any,
        params: list[tuple[str, Any]] | None,
        auth: httpx.Auth | None,
    ) -> httpx.Response | Reply:
        """Send the masked request upstream; the reply when no response comes."""
        length = header(exchange.headers, "content-length")
        if not isinstance(body, bytes) and length:
            # Streamed unchanged, so its length still holds; without it
            # httpx would send the body chunked.
            headers["content-length"] = length
        request = self._client.build_request(
            exchange.method, url, headers=headers, content=body, params=params
        )
        upstream_span = tracing.start("privyx.upstream", exchange.span)
        if exchange.dialect is not None:
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
                session_id=exchange.session_id,
                request_id=exchange.request_id,
                duration_ms=_elapsed_ms(exchange.started),
            )
            if not isinstance(exc, httpx.RequestError):
                raise
            status, error_headers, error = upstream_failure(exc)
            return _error(status, error, error_headers)
        tracing.end(upstream_span)
        return response

    # -- replies -------------------------------------------------------------

    async def _answer(
        self,
        exchange: Exchange,
        response: httpx.Response,
        schema: str | None,
        url: str,
        transform_ms: float | None,
    ) -> Reply:
        """The reply to ``response``: restored as it streams, restored whole, or relayed."""
        dialect, sid = exchange.dialect, exchange.session_id
        assert sid is not None
        content_type = response.headers.get("content-type", "")
        headers = filter_response_headers(response.headers)
        headers["x-privyx-session"] = sid

        # Restored as it streams: SSE, and the JSON lines of a format that streams them.
        line_type = dialect.line_stream if dialect is not None else None
        lines = line_type if line_type is not None and line_type in content_type else None
        is_stream = (_SSE in content_type or lines is not None) and (
            exchange.method.upper() != "HEAD"
        )
        tracing.annotate(exchange.span, **{"privyx.stream": is_stream})
        self._audit.request(
            method=exchange.method,
            # An unmatched URL may itself contain PII. The route-decision
            # event exposes the gap without copying a client-supplied path.
            path=exchange.path if dialect is not None else "[unmatched]",
            schema=schema,
            status=response.status_code,
            session_id=sid,
            stream=is_stream,
            duration_ms=_elapsed_ms(exchange.started),
            upstream=urlsplit(url).netloc,
            request_id=exchange.request_id,
            transform_ms=transform_ms,
        )

        if exchange.method.upper() == "HEAD":
            await response.aclose()
            if "content-length" in response.headers:
                headers["content-length"] = response.headers["content-length"]
            return Reply(response.status_code, headers, content_type or None, b"")
        if is_stream:
            try:
                # Read before the 200 goes out, so a failing vault is a 503.
                stored = exchange.ephemeral or await self._engine.vault.get(sid)
            except VaultError:
                await response.aclose()
                raise
            mapping = stored.mapping if stored is not None else {}
            headers.pop("content-type", None)  # media_type carries it, avoid duplicate
            headers.setdefault("cache-control", "no-cache")
            restoring = self._stream(exchange, response, mapping, lines=lines)
            return self._streaming(exchange, response, headers, lines or _SSE, restoring)
        if dialect is not None and _JSON in content_type:
            return await self._batch_reply(exchange, response, headers, content_type)
        # Nothing to restore: forwarded as it arrives.  Read whole first, like a
        # restored one, a download of any size sat in memory and reached the
        # client only after its last byte.
        relaying = self._relay(exchange, response)
        return self._streaming(exchange, response, headers, content_type or None, relaying)

    def _streaming(
        self,
        exchange: Exchange,
        response: httpx.Response,
        headers: dict[str, str],
        media_type: str | None,
        chunks: AsyncIterator[Any],
    ) -> Reply:
        """A reply sent as ``chunks`` arrive, cleaned up however the client leaves it."""
        return Reply(
            status_code=response.status_code,
            headers=headers,
            media_type=media_type,
            stream=AuditedStream(
                chunks, response=response, cleanup=lambda: self._abandon(exchange)
            ),
        )

    @asynccontextmanager
    async def _delivering(
        self,
        exchange: Exchange,
        response: httpx.Response,
        phase: str,
        ended: Callable[[BaseException | None, bool], None],
    ) -> AsyncIterator[None]:
        """Deliver a reply in the block, then close ``response`` and end the exchange.

        ``ended(failure, aborted)`` hears how it went: ``failure`` is what
        broke the reply, and ``aborted`` says the client went away first.
        """
        failure: BaseException | None = None
        aborted = False
        try:
            yield
        except Exception as exc:
            failure = exc
            self._audit.error(
                phase=phase,
                error_type=type(exc).__name__,
                session_id=exchange.session_id,
                request_id=exchange.request_id,
                status=response.status_code,
                duration_ms=_elapsed_ms(exchange.started),
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
                ended(failure, aborted)
                tracing.annotate(exchange.span, **{"privyx.aborted": aborted or None})
                tracing.end(exchange.span, error=failure)
                self._end_session(exchange)

    async def _stream(
        self,
        exchange: Exchange,
        response: httpx.Response,
        mapping: dict[str, str],
        *,
        lines: str | None = None,
    ) -> AsyncIterator[str]:
        """Restore a streamed reply: SSE events, or with ``lines`` JSON lines."""
        dialect, sid = exchange.dialect, exchange.session_id
        assert sid is not None
        restore_span = tracing.start("privyx.restore", exchange.span)
        status = response.status_code
        frames = 0
        restored = _Restored()
        meter = UsageMeter()
        # What the tap keeps, when it is on: the upstream's bytes and ours.
        tapped: tuple[list[bytes], list[str]] | None = ([], []) if self._tap and dialect else None

        def ended(failure: BaseException | None, aborted: bool) -> None:
            if failure is None:
                meter.close()
                self._audit.restore(
                    sid, transformations=restored.count, request_id=exchange.request_id
                )
                self._audit.response(
                    status=status,
                    stream=True,
                    frames=frames,
                    restored=restored.count,
                    session_id=sid,
                    request_id=exchange.request_id,
                    duration_ms=_elapsed_ms(exchange.started),
                    aborted=aborted,
                    usage=meter.usage,
                    model=meter.model,
                )
                if self._tap is not None and tapped is not None:
                    self._tap.record(
                        sid,
                        exchange.request_id,
                        "response",
                        status=status,
                        stream=True,
                        aborted=aborted,
                        upstream=b"".join(tapped[0]).decode("utf-8", "replace"),
                        restored="".join(tapped[1]),
                    )
            tracing.annotate(restore_span, **{"privyx.restored": restored.count})
            tracing.end(restore_span, error=failure)

        async with self._delivering(exchange, response, "stream", ended):
            # Count restores by wrapping the resolver: a non-None lookup is one
            # pseudonym reversed.  (Covers the codec path — pseudonym/hash/encrypt;
            # faker's literal trie restore does not consult ``resolve``.)
            resolve = restored.counting(resolver_for(self._engine.operator, mapping))
            codec = self._engine.codec
            make = select_processor_factory(self._engine.operator, codec, resolve, mapping)
            router: StreamRouter | LineRouter
            if lines and dialect is not None:
                objects = _objects(dialect, make)
                if objects is None:
                    raise TypeError(f"dialect {dialect.name!r} streams objects it cannot restore")
                router = LineRouter(objects)
            elif dialect is not None:
                router = dialect.stream(codec, resolve, make)
            else:
                router = StreamRouter(codec, resolve, make)
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

    async def _relay(self, exchange: Exchange, response: httpx.Response) -> AsyncIterator[bytes]:
        size = 0

        def ended(failure: BaseException | None, aborted: bool) -> None:
            if failure is None:
                self._audit.response(
                    status=response.status_code,
                    stream=False,
                    size=size,
                    restored=0,
                    session_id=exchange.session_id,
                    request_id=exchange.request_id,
                    duration_ms=_elapsed_ms(exchange.started),
                    aborted=aborted,
                )

        async with self._delivering(exchange, response, "response", ended):
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                yield chunk

    def _abandon(self, exchange: Exchange) -> None:
        """Clean up a reply the client closed before reading any of it.

        Its generator never started, so its ``finally`` does not run either.
        """
        tracing.annotate(exchange.span, **{"privyx.aborted": True})
        tracing.end(exchange.span)
        self._end_session(exchange)

    def _end_session(self, exchange: Exchange) -> None:
        """End the request's ephemeral session, if it has one.

        No I/O, so a cancelled stream cannot interrupt it, and nothing to fail.
        """
        if exchange.ephemeral is not None:
            self._engine.end_ephemeral_session(exchange.ephemeral, request_id=exchange.request_id)

    async def _batch_reply(
        self,
        exchange: Exchange,
        response: httpx.Response,
        headers: dict[str, str],
        content_type: str,
    ) -> Reply:
        dialect, sid = exchange.dialect, exchange.session_id
        assert dialect is not None and sid is not None
        data = await response.aread()
        await response.aclose()
        restore_span = tracing.start("privyx.restore", exchange.span)
        meter = UsageMeter()
        upstream_data = data
        payload = _loads(data)
        meter.feed_json(payload)  # embeddings report usage too
        if dialect.restores_reply and isinstance(payload, dict | list):
            try:
                restored: Any
                if isinstance(payload, list):
                    # A stream sent as one JSON array of chunks (Gemini's without SSE).
                    restored = await self._restore_chunks(exchange, payload)
                else:
                    restored = await restore_response(
                        payload, self._engine, sid, session=exchange.ephemeral, dialect=dialect
                    )
            except BaseException as exc:  # the vault failed: answered with 503
                tracing.end(restore_span, error=exc)
                raise
            if restored is not None:
                data = json.dumps(restored, ensure_ascii=False).encode("utf-8")
        restored_n = self._audit.flush_restore()
        tracing.annotate(restore_span, **{"privyx.restored": restored_n})
        tracing.end(restore_span)
        self._audit.response(
            status=response.status_code,
            stream=False,
            size=len(data),
            restored=restored_n,
            session_id=sid,
            request_id=exchange.request_id,
            duration_ms=_elapsed_ms(exchange.started),
            usage=meter.usage,
            model=meter.model,
        )
        if self._tap is not None:
            self._tap.record(
                sid,
                exchange.request_id,
                "response",
                status=response.status_code,
                stream=False,
                upstream=_tapped_body(upstream_data),
                restored=_tapped_body(data),
            )
        return Reply(response.status_code, headers, content_type or None, data)

    async def _restore_chunks(self, exchange: Exchange, chunks: list[Any]) -> list[Any] | None:
        """Restore a stream sent as one JSON array (Gemini's), counted in the audit.

        The array is read whole first: the client gets it at the end, not as it
        is generated.  ``None`` for a format that sends no such stream: the body
        is relayed as it came.
        """
        dialect, sid = exchange.dialect, exchange.session_id
        assert dialect is not None and sid is not None
        stored = exchange.ephemeral or await self._engine.vault.get(sid)
        operator, mapping = self._engine.operator, stored.mapping if stored is not None else {}
        counted = _Restored()
        resolve = counted.counting(resolver_for(operator, mapping))
        make = select_processor_factory(operator, self._engine.codec, resolve, mapping)
        objects = _objects(dialect, make)
        if objects is None:
            return None
        if stored is None:
            return chunks
        restored = restore_chunks(chunks, objects)
        self._audit.restore(sid, transformations=counted.count)
        return restored


class _Restored:
    """How many pseudonyms a resolver reversed."""

    def __init__(self) -> None:
        self.count = 0

    def counting(self, resolve: Callable[[str], str | None]) -> Callable[[str], str | None]:
        """``resolve``, counting each token it finds a value for."""

        def counted(token: str) -> str | None:
            original = resolve(token)
            self.count += original is not None
            return original

        return counted


def _objects(dialect: Dialect, make: Any) -> Any:
    """``dialect``'s restorer of whole JSON objects, with ``make``'s for whole strings.

    ``None`` for a format that sends none.
    """
    full = make()
    return dialect.objects(make, lambda text: full.feed(text) + full.flush())


def scan_failure(exc: Exception) -> tuple[int, dict[str, Any]]:
    """The status and body answering a request that could not be masked."""
    return (422, LEAK_BODY) if isinstance(exc, LeakError) else (503, SCAN_FAILED_BODY)


def log_scan_failure(exc: Exception) -> None:
    """Log a masking failure: the class at WARNING, the traceback only at DEBUG.

    The message stays out of the console because it may quote request text;
    ``log.file`` at DEBUG, which is owner-only, keeps the full traceback.
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
