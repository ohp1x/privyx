"""Execution context shared through the privacy pipeline.

Carries configuration, the vault, and per-request metadata without coupling
the core to any specific transport (HTTP, SSE, CLI, ...).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Context:
    """Immutable-ish context handed to detectors, policies, and operators.

    Attributes:
        session_id: Optional session identifier for stateful operations.
        vault: Optional vault instance for storing/retrieving mappings.
        metadata: Free-form request metadata (provider, model, headers, ...).
        counters: Counts a component reports back for the audit trail, such as
            ``llm_calls`` or ``llm_fallbacks``.  The engine records them on
            ``session.transform`` as ``detector_counts``, so they must be
            counts only, never text.  Copies of a context share them.
    """

    session_id: str | None = None
    vault: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    counters: Counter[str] = field(default_factory=Counter)

    def with_session(self, session_id: str) -> Context:
        return Context(
            session_id=session_id, vault=self.vault, metadata=self.metadata, counters=self.counters
        )

    def clone(self, **overrides: Any) -> Context:
        """Return a copy of this context with some fields overridden."""
        session_id: str | None = overrides.get("session_id", self.session_id)
        vault: Any | None = overrides.get("vault", self.vault)
        metadata: dict[str, Any] = overrides.get("metadata", dict(self.metadata))
        return Context(
            session_id=session_id, vault=vault, metadata=metadata, counters=self.counters
        )
