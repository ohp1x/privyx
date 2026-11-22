"""HMAC anchor — deterministic, keyed pseudonymization."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from privyx.anchors._base import Anchor
from privyx.core import ConfigError, Context, Services


class HMACAnchor(Anchor):
    """HMAC-SHA256 of the value under ``secret``, which must be kept secret.

    ``secret: auto``, the default, is the per-user key in
    ``~/.privyx/anchor.key`` (``PRIVYX_ANCHOR_KEY_PATH``), read or created by
    :meth:`open`, or on first use when the anchor was not opened.  The app
    builds no anchor for an empty ``secret``.
    """

    name = "hmac"

    class Options(Anchor.Options):
        secret: str = "auto"  # the per-user key in ~/.privyx/anchor.key; "" for none

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        secret = self.options.secret
        self._key = None if secret == "auto" else secret.encode()

    async def open(self, services: Services) -> None:
        """Read the per-user key for ``secret: auto``, creating it on first use.

        Raises:
            ConfigError: If the key cannot be read or created.
        """
        self._resolved()

    async def anchor(self, text: str, context: Context) -> str:
        mac = hmac.new(self._resolved(), text.encode(), hashlib.sha256)
        return f"PRIVYX_{mac.hexdigest()}"

    def _resolved(self) -> bytes:
        if self._key is None:
            from privyx.security.keys import read_or_create_anchor_secret

            self._key = read_or_create_anchor_secret().encode()
        return self._key


class PASPAnchor(Anchor):
    """Reserved: a ``pasp`` anchor is not implemented, so building one fails."""

    name = "pasp"
    Options = HMACAnchor.Options

    def __init__(self, **options: Any) -> None:
        raise ConfigError("anchor type 'pasp' is not implemented yet; use 'hmac'")

    async def anchor(self, text: str, context: Context) -> str:  # pragma: no cover
        raise NotImplementedError
