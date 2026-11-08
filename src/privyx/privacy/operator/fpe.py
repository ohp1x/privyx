"""Format-preserving operator — substitutes that pass format validation.

A token such as ``<PRIVYX_EMAIL_1>`` breaks a tool whose argument must be an
email address, an IP, a UUID, or a card number that passes the Luhn check.
This operator substitutes a value of the same format instead, taken from
ranges that cannot belong to anyone:

* an email address at ``example.invalid`` (RFC 2606 reserves ``.invalid``);
* an IPv4 address in ``10.0.0.0/8`` (RFC 1918), an IPv6 one in ``fd00::/8``;
* a version 4 UUID;
* a card number starting ``400000``, the prefix of payment test cards, with a
  valid Luhn check digit;
* anything else: a random letter for each letter and a random digit for each
  digit, punctuation and length unchanged.

Unlike :class:`~privyx.privacy.operator.faker.FakerOperator` it needs no
extra package, and its values do not look like real people.  Like it, a reply
is restored by matching the exact substituted strings, so one the model writes
on its own comes back as the original too.
"""

from __future__ import annotations

import hashlib
import ipaddress
import random
import string
import uuid

from privyx.privacy.operator.faker import LiteralOperator

#: Prefix of the substitute card numbers.
_CARD_PREFIX = "400000"


class FPEOperator(LiteralOperator):
    """Replace each value with a format-valid substitute from a reserved range.

    The substitute is derived from the value and ``seed``, so the same value
    gets the same substitute in every session and run.

    Args:
        seed: Optional salt mixed into the derivation.  Two deployments with
            different seeds substitute the same value differently.
    """

    name = "fpe"

    def __init__(self, seed: int | None = None) -> None:
        self._seed = seed

    def _fake_for(self, entity_type: str, original: str, salt: int = 0) -> str:
        digest = hashlib.sha256(f"{self._seed}:{entity_type}:{original}:{salt}".encode()).digest()
        return substitute(entity_type, original, random.Random(digest))


def substitute(entity_type: str, value: str, rng: random.Random) -> str:
    """A value in ``value``'s format, drawn from ``rng``."""
    local, at, domain = value.rpartition("@")
    if at and local and domain:
        return f"user-{rng.getrandbits(32):08x}@example.invalid"
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        if address.version == 4:
            return str(ipaddress.IPv4Address((10 << 24) | rng.getrandbits(24)))
        return str(ipaddress.IPv6Address((0xFD << 120) | rng.getrandbits(120)))
    try:
        uuid.UUID(value)
    except ValueError:
        pass
    else:
        if len(value) == 36:  # the hyphenated form, not 32 bare hex digits
            return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    digits = sum(c.isdigit() for c in value)
    if "CARD" in entity_type.upper() and digits >= len(_CARD_PREFIX) + 1:
        return _card(value, digits, rng)
    return "".join(_same_class(c, rng) for c in value)


def _same_class(char: str, rng: random.Random) -> str:
    if char.isdigit():
        return rng.choice(string.digits)
    if char in string.ascii_lowercase:
        return rng.choice(string.ascii_lowercase)
    if char in string.ascii_uppercase:
        return rng.choice(string.ascii_uppercase)
    if char.isalpha():
        return rng.choice(string.ascii_letters)
    return char


def _card(value: str, length: int, rng: random.Random) -> str:
    """A Luhn-valid number of ``length`` digits, laid out like ``value``."""
    body = _CARD_PREFIX + "".join(rng.choice(string.digits) for _ in range(length - 7))
    digits = iter(body + _luhn_digit(body))
    return "".join(next(digits) if c.isdigit() else c for c in value)


def _luhn_digit(body: str) -> str:
    """The check digit that makes ``body`` + it pass the Luhn check."""
    total = 0
    for i, c in enumerate(reversed(body)):
        d = int(c)
        if i % 2 == 0:  # doubled: the check digit will sit to its right
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return str(-total % 10)
