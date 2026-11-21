"""Session-id resolution for header-less clients.

A client that never sends ``x-privyx-session`` (Claude Code, codex, aider, the
OpenAI CLI, ...) would otherwise get a fresh, throwaway session on every HTTP
call — so a multi-turn conversation is re-tokenized from scratch each turn and
shows up as many sessions.  This module turns the configured ``session.strategy``
into a *stable* session id derived from what the request already carries, so
:meth:`~privyx.engine.PrivacyEngine.get_or_create_session` reuses one
session across a conversation's turns.

The rules (see :class:`~privyx.config.schema.SessionConfig`):

- an explicit ``x-privyx-session`` header always wins (``source="header"``);
- ``ephemeral`` returns ``None`` — the engine mints a per-request id, unchanged;
- ``client`` derives from the client credential;
- ``conversation`` derives from the credential *and* the first user message,
  which the dialect finds (:meth:`~privyx.dialects.Dialect.conversation_key`).

A derived id has the same ``ses_<16 hex>`` shape as a minted one, so nothing
downstream can tell them apart.  Deriving from the credential scopes a session to
one caller, so two callers can never share a pseudonym map — the cross-user
collision veilstream had with a single shared session.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from privyx.dialects import Dialect


def resolve_session_id(
    strategy: str,
    *,
    header_value: str | None,
    headers: Mapping[str, str],
    payload: Any,
    dialect: Dialect | None,
) -> tuple[str | None, str]:
    """Resolve the session id and its provenance for one request.

    Returns ``(session_id, source)`` where ``source`` is one of ``"header"``,
    ``"client"``, ``"conversation"``, or ``"ephemeral"``.  A ``None`` id (only on
    the ephemeral path) tells the engine to mint a fresh per-request id.
    """
    if header_value:
        return header_value, "header"
    if strategy == "client":
        credential = _credential(headers)
        if credential:
            return _derive(credential), "client"
    elif strategy == "conversation":
        credential = _credential(headers)
        first = (
            dialect.conversation_key(payload)
            if dialect is not None and isinstance(payload, Mapping)
            else ""
        )
        if credential or first:
            return _derive(f"{credential}\x00{first}"), "conversation"
    return None, "ephemeral"


def _derive(material: str) -> str:
    """A stable ``ses_<16 hex>`` id for ``material`` (a minted id's exact shape)."""
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"ses_{digest[:16]}"


def _credential(headers: Mapping[str, str]) -> str:
    """The client credential to key a session on, or ``""`` when absent.

    ``Authorization`` (OpenAI-style bearer), ``x-api-key`` (Anthropic-style),
    or ``x-goog-api-key`` (Gemini), matched case-insensitively since header
    casing is not guaranteed.
    """
    wanted = ("authorization", "x-api-key", "x-goog-api-key")
    for key, value in headers.items():
        if key.lower() in wanted and value:
            return value
    return ""
