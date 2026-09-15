"""PII-safe audit trail.

Records security-relevant events — session creation, transform, restore, and
proxy requests — as one JSON object per line in a dedicated append-only file
(see :func:`privyx.core.builder.build_audit_logger`).

The audit path is **PII-safe by construction**: the typed helpers accept only
entity *types*, *counts*, and non-PII scalars.  Raw detected text, original
values, and pseudonyms never reach this module — :meth:`AuditLogger.transform`
takes a pre-computed ``{entity_type: count}`` histogram, not spans.  This is the
"counts, not content" guarantee documented in ``docs/security/data-handling.md``.

Writing is resilient: a failed write is logged to the ``privyx`` application
logger and swallowed, so an audit failure can never break a request.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, TextIO

_log = logging.getLogger("privyx")


@dataclass(slots=True, frozen=True)
class AuditEvent:
    """One audit record.

    ``fields`` carries the event-specific, PII-safe metadata; it is flattened
    into the serialized object alongside ``ts``/``event``/``session_id`` so the
    JSONL is easy to query.
    """

    event: str
    timestamp: float
    session_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.timestamp,
            "event": self.event,
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

    def emit(self, event: str, *, session_id: str | None = None, **fields: Any) -> None:
        """Write one event as a JSON line.  Never raises into the caller."""
        writer = self._writer
        if writer is None:
            return
        line = AuditEvent(
            event=event, timestamp=time.time(), session_id=session_id, fields=fields
        ).to_json()
        try:
            with self._lock:
                writer.write(line + "\n")
                writer.flush()
        except Exception:  # audit must never break the request path
            _log.exception("audit write failed")

    # -- typed, PII-safe helpers ------------------------------------------

    def session_created(self, session_id: str, *, client_supplied: bool = False) -> None:
        """A new session was created (``client_supplied`` = id came from the client)."""
        self.emit("session.created", session_id=session_id, client_supplied=client_supplied)

    def transform(
        self, session_id: str, *, entity_counts: dict[str, int], transformations: int
    ) -> None:
        """Text was pseudonymized.

        Args:
            entity_counts: ``{entity_type: count}`` histogram — types and counts
                only, never the matched text.
            transformations: Number of replacements applied.
        """
        self.emit(
            "transform",
            session_id=session_id,
            entity_counts=entity_counts,
            transformations=transformations,
        )

    def restore(self, session_id: str, *, transformations: int) -> None:
        """Pseudonyms were reversed in a response."""
        self.emit("restore", session_id=session_id, transformations=transformations)

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
    ) -> None:
        """A proxied request completed (for streams, ``duration_ms`` is time to
        the upstream response headers, not the whole stream)."""
        self.emit(
            "proxy.request",
            session_id=session_id,
            method=method,
            path=path,
            schema=schema,
            status=status,
            stream=stream,
            duration_ms=duration_ms,
            upstream=upstream,
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
