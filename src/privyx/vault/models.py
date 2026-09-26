"""Pydantic models for vault payloads (serialization/validation)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VaultRecord(BaseModel):
    """A serializable session record."""

    session_id: str
    mapping: dict[str, str] = Field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_session(cls, session: Any) -> VaultRecord:
        return cls(
            session_id=session.session_id,
            mapping=session.mapping,
            created_at=session.created_at,
            updated_at=session.updated_at,
            metadata=session.metadata,
        )

    def to_session(self) -> Any:
        from privyx.core.session import Session

        return Session.from_dict(self.model_dump())
