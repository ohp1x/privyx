"""The anchor kind: derive a pseudonym's id from the value and a key.

An anchor makes the same value get the same pseudonym in every session and
process that shares its key, without a mapping to look it up in.  The
mapping is still kept in the vault, to restore the value: an anchor is
one-way.
"""

from __future__ import annotations

from abc import abstractmethod

from privyx.core import Component, Context


class Anchor(Component, kind="anchor"):
    """Derive a deterministic, keyed string from a value."""

    @abstractmethod
    async def anchor(self, text: str, context: Context) -> str:
        """The keyed derivation of ``text``."""
