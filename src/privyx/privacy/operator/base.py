"""Operator protocol, base class, and the shared placeholder-restore routine."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session


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


def restore(text: str, session: Session, pattern: re.Pattern[str]) -> TransformResult:
    """Replace every placeholder in ``text`` that ``session`` knows about.

    Shared by the reversible operators so each one only has to describe the
    *shape* of its placeholder; the matching, the right-to-left replacement
    that keeps earlier offsets valid, and the "leave unknown placeholders
    alone" rule live here rather than being reimplemented per operator.

    Unknown placeholders are passed through untouched: a stream may legitimately
    contain text that looks like a placeholder but was never issued by us, and
    dropping or mangling it would corrupt the response.

    Args:
        text: Text containing placeholders.
        session: Source of truth for placeholder → original.
        pattern: Matches one placeholder.
    """
    result_text = text
    transforms: list[Transformation] = []

    for match in sorted(pattern.finditer(text), key=lambda m: m.start(), reverse=True):
        placeholder = match.group()
        original = session.get(placeholder)
        if original is None:
            continue
        result_text = result_text[: match.start()] + original + result_text[match.end() :]
        transforms.append(Transformation(match.start(), match.end(), placeholder, original))

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