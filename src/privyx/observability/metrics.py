"""Counters aggregated from audit events.

One :class:`AuditStats` serves two readers: the running proxy feeds it every
event it emits (served as Prometheus text at ``GET /metrics``), and
``privyx audit stats`` feeds it the records of an audit log file.  Both see
the same PII-safe fields the trail holds — event names, entity types, counts —
so neither can expose more than the audit log already does.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AuditStats:
    """Running totals over a stream of audit records (``AuditEvent.to_dict()``)."""

    events: Counter[str] = field(default_factory=Counter)
    entities: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    restored: int = 0
    response_ms: float = 0.0
    first_ts: float | None = None
    last_ts: float | None = None

    def add(self, record: dict[str, Any]) -> None:
        event = record["event"]
        self.events[event] += 1
        ts = record.get("ts")
        if isinstance(ts, int | float):
            self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        if event == "session.transform":
            self.entities.update(record.get("entity_counts") or {})
        elif event == "session.restore":
            self.restored += record.get("transformations") or 0
        elif event == "proxy.response":
            self.response_ms += record.get("duration_ms") or 0.0
        elif event == "proxy.error":
            self.errors[record.get("phase") or "unknown"] += 1

    def prometheus(self) -> str:
        """Render the totals in the Prometheus text exposition format."""
        responses = self.events["proxy.response"]
        lines = [
            *_family(
                "privyx_audit_events_total",
                "counter",
                "Audit events recorded since start, by event type.",
                "event",
                self.events,
            ),
            *_family(
                "privyx_entities_masked_total",
                "counter",
                "Entities pseudonymized in requests, by entity type.",
                "entity_type",
                self.entities,
            ),
            *_family(
                "privyx_proxy_errors_total",
                "counter",
                "Failed exchanges, by the phase that failed.",
                "phase",
                self.errors,
            ),
            "# HELP privyx_pseudonyms_restored_total Pseudonyms restored in responses.",
            "# TYPE privyx_pseudonyms_restored_total counter",
            f"privyx_pseudonyms_restored_total {self.restored}",
            "# HELP privyx_response_duration_seconds Total exchange time of delivered responses.",
            "# TYPE privyx_response_duration_seconds summary",
            f"privyx_response_duration_seconds_sum {self.response_ms / 1000}",
            f"privyx_response_duration_seconds_count {responses}",
        ]
        return "\n".join(lines) + "\n"


def _family(name: str, kind: str, help_: str, label: str, counts: Counter[str]) -> list[str]:
    return [
        f"# HELP {name} {help_}",
        f"# TYPE {name} {kind}",
        *(f'{name}{{{label}="{_escape(k)}"}} {v}' for k, v in sorted(counts.items())),
    ]


def _escape(value: str) -> str:
    # Entity types come from config and plugins, so escape per the text format.
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
