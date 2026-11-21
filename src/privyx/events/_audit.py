"""The audit notifications: facts after they happened, with counts and types, never values.

Each is one record of the audit trail, under a versioned envelope::

    {"schema_version": 1, "time": "2026-09-18T03:11:18.737Z", "ts": 1789696678.737,
     "event": "session.transform", "request_id": "req_9c1e2f3a4b5c",
     "session_id": "ses_ab", "entity_counts": {"EMAIL": 1}, "transformations": 1}
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from privyx.core import Notification

#: On-disk record schema version.  Bumped on a backward-incompatible change to
#: the envelope or the vocabulary, so a reader can branch on it.
SCHEMA_VERSION = 1


class AuditEventType(StrEnum):
    """The audit vocabulary: ``session.*`` for a session's masking, ``proxy.*`` for one
    HTTP exchange, ``run.*`` for one ``privyx run`` launch."""

    SESSION_CREATED = "session.created"
    SESSION_TRANSFORM = "session.transform"
    SESSION_RESTORE = "session.restore"
    SESSION_DELETED = "session.deleted"
    PROXY_REQUEST = "proxy.request"
    PROXY_RESPONSE = "proxy.response"
    PROXY_ERROR = "proxy.error"
    PROXY_PASSTHROUGH = "proxy.passthrough"
    PROXY_BLOCKED = "proxy.blocked"
    RUN_UNPROXIED = "run.unproxied"
    RUN_BUDGET_EXCEEDED = "run.budget_exceeded"


def _iso(ts: float) -> str:
    """An epoch timestamp as an ISO-8601 UTC millisecond string."""
    return (
        datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


@dataclass(frozen=True, eq=False)
class AuditEvent(Notification):
    """One audit record: its envelope and its fields, counts and types only."""

    name: ClassVar[str] = "audit"
    timestamp: float
    session_id: str | None = None
    request_id: str | None = None
    #: The event's own fields, flattened into the record beside the envelope.
    fields: Mapping[str, Any] = field(default_factory=dict)

    @property
    def event(self) -> str:
        """The event's name in the vocabulary."""
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "time": _iso(self.timestamp),
            "ts": self.timestamp,
            "event": self.name,
            "request_id": self.request_id,
            "session_id": self.session_id,
            **self.fields,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


class SessionCreated(AuditEvent):
    """A session was created."""

    name: ClassVar[str] = AuditEventType.SESSION_CREATED


class SessionTransform(AuditEvent):
    """Text was masked: entity counts per type."""

    name: ClassVar[str] = AuditEventType.SESSION_TRANSFORM


class SessionRestore(AuditEvent):
    """Pseudonyms were restored in a reply."""

    name: ClassVar[str] = AuditEventType.SESSION_RESTORE


class SessionDeleted(AuditEvent):
    """A session was removed from the vault."""

    name: ClassVar[str] = AuditEventType.SESSION_DELETED


class ProxyRequest(AuditEvent):
    """A request went upstream, and the upstream answered with headers."""

    name: ClassVar[str] = AuditEventType.PROXY_REQUEST


class ProxyResponse(AuditEvent):
    """A reply was delivered, or its stream ended early: status, size, usage."""

    name: ClassVar[str] = AuditEventType.PROXY_RESPONSE

    @property
    def status(self) -> int:
        return int(self.fields.get("status", 0))

    @property
    def aborted(self) -> bool:
        """Whether the client left the stream before its end."""
        return bool(self.fields.get("aborted", False))

    @property
    def usage(self) -> Mapping[str, int] | None:
        """The tokens the upstream reported: input, output, cache_read, cache_write."""
        usage = self.fields.get("usage")
        return usage if isinstance(usage, Mapping) else None


class ProxyError(AuditEvent):
    """An exchange failed: the phase, and the error's class name."""

    name: ClassVar[str] = AuditEventType.PROXY_ERROR


class ProxyPassthrough(AuditEvent):
    """A request outside the masking routes was relayed unmasked."""

    name: ClassVar[str] = AuditEventType.PROXY_PASSTHROUGH


class ProxyBlocked(AuditEvent):
    """A request was refused before it went upstream."""

    name: ClassVar[str] = AuditEventType.PROXY_BLOCKED


class RunUnproxied(AuditEvent):
    """A ``privyx run`` sent no request upstream on a masking route."""

    name: ClassVar[str] = AuditEventType.RUN_UNPROXIED


class RunBudgetExceeded(AuditEvent):
    """A ``privyx run`` budget was reached, and the next turn refused."""

    name: ClassVar[str] = AuditEventType.RUN_BUDGET_EXCEEDED


#: Each audit notification class, by its name in the vocabulary.
AUDIT_EVENTS: dict[str, type[AuditEvent]] = {
    cls.name: cls
    for cls in (
        SessionCreated,
        SessionTransform,
        SessionRestore,
        SessionDeleted,
        ProxyRequest,
        ProxyResponse,
        ProxyError,
        ProxyPassthrough,
        ProxyBlocked,
        RunUnproxied,
        RunBudgetExceeded,
    )
}
