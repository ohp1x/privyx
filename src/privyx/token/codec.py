"""Token codec — the *only* place that knows how a token looks in text.

A :class:`TokenCodec` converts between a :class:`~privyx.token.model.LogicalToken`
and its textual representation.  :class:`FormatCodec` is a codec driven entirely
by a configurable format string such as::

    <{namespace}_{type}_{id}>        # the default
    [[{namespace}:{type}:{id}]]
    <{namespace}:{type}:{id}>

so switching token syntax is a config change, not a code change.

The codec compiles the format into two regexes:

* **``_token_re``** — a *complete* token.  Fields are lazy so that the required
  trailing delimiters pin down where each field ends, which keeps parsing
  deterministic even when a field grammar overlaps a delimiter (``CREDIT_CARD``
  contains ``_``, the default field separator).  Drives :meth:`parse`,
  :meth:`finditer`, and :meth:`match_at`.
* **``_prefix_re``** — any *viable prefix* of a token (prefix-closure of the
  token language).  Drives :meth:`longest_prefix_len`, which the streaming
  scanner uses to decide whether a tail reaching the end of a buffer might still
  grow into a token and must be held back.

Field grammars are capped in length so neither regex can be forced to buffer or
backtrack over an unbounded run.

A model sometimes writes a token without the format's edge literals
(``PRIVYX_EMAIL_1`` for ``<PRIVYX_EMAIL_1>``).  The ``restorable*`` methods find
that *bare* form too, for the restore paths only: between word boundaries and
with the codec's own namespace in front, which is what keeps it from reading
every word as a token.  A format with no edge literal, or one that does not
begin with ``{namespace}``, has no bare form.

A model may also write a token's id and nothing else (``9F3A1C2B7D4E5F60`` for
``<PRIVYX_EMAIL_9F3A1C2B7D4E5F60>``).  No syntax tells that from any other word,
so the same methods find it only among the ids a session's tokens carry
(:meth:`FormatCodec.ids`), as a whole word, and only when the id is long enough
not to be ordinary text.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from privyx.token.errors import TokenFormatError
from privyx.token.model import LogicalToken

#: What may not touch a bare token, or an id on its own, on either side.
_WORD = "[A-Za-z0-9_]"

#: The shortest id looked for on its own.  A counter (``1``, ``12``) is ordinary
#: text; a digest this long does not turn up by chance.
_ALONE_MIN = 12

#: Field body grammars, as ``(regex_body, max_len)``.  ``regex_body`` matches one
#: full field value (minimum one character); ``max_len`` caps it.  ``namespace``
#: and ``type`` must start with a letter; ``type`` may contain ``_`` so entity
#: types like ``CREDIT_CARD`` round-trip; ``id`` is bare alphanumerics, wide
#: enough for a decimal counter, an anchor's upper-hex token, and a hash's
#: lower-hex digest.
_DEFAULT_GRAMMARS: dict[str, tuple[str, int]] = {
    "namespace": (r"[A-Za-z][A-Za-z0-9]*", 32),
    "type": (r"[A-Za-z][A-Za-z0-9_]*", 64),
    "id": (r"[A-Za-z0-9]+", 64),
}

_KNOWN_FIELDS = frozenset(_DEFAULT_GRAMMARS)
_PLACEHOLDER = re.compile(r"\{([a-z]+)\}")


@dataclass(frozen=True, slots=True)
class TokenMatch:
    """A token located within a larger string."""

    start: int
    end: int
    text: str
    token: LogicalToken


class TokenIds:
    """A session's tokens by id, for finding an id a model wrote on its own.

    Built by :meth:`FormatCodec.ids`.  An id that two tokens carry maps to
    ``None``: nothing says which of them the id alone stands for.
    """

    def __init__(self, tokens: dict[str, str | None]) -> None:
        self._tokens = tokens
        self._sorted = sorted(tokens)

    def token(self, identifier: str) -> str | None:
        """The one token whose id is ``identifier``, or ``None``."""
        return self._tokens.get(identifier)

    def begins(self, text: str) -> bool:
        """Whether ``text`` is the start of an id, or a whole one."""
        at = bisect_left(self._sorted, text)
        return at < len(self._sorted) and self._sorted[at].startswith(text)


@runtime_checkable
class TokenCodec(Protocol):
    """Converts between logical tokens and their textual form."""

    @property
    def namespace(self) -> str:
        """Default namespace stamped onto tokens this codec creates."""
        ...

    def encode(self, token: LogicalToken) -> str: ...

    def parse(self, text: str) -> LogicalToken | None: ...

    def finditer(self, text: str) -> Iterator[TokenMatch]: ...

    def match_at(self, text: str, pos: int) -> TokenMatch | None: ...

    def longest_prefix_len(self, text: str, pos: int) -> int: ...


@dataclass(frozen=True, slots=True)
class _Field:
    name: str
    greedy: str  # full field body, greedy (for prefix matching)
    lazy: str  # full field body, lazy (for complete-token matching)
    fullmatch: re.Pattern[str]  # validates a whole field value at encode time


class FormatCodec:
    """A :class:`TokenCodec` built from a format string.

    Args:
        format: Template with ``{namespace}``, ``{type}``, and ``{id}``
            placeholders.  ``{type}`` and ``{id}`` are required; ``{namespace}``
            is optional (when absent, parsed tokens take the codec's configured
            ``namespace``).  Adjacent placeholders must be separated by a
            non-empty literal delimiter, otherwise the boundary is ambiguous.
        namespace: Default namespace stamped onto tokens the operator creates and
            onto parsed tokens when the format omits ``{namespace}``.
        grammars: Optional per-field grammar overrides (advanced/extensibility).

    Raises:
        TokenFormatError: If the format is unusable.
    """

    def __init__(
        self,
        format: str,
        namespace: str = "PRIVYX",
        grammars: dict[str, tuple[str, int]] | None = None,
    ) -> None:
        self._format = format
        self._namespace = namespace
        grammars = grammars or _DEFAULT_GRAMMARS

        segments = self._parse_format(format)
        self._fields: dict[str, _Field] = {}
        for kind, value in segments:
            if kind == "field":
                self._fields[value] = self._build_field(value, grammars)

        if "type" not in self._fields or "id" not in self._fields:
            raise TokenFormatError(f"token format must contain {{type}} and {{id}}: {format!r}")

        token_pattern = self._build_token_pattern(segments)
        self._token_re = re.compile(token_pattern)
        self._prefix_re = re.compile(self._build_prefix_pattern(segments))

        # The bare form: the token without its edge literals.
        self._lead = segments[0][1] if segments[0][0] == "lit" else ""
        self._trail = segments[-1][1] if segments[-1][0] == "lit" else ""
        core = segments[bool(self._lead) : len(segments) - bool(self._trail)]
        loose: list[str] = []  # what may stand for a token between word boundaries
        self._bare_re: re.Pattern[str] | None = None
        self._bare_prefix_re: re.Pattern[str] | None = None
        if (self._lead or self._trail) and core[0] == ("field", "namespace"):
            bare = [("lit", namespace), *core[1:]]
            body = "".join(
                re.escape(value) if kind == "lit" else f"(?:{self._fields[value].greedy})"
                for kind, value in bare
            )
            loose.append(f"{body}(?!{_WORD})")
            self._bare_re = re.compile(f"(?<!{_WORD}){loose[0]}")
            self._bare_prefix_re = re.compile(f"(?<!{_WORD}){self._build_prefix_pattern(bare)}")
        self._written_re = self._written(token_pattern, loose)

        # An id on its own: a whole word in the id's grammar, _ALONE_MIN or longer.
        # One still being written is a word that reaches the end of the text.
        identifier = self._fields["id"].greedy
        loose.append(f"(?={_WORD}{{{_ALONE_MIN}}})(?P<alone>{identifier})(?!{_WORD})")
        self._alone_re = re.compile(f"(?<!{_WORD}){loose[-1]}")
        self._alone_prefix_re = re.compile(f"(?<!{_WORD})(?:{identifier})\\Z")
        self._written_ids_re = self._written(token_pattern, loose)

    @classmethod
    def default(cls) -> FormatCodec:
        """The built-in codec: ``<{namespace}_{type}_{id}>`` with namespace ``PRIVYX``."""
        return cls("<{namespace}_{type}_{id}>", "PRIVYX")

    @property
    def namespace(self) -> str:
        return self._namespace

    # -- serialization -------------------------------------------------------

    def encode(self, token: LogicalToken) -> str:
        """Render ``token`` as text, validating each field against its grammar.

        Validation is what stops a field value from smuggling delimiters into the
        output and producing an ambiguous or unparseable token.
        """
        values = {
            "namespace": token.namespace,
            "type": token.type,
            "id": token.identifier,
        }
        for name, field in self._fields.items():
            if not field.fullmatch.match(values[name]):
                raise TokenFormatError(
                    f"token {name} {values[name]!r} is not representable in format {self._format!r}"
                )
        return self._format.format(**values)

    def parse(self, text: str) -> LogicalToken | None:
        """Parse ``text`` as exactly one token, or return ``None``."""
        match = self._token_re.fullmatch(text)
        return self._to_token(match) if match else None

    def finditer(self, text: str) -> Iterator[TokenMatch]:
        """Yield every complete token in ``text``, left to right, non-overlapping."""
        for match in self._token_re.finditer(text):
            token = self._to_token(match)
            if token is not None:
                yield TokenMatch(match.start(), match.end(), match.group(0), token)

    def match_at(self, text: str, pos: int) -> TokenMatch | None:
        """Return the complete token starting exactly at ``pos``, or ``None``."""
        match = self._token_re.match(text, pos)
        if match is None:
            return None
        token = self._to_token(match)
        if token is None:
            return None
        return TokenMatch(match.start(), match.end(), match.group(0), token)

    def longest_prefix_len(self, text: str, pos: int) -> int:
        """Length of the longest prefix of ``text[pos:]`` that could still be a token.

        Returns 0 when the character at ``pos`` cannot begin any token.  Used by
        the streaming scanner to hold back a tail that may complete in a later
        chunk.
        """
        match = self._prefix_re.match(text, pos)
        return match.end() - pos if match else 0

    # -- restore: full and bare forms, and the id alone -------------------------

    def ids(self, tokens: Iterable[str]) -> TokenIds | None:
        """Index ``tokens``, a session's, by the ids long enough to stand alone.

        ``None`` when no id is: a session of counters has nothing to look for.
        """
        found: dict[str, str | None] = {}
        for token in tokens:
            match = self._token_re.fullmatch(token)
            if match is not None and len(identifier := match["id"]) >= _ALONE_MIN:
                found[identifier] = None if identifier in found else token
        return TokenIds(found) if found else None

    def restorable(self, text: str, ids: TokenIds | None = None) -> Iterator[tuple[int, int, str]]:
        """Yield ``(start, end, token)`` for every token written in ``text``.

        ``token`` is the token's full text, the key a session knows it by;
        ``text[start:end]`` is how it was written, which may be the bare form
        or, among ``ids``, the id alone.  Left to right and non-overlapping, a
        full token before a bare one before an id at the same position: the
        order :meth:`restorable_at` gives one position at a time, so a stream
        and a batch find the same tokens.
        """
        written = self._written_re if ids is None else self._written_ids_re
        pos = 0
        while match := written.search(text, pos):
            start, pos = match.span()
            token = match.group(0)
            if ids is not None and match.lastgroup == "alone":
                issued = ids.token(token)
                if issued is None:  # a word like any other: go on from inside it
                    pos = start + 1
                    continue
                token = issued
            elif match.lastgroup != "full":
                token = self._full(token)
            yield start, match.end(), token

    def restorable_at(
        self, text: str, pos: int, ids: TokenIds | None = None
    ) -> tuple[int, str] | None:
        """Return ``(end, token)`` for the token written exactly at ``pos``, or ``None``."""
        match = self._token_re.match(text, pos)
        if match is not None:
            return match.end(), match.group(0)
        if self._bare_re is not None and (match := self._bare_re.match(text, pos)):
            return match.end(), self._full(match.group(0))
        if ids is not None and (match := self._alone_re.match(text, pos)):
            token = ids.token(match.group(0))
            if token is not None:
                return match.end(), token
        return None

    def restorable_prefix_len(self, text: str, pos: int, ids: TokenIds | None = None) -> int:
        """:meth:`longest_prefix_len`, counting a bare token still being written.

        A complete bare token that reaches the end of ``text`` counts too: only
        the next character says whether it ends there.  The same goes for a word
        that reaches the end of ``text`` and is, or begins, one of ``ids``.
        """
        longest = self.longest_prefix_len(text, pos)
        if self._bare_prefix_re is not None and (match := self._bare_prefix_re.match(text, pos)):
            longest = max(longest, match.end() - pos)
        if (
            ids is not None
            and (match := self._alone_prefix_re.match(text, pos))
            and ids.begins(match.group(0))
        ):
            longest = max(longest, match.end() - pos)
        return longest

    def _full(self, bare: str) -> str:
        return f"{self._lead}{bare}{self._trail}"

    @staticmethod
    def _written(token_pattern: str, loose: list[str]) -> re.Pattern[str]:
        """A full token, or one of the ``loose`` forms between word boundaries.

        The boundary on the left is checked once for all of them: inside a
        word, which is most of a text, a search then drops them together.
        """
        forms = f"|(?<!{_WORD})(?:{'|'.join(loose)})" if loose else ""
        return re.compile(f"(?P<full>{token_pattern}){forms}")

    # -- internals -----------------------------------------------------------

    def _to_token(self, match: re.Match[str]) -> LogicalToken | None:
        groups = match.groupdict()
        namespace = groups.get("namespace") or self._namespace
        try:
            return LogicalToken(
                namespace=namespace,
                type=groups["type"],
                identifier=groups["id"],
            )
        except ValueError:  # pragma: no cover - grammar already excludes these
            return None

    @staticmethod
    def _parse_format(format: str) -> list[tuple[str, str]]:
        """Split ``format`` into ordered ``("lit", text)`` / ``("field", name)`` segments."""
        segments: list[tuple[str, str]] = []
        seen: set[str] = set()
        pos = 0
        prev_kind: str | None = None
        for match in _PLACEHOLDER.finditer(format):
            literal = format[pos : match.start()]
            if literal:
                segments.append(("lit", literal))
                prev_kind = "lit"
            name = match.group(1)
            if name not in _KNOWN_FIELDS:
                raise TokenFormatError(
                    f"unknown token field {{{name}}}; expected one of {sorted(_KNOWN_FIELDS)}"
                )
            if name in seen:
                raise TokenFormatError(f"token field {{{name}}} appears more than once")
            if prev_kind == "field":
                raise TokenFormatError(
                    f"token fields must be separated by a delimiter: near {{{name}}}"
                )
            seen.add(name)
            segments.append(("field", name))
            prev_kind = "field"
            pos = match.end()

        trailing = format[pos:]
        if trailing:
            segments.append(("lit", trailing))

        for kind, value in segments:
            if kind == "lit" and ("{" in value or "}" in value):
                raise TokenFormatError(f"stray brace in token format literal: {value!r}")
        if not segments:
            raise TokenFormatError("token format is empty")
        return segments

    @staticmethod
    def _build_field(name: str, grammars: dict[str, tuple[str, int]]) -> _Field:
        body, max_len = grammars.get(name, _DEFAULT_GRAMMARS[name])
        # Cap the trailing repetition so neither regex can run away: a body of the
        # form ``<first><rest>*`` becomes ``<first><rest>{0,max-1}``.
        capped = _cap_repetition(body, max_len)
        return _Field(
            name=name,
            greedy=capped,
            lazy=_make_lazy(capped),
            fullmatch=re.compile(f"(?:{capped})\\Z"),
        )

    def _build_token_pattern(self, segments: list[tuple[str, str]]) -> str:
        parts: list[str] = []
        for kind, value in segments:
            if kind == "lit":
                parts.append(re.escape(value))
            else:
                field = self._fields[value]
                parts.append(f"(?P<{value}>{field.lazy})")
        return "".join(parts)

    def _build_prefix_pattern(self, segments: list[tuple[str, str]]) -> str:
        """Prefix-closure of the token language as a nested-optional pattern.

        Each segment must be fully present to advance to the next; only the last
        present segment may be a proper prefix (a partial literal, or a field
        still being typed).  This is exactly the set of strings that *could* grow
        into a complete token.
        """

        def build(index: int) -> str:
            if index >= len(segments):
                return ""
            kind, value = segments[index]
            rest = build(index + 1)
            if kind == "field":
                field = self._fields[value]
                cont = f"{field.greedy}(?:{rest})?" if rest else field.greedy
                return f"(?:{cont})?"
            escaped = re.escape(value)
            cont = f"{escaped}(?:{rest})?" if rest else escaped
            # Proper non-empty prefixes of a multi-char literal, longest first so
            # the engine prefers the longest viable prefix.
            partials = [re.escape(value[:k]) for k in range(len(value) - 1, 0, -1)]
            alternatives = "|".join([cont, *partials])
            return f"(?:{alternatives})?"

        return build(0)


def _cap_repetition(body: str, max_len: int) -> str:
    """Bound an unbounded ``*``/``+`` field body to ``max_len`` characters.

    Bodies are one of two shapes we control:
      ``<class>*``            (leading ``+`` implied by min-1 elsewhere) → ``<class>{1,max}``
      ``<class1><class2>*``   → ``<class1><class2>{0,max-1}``
    """
    if body.endswith("+"):
        return f"{body[:-1]}{{1,{max_len}}}"
    if body.endswith("*"):
        return f"{body[:-1]}{{0,{max_len - 1}}}"
    return body  # already bounded


def _make_lazy(body: str) -> str:
    """Return a lazy variant of a ``{m,n}``-quantified body."""
    return re.sub(r"(\{\d+,\d+\})$", r"\1?", body)
