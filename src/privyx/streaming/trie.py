"""Trie for incremental matching of pseudonyms across chunk boundaries."""

from __future__ import annotations

from collections.abc import Iterator


class TrieNode:
    """A single node in the pseudonym trie."""

    __slots__ = ("children", "terminal")

    def __init__(self) -> None:
        self.children: dict[str, TrieNode] = {}
        self.terminal: str | None = None  # original value if this node ends a pseudonym

    def add(self, token: str, original: str) -> None:
        node: TrieNode = self
        for ch in token:
            child = node.children.get(ch)
            if child is None:
                child = TrieNode()
                node.children[ch] = child
            node = child
        node.terminal = original

    def has_prefix(self, token: str) -> bool:
        node: TrieNode | None = self
        for ch in token:
            if node is None:
                return False
            node = node.children.get(ch)
        return node is not None


class PseudonymTrie:
    """Trie mapping pseudonyms → original values for streaming lookup."""

    def __init__(self) -> None:
        self._root = TrieNode()
        self._longest = 0

    @property
    def longest_pseudonym(self) -> int:
        return self._longest

    def add(self, pseudonym: str, original: str) -> None:
        self._root.add(pseudonym, original)
        self._longest = max(self._longest, len(pseudonym))

    def update(self, mapping: dict[str, str]) -> None:
        for pseudo, original in mapping.items():
            self.add(pseudo, original)

    def resolve(self, token: str) -> str | None:
        """Return the original for an exact pseudonym match."""
        node: TrieNode | None = self._root
        for ch in token:
            if node is None:
                return None
            node = node.children.get(ch)
        return node.terminal if node is not None else None

    def longest_prefix_len(self, text: str, start: int = 0) -> int:
        """Return the length of the longest prefix of any pseudonym starting at ``start``.

        Returns 0 if the character at ``start`` cannot begin any pseudonym.
        """
        node: TrieNode | None = self._root
        longest = 0
        i = start
        while i < len(text):
            if node is None:
                break
            node = node.children.get(text[i])
            if node is None:
                break
            longest = i - start + 1
            i += 1
        return longest

    def match_at(self, text: str, start: int) -> tuple[str, str] | None:
        """Find the longest pseudonym starting at ``start``.

        Returns ``(pseudonym, original)`` or None.  Used to resolve a partial
        pseudonym that may span a chunk boundary.
        """
        node: TrieNode | None = self._root
        best: tuple[str, str] | None = None
        i = start
        while i < len(text):
            if node is None:
                break
            node = node.children.get(text[i])
            if node is None:
                break
            if node.terminal is not None:
                best = (text[start : i + 1], node.terminal)
            i += 1
        return best

    def prefixes(self) -> Iterator[str]:
        """Yield all pseudonyms stored in the trie."""

        def walk(node: TrieNode, prefix: str) -> Iterator[str]:
            if node.terminal is not None:
                yield prefix
            for ch, child in node.children.items():
                yield from walk(child, prefix + ch)

        yield from walk(self._root, "")
