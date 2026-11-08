"""Built-in entities told apart from look-alikes by a checksum, structure, or context.

Each entity here is a regex plus a check its match must pass, like ``_luhn`` for
``CREDIT_CARD``.  They are off by default: a detector turns them on with
``enable``, so a patch release does not change what an existing config masks.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Collection
from datetime import date
from hashlib import sha256

#: How many words on each side :func:`near` looks at, as Presidio does.
CONTEXT_RADIUS = 5

_WORD = re.compile(r"[^\W_]+")


def near(text: str, start: int, end: int, words: Collection[str]) -> bool:
    """Whether one of ``words`` (lower case, may be phrases) is within five words of the span."""
    before = _WORD.findall(text[max(0, start - 120) : start])[-CONTEXT_RADIUS:]
    after = _WORD.findall(text[end : end + 120])[:CONTEXT_RADIUS]
    window = f" {' '.join(before + after).lower()} "
    return any(f" {word} " in window for word in words)


# --- Crypto wallets ---------------------------------------------------------

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

#: Bitcoin (base58check and bech32/bech32m), EVM, and Solana addresses.  The
#: lookahead drops words too short to be one before trying each kind.
WALLET_PATTERN = r"""(?x)
(?<![\w]) (?=\w{26})
(?:
    (?P<base58>[13][1-9A-HJ-NP-Za-km-z]{25,34})
  | (?P<bech32>(?i:bc1[02-9ac-hj-np-z]{11,71}))
  | (?P<evm>0x[0-9a-fA-F]{40})
  | (?P<solana>[1-9A-HJ-NP-Za-km-z]{32,44})
)
(?![\w])
"""

#: A Solana address has no checksum, so it needs one of these nearby.
_SOLANA_CONTEXT = frozenset({"solana", "sol", "wallet", "address", "pubkey", "public key"})


def _base58_decode(value: str) -> bytes:
    number = 0
    for char in value:
        number = number * 58 + _BASE58.index(char)
    zeros = len(value) - len(value.lstrip("1"))
    return b"\0" * zeros + number.to_bytes((number.bit_length() + 7) // 8, "big")


def _base58check(value: str) -> bool:
    """A mainnet pay-to-key-hash (``1…``) or pay-to-script-hash (``3…``) address."""
    raw = _base58_decode(value)
    checksum = sha256(sha256(raw[:-4]).digest()).digest()[:4]
    return len(raw) == 25 and raw[0] in (0, 5) and raw[-4:] == checksum


def _bech32(value: str) -> bool:
    """A mainnet SegWit address with a valid bech32 (BIP 173) or bech32m (BIP 350) checksum."""
    if value != value.lower() and value != value.upper():
        return False
    data = value.lower()[3:]
    values = [3, 3, 0, 2, 3] + [_BECH32.index(char) for char in data]  # "bc" expanded
    check = 1
    for item in values:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ item
        for i, generator in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            if top >> i & 1:
                check ^= generator
    return check in (1, 0x2BC830A3)


def _wallet(match: re.Match[str]) -> bool:
    """Whether the address checks out.

    A ``1…`` string that fails the Bitcoin checksum may still be a Solana
    address, so it gets the Solana check.  An EVM address is taken as is: its
    mixed-case checksum (EIP-55) needs Keccak-256, which the standard library
    lacks, and a typo in one is as revealing as the address.
    """
    if match["base58"] and _base58check(match[0]):
        return True
    if match["bech32"]:
        return _bech32(match[0])
    if match["evm"]:
        return True
    return len(_base58_decode(match[0])) == 32 and near(
        match.string, match.start(), match.end(), _SOLANA_CONTEXT
    )


# --- National IDs -----------------------------------------------------------

#: Province codes of an Indonesian NIK (Kemendagri).
_NIK_PROVINCES = frozenset(
    [*range(11, 20), 21, *range(31, 37), 51, 52, 53, *range(61, 66), *range(71, 77), 81, 82]
    + [*range(91, 97)]
)


def _real_date(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _nik(match: re.Match[str]) -> bool:
    """Indonesian NIK: province, regency, district, birth date (+40 for women), serial."""
    nik = match[0]
    day = int(nik[6:8])
    return (
        int(nik[:2]) in _NIK_PROVINCES
        and nik[2:4] != "00"
        and nik[4:6] != "00"
        and _real_date(2000, int(nik[8:10]), day - 40 if day > 40 else day)
        and nik[12:] != "0000"
    )


def _npwp(match: re.Match[str]) -> bool:
    """Indonesian NPWP: the dotted form as written; 15 bare digits next to ``npwp``."""
    return "." in match[0] or near(match.string, match.start(), match.end(), {"npwp"})


def _nric_fin(match: re.Match[str]) -> bool:
    """Singapore NRIC/FIN: weighted digit sum picks the final letter."""
    value = match[0]
    total = sum(int(d) * w for d, w in zip(value[1:8], (2, 7, 6, 5, 4, 3, 2), strict=True))
    total += 4 if value[0] in "TG" else 0
    letters = "JZIHGFEDCBA" if value[0] in "ST" else "XWUTRQPNMLK"
    return letters[total % 11] == value[8]


def _mykad(match: re.Match[str]) -> bool:
    """Malaysian MyKad: ``YYMMDD-PB-###G`` with a real birth date."""
    value = match[0]
    return _real_date(2000, int(value[2:4]), int(value[4:6])) and value[7:9] != "00"


