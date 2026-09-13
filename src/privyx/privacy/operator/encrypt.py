"""Placeholder for encryption-based operator (requires cryptography package)."""

from __future__ import annotations

from privyx.core.context import Context
from privyx.core.result import Detection, TransformResult
from privyx.core.session import Session
from privyx.privacy.operator.base import BaseOperator


class EncryptOperator(BaseOperator):
    """Replace sensitive spans with encrypted tokens.

    NOTE: This is a placeholder.  Implementation requires the ``cryptography``
    package and a key management strategy.  Use ``PseudonymOperator`` for most
    reversible scenarios.
    """

    name = "encrypt"

    async def pseudonymize(
        self,
        text: str,
        detection: Detection,
        session: Session,
        context: Context,
    ) -> TransformResult:
        # TODO: implement with Fernet or AES-GCM
        raise NotImplementedError("EncryptOperator is not yet implemented")

    async def deanonymize(
        self,
        text: str,
        session: Session,
        context: Context,
    ) -> TransformResult:
        raise NotImplementedError("EncryptOperator is not yet implemented")