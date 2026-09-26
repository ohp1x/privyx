"""Redaction operator — replaces sensitive spans with a fixed token."""

from __future__ import annotations

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.operator.base import BaseOperator


class RedactOperator(BaseOperator):
    """Replace sensitive spans with a redaction token.

    This operator is NOT reversible — deanonymization returns the token as-is.
    Use for zero-trust scenarios or when session state is not desired.
    """

    name = "redact"

    def __init__(self, token: str = "[REDACTED]") -> None:
        self._token = token

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        detection = detection.merged(text)
        result_text = text
        transforms: list[Transformation] = []
        for span in reversed(detection.spans):
            result_text = result_text[: span.start] + self._token + result_text[span.end :]
            transforms.append(Transformation(span.start, span.end, span.text, self._token))
        transforms.reverse()
        return TransformResult(text=result_text, transformations=transforms)

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        # Redaction is irreversible.
        return TransformResult(text=text, transformations=[])