_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def _dni(match: re.Match[str]) -> bool:
    """Spanish DNI (NIF of a person): eight digits and a mod-23 letter."""
    value = match[0].replace("-", "")
    return _DNI_LETTERS[int(value[:8]) % 23] == value[8]


def _nie(match: re.Match[str]) -> bool:
    """Spanish NIE: X, Y, or Z (0, 1, 2), seven digits, and a mod-23 letter."""
    value = match[0].replace("-", "")
    return _DNI_LETTERS[int(str("XYZ".index(value[0])) + value[1:8]) % 23] == value[8]


def _nhs(match: re.Match[str]) -> bool:
    """UK NHS number: mod-11 check digit."""
    digits = [int(char) for char in match[0] if char.isdigit()]
    check = 11 - sum(d * w for d, w in zip(digits[:9], range(10, 1, -1), strict=True)) % 11
    return (0 if check == 11 else check) == digits[9]


_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def _aadhaar(match: re.Match[str]) -> bool:
    """Indian Aadhaar: Verhoeff check digit."""
    check = 0
    digits = [int(char) for char in match[0] if char.isdigit()]
    for i, digit in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][digit]]
    return check == 0


# --- High entropy (opt-in) --------------------------------------------------

#: Bits per character a random-looking string needs; ``entropy_threshold`` overrides it.
#: A random 16-character key scores about 3.75 (its 16 characters cap it at 4).
ENTROPY_THRESHOLD = 3.0

#: Runs of 16 to 512 key characters, Base64 padding included.  Longer runs
#: (images, archives) and Privyx tokens (``<PRIVYX_EMAIL_…>``) are left out.
HIGH_ENTROPY_PATTERN = r"(?<![<\w+/-])[A-Za-z0-9+/_-]{16,512}+={0,2}(?![\w+/=-])"

_HEX = re.compile(r"[0-9a-fA-F-]+")
#: The words of an identifier: ``get``, ``User``, ``HTTP``, ``2``.
_CHUNK = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

#: Longest average word an identifier-looking string may have to count as
#: random.  ``getUserById2Async`` averages 2.8; 95% of random 16-character
#: keys average 2.7 or less, longer ones less still.
_MAX_CHUNK = 2.75


def shannon(value: str) -> float:
    """Shannon entropy of ``value`` in bits per character."""
    counts = Counter(value).values()
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts)


def high_entropy(threshold: float) -> Callable[[re.Match[str]], bool]:
    """A check passing random-looking strings: upper case, lower case, and digits
    mixed so that no words show, at ``threshold`` bits per character or more.

    Hex (git and other hashes, UUIDs, colors) is left out: most hex in text is a
    public hash.  Identifiers such as ``OAuth2AuthorizationCode`` are left out
    by their words, which entropy alone cannot tell from a key this short.
    """

    def check(match: re.Match[str]) -> bool:
        value = match[0].rstrip("=")
        if not (
            any(c.isupper() for c in value)
            and any(c.islower() for c in value)
            and any(c.isdigit() for c in value)
        ) or _HEX.fullmatch(value):
            return False
        chunks = _CHUNK.findall(value)
        return sum(map(len, chunks)) / len(chunks) <= _MAX_CHUNK and shannon(value) >= threshold

    return check


# --- Assignment heuristic (opt-in) ------------------------------------------

#: Any name assigned a value of 12 to 256 characters, in env, YAML, JSON, or code.
ASSIGNMENT_PATTERN = r"""(?x)
(?<![\w.-]) (?P<name> [A-Za-z_][\w.-]{0,63}+ ) ["']? [ \t]*
(?: := | => | [:=](?![=:]) ) [ \t]*
(?: (?P<q>["'`]) | (?![$(\[{]) )
(?P<value> [^\s"'`,;(){}\[\]<>=]{12,256}+ ={0,2} )
(?(q) (?P=q) | (?![^\s,;)}\]]) )
"""

