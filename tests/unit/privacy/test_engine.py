"""Tests for the privacy engine and operators."""

from __future__ import annotations

import io
import json

import pytest

from privyx.core.engine import KEY_PROBE_LIMIT, PrivacyEngine
from privyx.core.errors import SessionNotFoundError, VaultError
from privyx.core.session import Session
from privyx.observability.audit import AuditLogger
from privyx.privacy.detector.builtin import DEFAULT_PATTERNS, RegexDetector
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


async def test_transform_with_a_key_finds_what_only_the_key_tells() -> None:
    """``{"password": "hunter2"}``: alone the value is nothing, with its key a
    secret.  A context word in the key counts, and so does what only the value
    alone matches (an anchored pattern)."""
    buf = io.StringIO()
    engine = PrivacyEngine(
        detector=RegexDetector(
            {"SECRET": DEFAULT_PATTERNS["SECRET"], "EMAIL": DEFAULT_PATTERNS["EMAIL"]}
            | {"BADGE": r"^EMP\d{4}$"},
            context={"EMAIL": ["owner"]},
        ),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=AuditLogger(buf),
    )
    session = await engine.get_or_create_session()

    assert (await engine.transform("hunter2", session=session)).text == "hunter2"
    assert (await engine.transform("hunter2", session=session, key="password")).text == (
        "<PRIVYX_SECRET_1>"
    )
    owner = await engine.transform("contact bob@example.com", session=session, key="owner")
    assert owner.text == "contact <PRIVYX_EMAIL_2>"
    assert (await engine.transform("EMP1234", session=session, key="badge")).text == (
        "<PRIVYX_BADGE_3>"
    )
    # The key's span holds the whole value: no part of it is left out.
    whole = await engine.transform("a bob@example.com b", session=session, key="password")
    assert whole.text == "<PRIVYX_SECRET_4>"

    records = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    counts = [r["entity_counts"] for r in records if r["event"] == "session.transform"]
    assert counts == [{"SECRET": 1}, {"EMAIL": 1}, {"BADGE": 1}, {"SECRET": 1}]
    assert "hunter2" not in buf.getvalue()


@pytest.mark.asyncio
async def test_transform_detects_only_a_short_value_with_its_key() -> None:
    """A value longer than ``KEY_PROBE_LIMIT`` (a file's content) is detected
    alone, once: the key is not looked at."""
    engine = PrivacyEngine(
        detector=RegexDetector({"SECRET": DEFAULT_PATTERNS["SECRET"]}),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=AuditLogger(io.StringIO()),
    )
    session = await engine.get_or_create_session()
    short = "x" * KEY_PROBE_LIMIT
    long = "x" * (KEY_PROBE_LIMIT + 1)

    assert (await engine.transform(short, session=session, key="password")).text == (
        "<PRIVYX_SECRET_1>"
    )
    assert (await engine.transform(long, session=session, key="password")).text == long


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


async def test_compact_tokens_from_config_round_trip() -> None:
    from privyx.config.schema import Settings
    from privyx.core.builder import build_engine

    engine, close = await build_engine(
        Settings(vault={"type": "memory"}, token={"compact": True}, audit={"enabled": False})
    )
    try:
        session = await engine.get_or_create_session()
        masked = await engine.transform("mail alice@example.com", session=session)
        assert masked.text == "mail <E~1>"
        restored = await engine.restore("hi <E~1>", session.session_id, session=session)
        assert restored.text == "hi alice@example.com"
    finally:
        await close()


async def test_dlp_check_comes_from_proxy_config() -> None:
    from privyx.config.schema import Settings
    from privyx.core.builder import build_engine

    off, close_off = await build_engine(Settings(vault={"type": "memory"}))
    on, close_on = await build_engine(Settings(vault={"type": "memory"}, proxy={"dlp_check": True}))
    await close_off()
    await close_on()
    assert (off.dlp_check, on.dlp_check) == (False, True)


async def test_transform_takes_spans_already_decided() -> None:
    """``detection=`` (since 0.1.31) still replaces the detection step."""
    from privyx.core.result import Detection, Span

    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    session = await engine.get_or_create_session()
    spans = [Span(5, 8, "PERSON", "Ana")]
    out = await engine.transform(
        "call Ana, ana@example.com", session=session, detection=Detection(spans)
    )
    assert out.text == "call <PRIVYX_PERSON_1>, ana@example.com"
    with pytest.raises(ValueError, match="not both"):
        await engine.transform("x", session=session, detection=Detection(), key="password")
