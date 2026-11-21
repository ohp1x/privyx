"""The ``embeddings`` dialect: text sent to be embedded, and a reply of numbers."""

from __future__ import annotations

from typing import Any

from privyx.dialects._base import CONTENT_KEYS, PREFIX_KEYS, Dialect


class EmbeddingsDialect(Dialect):
    """OpenAI's and Ollama's embeddings: ``input`` masked, the vectors relayed as they are."""

    name = "embeddings"
    content_keys = CONTENT_KEYS
    prefix_keys = PREFIX_KEYS
    restores_reply = False

    def turn(self, path: str) -> bool:
        return False

    def conversation_key(self, payload: Any) -> str:
        return ""
