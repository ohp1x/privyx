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
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from privyx.token.errors import TokenFormatError
from privyx.token.model import LogicalToken

#: What may not touch a bare token on either side.
_WORD = "[A-Za-z0-9_]"

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
        self._written_re: re.Pattern[str] | None = None
        self._bare_re: re.Pattern[str] | None = None
        self._bare_prefix_re: re.Pattern[str] | None = None
        if (self._lead or self._trail) and core[0] == ("field", "namespace"):
            bare = [("lit", namespace), *core[1:]]
            body = "".join(
                re.escape(value) if kind == "lit" else f"(?:{self._fields[value].greedy})"
                for kind, value in bare
            )
            bare_pattern = f"(?<!{_WORD}){body}(?!{_WORD})"
            self._bare_re = re.compile(bare_pattern)
            self._bare_prefix_re = re.compile(f"(?<!{_WORD}){self._build_prefix_pattern(bare)}")
            self._written_re = re.compile(f"(?P<full>{token_pattern})|{bare_pattern}")

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

    # -- restore: full and bare forms ------------------------------------------

    def restorable(self, text: str) -> Iterator[tuple[int, int, str]]:
        """Yield ``(start, end, token)`` for every token written in ``text``.

        ``token`` is the token's full text, the key a session knows it by;
        ``text[start:end]`` is how it was written, which may be the bare form.
        Left to right and non-overlapping, a full token before a bare one at the
        same position: the order :meth:`restorable_at` gives one position at a
        time, so a stream and a batch find the same tokens.
        """
        if self._written_re is None:
            for found in self.finditer(text):
                yield found.start, found.end, found.text
            return
        for match in self._written_re.finditer(text):
            written = match.group(0)
            full = match.group("full") is not None
            yield match.start(), match.end(), written if full else self._full(written)

    def restorable_at(self, text: str, pos: int) -> tuple[int, str] | None:
        """Return ``(end, token)`` for the token written exactly at ``pos``, or ``None``."""
        match = self._token_re.match(text, pos)
        if match is not None:
            return match.end(), match.group(0)
        if self._bare_re is not None and (match := self._bare_re.match(text, pos)):
            return match.end(), self._full(match.group(0))
        return None

    def restorable_prefix_len(self, text: str, pos: int) -> int:
        """:meth:`longest_prefix_len`, counting a bare token still being written.

        A complete bare token that reaches the end of ``text`` counts too: only
        the next character says whether it ends there.
        """
        longest = self.longest_prefix_len(text, pos)
        if self._bare_prefix_re is not None and (match := self._bare_prefix_re.match(text, pos)):
            longest = max(longest, match.end() - pos)
        return longest

    def _full(self, bare: str) -> str:
        return f"{self._lead}{bare}{self._trail}"

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
