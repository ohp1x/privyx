"""PII-safe audit trail.

Records security-relevant events — session creation, transform, restore, and
proxy request/response/error — as one JSON object per line in a dedicated
append-only file (see :func:`privyx.core.builder.build_audit_logger`).

Every record shares a **versioned envelope** so the format can evolve without
breaking a downstream reader::

    {"schema_version": 1, "time": "2026-09-18T03:11:18.737Z", "ts": 1789696678.737,
     "event": "session.transform", "request_id": "req_9c1e2f3a4b5c",
     "session_id": "ses_ab", "entity_counts": {"EMAIL": 1}, "transformations": 1}

- ``schema_version`` — bumped on a backward-incompatible change (:data:`SCHEMA_VERSION`).
- ``time`` — human-readable ISO-8601 UTC; ``ts`` is the epoch float kept for cheap
  ordering / range queries.
- ``request_id`` — correlates every event of one proxied exchange; ``null`` for
  standalone library / CLI calls that run outside a request scope.
- ``event`` — one of :class:`AuditEventType`, the single source of truth for the
  vocabulary (so names never drift the way a scatter of string literals would).

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

Writing is resilient: a failed write is logged to the ``privyx`` application
logger and swallowed, so an audit failure can never break a request.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TextIO

_log = logging.getLogger("privyx")

#: On-disk record schema version.  Bump on any backward-incompatible change to the
#: envelope or the event vocabulary so a reader can branch on it.
SCHEMA_VERSION = 1


class AuditEventType(StrEnum):
    """The audit event vocabulary — declared once so names never drift.

    Consistent ``<domain>.<action>`` naming: ``session.*`` for privacy-pipeline
    events tied to a session, ``proxy.*`` for one HTTP exchange.
    """

    SESSION_CREATED = "session.created"
    SESSION_TRANSFORM = "session.transform"
    SESSION_RESTORE = "session.restore"
    SESSION_DELETED = "session.deleted"
    PROXY_REQUEST = "proxy.request"
    PROXY_RESPONSE = "proxy.response"
    PROXY_ERROR = "proxy.error"


def _iso(ts: float) -> str:
    """Format an epoch timestamp as an ISO-8601 UTC millisecond string."""
    return (
        datetime.fromtimestamp(ts, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(slots=True)
class _RequestScope:
    """Per-request correlation id plus the transform/restore accumulators.

    Held in a :class:`~contextvars.ContextVar` (task-local, so concurrent
    requests never share one) for the duration of a proxied exchange.
    """

    request_id: str
    session_id: str | None = None
    transform_counts: dict[str, int] = field(default_factory=dict)
    transform_n: int = 0
    restore_n: int = 0


#: Active request scope, or ``None`` outside a proxied exchange.
_scope: ContextVar[_RequestScope | None] = ContextVar("privyx_audit_scope", default=None)


@dataclass(slots=True, frozen=True)
class AuditEvent:
    """One audit record.

    ``fields`` carries the event-specific, PII-safe metadata; it is flattened
    into the serialized object alongside the envelope so the JSONL stays easy to
    query with ``jq`` or a SQL column.
    """

    event: str
    timestamp: float
    session_id: str | None = None
    request_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "time": _iso(self.timestamp),
            "ts": self.timestamp,
            "event": self.event,
            "request_id": self.request_id,
            "session_id": self.session_id,
            **self.fields,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


class AuditLogger:
    """Append PII-safe audit events to a writer as JSON lines.

    Args:
        writer: An open text stream to append to — a file in production, a
            ``StringIO`` in tests.  ``None`` disables the logger: every method
            becomes a no-op, so callers need no ``if audit:`` guards.
    """

    def __init__(self, writer: TextIO | None = None) -> None:
        self._writer = writer
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    def emit(
        self,
        event: str,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
        **fields: Any,
    ) -> None:
        """Write one event as a JSON line.  Never raises into the caller.

        ``request_id`` / ``session_id`` fall back to the active request scope when
        not given, so an event emitted deep in the engine still correlates.
        """
        writer = self._writer
        if writer is None:
            return
        scope = _scope.get()
        if scope is not None:
            if request_id is None:
                request_id = scope.request_id
            if session_id is None:
                session_id = scope.session_id
        line = AuditEvent(
            event=event,
            timestamp=time.time(),
            session_id=session_id,
            request_id=request_id,
            fields=fields,
        ).to_json()
        try:
            with self._lock:
                writer.write(line + "\n")
                writer.flush()
        except Exception:  # audit must never break the request path
            _log.exception("audit write failed")

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
        if scope is None or scope.transform_n == 0:
            return 0
        total = scope.transform_n
        self.emit(
            AuditEventType.SESSION_TRANSFORM,
            session_id=scope.session_id,
            entity_counts=dict(scope.transform_counts),
            transformations=total,
        )
        scope.transform_counts = {}
        scope.transform_n = 0
        return total

    def flush_restore(self) -> int:
        """Emit the scope's aggregated ``session.restore`` and return its count."""
        scope = _scope.get()
        if scope is None or scope.restore_n == 0:
            return 0
        total = scope.restore_n
        self.emit(
            AuditEventType.SESSION_RESTORE,
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
        self.emit(AuditEventType.SESSION_CREATED, session_id=session_id, source=source)

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
            AuditEventType.SESSION_DELETED,
            session_id=session_id,
            request_id=request_id,
            reason=reason,
            mapping_count=mapping_count,
        )

    def transform(
        self, session_id: str, *, entity_counts: dict[str, int], transformations: int
    ) -> None:
        """Text was pseudonymized.

        Args:
            entity_counts: ``{entity_type: count}`` histogram — types and counts
                only, never the matched text.
            transformations: Number of replacements applied.

        Within a request scope the counts accumulate into one ``session.transform``
        per exchange (flushed by :meth:`flush_transform`); outside a scope the event
        is written immediately.  A no-op transform (``transformations == 0``) is
        never recorded — the bulk of a request is untouched text, and one line per
        skipped field buries the events that matter.
        """
        if not transformations:
            return
        scope = _scope.get()
        if scope is not None:
            for entity_type, count in entity_counts.items():
                scope.transform_counts[entity_type] = (
                    scope.transform_counts.get(entity_type, 0) + count
                )
            scope.transform_n += transformations
            if scope.session_id is None:
                scope.session_id = session_id
            return
        self.emit(
            AuditEventType.SESSION_TRANSFORM,
            session_id=session_id,
            entity_counts=entity_counts,
            transformations=transformations,
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
            AuditEventType.SESSION_RESTORE,
            session_id=session_id,
            request_id=request_id,
            transformations=transformations,
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
    ) -> None:
        """The upstream responded with headers.

        ``duration_ms`` is time to the upstream *response headers* (time to first
        byte), not the whole stream — :meth:`response` records the total.
        """
        self.emit(
            AuditEventType.PROXY_REQUEST,
            session_id=session_id,
            request_id=request_id,
            method=method,
            path=path,
            schema=schema,
            status=status,
            stream=stream,
            duration_ms=duration_ms,
            upstream=upstream,
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
    ) -> None:
        """A proxied response was fully delivered to the client.

        ``duration_ms`` is the *total* exchange time.  ``size`` (batch bytes) or
        ``frames`` (streamed SSE frames) describes the response volume, and
        ``restored`` counts the pseudonyms reversed on the way back.
        """
        fields: dict[str, Any] = {
            "status": status,
            "stream": stream,
            "restored": restored,
            "duration_ms": duration_ms,
        }
        if size is not None:
            fields["bytes"] = size
        if frames is not None:
            fields["frames"] = frames
        self.emit(
            AuditEventType.PROXY_RESPONSE,
            session_id=session_id,
            request_id=request_id,
            **fields,
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
    ) -> None:
        """A proxied exchange failed.

        ``phase`` is where it broke (``upstream`` / ``stream`` / ``response``) and
        ``error_type`` is the exception's *class name* — never its message, which
        could carry payload text.
        """
        fields: dict[str, Any] = {"phase": phase, "error_type": error_type}
        if status is not None:
            fields["status"] = status
        if duration_ms is not None:
            fields["duration_ms"] = duration_ms
        self.emit(
            AuditEventType.PROXY_ERROR,
            session_id=session_id,
            request_id=request_id,
            **fields,
        )

    def close(self) -> None:
        """Flush and close the writer; safe to call more than once."""
        writer = self._writer
        if writer is None:
            return
        self._writer = None
        try:
            writer.flush()
            writer.close()
        except Exception:  # pragma: no cover - defensive
            _log.exception("audit close failed")
