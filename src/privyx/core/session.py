"""Session model and registry.

A Session isolates all pseudonym mapping state for one logical conversation,
client, or vault key. State that outlives a request goes through a
:class:`Vault` backend, never process memory; an ephemeral session, which lives
for one request in one process, stays in memory
(:meth:`~privyx.core.engine.PrivacyEngine.ephemeral_session`).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def new_session_id(prefix: str = "ses") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class Session:
    """Stateful pseudonym mapping for a logical conversation.

    Attributes:
        session_id: Unique identifier for the session.
        mapping: Pseudonym -> original value mapping.
        reverse: Original value -> pseudonym mapping (memory cache only).
        created_at: Unix timestamp of creation.
        updated_at: Unix timestamp of last update.
        metadata: Free-form session metadata (provider, model, ...).
    """

    session_id: str = field(default_factory=new_session_id)
    mapping: dict[str, str] = field(default_factory=dict)
    reverse: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def put(self, pseudonym: str, original: str) -> None:
        self.mapping[pseudonym] = original
        self.reverse[original] = pseudonym
        self.updated_at = time.time()

    def get(self, pseudonym: str) -> str | None:
        return self.mapping.get(pseudonym)

    def pseudonym_for(self, original: str) -> str | None:
        return self.reverse.get(original)

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "mapping": dict(self.mapping),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        session = cls(
            session_id=data["session_id"],
            mapping=data.get("mapping", {}),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            metadata=data.get("metadata", {}),
        )
        session.reverse = {v: k for k, v in session.mapping.items()}
        return session
