"""Built-in entities with a checksum, structure, or context check, and context words."""

from __future__ import annotations

import hashlib
import secrets
import string

import pytest

from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.privacy.detector.catalog import _BASE58
from privyx.privacy.detector.yaml import build_detector

#: The wallet and national-ID entities, turned on for every test below.
CATALOG = [
    "CRYPTO_WALLET",
    "ID_NIK",
    "ID_NPWP",
    "SG_NRIC_FIN",
    "MY_NRIC",
    "ES_NIF",
    "ES_NIE",
    "UK_NHS",
    "IN_AADHAAR",
]


def _found(text: str, **config: object) -> list[tuple[str, str]]:
    detector = build_detector({"type": "regex", "cache": False, "enable": CATALOG, **config})
    detection = detector.detect_sync(text, Context())  # type: ignore[attr-defined]
    return [(s.entity_type, s.text) for s in detection.merged(text).spans]


def _base58(raw: bytes) -> str:
    number, out = int.from_bytes(raw, "big"), ""
    while number:
        number, rest = divmod(number, 58)
        out = _BASE58[rest] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def _btc(version: int, payload: bytes) -> str:
    body = bytes([version]) + payload
    return _base58(body + hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4])


P2PKH = _btc(0, bytes(range(20)))
P2SH = _btc(5, bytes(range(20, 40)))
SOLANA = _base58(bytes(range(1, 33)))
#: BIP 173 and BIP 350 test vectors.
BECH32 = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
BECH32M = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"


@pytest.mark.parametrize(
    "address", [P2PKH, P2SH, BECH32, BECH32M, BECH32.upper(), "0x" + "ab" * 20]
)
def test_wallet_addresses_with_a_valid_checksum_are_masked(address: str) -> None:
    assert _found(f"send to {address} today") == [("CRYPTO_WALLET", address)]


@pytest.mark.parametrize(
    "text",
    [
        P2PKH[:-1] + ("x" if P2PKH[-1] != "x" else "y"),  # checksum broken
        BECH32[:-1] + "5",
        BECH32[:10] + BECH32[10:].upper(),  # mixed case
        "0x" + "ab" * 19,  # 38 hex digits
        f"just {SOLANA}",  # no checksum, no context
    ],
)
def test_wallet_look_alikes_are_kept(text: str) -> None:
    assert _found(text) == []


def test_solana_address_needs_a_context_word() -> None:
    assert _found(f"my solana wallet: {SOLANA}") == [("CRYPTO_WALLET", SOLANA)]


def test_solana_address_that_looks_like_bitcoin_gets_the_solana_check() -> None:
    address = _base58(bytes(31) + b"\x01")  # 32 characters starting with 1
    assert _found(f"my solana wallet: {address}") == [("CRYPTO_WALLET", address)]


def test_wallets_and_national_ids_are_off_by_default() -> None:
    text = f"send {P2PKH}, DNI 12345678Z, NIK 3201014501900001"
    assert _found(text, enable=[]) == []


@pytest.mark.parametrize(
    ("text", "entity", "value"),
    [
        ("NIK 3201014501900001", "ID_NIK", "3201014501900001"),
        ("NIK 3201015501900001", "ID_NIK", "3201015501900001"),  # woman: day + 40
        ("NPWP 01.234.567.8-901.000", "ID_NPWP", "01.234.567.8-901.000"),
        ("npwp: 012345678901000", "ID_NPWP", "012345678901000"),
        ("NRIC S1234567D", "SG_NRIC_FIN", "S1234567D"),
        ("FIN G1234567X", "SG_NRIC_FIN", "G1234567X"),
        ("MyKad 850101-14-5678", "MY_NRIC", "850101-14-5678"),
        ("DNI 12345678Z", "ES_NIF", "12345678Z"),
        ("NIE X1234567L", "ES_NIE", "X1234567L"),
        ("NHS number 943 476 5919", "UK_NHS", "943 476 5919"),
        ("aadhaar 2345 6789 0124", "IN_AADHAAR", "2345 6789 0124"),
    ],
)
def test_national_ids_that_check_out_are_masked(text: str, entity: str, value: str) -> None:
    assert _found(text) == [(entity, value)]


