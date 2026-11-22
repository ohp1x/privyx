"""Tests for the privacy engine and operators."""

from __future__ import annotations

import contextlib
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from privyx.app import read_config
from privyx.core import Session, SessionNotFoundError, VaultError
from privyx.detectors import RegexDetector
from privyx.detectors._pattern import DEFAULT_PATTERNS
from privyx.engine import PrivacyEngine
from privyx.engine._engine import KEY_PROBE_LIMIT
from privyx.operators._pseudonym import PseudonymOperator
from privyx.operators._redact import RedactOperator
from privyx.policies import DefaultPolicy, StrictPolicy
from privyx.testing import audit_to, opened
from privyx.vaults import MemoryVault


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
        policy=StrictPolicy(allowed=["EMAIL"]),
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
        audit=audit_to(buf),
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
            builtins=False,
            patterns={"SECRET": DEFAULT_PATTERNS["SECRET"], "EMAIL": DEFAULT_PATTERNS["EMAIL"]}
            | {"BADGE": r"^EMP\d{4}$"},
            context={"EMAIL": ["owner"]},
            underscore_separates_words=False,
        ),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit_to(buf),
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
        detector=RegexDetector(
            builtins=False,
            patterns={"SECRET": DEFAULT_PATTERNS["SECRET"]},
            underscore_separates_words=False,
        ),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit_to(io.StringIO()),
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
        audit=audit_to(buf),
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
        audit=audit_to(buf),
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
        audit=audit_to(buf),
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
    from privyx.detectors._llm import LLMDetector

    class _Down:
        async def complete(self, prompt: str) -> str:
            raise RuntimeError("down")

    buf = io.StringIO()
    engine = PrivacyEngine(
        detector=LLMDetector(client=_Down(), llm_fallback_on_error=True),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
        audit=audit_to(buf),
    )

    result = await engine.transform("mail ann@example.com")

    assert "ann@example.com" not in result.text  # the regex fallback still masked it
    transform = [json.loads(line) for line in buf.getvalue().splitlines()][-1]
    assert transform["event"] == "session.transform"
    assert transform["detector_counts"] == {"llm_fallbacks": 1}


async def test_compact_tokens_from_config_round_trip() -> None:
    from privyx.app._build import build_engine
    from privyx.config.schema import Settings

    engine, close = await build_engine(
        Settings(
            vault={"type": "memory"},
            token={"compact": True},
            anchor={"secret": ""},  # a counter id, which the test names
        )
    )
    try:
        session = await engine.get_or_create_session()
        masked = await engine.transform("mail alice@example.com", session=session)
        assert masked.text == "mail <E~1>"
        restored = await engine.restore("hi <E~1>", session.session_id, session=session)
        assert restored.text == "hi alice@example.com"
    finally:
        await close()


@pytest.mark.parametrize(
    ("operator", "length"),
    [
        ({}, 16),
        ({"id_length": 6}, 6),
        ({"type": "encrypt"}, 16),
        ({"type": "encrypt", "id_length": 6}, 6),
    ],
)
async def test_operator_id_length_sets_anchored_and_encrypt_ids(
    operator: dict[str, object], length: int
) -> None:
    import re

    from privyx.app._build import build_engine
    from privyx.config.schema import Settings

    if operator.get("type") == "encrypt":
        pytest.importorskip("cryptography")
        operator = {**operator, "key": "ab" * 32}
    engine, close = await build_engine(
        Settings(
            vault={"type": "memory"},
            anchor={"secret": "test-secret"},
            operator=operator,
        )
    )
    try:
        session = await engine.get_or_create_session()
        masked = await engine.transform("mail alice@example.com", session=session)
        assert re.fullmatch(rf"mail <PRIVYX_EMAIL_[0-9A-F]{{{length}}}>", masked.text)
        restored = await engine.restore(masked.text, session.session_id, session=session)
        assert restored.text == "mail alice@example.com"
    finally:
        await close()


@pytest.mark.parametrize("length", [5, 65])
def test_operator_id_length_outside_6_to_64_is_rejected(length: int) -> None:
    from pydantic import ValidationError

    from privyx.operators import EncryptOperator, PseudonymOperator

    for operator in (PseudonymOperator, EncryptOperator):
        with pytest.raises(ValidationError):
            operator(id_length=length, key="ab" * 32) if operator is EncryptOperator else operator(
                id_length=length
            )


def test_the_old_token_id_length_moves_to_the_operator_with_a_warning(tmp_path: Path) -> None:

    config = tmp_path / "privyx.yaml"
    config.write_text("token:\n  id_length: 8\n")
    warnings: list[str] = []

    loaded = read_config(config)
    warnings = loaded.warnings
    settings = loaded.settings

    assert settings.operator.model_dump()["id_length"] == 8
    assert "'token.id_length' is deprecated; write 'operator.id_length' (removed in 0.4.0)" in (
        warnings
    )


async def test_dlp_check_comes_from_proxy_config() -> None:
    from privyx.app._build import build_engine
    from privyx.config.schema import Settings

    on, close_on = await build_engine(Settings(vault={"type": "memory"}))  # the default
    off, close_off = await build_engine(
        Settings(vault={"type": "memory"}, proxy={"dlp_check": False})
    )
    await close_off()
    await close_on()
    assert (off.dlp_check, on.dlp_check) == (False, True)


async def test_the_dlp_check_splits_at_underscores_when_a_detector_does() -> None:
    from privyx.app._build import build_engine
    from privyx.config.schema import Settings

    flags = []
    for detector in (
        {"regex": {"underscore_separates_words": False}},
        {
            "regex": {"underscore_separates_words": False},
            "own": {"type": "regex", "builtins": False, "underscore_separates_words": True},
        },
    ):
        engine, close = await build_engine(Settings(vault={"type": "memory"}, detectors=detector))
        await close()
        flags.append(engine.underscore_separates_words)
    assert flags == [False, True]


async def test_transform_takes_spans_already_decided() -> None:
    """``detection=`` (since 0.1.31) still replaces the detection step."""
    from privyx.core import Detection, Span

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


class _LockingVault(MemoryVault):
    """A memory vault with the cross-process lock a Redis vault has."""

    def __init__(self) -> None:
        super().__init__()
        self.locked: list[str] = []

    @contextlib.asynccontextmanager
    async def lock(self, session_id: str) -> AsyncIterator[str]:
        self.locked.append(session_id)
        yield "token"

    async def save(self, session: Session, *, lock: str | None = None) -> None:
        await super().save(session)


@pytest.mark.parametrize(("secret", "locked"), [("k" * 64, False), ("auto", True)])
async def test_an_anchor_skips_the_shared_lock_only_with_one_key_for_all(
    secret: str, locked: bool
) -> None:
    """A key every process shares makes pseudonyms value-derived; ``auto`` is each host's own."""
    from privyx.anchors import HMACAnchor

    vault = _LockingVault()
    anchor = HMACAnchor(secret=secret)
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=await opened(PseudonymOperator(), anchor=anchor),
        vault=vault,
    )
    async with engine.session_scope("s1"):
        pass
    assert bool(vault.locked) == locked
