"""The audit notifications: facts after they happened, with counts and types, never values.

Each is one record of the audit trail, under a versioned envelope::

    {"schema_version": 1, "time": "2026-09-18T03:11:18.737Z", "ts": 1789696678.737,
     "event": "session.transform", "request_id": "req_9c1e2f3a4b5c",
     "session_id": "ses_ab", "entity_counts": {"EMAIL": 1}, "transformations": 1}
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from privyx.core import Notification

#: On-disk record schema version.  Bumped on a backward-incompatible change to
#: the envelope or the vocabulary, so a reader can branch on it.
SCHEMA_VERSION = 1


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

    name: ClassVar[str] = "session.created"


class SessionTransform(AuditEvent):
    """Text was masked: entity counts per type."""

    name: ClassVar[str] = "session.transform"


class SessionRestore(AuditEvent):
    """Pseudonyms were restored in a reply."""

    name: ClassVar[str] = "session.restore"


class SessionDeleted(AuditEvent):
    """A session was removed from the vault."""

    name: ClassVar[str] = "session.deleted"


class ProxyRequest(AuditEvent):
    """A request went upstream, and the upstream answered with headers."""

    name: ClassVar[str] = "proxy.request"


class ProxyResponse(AuditEvent):
    """A reply was delivered, or its stream ended early: status, size, usage."""

    name: ClassVar[str] = "proxy.response"

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
        return _usage(self.fields)


class ProxyError(AuditEvent):
    """An exchange failed: the phase, the error's class name, and any usage reported first."""

    name: ClassVar[str] = "proxy.error"

    @property
    def usage(self) -> Mapping[str, int] | None:
        """The tokens a stream reported before it broke."""
        return _usage(self.fields)


class ProxyPassthrough(AuditEvent):
    """A request outside the masking routes was relayed unmasked."""

    name: ClassVar[str] = "proxy.passthrough"


class ProxyBlocked(AuditEvent):
    """A request was refused before it went upstream."""

    name: ClassVar[str] = "proxy.blocked"


class RunUnproxied(AuditEvent):
    """A ``privyx run`` sent no request upstream on a masking route."""

    name: ClassVar[str] = "run.unproxied"


class RunBudgetExceeded(AuditEvent):
    """A ``privyx run`` budget was reached, and the next turn refused."""

    name: ClassVar[str] = "run.budget_exceeded"


@dataclass(frozen=True, eq=False)
class AuditWriteFailed(Notification):
    """The trail could not write a record, which is lost; not a record itself."""

    name: ClassVar[str] = "audit.write_failed"


#: The fields of every record; the others are its event's own.
_ENVELOPE = frozenset({"schema_version", "time", "ts", "event", "request_id", "session_id"})


def _usage(fields: Mapping[str, Any]) -> Mapping[str, int] | None:
    """The ``usage`` of a record's fields, if it has one."""
    usage = fields.get("usage")
    return usage if isinstance(usage, Mapping) else None


def local_time(epoch: float) -> str:
    """An epoch timestamp as local time, to the second."""
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def record_line(record: Mapping[str, Any]) -> str:
    """``record`` as one plain line; raises on a record it cannot read."""
    extras = " ".join(f"{k}={_value(v)}" for k, v in record.items() if k not in _ENVELOPE)
    return (
        f"{local_time(record['ts'])}  {record['event']:<17}  "
        f"{record.get('request_id') or '-':<16}  {record.get('session_id') or '-'}  {extras}"
    )


def _value(value: Any) -> str:
    if isinstance(value, dict):  # entity_counts
        return ",".join(f"{k}:{v}" for k, v in value.items())
    return value if isinstance(value, str) else json.dumps(value)


def breakdown(counts: Counter[str]) -> str:
    """``counts`` in parentheses, the largest first; nothing without any."""
    return f" ({', '.join(f'{k} {v}' for k, v in counts.most_common())})" if counts else ""