@pytest.mark.parametrize(
    "text",
    [
        "3201019913900001",  # month 13
        "9901014501900001",  # no province 99
        "3200014501900001",  # regency 00
        "id 1731045678123456",  # a microsecond timestamp
        "012345678901000",  # 15 digits, no NPWP nearby
        "S1234567A",  # wrong check letter
        "851301-14-5678",  # month 13
        "12345678A",
        "943 476 5918",  # NHS checksum broken
        "number 943 476 5919",  # NHS without its context word
        "2345 6789 0124",  # Aadhaar without its context word
        "aadhaar 2345 6789 0125",
    ],
)
def test_national_id_look_alikes_are_kept(text: str) -> None:
    assert [entity for entity, _ in _found(text)] in ([], ["PHONE"])


def test_a_context_word_must_be_near_a_match() -> None:
    config = {"type": "yaml", "patterns": {"PASSPORT": r"\b[A-Z]\d{7}\b"}}
    context = {"context": {"PASSPORT": ["passport", "paspor"]}}
    text = "order A1234567 shipped to the old office last week; passport no. B7654321"
    assert _found(text, **config) == [("PASSPORT", "A1234567"), ("PASSPORT", "B7654321")]
    assert _found(text, **config, **context) == [("PASSPORT", "B7654321")]
    far = "passport one two three four five six A1234567"
    assert _found(far, **config, **context) == []


def test_context_replaces_a_builtin_entitys_own_words() -> None:
    text = "kesehatan 943 476 5919"
    assert _found(text, context={"UK_NHS": ["kesehatan"]}) == [("UK_NHS", "943 476 5919")]


def test_context_for_an_entity_the_detector_lacks_is_rejected() -> None:
    with pytest.raises(ConfigError, match="PASPORT"):
        build_detector({"type": "regex", "context": {"PASPORT": ["passport"]}})


def test_enable_rejects_an_unknown_entity() -> None:
    with pytest.raises(ConfigError, match="MRNN"):
        build_detector({"type": "regex", "enable": ["MRNN"]})


_KEY = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40))


@pytest.mark.parametrize(
    "text",
    [
        "token q8Rz2LmXv4Tn7Kp1Wd9Hs3Jc in the log",
        "YW5uOmh1bnRlcjJyb2Nrcw==",
    ],
)
def test_high_entropy_strings_are_masked_when_enabled(text: str) -> None:
    value = text.split()[1] if " " in text else text
    assert _found(text) == []
    assert _found(text, enable=["HIGH_ENTROPY"]) == [("HIGH_ENTROPY", value)]


@pytest.mark.parametrize(
    "text",
    [
        "commit 3f2a9b1c4d5e6f708192a3b4c5d6e7f8091a2b3c",
        "id 123e4567-e89b-12d3-a456-426614174000",
        "OAuth2AuthorizationCode getUserById2Async ISO8601DateFormatter",
        "IMG_20240101_123456 k8sDeploymentConfig1",
        "token <PRIVYX_EMAIL_ff8d9819fc0e> here",
        "aA1aA1aA1aA1aA1aA1aA1",  # random-looking classes, little entropy
    ],
)
def test_hashes_uuids_and_identifiers_are_not_high_entropy(text: str) -> None:
    assert _found(text, enable=["HIGH_ENTROPY"]) == []


def test_entropy_threshold_is_configurable() -> None:
    text = f"key {_KEY}"
    assert _found(text, enable=["HIGH_ENTROPY"]) == [("HIGH_ENTROPY", _KEY)]
    assert _found(text, enable=["HIGH_ENTROPY"], entropy_threshold=7.0) == []
