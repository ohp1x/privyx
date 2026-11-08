"""Tests for :class:`FPEOperator`."""

from __future__ import annotations

import ipaddress
import re
import uuid

import pytest

from privyx.core.builder import build_operator
from privyx.core.context import Context
from privyx.core.result import Detection, Span
from privyx.core.session import Session
from privyx.privacy.operator.fpe import FPEOperator


def _luhn(digits: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _fake(entity_type: str, value: str, seed: int | None = None) -> str:
    return FPEOperator(seed=seed)._fake_for(entity_type, value)


def test_email_goes_to_a_reserved_domain() -> None:
    assert re.fullmatch(r"user-[0-9a-f]{8}@example\.invalid", _fake("EMAIL", "alice@example.com"))


@pytest.mark.parametrize(
    ("value", "network"), [("192.168.1.254", "10.0.0.0/8"), ("2001:db8::1", "fd00::/8")]
)
def test_ip_goes_to_a_private_range(value: str, network: str) -> None:
    assert ipaddress.ip_address(_fake("IP_ADDRESS", value)) in ipaddress.ip_network(network)


def test_uuid_stays_a_uuid() -> None:
    fake = _fake("ID", "123e4567-e89b-12d3-a456-426614174000")
    assert uuid.UUID(fake).version == 4
    assert len(fake) == 36


@pytest.mark.parametrize("value", ["4111 1111 1111 1111", "4111-1111-1111-1111", "378282246310005"])
def test_card_passes_luhn_from_the_test_prefix_in_the_same_layout(value: str) -> None:
    fake = _fake("CREDIT_CARD", value)
    digits = re.sub(r"\D", "", fake)
    assert _luhn(digits)
    assert digits.startswith("400000")
    assert re.sub(r"\d", "0", fake) == re.sub(r"\d", "0", value)


def test_anything_else_keeps_its_character_classes() -> None:
    value = "sk-proj-abcDEF123"
    fake = _fake("API_KEY", value)
    assert fake != value
    assert [_class(c) for c in fake] == [_class(c) for c in value]


def _class(char: str) -> str:
    if char.isdigit():
        return "9"
    if char.islower():
        return "a"
    return "A" if char.isupper() else char


def test_the_same_value_gets_the_same_substitute_unless_the_seed_differs() -> None:
    assert _fake("EMAIL", "alice@example.com") == _fake("EMAIL", "alice@example.com")
    assert _fake("EMAIL", "alice@example.com", 1) != _fake("EMAIL", "alice@example.com", 2)


async def test_round_trips_through_the_session() -> None:
    text = "mail alice@example.com from 192.168.1.254"
    spans = [("EMAIL", "alice@example.com"), ("IP_ADDRESS", "192.168.1.254")]
    detection = Detection(
        spans=[Span(text.index(v), text.index(v) + len(v), kind, v) for kind, v in spans]
    )
    operator, session = FPEOperator(), Session()

    masked = await operator.pseudonymize(text, detection, session, Context())

    assert "alice@example.com" not in masked.text
    assert "192.168.1.254" not in masked.text
    assert (await operator.deanonymize(masked.text, session, Context())).text == text


def test_built_from_config() -> None:
    operator = build_operator({"type": "fpe", "seed": 3})
    assert isinstance(operator, FPEOperator)
    assert operator.stream_restore == "literal"
