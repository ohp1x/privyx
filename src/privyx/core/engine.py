"""Privacy Engine — the orchestrator of the privacy pipeline.

The engine wires together detectors, policies, operators, anchors, and vaults:

    Detect → Policy → Pseudonymize → Vault

and their inverse:

    Deanonymize ← Vault

The engine is transport-agnostic: it has no knowledge of HTTP, SSE, or any
provider SDK. Gateways and proxies call :meth:`PrivacyEngine.transform` and
:meth:`PrivacyEngine.restore` on raw text.
"""

from __future__ import annotations

from typing import Any

from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.core.result import TransformResult
from privyx.core.session import Session
from privyx.privacy.detector.base import Detector
from privyx.privacy.operator.base import Operator
from privyx.privacy.policy.base import Policy
from privyx.token.codec import FormatCodec, TokenCodec
from privyx.vault.base import Vault


class PrivacyEngine:
    """Orchestrates the privacy pipeline for one configuration.

    Args:
        detector: Entity detector implementation.
        policy: Policy deciding which detections to act on.
        operator: Operator that pseudonymizes/deanonymizes text.
        vault: Session storage backend.
        codec: Token codec.  Shared with the operator so generation, batch
            restore, and streaming all recognize tokens the same way; defaults
            to the built-in syntax.  Exposed so transports (e.g. the streaming
            proxy) can recognize tokens without knowing their syntax.
    """

    def __init__(
        self,
        detector: Detector | None = None,
        policy: Policy | None = None,
        operator: Operator | None = None,
        vault: Vault | None = None,
        codec: TokenCodec | None = None,
    ) -> None:
        if detector is None:
            raise ConfigError("PrivacyEngine requires a detector")
        if policy is None:
            raise ConfigError("PrivacyEngine requires a policy")
        if operator is None:
            raise ConfigError("PrivacyEngine requires an operator")
        if vault is None:
            raise ConfigError("PrivacyEngine requires a vault")
        self._detector = detector
        self._policy = policy
        self._operator = operator
        self._vault = vault
        self._codec = codec or FormatCodec.default()

    @property
    def detector(self) -> Detector:
        return self._detector

    @property
    def policy(self) -> Policy:
        return self._policy

    @property
    def operator(self) -> Operator:
        return self._operator

    @property
    def vault(self) -> Vault:
        return self._vault

    @property
    def codec(self) -> TokenCodec:
        return self._codec

    async def get_or_create_session(self, session_id: str | None = None) -> Session:
        """Return an existing session or create (and persist) a new one."""
        if session_id:
            existing = await self._vault.get(session_id)
            if existing is not None:
                return existing
        session = Session(session_id=session_id) if session_id else Session()
        await self._vault.create(session)
        return session

    async def transform(
        self,
        text: str,
        session: Session | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TransformResult:
        """Pseudonymize sensitive spans in ``text``.

        Args:
            text: Source text to transform.
            session: Optional existing session; created if omitted.
            session_id: Optional session id; implies ``session``.
            metadata: Free-form metadata attached to the context.

        Returns:
            A :class:`TransformResult` with the pseudonymized text.
        """
        if session is None:
            session = await self.get_or_create_session(session_id)
        context = Context(session_id=session.session_id, vault=self._vault, metadata=metadata or {})

        detection = await self._detector.detect(text, context)
        detection = await self._policy.decide(detection, context)
        result = await self._operator.pseudonymize(text, detection, session, context)
        await self._vault.save(session)
        return result

    async def restore(
        self,
        text: str,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> TransformResult:
        """Deanonymize ``text`` using the session's mapping.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        from privyx.core.errors import SessionNotFoundError

        session = await self._vault.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"session not found: {session_id}")
        context = Context(session_id=session_id, vault=self._vault, metadata=metadata or {})
        return await self._operator.deanonymize(text, session, context)
