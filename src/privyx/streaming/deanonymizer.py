"""Incremental streaming deanonymizer.

Guarantee: the concatenation of all output deltas equals the deanonymization of
the concatenation of all input deltas, regardless of how tokens are split across
chunk boundaries — ``stream_process([c1, ..., cN]) == batch_process(c1 + ... +
cN)``.

Algorithm (shared by every recognizer)
--------------------------------------
Prepend any held-back tail (pending) to each incoming chunk, then scan left to
right:

- If the remaining text is a viable token prefix that reaches the end of the
  buffer, hold it back — it may continue in the next chunk.
- Else if a complete token starts here, emit its replacement and skip past it.
- Else emit the single character.

On :meth:`flush`, resolve any held-back text: a complete token becomes its
replacement; anything else is emitted verbatim.

Because each chunk is scanned from the start (pending + chunk), a held-back tail
that turns out NOT to continue is re-resolved correctly on the next scan.

Only the "is there a token here?" decision differs between callers, so it is
delegated to a :class:`~privyx.streaming.recognizer.Recognizer`: a trie of known
pseudonyms for :class:`StreamingDeanonymizer`, or the token codec for
:class:`TokenStreamProcessor`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from privyx.streaming.recognizer import CodecRecognizer, Recognizer, TrieRecognizer
from privyx.token.codec import TokenCodec


@runtime_checkable
class StreamDeanonymizer(Protocol):
    """A stateful text deanonymizer fed one chunk at a time.

    Both :class:`StreamingDeanonymizer` (literal-value matching) and
    :class:`TokenStreamProcessor` (codec-syntax matching) satisfy this, so a
    caller can pick the restoration strategy without depending on which.
    """

    def feed(self, chunk: str) -> str: ...

    def flush(self) -> str: ...


class _BufferedStream:
    """The hold-back scan loop, parameterized by a recognizer."""

    def __init__(self, recognizer: Recognizer) -> None:
        self._recognizer = recognizer
        self._pending = ""

    def feed(self, chunk: str) -> str:
        """Feed a chunk and return the transformed output delta."""
        text = self._pending + chunk
        self._pending = ""
        recognizer = self._recognizer
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            prefix_len = recognizer.longest_prefix_len(text, i)
            if prefix_len > 0 and i + prefix_len == n:
                # The tail is a viable token prefix that may continue next chunk.
                self._pending = text[i:]
                break
            match = recognizer.match_at(text, i)
            if match is not None:
                matched, replacement = match
                out.append(replacement)
                i += len(matched)
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    def flush(self) -> str:
        """Flush any pending buffered text at end of stream."""
        if not self._pending:
            return ""
        match = self._recognizer.match_at(self._pending, 0)
        if match is not None and len(match[0]) == len(self._pending):
            out = match[1]
        else:
            out = self._pending
        self._pending = ""
        return out


class StreamingDeanonymizer(_BufferedStream):
    """Stateful deanonymizer that matches the pseudonyms a session has issued.

    Args:
        mapping: Optional initial pseudonym → original mapping.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._trie = TrieRecognizer(mapping)
        super().__init__(self._trie)

    def update_mapping(self, mapping: dict[str, str]) -> None:
        """Add or refresh pseudonym mappings mid-stream."""
        self._trie.update(mapping)


class TokenStreamProcessor(_BufferedStream):
    """Stateful deanonymizer that recognizes tokens by the configured syntax.

    Args:
        codec: The token codec (syntax authority).
        resolve: Maps a token's text to its original value, or ``None`` if unknown.
    """

    def __init__(self, codec: TokenCodec, resolve: Callable[[str], str | None]) -> None:
        super().__init__(CodecRecognizer(codec, resolve))
