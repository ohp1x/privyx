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

from collections import Counter
from typing import Any

from privyx.core.context import Context
from privyx.core.errors import ConfigError, VaultError
from privyx.core.result import Detection, TransformResult
from privyx.core.session import Session
from privyx.observability.audit import AuditLogger
from privyx.privacy.detector.base import Detector
from privyx.privacy.operator.base import Operator
from privyx.privacy.policy.base import Policy
from privyx.token.codec import FormatCodec, TokenCodec
from privyx.vault.base import Vault


def _entity_counts(detection: Detection) -> dict[str, int]:
    """A ``{entity_type: count}`` histogram — types and counts only, no text."""
    return dict(Counter(span.entity_type for span in detection.spans))


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
        audit: Optional audit logger for PII-safe privacy events (session
            created, transform, restore).  Defaults to a disabled no-op logger,
            so the engine stays transport-agnostic and testable without a file.
    """

    def __init__(
        self,
        detector: Detector | None = None,
        policy: Policy | None = None,
        operator: Operator | None = None,
        vault: Vault | None = None,
        codec: TokenCodec | None = None,
        audit: AuditLogger | None = None,
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
        self._audit = audit or AuditLogger(None)

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

    async def get_or_create_session(
        self, session_id: str | None = None, *, source: str = "ephemeral"
    ) -> Session:
        """Return an existing session or create (and persist) a new one.

        ``source`` is recorded on the ``session.created`` audit event and
        describes how ``session_id`` was chosen (see :mod:`privyx.proxy.session`);
        it has no effect when an existing session is reused.
        """
        if session_id:
            existing = await self._vault.get(session_id)
            if existing is not None:
                return existing
        session = Session(session_id=session_id) if session_id else Session()
        try:
            await self._vault.create(session)
        except VaultError:
            # A concurrent request created this id first — clients fan out
            # parallel requests at conversation start, and a derived (sticky) id
            # makes them collide. Adopt the winner rather than failing the race.
            existing = await self._vault.get(session.session_id)
            if existing is not None:
                return existing
            raise
        self._audit.session_created(session.session_id, source=source)
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
        # Every turn counts as activity, not only turns that add a mapping, so a
        # vault TTL never expires a conversation that is still in use.
        session.touch()
        await self._vault.save(session)
        self._audit.transform(
            session.session_id,
            entity_counts=_entity_counts(detection),
            transformations=len(result.transformations),
            detector_counts=context.counters,
        )
        return result

    async def delete_session(
        self,
        session_id: str,
        *,
        reason: str,
        request_id: str | None = None,
    ) -> bool:
        """Delete a session and audit the deletion after it succeeds.

        The mapping count is captured before deletion for the PII-safe audit
        event.  A missing session is a no-op, which makes cleanup idempotent
        when a vault TTL or another owner removed it first.

        Raises:
            VaultError: If reading or deleting the session fails.  No
                ``session.deleted`` event is emitted in that case.
        """
        session = await self._vault.get(session_id)
        if session is None:
            return False
        mapping_count = len(session.mapping)
        await self._vault.delete(session_id)
        self._audit.session_deleted(
            session_id,
            reason=reason,
            mapping_count=mapping_count,
            request_id=request_id,
        )
        return True

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
        result = await self._operator.deanonymize(text, session, context)
        self._audit.restore(session_id, transformations=len(result.transformations))
        return result
