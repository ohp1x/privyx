"""Built-in entities told apart from look-alikes by a checksum, structure, or context.

Each entity here is a regex plus a check its match must pass, like ``_luhn`` for
``CREDIT_CARD``.  They are off by default: a detector turns them on with
``enable``, so a patch release does not change what an existing config masks.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
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


# --- Registry ---------------------------------------------------------------

#: Off unless a detector lists them under ``enable``.
OPTIONAL_PATTERNS: dict[str, str] = {
    "CRYPTO_WALLET": WALLET_PATTERN,
}

CHECKS: dict[str, Callable[[re.Match[str]], bool]] = {
    "CRYPTO_WALLET": _wallet,
}

#: Words one of which must be near a match: the number alone says too little.
#: A detector's own ``context`` for the entity replaces these.
CONTEXT: dict[str, frozenset[str]] = {}
