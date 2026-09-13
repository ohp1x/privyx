"""Placeholder for Faker-based operator."""

from __future__ import annotations

from privyx.core.context import Context
from privyx.core.result import Detection, TransformResult
from privyx.core.session import Session
from privyx.privacy.operator.base import BaseOperator


class FakerOperator(BaseOperator):
    """Replace sensitive spans with realistic fake data (Faker).

    NOTE: This is a placeholder.  Requires the ``faker`` package.
    Deanonymization is not supported unless a mapping is stored in the vault.
    """

    name = "faker"

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        raise NotImplementedError("FakerOperator is not yet implemented")

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        raise NotImplementedError("FakerOperator is not yet implemented")