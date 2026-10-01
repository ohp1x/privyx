"""Operator protocol, base class, and the shared placeholder-restore routine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.transform.text import apply_replacements
from privyx.token.codec import TokenCodec


@runtime_checkable
class Operator(Protocol):
    """Protocol for transform operators."""

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult: ...

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult: ...


def restore(
    text: str,
    session: Session,
    codec: TokenCodec,
    resolve: Callable[[str], str | None] | None = None,
) -> TransformResult:
    """Replace every token in ``text`` that ``session`` knows about.

    Shared by the reversible operators: the *codec* decides what a token looks
    like, while the replacement and the "leave unknown tokens alone" rule live
    here rather than being reimplemented per operator.  No operator hard-codes
    token syntax.

    Unknown tokens are passed through untouched: a stream may legitimately
    contain text that fits the token syntax but was never issued by us, and
    dropping or mangling it would corrupt the response.  A token the model wrote
    without its edge literals (``PRIVYX_EMAIL_1``) is restored as well, under the
    same rule: only if this session issued it.

    Args:
        text: Text containing tokens.
        session: Source of truth for token text → original.
        codec: Locates tokens in ``text``.
        resolve: Maps a token's text to its original value, or ``None`` when the
            token is unknown.  Defaults to the session mapping's own lookup;
            ``encrypt`` supplies a decrypting resolver because its mapping stores
            ciphertext rather than plaintext.
    """
    resolve = resolve if resolve is not None else session.get
    transforms: list[Transformation] = []

    # A token may be written without its edge literals; a codec that does not
    # know that form (a plugin's) keeps to full tokens.
    restorable = getattr(codec, "restorable", None)
    full = ((m.start, m.end, m.text) for m in codec.finditer(text))
    found = restorable(text) if restorable else full
    for start, end, token in sorted(found):
        original = resolve(token)
        if original is None:
            continue
        transforms.append(Transformation(start, end, text[start:end], original))

    return TransformResult(text=apply_replacements(text, transforms), transformations=transforms)


class BaseOperator(ABC):
    """Convenience base class for operators."""

    name: str = ""
    #: How a *stream* of this operator's output is reversed.  ``"token"`` (the
    #: default) means the substitutions are codec tokens, recognized by syntax;
    #: ``"literal"`` means they are ordinary text, recognized by matching the
    #: exact substituted values (a trie).  The proxies read this to pick the
    #: streaming recognizer; operators that are not reversible can leave it as-is
    #: (no substitution will match).
    stream_restore: str = "token"

    def build_resolver(self, mapping: dict[str, str]) -> Callable[[str], str | None]:
        """Map a token's text to its original value, for restore (batch + stream).

        The default is the session mapping's own lookup, because most operators
        store the plaintext there.  An operator whose mapping value is *not* the
        plaintext overrides this: ``encrypt`` stores ciphertext and returns a
        resolver that decrypts.  Read by the proxies via ``resolver_for`` so a
        plugin predating this method still works.
        """
        return mapping.get

    @abstractmethod
    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult: ...

    @abstractmethod
    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult: ...
