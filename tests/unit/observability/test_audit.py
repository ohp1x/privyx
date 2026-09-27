"""Tests for the PII-safe audit trail."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from privyx.config.schema import AuditConfig, Settings
from privyx.core.builder import build_audit_logger
from privyx.observability.audit import SCHEMA_VERSION, AuditEvent, AuditLogger


def _lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


def test_audit_event_to_json_has_versioned_envelope() -> None:
    event = AuditEvent(
        event="session.transform",
        timestamp=1.5,
        session_id="ses_x",
        request_id="req_y",
        fields={"entity_counts": {"EMAIL": 2}, "transformations": 3},
    )
    record = json.loads(event.to_json())
    assert record == {
        "schema_version": SCHEMA_VERSION,
        "time": "1970-01-01T00:00:01.500Z",
        "ts": 1.5,
        "event": "session.transform",
        "request_id": "req_y",
        "session_id": "ses_x",
        "entity_counts": {"EMAIL": 2},
        "transformations": 3,
    }


def test_emit_writes_one_json_line_per_event() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)

    audit.session_created("ses_1", source="header")
    audit.transform("ses_1", entity_counts={"EMAIL": 1}, transformations=1)
    audit.restore("ses_1", transformations=1)

    records = _lines(buf)
    assert [r["event"] for r in records] == [
        "session.created",
        "session.transform",
        "session.restore",
    ]
    assert records[0]["source"] == "header"
    assert records[1]["entity_counts"] == {"EMAIL": 1}
    # Outside a request scope every event's request_id is null.
    assert all(r["request_id"] is None for r in records)


def test_request_scope_aggregates_transform_and_restore() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)

    token = audit.begin_request("req_abc")
    audit.transform("ses_1", entity_counts={"EMAIL": 1}, transformations=1)
    audit.transform("ses_1", entity_counts={"EMAIL": 2, "PERSON": 1}, transformations=3)
    audit.restore("ses_1", transformations=2)
    # Nothing is written while the scope accumulates.
    assert buf.getvalue() == ""

    audit.flush_transform()
    audit.flush_restore()
    audit.end_request(token)

    records = _lines(buf)
    assert [r["event"] for r in records] == ["session.transform", "session.restore"]
    transform = records[0]
    assert transform["entity_counts"] == {"EMAIL": 3, "PERSON": 1}
    assert transform["transformations"] == 4
    assert transform["request_id"] == "req_abc"
    assert transform["session_id"] == "ses_1"
    assert records[1]["transformations"] == 2
    assert records[1]["request_id"] == "req_abc"


def test_end_request_flushes_pending_aggregates() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)

    token = audit.begin_request("req_x")
    audit.transform("ses_1", entity_counts={"EMAIL": 1}, transformations=1)
    audit.end_request(token)  # flushes what the caller did not

    records = _lines(buf)
    assert [r["event"] for r in records] == ["session.transform"]
    assert records[0]["request_id"] == "req_x"
    # Scope is closed: a later call reverts to immediate, uncorrelated emit.
    audit.transform("ses_2", entity_counts={"EMAIL": 1}, transformations=1)
    assert _lines(buf)[-1]["request_id"] is None


def test_session_deleted_event_is_pii_safe() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)

    audit.session_deleted(
        "ses_1",
        reason="ephemeral_request_complete",
        mapping_count=3,
        request_id="req_1",
    )

    (record,) = _lines(buf)
    assert record["event"] == "session.deleted"
    assert record["session_id"] == "ses_1"
    assert record["reason"] == "ephemeral_request_complete"
    assert record["mapping_count"] == 3
    assert record["request_id"] == "req_1"
    assert "alice@example.com" not in buf.getvalue()


def test_response_event_fields() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)

    audit.response(
        status=200,
        stream=True,
        frames=12,
        restored=3,
        session_id="ses_1",
        request_id="req_1",
        duration_ms=42.0,
    )

    (record,) = _lines(buf)
    assert record["event"] == "proxy.response"
    assert record["stream"] is True
    assert record["frames"] == 12
    assert record["restored"] == 3
    assert record["request_id"] == "req_1"
    assert "bytes" not in record  # frames, not bytes, for a stream


def test_error_event_records_class_not_message() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)

    try:
        raise ValueError("boom alice@example.com")
    except ValueError as exc:
        audit.error(
            phase="upstream",
            error_type=type(exc).__name__,
            session_id="ses_1",
            request_id="req_1",
            duration_ms=1.0,
        )

    written = buf.getvalue()
    assert "alice@example.com" not in written  # never the exception message
    (record,) = _lines(buf)
    assert record["event"] == "proxy.error"
    assert record["phase"] == "upstream"
    assert record["error_type"] == "ValueError"
    assert record["request_id"] == "req_1"


def test_disabled_logger_is_a_noop() -> None:
    audit = AuditLogger(None)
    assert audit.enabled is False
    # No writer, no error, nothing recorded — including the new helpers.
    audit.transform("ses_1", entity_counts={"EMAIL": 1}, transformations=1)
    audit.request(
        method="POST",
        path="/v1/chat/completions",
        schema="openai",
        status=200,
        session_id="ses_1",
        stream=False,
        duration_ms=1.0,
        upstream="api.openai.com",
    )
    audit.response(status=200, stream=False, size=10, session_id="ses_1", duration_ms=1.0)
    audit.error(phase="upstream", error_type="ValueError")


def test_write_failure_does_not_propagate() -> None:
    class _Boom(io.StringIO):
        def write(self, s: str) -> int:
            raise OSError("disk full")

    audit = AuditLogger(_Boom())
    # Must not raise — an audit failure can never break the request path.
    audit.transform("ses_1", entity_counts={"EMAIL": 1}, transformations=1)


def test_transform_records_counts_not_content() -> None:
    """The transform helper takes a histogram, so raw PII cannot leak into the trail."""
    buf = io.StringIO()
    audit = AuditLogger(buf)

    audit.transform("ses_1", entity_counts={"EMAIL": 1}, transformations=1)

    written = buf.getvalue()
    assert "alice@example.com" not in written
    assert '"EMAIL": 1' in written


def test_request_event_fields() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)

    audit.request(
        method="POST",
        path="/v1/messages",
        schema="anthropic",
        status=200,
        session_id="ses_1",
        stream=True,
        duration_ms=12.34,
        upstream="api.anthropic.com",
        request_id="req_1",
    )

    (record,) = _lines(buf)
    assert record["event"] == "proxy.request"
    assert record["method"] == "POST"
    assert record["path"] == "/v1/messages"
    assert record["schema"] == "anthropic"
    assert record["stream"] is True
    assert record["upstream"] == "api.anthropic.com"
    assert record["request_id"] == "req_1"


def test_build_audit_logger_enabled_writes_file(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    settings = Settings(audit=AuditConfig(enabled=True, path=str(path)))

    audit = build_audit_logger(settings)
    try:
        assert audit.enabled is True
        audit.session_created("ses_1")
    finally:
        audit.close()

    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert records[0]["event"] == "session.created"
    assert records[0]["schema_version"] == SCHEMA_VERSION


def test_build_audit_logger_disabled_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "audit.log"
    settings = Settings(audit=AuditConfig(enabled=False, path=str(path)))

    audit = build_audit_logger(settings)
    audit.session_created("ses_1")
    audit.close()

    assert audit.enabled is False
    assert not path.exists()  # disabled must not open or create the file


def test_no_op_transform_and_restore_are_not_recorded() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)

    audit.transform("ses_1", entity_counts={}, transformations=0)
    audit.restore("ses_1", transformations=0)
    audit.transform("ses_1", entity_counts={"EMAIL": 1}, transformations=1)

    assert [r["event"] for r in _lines(buf)] == ["session.transform"]


def test_stats_count_every_event_even_when_the_trail_is_disabled() -> None:
    audit = AuditLogger(None)

    audit.transform("ses_1", entity_counts={"EMAIL": 2, "PERSON": 1}, transformations=3)
    audit.restore("ses_1", transformations=2)
    audit.response(status=200, stream=False, duration_ms=1500.0)
    audit.error(phase="upstream", error_type="ConnectError")

    stats = audit.stats
    assert stats.events["session.transform"] == 1
    assert stats.entities == {"EMAIL": 2, "PERSON": 1}
    assert stats.restored == 2
    assert stats.errors == {"upstream": 1}
    text = stats.prometheus()
    assert 'privyx_entities_masked_total{entity_type="EMAIL"} 2' in text
    assert 'privyx_proxy_errors_total{phase="upstream"} 1' in text
    assert "privyx_response_duration_seconds_sum 1.5" in text
    assert "privyx_response_duration_seconds_count 1" in text


def test_prometheus_escapes_label_values() -> None:
    audit = AuditLogger(None)
    audit.transform("ses_1", entity_counts={'A"B\\C\nD': 1}, transformations=1)

    assert 'entity_type="A\\"B\\\\C\\nD"} 1' in audit.stats.prometheus()
