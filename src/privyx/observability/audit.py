"""PII-safe audit notifications, emitted as an exchange goes.

Security-relevant facts — session creation, transform, restore, and proxy
request/response/error — become notifications on the app's event bus
(:mod:`privyx.events`), each one record of the audit trail: the ``audit``
feature writes them as JSON lines, and the ``metrics`` feature counts them.

- ``request_id`` correlates every event of one proxied exchange; ``null`` for
  standalone library / CLI calls that run outside a request scope.
- ``event`` is the name of an :class:`~privyx.events.AuditEvent` class: those
  classes are the vocabulary.

The trail is **PII-safe by construction**: the typed helpers accept only entity
*types*, *counts*, and non-PII scalars.  Raw detected text, original values, and
pseudonyms never reach this module — :meth:`AuditLogger.transform` takes a
pre-computed ``{entity_type: count}`` histogram, not spans, and
:meth:`AuditLogger.error` records an exception's *class name*, never its message.
This is the "counts, not content" guarantee documented in
``docs/security/data-handling.md``.

**Per-request aggregation.**  A proxied request pseudonymizes many text leaves,
each a separate :meth:`transform` call.  Emitting one line per leaf buries the
signal, so within a request scope (opened by the proxy via :meth:`begin_request`)
transform/restore calls *accumulate* and flush a single ``session.transform`` and
``session.restore`` per exchange.  Outside a scope the calls emit immediately, so
library and ``privyx mask`` use is unchanged.

A listener that fails is logged and the others still run, so an audit
failure can never break a request.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from privyx.core import EventBus
from privyx.events import (
    SCHEMA_VERSION,
    AuditEvent,
    ProxyBlocked,
    ProxyError,
    ProxyPassthrough,
    ProxyRequest,
    ProxyResponse,
    SessionCreated,
    SessionDeleted,
    SessionRestore,
    SessionTransform,
)

__all__ = ["SCHEMA_VERSION", "AuditEvent", "AuditLogger"]


@dataclass(slots=True)
class _RequestScope:
    """Per-request correlation id plus the transform/restore accumulators.

    Held in a :class:`~contextvars.ContextVar` (task-local, so concurrent
    requests never share one) for the duration of a proxied exchange.
    """

    request_id: str
    session_id: str | None = None
    transform_counts: dict[str, int] = field(default_factory=dict)
    detector_counts: dict[str, int] = field(default_factory=dict)
    transform_n: int = 0
    restore_n: int = 0


#: Active request scope, or ``None`` outside a proxied exchange.
_scope: ContextVar[_RequestScope | None] = ContextVar("privyx_audit_scope", default=None)


class AuditLogger:
    """Emit PII-safe audit notifications to an event bus.

    Args:
        events: The bus the notifications go to, the app's.  Without it, every
            method is a no-op, so callers need no ``if audit:`` guards.
    """

    def __init__(self, *, events: EventBus | None = None) -> None:
        self._events = events
        #: Requests sent upstream on a masking route, counted as they go out,
        #: so one still awaiting its response counts too.
        self.routed = 0
        #: Requests forwarded with a body on a path outside the masking routes.
        self.unmasked_bodies = 0

    def emit(
        self,
        event: type[AuditEvent],
        *,
        session_id: str | None = None,
        request_id: str | None = None,
        **fields: Any,
    ) -> None:
        """Emit one event.  Never raises into the caller.

        ``request_id`` / ``session_id`` fall back to the active request scope when
        not given, so an event emitted deep in the engine still correlates.
        """
        if self._events is None or not self._events.wants(event):
            return  # nobody hears it: no record is built
        scope = _scope.get()
        if scope is not None:
            if request_id is None:
                request_id = scope.request_id
            if session_id is None:
                session_id = scope.session_id
        self._events.notify(
            event(
                timestamp=time.time(), session_id=session_id, request_id=request_id, fields=fields
            )
        )

    # -- request correlation / aggregation --------------------------------

    def begin_request(self, request_id: str, *, session_id: str | None = None) -> Token[Any]:
        """Open a request scope so this exchange's events share ``request_id``.

        Returns a token to pass back to :meth:`end_request`.  Within the scope,
        :meth:`transform` / :meth:`restore` accumulate instead of writing.
        """
        return _scope.set(_RequestScope(request_id=request_id, session_id=session_id))

    def end_request(self, token: Token[Any]) -> None:
        """Flush any pending aggregates and close the request scope."""
        scope = _scope.get()
        if scope is not None:
            self.flush_transform()
            self.flush_restore()
        try:
            _scope.reset(token)
        except ValueError:  # pragma: no cover - token from another context
            _scope.set(None)

    def set_session(self, session_id: str) -> None:
        """Record the resolved session id on the active scope, if any."""
        scope = _scope.get()
        if scope is not None:
            scope.session_id = session_id

    def flush_transform(self) -> int:
        """Emit the scope's aggregated ``session.transform`` and return its count."""
        scope = _scope.get()
        if scope is None or (scope.transform_n == 0 and not scope.detector_counts):
            return 0
        total = scope.transform_n
        self.emit(
            SessionTransform,
            session_id=scope.session_id,
            entity_counts=dict(scope.transform_counts),
            transformations=total,
            **_present(detector_counts=scope.detector_counts or None),
        )
        scope.transform_counts = {}
        scope.detector_counts = {}
        scope.transform_n = 0
        return total

    def flush_restore(self) -> int:
        """Emit the scope's aggregated ``session.restore`` and return its count."""
        scope = _scope.get()
        if scope is None or scope.restore_n == 0:
            return 0
        total = scope.restore_n
        self.emit(
            SessionRestore,
            session_id=scope.session_id,
            transformations=total,
        )
        scope.restore_n = 0
        return total

    # -- typed, PII-safe helpers ------------------------------------------

    def session_created(self, session_id: str, *, source: str = "ephemeral") -> None:
        """A new session was created.

        ``source`` records how its id was chosen — ``header`` (the client sent
        ``x-privyx-session``), ``client`` / ``conversation`` (derived by the proxy
        from the request; see :mod:`privyx.proxy.session`), or ``ephemeral`` (a
        fresh per-request id).
        """
        self.emit(SessionCreated, session_id=session_id, source=source)

    def session_deleted(
        self,
        session_id: str,
        *,
        reason: str,
        mapping_count: int,
        request_id: str | None = None,
    ) -> None:
        """A session was successfully removed from the vault.

        ``mapping_count`` is a count only; mapping keys and original values never
        enter the audit trail.  Callers invoke this only after the vault confirms
        deletion, so the event never claims that a failed cleanup succeeded.
        """
        self.emit(
            SessionDeleted,
            session_id=session_id,
            request_id=request_id,
            reason=reason,
            mapping_count=mapping_count,
        )

    def transform(
        self,
        session_id: str,
        *,
        entity_counts: dict[str, int],
        transformations: int,
        detector_counts: Mapping[str, int] | None = None,
    ) -> None:
        """Text was pseudonymized.

        Args:
            entity_counts: ``{entity_type: count}`` histogram — types and counts
                only, never the matched text.
            transformations: Number of replacements applied.
            detector_counts: What the detectors reported doing (``llm_calls``,
                ``llm_input_tokens``, ``llm_fallbacks``, ...); see
                :attr:`privyx.core.Context.counters`.

        Within a request scope the counts accumulate into one ``session.transform``
        per exchange (flushed by :meth:`flush_transform`); outside a scope the event
        is written immediately.  A no-op transform (``transformations == 0``) is
        not recorded — the bulk of a request is untouched text, and one line per
        skipped field buries the events that matter — unless a detector reported
        counts: an LLM call costs tokens and a fallback must be seen even when
        nothing was masked.
        """
        counters = {k: v for k, v in (detector_counts or {}).items() if v}
        if not transformations and not counters:
            return
        scope = _scope.get()
        if scope is not None:
            for entity_type, count in entity_counts.items():
                scope.transform_counts[entity_type] = (
                    scope.transform_counts.get(entity_type, 0) + count
                )
            for name, count in counters.items():
                scope.detector_counts[name] = scope.detector_counts.get(name, 0) + count
            scope.transform_n += transformations
            if scope.session_id is None:
                scope.session_id = session_id
            return
        self.emit(
            SessionTransform,
            session_id=session_id,
            entity_counts=entity_counts,
            transformations=transformations,
            **_present(detector_counts=counters or None),
        )

    def restore(
        self, session_id: str, *, transformations: int, request_id: str | None = None
    ) -> None:
        """Pseudonyms were reversed in a response.

        Accumulates within a request scope (one ``session.restore`` per exchange,
        flushed by :meth:`flush_restore`); a no-op restore is not recorded.  The
        streaming path restores outside a scope and passes ``request_id``
        explicitly so the event still correlates.
        """
        if not transformations:
            return
        scope = _scope.get()
        if scope is not None:
            scope.restore_n += transformations
            if scope.session_id is None:
                scope.session_id = session_id
            return
        self.emit(
            SessionRestore,
            session_id=session_id,
            request_id=request_id,
            transformations=transformations,
        )

    def route_decision(self, *, allowed: bool, method: str, reason: str = "unknown_route") -> None:
        """An unknown route was allowed or blocked, without recording its URL.

        Paths, query strings, headers and bodies can all hold secrets. Only the
        decision and an HTTP method from a fixed vocabulary enter this event.
        The active request scope supplies correlation with later events.
        """
        safe_method = method.upper()
        if safe_method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            safe_method = "OTHER"
        self.emit(
            ProxyPassthrough if allowed else ProxyBlocked,
            method=safe_method,
            reason=reason,
            body_masked=False,
        )

    def request(
        self,
        *,
        method: str,
        path: str,
        schema: str | None,
        status: int,
        session_id: str | None,
        stream: bool,
        duration_ms: float,
        upstream: str,
        request_id: str | None = None,
        transform_ms: float | None = None,
    ) -> None:
        """The upstream responded with headers.

        ``duration_ms`` is time to the upstream *response headers* (time to first
        byte), not the whole stream — :meth:`response` records the total.
        ``transform_ms`` is the part of it spent masking the request, when a
        body was masked.
        """
        self.emit(
            ProxyRequest,
            session_id=session_id,
            request_id=request_id,
            method=method,
            path=path,
            schema=schema,
            status=status,
            stream=stream,
            duration_ms=duration_ms,
            upstream=upstream,
            **_present(transform_ms=transform_ms),
        )

    def response(
        self,
        *,
        status: int,
        stream: bool,
        duration_ms: float,
        session_id: str | None = None,
        request_id: str | None = None,
        size: int | None = None,
        frames: int | None = None,
        restored: int = 0,
        aborted: bool = False,
        usage: Mapping[str, int] | None = None,
        model: str | None = None,
    ) -> None:
        """A proxied response was fully delivered, or its stream aborted.

        ``duration_ms`` is the *total* exchange time.  ``size`` (batch bytes) or
        ``frames`` (streamed SSE frames) describes the response volume, and
        ``restored`` counts the pseudonyms reversed on the way back.
        ``aborted`` marks a stream the client left before its end; the counts
        then cover what was sent until then.  ``usage`` is the token counts the
        upstream reported (see :func:`privyx.observability.usage.normalize`)
        and ``model`` the model id it named.
        """
        self.emit(
            ProxyResponse,
            session_id=session_id,
            request_id=request_id,
            status=status,
            stream=stream,
            restored=restored,
            duration_ms=duration_ms,
            **_present(
                bytes=size,
                frames=frames,
                aborted=aborted or None,
                usage=None if usage is None else dict(usage),
                model=model,
            ),
        )

    def error(
        self,
        *,
        phase: str,
        error_type: str,
        session_id: str | None = None,
        request_id: str | None = None,
        status: int | None = None,
        duration_ms: float | None = None,
        listener: str | None = None,
        usage: Mapping[str, int] | None = None,
    ) -> None:
        """A proxied exchange failed.

        ``phase`` is where it broke (``upstream`` / ``stream`` / ``response``) and
        ``error_type`` is the exception's *class name* — never its message, which
        could carry payload text.  ``usage`` is the token counts a stream
        reported before it broke.
        """
        self.emit(
            ProxyError,
            session_id=session_id,
            request_id=request_id,
            phase=phase,
            error_type=error_type,
            # The feature's listener that failed: its name, no value.
            **_present(
                status=status,
                duration_ms=duration_ms,
                listener=listener,
                usage=None if usage is None else dict(usage),
            ),
        )


def _present(**fields: Any) -> dict[str, Any]:
    """``fields`` without the ones left out of a record: those that are ``None``."""
    return {name: value for name, value in fields.items() if value is not None}
