"""Redaction operator — replaces sensitive spans with a fixed token."""

from __future__ import annotations

from privyx.core.context import Context
from privyx.core.result import Detection, Transformation, TransformResult
from privyx.core.session import Session
from privyx.privacy.operator.base import BaseOperator
from privyx.privacy.transform.text import apply_replacements


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
        transforms = [
            Transformation(span.start, span.end, span.text, self._token) for span in detection.spans
        ]
        return TransformResult(
            text=apply_replacements(text, transforms), transformations=transforms
        )

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        # Redaction is irreversible.
        return TransformResult(text=text, transformations=[])
