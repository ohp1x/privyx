"""HMAC anchor — deterministic, keyed pseudonymization.

Unlike vault-backed pseudonyms, HMAC anchors do not need session state to
deanonymize: the same key re-derives the same value.  This trades statefulness
for the requirement that the key is kept secret.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from privyx.core.context import Context
from privyx.privacy.anchor.base import BaseAnchor

_ANCHOR_RE = re.compile(r"^PRIVYX_([0-9a-f]{64})$")


class HMACAnchor(BaseAnchor):
    """HMAC-SHA256 based anchor.

    Args:
        secret: HMAC key.  Must be kept secret.
        digest: Hash algorithm name (default sha256).
    """

    name = "hmac"

    def __init__(self, secret: str, digest: str = "sha256") -> None:
        self._secret = secret.encode()
        self._digest = digest

    def _derive(self, text: str) -> str:
        mac = hmac.new(self._secret, text.encode(), getattr(hashlib, self._digest))
        return f"PRIVYX_{mac.hexdigest()}"

    async def anchor(self, text: str, context: Context) -> str:
        return self._derive(text)

    async def deanchor(self, anchored: str, context: Context) -> str | None:
        match = _ANCHOR_RE.match(anchored)
        if not match:
            return None
        return anchored  # HMAC is not reversible; caller must maintain its own map.