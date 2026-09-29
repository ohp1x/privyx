"""Tests for the privacy engine and operators."""

from __future__ import annotations

import io
import json

import pytest

from privyx.core.engine import PrivacyEngine
from privyx.core.errors import SessionNotFoundError, VaultError
from privyx.core.session import Session
from privyx.observability.audit import AuditLogger
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.operator.redact import RedactOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.privacy.policy.strict import StrictPolicy
from privyx.vault.memory import MemoryVault


@pytest.mark.asyncio
async def test_transform_then_restore_roundtrip() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    text = "Contact alice@example.com or +1 (555) 123-4567"
    session = await engine.get_or_create_session()
    transformed = await engine.transform(text, session=session)
    assert transformed.text != text
    assert "alice@example.com" not in transformed.text

    restored = await engine.restore(transformed.text, session.session_id)
    assert restored.text == text


@pytest.mark.asyncio
async def test_consistent_pseudonym_for_same_value() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    session = await engine.get_or_create_session()
    r1 = await engine.transform("mail alice@example.com", session=session)
    r2 = await engine.transform("mail alice@example.com", session=session)
    assert r1.text == r2.text


@pytest.mark.asyncio
async def test_transform_keeps_a_ttl_session_alive_without_new_mappings() -> None:
    """Last activity is the last turn, not the last turn that added a mapping."""
    vault = MemoryVault(ttl=60)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault,
    )
    session = await engine.get_or_create_session()
    session.updated_at -= 120

    await engine.transform("nothing sensitive here", session=session)

    assert await vault.get(session.session_id) is not None


@pytest.mark.asyncio
async def test_restore_missing_session_raises() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    with pytest.raises(SessionNotFoundError):
        await engine.restore("whatever", "does-not-exist")


@pytest.mark.asyncio
async def test_redact_operator() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=RedactOperator(),
        vault=MemoryVault(),
    )
    session = await engine.get_or_create_session()
    transformed = await engine.transform("email alice@example.com", session=session)
    assert "alice@example.com" not in transformed.text
    assert "[REDACTED]" in transformed.text


@pytest.mark.asyncio
async def test_strict_policy_filters_entities() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=StrictPolicy({"EMAIL"}),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    session = await engine.get_or_create_session()
    transformed = await engine.transform("alice@example.com and 192.168.0.1", session=session)
    # IP_ADDRESS should be filtered out by the strict policy.
    assert "192.168.0.1" in transformed.text
    assert "alice@example.com" not in transformed.text


@pytest.mark.asyncio
async def test_engine_emits_pii_safe_audit_events() -> None:
    buf = io.StringIO()
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=AuditLogger(buf),
    )
    session = await engine.get_or_create_session()
    await engine.transform("mail alice@example.com", session=session)

    written = buf.getvalue()
    # The whole point: no original value ever reaches the audit trail.
    assert "alice@example.com" not in written

    records = [json.loads(line) for line in written.splitlines() if line]
    assert "session.created" in {r["event"] for r in records}
    transform = next(r for r in records if r["event"] == "session.transform")
    assert transform["entity_counts"] == {"EMAIL": 1}
    assert transform["transformations"] == 1


@pytest.mark.asyncio
async def test_delete_session_audits_after_vault_delete() -> None:
    buf = io.StringIO()
    vault = MemoryVault()
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault,
        audit=AuditLogger(buf),
    )
    session = await engine.get_or_create_session()
    await engine.transform("mail alice@example.com", session=session)

    assert (
        await engine.delete_session(
            session.session_id,
            reason="ephemeral_request_complete",
            request_id="req_1",
        )
        is True
    )
    assert await vault.get(session.session_id) is None

    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    deleted = next(r for r in records if r["event"] == "session.deleted")
    assert deleted["mapping_count"] == 1
    assert deleted["request_id"] == "req_1"
    assert buf.getvalue().index('"event": "session.deleted"') > buf.getvalue().index(
        '"event": "session.transform"'
    )


@pytest.mark.asyncio
async def test_a_failed_create_of_a_minted_id_is_not_retried_as_a_race() -> None:
    """A fresh id cannot collide: asking a failing vault again only doubles the wait."""

    class DownVault(MemoryVault):
        gets = 0

        async def create(self, session: Session) -> None:
            raise VaultError("redis vault: Timeout reading from socket")

        async def get(self, session_id: str) -> Session | None:
            self.gets += 1
            return await super().get(session_id)

    vault = DownVault()
    engine = PrivacyEngine(
        detector=RegexDetector(), policy=DefaultPolicy(), operator=PseudonymOperator(), vault=vault
    )

    with pytest.raises(VaultError):
        await engine.get_or_create_session()
    assert vault.gets == 0


@pytest.mark.asyncio
async def test_delete_missing_session_is_noop() -> None:
    buf = io.StringIO()
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=AuditLogger(buf),
    )

    assert (
        await engine.delete_session(
            "ses_missing", reason="ephemeral_request_complete", request_id="req_1"
        )
        is False
    )
    assert buf.getvalue() == ""


@pytest.mark.asyncio
async def test_delete_session_failure_does_not_audit_deleted() -> None:
    class FailingVault(MemoryVault):
        async def delete(self, session_id: str) -> None:
            raise RuntimeError("disk full")

    buf = io.StringIO()
    vault = FailingVault()
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=vault,
        audit=AuditLogger(buf),
    )
    session = await engine.get_or_create_session()

    with pytest.raises(RuntimeError):
        await engine.delete_session(
            session.session_id, reason="ephemeral_request_complete", request_id="req_1"
        )

    assert await vault.get(session.session_id) is not None
    assert "session.deleted" not in buf.getvalue()


@pytest.mark.asyncio
async def test_llm_fallback_reaches_the_transform_audit_event() -> None:
    from privyx.privacy.detector.llm import LLMDetector

    class _Down:
        async def complete(self, prompt: str) -> str:
            raise RuntimeError("down")

    buf = io.StringIO()
    engine = PrivacyEngine(
        detector=LLMDetector(client=_Down(), fallback_on_error=True),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=AuditLogger(buf),
    )

    result = await engine.transform("mail ann@example.com")

    assert "ann@example.com" not in result.text  # the regex fallback still masked it
    transform = [json.loads(line) for line in buf.getvalue().splitlines()][-1]
    assert transform["event"] == "session.transform"
    assert transform["detector_counts"] == {"llm_fallbacks": 1}
