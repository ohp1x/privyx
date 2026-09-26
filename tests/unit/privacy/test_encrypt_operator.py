"""Tests for :class:`EncryptOperator`.

The ``cryptography`` package is an optional extra, so this module is skipped
entirely when it is not installed rather than failing to import.
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from privyx.core.builder import build_operator  # noqa: E402
from privyx.core.context import Context  # noqa: E402
from privyx.core.errors import ConfigError  # noqa: E402
from privyx.core.result import Detection, Span  # noqa: E402
from privyx.core.session import Session  # noqa: E402
from privyx.privacy.operator.encrypt import EncryptOperator  # noqa: E402
from privyx.security.keys import load_key  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
OTHER_KEY = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

TEXT = "Email alice@example.com or bob@example.com, ssn 123-45-6789"


def _operator(key: str = KEY) -> EncryptOperator:
    return EncryptOperator(key=load_key(key))


def _detection() -> Detection:
    return Detection(
        spans=[
            Span(
                TEXT.index("alice@example.com"),
                TEXT.index("alice@example.com") + 17,
                "EMAIL",
                "alice@example.com",
            ),
            Span(
                TEXT.index("bob@example.com"),
                TEXT.index("bob@example.com") + 15,
                "EMAIL",
                "bob@example.com",
            ),
            Span(TEXT.index("123-45-6789"), TEXT.index("123-45-6789") + 11, "SSN", "123-45-6789"),
        ]
    )


# --- substitution & round-trip ---------------------------------------------


async def test_replaces_originals_with_tokens() -> None:
    result = await _operator().pseudonymize(TEXT, _detection(), Session(), Context())
    assert "alice@example.com" not in result.text
    assert "bob@example.com" not in result.text
    assert "123-45-6789" not in result.text


async def test_round_trips_through_the_session() -> None:
    operator = _operator()
    session = Session()
    result = await operator.pseudonymize(TEXT, _detection(), session, Context())
    restored = await operator.deanonymize(result.text, session, Context())
    assert restored.text == TEXT


async def test_no_plaintext_is_stored_at_rest() -> None:
    """The distinctive property: the vault mapping holds ciphertext, not plaintext."""
    session = Session()
    await _operator().pseudonymize(TEXT, _detection(), session, Context())
    assert session.mapping  # something was stored
    for value in session.mapping.values():
        assert "alice@example.com" not in value
        assert "bob@example.com" not in value
        assert "123-45-6789" not in value


async def test_same_value_reuses_its_token() -> None:
    text = "a@b.com and a@b.com"
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com"), Span(12, 19, "EMAIL", "a@b.com")])
    result = await _operator().pseudonymize(text, detection, Session(), Context())
    tokens = [t.replacement for t in result.transformations]
    assert tokens[0] == tokens[1]  # deterministic id ⇒ dedup


async def test_distinct_values_get_distinct_tokens_and_all_round_trip() -> None:
    emails = [f"user{i}@example.com" for i in range(20)]
    text = " ".join(emails)
    pos, spans = 0, []
    for email in emails:
        start = text.index(email, pos)
        spans.append(Span(start, start + len(email), "EMAIL", email))
        pos = start + len(email)
    operator = _operator()
    session = Session()
    result = await operator.pseudonymize(text, Detection(spans=spans), session, Context())
    assert len(session.mapping) == len(emails)
    restored = await operator.deanonymize(result.text, session, Context())
    assert restored.text == text


# --- determinism ------------------------------------------------------------


async def test_same_key_encrypts_a_value_identically() -> None:
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com")])
    one = await _operator().pseudonymize("a@b.com", detection, Session(), Context())
    two = await _operator().pseudonymize("a@b.com", detection, Session(), Context())
    assert one.text == two.text  # same token across instances/sessions


# --- restore semantics ------------------------------------------------------


async def test_deanonymize_leaves_unknown_tokens_alone() -> None:
    text = "nothing <PRIVYX_EMAIL_DEADBEEFDEADBEEF> here"
    result = await _operator().deanonymize(text, Session(), Context())
    assert result.text == text
    assert result.transformations == []


async def test_wrong_key_cannot_decrypt_and_passes_token_through() -> None:
    """A token whose ciphertext fails the GCM tag is left untouched, not garbled."""
    session = Session()
    result = await _operator(KEY).pseudonymize(TEXT, _detection(), session, Context())
    # A different key cannot authenticate the stored ciphertext.
    restored = await _operator(OTHER_KEY).deanonymize(result.text, session, Context())
    assert restored.text == result.text  # tokens survive verbatim
    assert restored.transformations == []


async def test_corrupt_ciphertext_passes_through() -> None:
    session = Session()
    result = await _operator().pseudonymize(TEXT, _detection(), session, Context())
    # Corrupt every stored value; nothing should decrypt.
    session.mapping = {token: "not-valid-ciphertext" for token in session.mapping}
    restored = await _operator().deanonymize(result.text, session, Context())
    assert restored.text == result.text


# --- builder wiring & fail-fast --------------------------------------------


def test_operator_uses_the_token_stream_restore() -> None:
    """encrypt emits codec tokens, so streaming uses the codec recognizer."""
    assert _operator().stream_restore == "token"


async def test_builder_builds_encrypt_with_a_key() -> None:
    operator = build_operator({"type": "encrypt", "key": KEY})
    assert isinstance(operator, EncryptOperator)
    detection = Detection(spans=[Span(0, 7, "EMAIL", "a@b.com")])
    result = await operator.pseudonymize("a@b.com", detection, Session(), Context())
    assert result.text != "a@b.com"


def test_builder_without_a_key_fails_fast() -> None:
    with pytest.raises(ConfigError):
        build_operator({"type": "encrypt"})


def test_builder_with_a_malformed_key_fails_fast() -> None:
    with pytest.raises(ConfigError):
        build_operator({"type": "encrypt", "key": "tooshort"})
