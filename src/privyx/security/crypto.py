"""Authenticated encryption for the ``encrypt`` operator.

A :class:`DeterministicCipher` wraps AES-256-GCM with a **deterministic**,
SIV-style nonce: the nonce is an HMAC of the plaintext, so encrypting the same
value twice yields byte-identical output.  That determinism is what lets the
operator dedupe (same value ⇒ same token ⇒ idempotent vault write); the only
information it reveals is plaintext *equality*, which every reversible Privyx
operator already reveals (same value ⇒ same token).

Nonce reuse is safe here precisely because the nonce is a function of the
plaintext: an identical nonce implies an identical plaintext (bar an HMAC
collision), never the same nonce over two *different* messages — the case GCM
forbids.  The GCM tag authenticates every value, so a corrupted or foreign blob
fails to decrypt and is passed through untouched rather than yielding garbage.

Importing this module requires the optional ``cryptography`` extra; the operator
imports it lazily and turns a missing package into a startup ``ConfigError``.
"""

from __future__ import annotations

import base64
import binascii

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from privyx.security.keys import derive

#: AES-GCM nonce length in bytes (96 bits — the standard/recommended size).
NONCE_BYTES = 12


class DeterministicCipher:
    """AES-256-GCM with a plaintext-derived nonce (reversible, deterministic).

    Args:
        key: 32-byte AES-256 key (validated by :class:`AESGCM`).
    """

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` to a URL-safe base64 ``nonce ‖ ciphertext‖tag``."""
        nonce = derive(self._key, "nonce", plaintext)[:NONCE_BYTES]
        blob = nonce + self._aead.encrypt(nonce, plaintext.encode(), None)
        return base64.urlsafe_b64encode(blob).decode("ascii")

    def decrypt(self, blob: str) -> str | None:
        """Decrypt a value produced by :meth:`encrypt`, or ``None`` if it is not one.

        A value that is not valid base64, is too short, fails the GCM tag, or is
        not UTF-8 is treated as "not ours" and reported as ``None`` so the caller
        leaves it untouched (never a hard error mid-stream).
        """
        try:
            raw = base64.urlsafe_b64decode(blob)
        except (binascii.Error, ValueError):
            return None
        if len(raw) <= NONCE_BYTES:
            return None
        nonce, ciphertext = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, ciphertext, None).decode()
        except (InvalidTag, ValueError):
            return None
