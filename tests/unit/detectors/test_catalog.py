"""Built-in entities with a checksum, structure, or context check, and context words."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from privyx import load_config
from privyx.app._build import build_detector
from privyx.core import BUILTIN_ENTITIES, ConfigError, Context
from privyx.detectors._bip39 import WORDS as BIP39_WORDS
from privyx.detectors._catalog import _BASE58, _BECH32, _polymod, shannon
from privyx.detectors._pattern import DEFAULT_PATTERNS, OPTIONAL_PATTERNS
from privyx.policies import StrictPolicy

#: The wallet, national-ID, and bank account entities, turned on for every test below.
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
    "IBAN",
]


def _found(text: str, **config: object) -> list[tuple[str, str]]:
    detector = build_detector({"regex": {"cache": False, "enable": CATALOG, **config}})
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


WALLET_AND_IDS = f"send {P2PKH}, DNI 12345678Z, NIK 3201014501900001"
FOUND = [("CRYPTO_WALLET", P2PKH), ("ES_NIF", "12345678Z"), ("ID_NIK", "3201014501900001")]


def test_wallets_and_national_ids_are_on_by_default_in_regex() -> None:
    assert _found(WALLET_AND_IDS, enable=[]) == FOUND


def test_without_builtins_enable_takes_wallets_and_national_ids() -> None:
    assert _found(WALLET_AND_IDS, builtins=False, enable=[]) == []
    assert _found(WALLET_AND_IDS, builtins=False) == FOUND


def test_disable_turns_wallets_and_national_ids_off(tmp_path: Path) -> None:
    config = tmp_path / "privyx.yaml"
    config.write_text(f"detectors:\n  regex:\n    disable: {CATALOG}\n")
    detector = build_detector(load_config(config).detectors)
    assert detector.detect_sync(WALLET_AND_IDS, Context()).spans == []


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


@pytest.mark.parametrize(
    ("text", "value"),
    [
        # A word after the number stays out of it: each country's length is exact.
        ("refund DE89 3704 0044 0532 0130 00 EUR", "DE89 3704 0044 0532 0130 00"),
        ("PL61 1090 1014 0000 0712 1981 2874 BIC", "PL61 1090 1014 0000 0712 1981 2874"),
        ("iban:DE89370400440532013000.", "DE89370400440532013000"),
        ("to GB82WEST12345698765432", "GB82WEST12345698765432"),
        ("MT84 MALT 0110 0001 2345 MTLC AST0 01S", "MT84 MALT 0110 0001 2345 MTLC AST0 01S"),
        ("NO9386011117947", "NO9386011117947"),  # the shortest
        ("invoice_DE89370400440532013000.pdf", "DE89370400440532013000"),  # `_` separates
        # A code of the same shape before it fails the check; the IBAN starts inside it.
        ("PL00 PL61 1090 1014 0000 0712 1981 2874", "PL61 1090 1014 0000 0712 1981 2874"),
    ],
)
def test_ibans_that_check_out_are_masked_whole(text: str, value: str) -> None:
    # Not a phone number from its middle groups, which left the rest to the provider.
    assert _found(text) == [("IBAN", value)]
    assert _found(text, enable=[]) == [("IBAN", value)]  # on by default


@pytest.mark.parametrize(
    "text",
    [
        "DE89370400440532013001",  # check digits broken
        "DE8937040044053201300",  # a character short for Germany
        "xDE89370400440532013000",  # inside a word
        "AB12 3456 7890 1234",  # no country AB
    ],
)
def test_iban_look_alikes_are_kept(text: str) -> None:
    assert [entity for entity, _ in _found(text)] in ([], ["PHONE"])


def test_an_iban_glued_by_underscores_needs_underscore_separates_words() -> None:
    text = "invoice_DE89370400440532013000_scan.pdf"
    assert _found(text) == [("IBAN", "DE89370400440532013000")]
    assert _found(text, underscore_separates_words=False) == []


def test_disable_iban_leaves_its_groups_to_phone() -> None:
    text = "DE89 3704 0044 0532 0130 00"
    assert _found(text, enable=[], disable=["IBAN"]) == [("PHONE", "3704 0044 0532 0130")]


def test_a_context_word_must_be_near_a_match() -> None:
    config = {"builtins": False, "patterns": {"PASSPORT": r"\b[A-Z]\d{7}\b"}}
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
        build_detector({"regex": {"context": {"PASPORT": ["passport"]}}})


def test_enable_rejects_an_unknown_entity() -> None:
    with pytest.raises(ConfigError, match="MRNN"):
        build_detector({"regex": {"enable": ["MRNN"]}})


#: Fixed, so the check cannot fail on a draw without a digit or with a long word.
_KEY = "Xk7pR2mQ9vLt4" + "Wn8sJd3HbZ6cY"


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
    bits = shannon(_KEY)
    assert _found(text, enable=["HIGH_ENTROPY"]) == [("HIGH_ENTROPY", _KEY)]
    assert _found(text, enable=["HIGH_ENTROPY"], entropy_threshold=bits) == [("HIGH_ENTROPY", _KEY)]
    assert _found(text, enable=["HIGH_ENTROPY"], entropy_threshold=bits + 0.01) == []


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('FOO_BAR_XYZ="hunter2hunter2"', "hunter2hunter2"),
        ("export WEIRD_THING=Zq7Xw2Lp9Rt4", "Zq7Xw2Lp9Rt4"),
        ('const opaque = "mail-Pass-4821"', "mail-Pass-4821"),
        ('{"blob": "a8f!kd93@ld0q"}', "a8f!kd93@ld0q"),
        ("  shared: c2lnbmluZy1rZXk=", "c2lnbmluZy1rZXk="),
    ],
)
def test_assigned_values_are_masked_when_enabled(text: str, value: str) -> None:
    assert _found(text) == []
    assert _found(text, enable=["ASSIGNED_SECRET"]) == [("ASSIGNED_SECRET", value)]


@pytest.mark.parametrize(
    "text",
    [
        'title = "internationalization"',  # a word
        "enabled = false",
        'path = "/usr/local/lib/python3.12"',
        'endpoint = "https://api.example.com/v1"',
        'host: "db1.internal.example"',
        'image = "billing/1.4.2"',
        'started = "2026-01-15T09:30:00Z"',
        'model = "claude-opus-5-5"',
        "commit_sha = 3f2a9b1c4d5e6f708192a3b4c5d6e7f8091a2b3c",
        "user_id = usr_12345abcdef",
        "pattern = ICD10_PATTERN",  # a variable
        "port = 123456789012",
        'contact = "ana@example.com"',
    ],
)
def test_assigned_values_that_cannot_be_secrets_are_kept(text: str) -> None:
    assert [e for e, _ in _found(text, enable=["ASSIGNED_SECRET"])] in ([], ["EMAIL"])


@pytest.mark.parametrize(
    ("text", "entity", "value"),
    [
        ("Patient MRN: 00123456", "MRN", "00123456"),
        ("medical record number #AB-1234567", "MRN", "AB-1234567"),
        ("No. RM 0012345", "MRN", "0012345"),
        ("Diagnosis: E11.9 (type 2 diabetes)", "ICD10", "E11.9"),
        ("ICD-10 code S72.001A", "ICD10", "S72.001A"),
        ("Diagnosis: U07.1", "ICD10", "U07.1"),
    ],
)
def test_medical_identifiers_are_masked_when_enabled(text: str, entity: str, value: str) -> None:
    assert _found(text) == []
    assert _found(text, enable=["MRN", "ICD10"]) == [(entity, value)]


@pytest.mark.parametrize("text", ["see section A12 of the manual", "error E11.9 in build", "MRN"])
def test_medical_look_alikes_are_kept(text: str) -> None:
    assert _found(text, enable=["MRN", "ICD10"]) == []


def test_strict_policy_keeps_an_enabled_opt_in_entity() -> None:
    detector = build_detector({"regex": {"cache": False, "enable": ["MRN"]}})
    detection = detector.detect_sync("MRN 00123456", Context())  # type: ignore[attr-defined]
    kept = StrictPolicy().decide_sync(detection, Context())
    assert [s.entity_type for s in kept.spans] == ["MRN"]


def test_every_builtin_entity_is_known_to_the_strict_policy() -> None:
    assert DEFAULT_PATTERNS.keys() | OPTIONAL_PATTERNS.keys() <= BUILTIN_ENTITIES


#: Made up: the key and chain code of no wallet.
_SECRET = bytes(range(7, 39))
_CHAIN = bytes(9) + bytes(range(40, 72))  # depth, parent, and child, then the chain code
WIF = _btc(0x80, _SECRET + b"\x01")  # compressed: K... or L...
WIF_UNCOMPRESSED = _btc(0x80, _SECRET)  # 5...
WIF_TESTNET = _btc(0xEF, _SECRET + b"\x01")  # c...
XPRV = _btc(0x04, bytes.fromhex("88ade4") + _CHAIN + b"\0" + _SECRET)
TPRV = _btc(0x04, bytes.fromhex("358394") + _CHAIN + b"\0" + _SECRET)
XPUB = _btc(0x04, bytes.fromhex("88b21e") + _CHAIN + b"\x02" + _SECRET)


def _nsec(key: bytes) -> str:
    """``key`` as a Nostr secret key (NIP-19): bech32 after ``nsec``."""
    bits = int.from_bytes(key, "big") << 4  # 256 bits padded to 52 groups of five
    data = "".join(_BECH32[bits >> 5 * i & 31] for i in reversed(range(52)))
    check = _polymod("nsec", data + "qqqqqq") ^ 1
    return "nsec1" + data + "".join(_BECH32[check >> 5 * i & 31] for i in reversed(range(6)))


NSEC = _nsec(_SECRET)
SEED = "ocean ladder velvet frozen puzzle anchor tribe gentle orbit candy smoke ripple"


def _broken(value: str) -> str:
    """``value`` with its last character changed, so that its checksum fails."""
    return value[:-1] + ("q" if value[-1] != "q" else "p")


@pytest.mark.parametrize(
    "key",
    [WIF, WIF_UNCOMPRESSED, WIF_TESTNET, XPRV, TPRV, NSEC],
    ids=["wif", "wif-uncompressed", "wif-testnet", "xprv", "tprv", "nsec"],
)
def test_wallet_private_keys_that_check_out_are_masked_when_enabled(key: str) -> None:
    text = f"restore it with {key} tonight"
    assert _found(text) == []
    assert _found(text, enable=["WALLET_KEY"]) == [("WALLET_KEY", key)]


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        (f"seed phrase: {SEED}", SEED),
        (f'MNEMONIC="{SEED}"', SEED),
        (f"Secret Recovery Phrase:\n  {SEED.upper()}", SEED.upper()),
        (f"wallet_seed = '{SEED}'", SEED),
        # A word of the list next to the phrase goes with it; other words do not.
        (f"my seed phrase {SEED} keep it safe", f"phrase {SEED} keep"),
        (f"recovery words {SEED} store this backup somewhere safe", SEED),
    ],
)
def test_seed_phrases_after_their_word_are_masked_when_enabled(text: str, phrase: str) -> None:
    assert _found(text) == []
    assert _found(text, enable=["WALLET_KEY"]) == [("WALLET_KEY", phrase)]


@pytest.mark.parametrize(
    "text",
    [
        _broken(WIF),
        _broken(XPRV),
        _broken(NSEC),
        XPUB,  # a public key
        "seed: " + SEED.rsplit(" ", 1)[0],  # eleven words
        f"seeds {SEED}",  # no word for a phrase
        "seed the test database with fixtures and then check the output once more",
        "random.seed(42)",
    ],
)
def test_wallet_key_look_alikes_are_kept(text: str) -> None:
    assert _found(text, enable=["WALLET_KEY"]) == []


def test_the_bip39_list_is_the_published_one() -> None:
    text = "\n".join(sorted(BIP39_WORDS)) + "\n"
    published = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
    assert hashlib.sha256(text.encode()).hexdigest() == published
