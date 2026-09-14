"""The logical token — Privyx's syntax-independent view of a placeholder.

A :class:`LogicalToken` carries *what a token means* (which namespace, entity
type, and identifier it stands for) and says nothing about *how it looks* in
text.  Turning it into characters — and back — is the job of a
:class:`~privyx.token.codec.TokenCodec`.  Nothing outside the token subsystem
should construct textual delimiters; it should build a ``LogicalToken`` and hand
it to the codec.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Characters that must never appear in any token field regardless of the
#: configured syntax.  A field carrying whitespace or a control character could
#: not survive serialization into a single opaque token, and would be a vector
#: for smuggling framing (newlines, delimiters) into logs and streams.
_FORBIDDEN = set(" \t\r\n\f\v\0")


@dataclass(frozen=True, slots=True)
class LogicalToken:
    """The semantic identity of a placeholder.

    Attributes:
        namespace: Owning namespace (e.g. ``PRIVYX``).  Distinguishes Privyx
            tokens from any other bracketed text.
        type: Entity type the token stands for (e.g. ``EMAIL``, ``CREDIT_CARD``).
        identifier: Per-value identifier — a counter, an anchor digest, or a
            content hash, depending on the operator.

    Fields are validated for basic hygiene only; whether a value is
    *representable* in a particular syntax is decided by the codec at
    :meth:`~privyx.token.codec.TokenCodec.encode` time, since that depends on the
    configured field grammar.
    """

    namespace: str
    type: str
    identifier: str

    def __post_init__(self) -> None:
        for name, value in (
            ("namespace", self.namespace),
            ("type", self.type),
            ("identifier", self.identifier),
        ):
            if not value:
                raise ValueError(f"token {name} must be non-empty")
            if _FORBIDDEN.intersection(value):
                raise ValueError(f"token {name} contains forbidden characters: {value!r}")
