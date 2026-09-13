"""Tests for the privacy engine and operators."""

from __future__ import annotations

import pytest

from privyx.core.engine import PrivacyEngine
from privyx.core.errors import SessionNotFoundError
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