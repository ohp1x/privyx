"""Recognizers — the pluggable "is there a token here?" strategy for streaming.

The streaming reducer in :mod:`privyx.streaming.deanonymizer` is a fixed
hold-back loop that asks two questions of a recognizer at each position:

* :meth:`longest_prefix_len` — could the text starting here still grow into a
  token?  (Used to hold a tail back across a chunk boundary.)
* :meth:`match_at` — is there a *complete* token here, and what should replace
  it?

Two recognizers implement that contract:

* :class:`TrieRecognizer` matches the *exact pseudonym strings* a session has
  issued (a trie of known values).  Syntax-agnostic; this is the historical
  behavior of :class:`~privyx.streaming.deanonymizer.StreamingDeanonymizer`.
* :class:`CodecRecognizer` matches *any* text that fits the configured token
  syntax, reconstructs the :class:`~privyx.token.model.LogicalToken`, and
  resolves it — so streaming recognizes tokens the same way the codec does
  everywhere else (``.temp/token-system.md`` §7).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from privyx.streaming.trie import PseudonymTrie
from privyx.token.codec import TokenCodec


@runtime_checkable
class Recognizer(Protocol):
    """Decides where tokens are and what they become, one position at a time."""

    def longest_prefix_len(self, text: str, pos: int) -> int: ...

    def match_at(self, text: str, pos: int) -> tuple[str, str] | None:
        """Return ``(matched_text, replacement)`` for a token at ``pos``, else ``None``."""
        ...


class TrieRecognizer:
    """Recognize the exact pseudonym strings a session has issued."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._trie = PseudonymTrie()
        if mapping:
            self._trie.update(mapping)

    def update(self, mapping: dict[str, str]) -> None:
        self._trie.update(mapping)

    def longest_prefix_len(self, text: str, pos: int) -> int:
        return self._trie.longest_prefix_len(text, pos)

    def match_at(self, text: str, pos: int) -> tuple[str, str] | None:
        # The trie only holds known pseudonyms, so a match is always resolvable.
        return self._trie.match_at(text, pos)


class CodecRecognizer:
    """Recognize tokens by the configured syntax and resolve them via ``resolve``.

    Args:
        codec: The token codec (the syntax authority).
        resolve: Maps a token's *text* to its original value, or ``None`` when the
            token is unknown to this session.  An unknown but syntactically valid
            token is passed through verbatim — a stream may legitimately contain
            token-shaped text we never issued, and mangling it would corrupt the
            response (``.temp/token-system.md`` §6).
    """

    def __init__(self, codec: TokenCodec, resolve: Callable[[str], str | None]) -> None:
        self._codec = codec
        self._resolve = resolve

    def longest_prefix_len(self, text: str, pos: int) -> int:
        return self._codec.longest_prefix_len(text, pos)

    def match_at(self, text: str, pos: int) -> tuple[str, str] | None:
        match = self._codec.match_at(text, pos)
        if match is None:
            return None
        original = self._resolve(match.text)
        return (match.text, match.text if original is None else original)
