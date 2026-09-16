"""Tests for the PII-safe audit trail."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from privyx.config.schema import AuditConfig, Settings
from privyx.core.builder import build_audit_logger
from privyx.observability.audit import AuditEvent, AuditLogger


def _lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


def test_audit_event_to_json_flattens_fields() -> None:
    event = AuditEvent(
        event="transform",
        timestamp=1.5,
        session_id="ses_x",
        fields={"entity_counts": {"EMAIL": 2}, "transformations": 3},
    )
    record = json.loads(event.to_json())
    assert record == {
        "ts": 1.5,
        "event": "transform",
        "session_id": "ses_x",
        "entity_counts": {"EMAIL": 2},
        "transformations": 3,
    }


def test_emit_writes_one_json_line_per_event() -> None:
    buf = io.StringIO()
    audit = AuditLogger(buf)

    audit.session_created("ses_1", client_supplied=True)
    audit.transform("ses_1", entity_counts={"EMAIL": 1}, transformations=1)
    audit.restore("ses_1", transformations=1)

    records = _lines(buf)
    assert [r["event"] for r in records] == ["session.created", "transform", "restore"]
    assert records[0]["client_supplied"] is True
    assert records[1]["entity_counts"] == {"EMAIL": 1}


def test_disabled_logger_is_a_noop() -> None:
    audit = AuditLogger(None)
    assert audit.enabled is False
    # No writer, no error, nothing recorded.
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
    )

    (record,) = _lines(buf)
    assert record["event"] == "proxy.request"
    assert record["method"] == "POST"
    assert record["path"] == "/v1/messages"
    assert record["schema"] == "anthropic"
    assert record["stream"] is True
    assert record["upstream"] == "api.anthropic.com"


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

    assert [r["event"] for r in _lines(buf)] == ["transform"]
