"""Redaction operator — replaces sensitive spans with a fixed token."""

from __future__ import annotations

from typing import Any

from privyx.core import (
    Context,
    Detection,
    Session,
    Transformation,
    TransformResult,
    apply_replacements,
)
from privyx.operators._base import Operator


class RedactOperator(Operator):
    """Replace sensitive spans with ``token``, ``[REDACTED]`` by default.

    This operator is NOT reversible — deanonymization returns the token as-is.
    Use for zero-trust scenarios or when session state is not desired.
    """

    name = "redact"
    uses_anchor = False

    class Options(Operator.Options):
        #: What every value is replaced with.
        text: str = "[REDACTED]"

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._token = self.options.text
        self.fixed_text = self._token

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        detection = detection.merged(text)
        transforms = [
            Transformation(span.start, span.end, span.text, self._token, span.entity_type)
            for span in detection.spans
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
