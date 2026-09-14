"""Operator protocol, base class, and the shared placeholder-restore routine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
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


def restore(text: str, session: Session, codec: TokenCodec) -> TransformResult:
    """Replace every token in ``text`` that ``session`` knows about.

    Shared by the reversible operators: the *codec* decides what a token looks
    like, while the right-to-left replacement that keeps earlier offsets valid
    and the "leave unknown tokens alone" rule live here rather than being
    reimplemented per operator.  No operator hard-codes token syntax
    (``.temp/token-system.md`` §9).

    Unknown tokens are passed through untouched: a stream may legitimately
    contain text that fits the token syntax but was never issued by us, and
    dropping or mangling it would corrupt the response.

    Args:
        text: Text containing tokens.
        session: Source of truth for token text → original.
        codec: Locates tokens in ``text``.
    """
    result_text = text
    transforms: list[Transformation] = []

    for match in sorted(codec.finditer(text), key=lambda m: m.start, reverse=True):
        original = session.get(match.text)
        if original is None:
            continue
        result_text = result_text[: match.start] + original + result_text[match.end :]
        transforms.append(Transformation(match.start, match.end, match.text, original))

    transforms.reverse()
    return TransformResult(text=result_text, transformations=transforms)


class BaseOperator(ABC):
    """Convenience base class for operators."""

    name: str = ""

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