#: Names that hold an identifier, a hash, or a location rather than a secret.
_NOT_A_SECRET_NAME = re.compile(
    r"(?i)(?:^|[_.-]|(?<=[a-z])(?=[A-Z]))"
    r"(?:id|ids|uuid|guid|sha|hash|digest|checksum|commit|version|ref|branch|tag|name|"
    r"url|uri|host|hostname|domain|path|dir|file|image|region|zone|email|mail|session|"
    r"model|provider|engine|format|mode|level|env|environment|stage|locale|lang|"
    r"language|label|title|type|kind|status|state|pattern|regex|date|time|timestamp)$"
)
_DOTTED = re.compile(r"[\w-]+(?:\.[\w-]+)+")
_DATE_OR_VERSION = re.compile(r"\d{4}-\d{2}-\d{2}|(?:^|[/@v-])\d+(?:\.\d+)+$")
_SYMBOL = re.compile(r"[!#$%^&*+=?~@]")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")


def _wordy(value: str) -> bool:
    """Whether ``value`` reads as words: ``ICD10_PATTERN``, ``getUserById2``."""
    chunks = _CHUNK.findall(value)
    return bool(chunks) and sum(map(len, chunks)) / len(chunks) > _MAX_CHUNK


def _assigned_value(match: re.Match[str]) -> bool:
    """Whether the assigned value can be a secret.

    Not a word or identifier without digits, an unquoted name (a variable), a
    number, a boolean, a path or URL, a dotted name (host, module), a date or
    version, or a hash, ID, or model by the name it is assigned to.
    """
    name, value = match["name"], match["value"]
    if _NOT_A_SECRET_NAME.search(name):
        return False
    if not match["q"] and _IDENTIFIER.fullmatch(value) and _wordy(value):
        return False
    if value.startswith(("/", "~", "./", "../", "$")) or "://" in value or "\\" in value:
        return False
    if _DOTTED.fullmatch(value) or _DATE_OR_VERSION.search(value):
        return False
    if "@" in value and "." in value.rpartition("@")[2]:
        return False
    has_letter = any(c.isalpha() for c in value)
    has_digit = any(c.isdigit() for c in value)
    return has_letter and (has_digit or bool(_SYMBOL.search(value)))


# --- Registry ---------------------------------------------------------------

#: Off unless a detector lists them under ``enable``.  Each ID pattern starts
#: with a digit or letter, its left boundary checked after it: a pattern starting
#: with a lookbehind is tried at every position, which made it 3-5 times slower.
OPTIONAL_PATTERNS: dict[str, str] = {
    "CRYPTO_WALLET": WALLET_PATTERN,
    "ID_NIK": r"\d(?<![\d.-]\d)\d{15}(?![\d.-])",
    "ID_NPWP": r"\d(?<![\d.]\d)(?:\d\.\d{3}\.\d{3}\.\d-\d{3}\.\d{3}|\d{14})(?![\d.-])",
    "SG_NRIC_FIN": r"[STFG](?<!\w[STFG])\d{7}[A-Z]\b",
    "MY_NRIC": r"\d(?<![\d-]\d)\d{5}-\d{2}-\d{4}(?![\d-])",
    "ES_NIF": r"\d(?<!\w\d)\d{7}-?[A-Z]\b",
    "ES_NIE": r"[XYZ](?<!\w[XYZ])-?\d{7}-?[A-Z]\b",
    "UK_NHS": r"\d(?<!\d\d)(?<!\d[ -]\d)\d{2}([ -]?)\d{3}\1\d{4}(?![\d-])",
    "IN_AADHAAR": r"[2-9](?<!\d[2-9])(?<!\d[ -][2-9])\d{3}([ -]?)\d{4}\1\d{4}(?![\d-])",
    "HIGH_ENTROPY": HIGH_ENTROPY_PATTERN,
    "ASSIGNED_SECRET": ASSIGNMENT_PATTERN,
}

CHECKS: dict[str, Callable[[re.Match[str]], bool]] = {
    "CRYPTO_WALLET": _wallet,
    "ID_NIK": _nik,
    "ID_NPWP": _npwp,
    "SG_NRIC_FIN": _nric_fin,
    "MY_NRIC": _mykad,
    "ES_NIF": _dni,
    "ES_NIE": _nie,
    "UK_NHS": _nhs,
    "IN_AADHAAR": _aadhaar,
    "HIGH_ENTROPY": high_entropy(ENTROPY_THRESHOLD),
    "ASSIGNED_SECRET": _assigned_value,
}

#: Words one of which must be near a match: the number alone says too little.
#: A detector's own ``context`` for the entity replaces these.
CONTEXT: dict[str, frozenset[str]] = {
    "UK_NHS": frozenset({"nhs", "national health service"}),
    "IN_AADHAAR": frozenset({"aadhaar", "aadhar", "uid", "uidai"}),
}
