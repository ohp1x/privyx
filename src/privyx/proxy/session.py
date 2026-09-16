"""Session-id resolution for header-less clients.

A client that never sends ``x-privyx-session`` (Claude Code, codex, aider, the
OpenAI CLI, ...) would otherwise get a fresh, throwaway session on every HTTP
call — so a multi-turn conversation is re-tokenized from scratch each turn and
shows up as many sessions.  This module turns the configured ``session.strategy``
into a *stable* session id derived from what the request already carries, so
:meth:`~privyx.core.engine.PrivacyEngine.get_or_create_session` reuses one
session across a conversation's turns.

The rules (see :class:`~privyx.config.schema.SessionConfig`):

- an explicit ``x-privyx-session`` header always wins (``source="header"``);
- ``ephemeral`` returns ``None`` — the engine mints a per-request id, unchanged;
- ``client`` derives from the client credential;
- ``conversation`` derives from the credential *and* the first user message.

A derived id has the same ``ses_<16 hex>`` shape as a minted one, so nothing
downstream can tell them apart.  Deriving from the credential scopes a session to
one caller, so two callers can never share a pseudonym map — the cross-user
collision veilstream had with a single shared session.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

#: Wire schemas whose request body this module knows how to fingerprint.
_CHAT_SCHEMAS = frozenset({"openai", "anthropic"})


def resolve_session_id(
    strategy: str,
    *,
    header_value: str | None,
    headers: Mapping[str, str],
    payload: Any,
    schema: str | None,
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
        first = _first_user_message(payload) if schema in _CHAT_SCHEMAS else ""
        if credential or first:
            return _derive(f"{credential}\x00{first}"), "conversation"
    return None, "ephemeral"


def _derive(material: str) -> str:
    """A stable ``ses_<16 hex>`` id for ``material`` (a minted id's exact shape)."""
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"ses_{digest[:16]}"


def _credential(headers: Mapping[str, str]) -> str:
    """The client credential to key a session on, or ``""`` when absent.

    ``Authorization`` (OpenAI-style bearer) or ``x-api-key`` (Anthropic-style),
    matched case-insensitively since header casing is not guaranteed.
    """
    wanted = ("authorization", "x-api-key")
    for key, value in headers.items():
        if key.lower() in wanted and value:
            return value
    return ""


def _first_user_message(payload: Any) -> str:
    """Text of the first ``user`` message, or ``""`` when there is none.

    OpenAI and Anthropic share the shape read here: ``messages`` is a list of
    ``{"role", "content"}`` and ``content`` is a string or a list of
    ``{"type": "text", "text": ...}`` parts (mirrors
    :func:`privyx.proxy.schemas._transform_content`).  The first user message is
    immutable across a conversation's turns, so it is a stable fingerprint.
    """
    if not isinstance(payload, Mapping):
        return ""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if isinstance(message, Mapping) and message.get("role") == "user":
            return _content_text(message.get("content"))
    return ""


def _content_text(content: Any) -> str:
    """Flatten a message ``content`` (string or list of text parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, Mapping)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""
