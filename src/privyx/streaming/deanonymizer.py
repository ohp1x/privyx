"""Incremental streaming deanonymizer.

Guarantees:  the concatenation of all output deltas equals the
deanonymization of the concatenation of all input deltas, regardless of how
pseudonyms are split across chunk boundaries.

Algorithm
---------
1. Keep a trie of pseudonyms for the session.
2. Prepend any held-back tail (pending) to each incoming chunk, then scan
   left to right:
   - If the remaining text is a prefix of some pseudonym AND reaches the end
     of the buffer, hold it back (it may continue in the next chunk).
   - Else if it is a complete pseudonym, emit its original value.
   - Else emit the single character.
3. On ``flush``, resolve any held-back text: if it is a complete pseudonym,
   emit its original; otherwise emit it verbatim.

Because each chunk is scanned from the start (pending + chunk), a held-back
tail that turns out NOT to continue is re-resolved correctly on the next
scan — a complete pseudonym that no longer reaches the buffer end is emitted,
and an incomplete prefix is flushed as plain text.
"""

from __future__ import annotations

from privyx.streaming.trie import PseudonymTrie


class StreamingDeanonymizer:
    """Stateful deanonymizer for a single stream.

    Args:
        mapping: Optional initial pseudonym → original mapping.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._trie = PseudonymTrie()
        self._pending = ""
        if mapping:
            self._trie.update(mapping)

    def update_mapping(self, mapping: dict[str, str]) -> None:
        """Add or refresh pseudonym mappings mid-stream."""
        self._trie.update(mapping)

    def feed(self, chunk: str) -> str:
        """Feed a chunk and return the deanonymized output delta."""
        text = self._pending + chunk
        self._pending = ""
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            prefix_len = self._trie.longest_prefix_len(text, i)
            if prefix_len > 0 and i + prefix_len == n:
                # The tail is a prefix of a pseudonym that may continue in the
                # next chunk — hold it back.
                self._pending = text[i:]
                break
            match = self._trie.match_at(text, i)
            if match is not None:
                pseudo, original = match
                out.append(original)
                i += len(pseudo)
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    def flush(self) -> str:
        """Flush any pending buffered text at end of stream.

        A held-back tail that is a complete pseudonym resolves to its
        original; anything else is emitted verbatim.
        """
        if not self._pending:
            return ""
        match = self._trie.match_at(self._pending, 0)
        if match is not None and len(match[0]) == len(self._pending):
            out = match[1]
        else:
            out = self._pending
        self._pending = ""
        return out