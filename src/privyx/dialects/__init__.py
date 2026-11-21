"""Dialects: the API formats Privyx masks, and where each keeps its conversation (experimental).

A route of ``proxy.routes`` names its dialect.  A plugin's dialect must pass
:class:`privyx.testing.DialectSuite`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute
from privyx.dialects._base import CONTENT_KEYS as CONTENT_KEYS
from privyx.dialects._base import PREFIX_KEYS as PREFIX_KEYS
from privyx.dialects._base import Dialect

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.dialects._anthropic import AnthropicDialect
    from privyx.dialects._embeddings import EmbeddingsDialect
    from privyx.dialects._gemini import GeminiDialect
    from privyx.dialects._ollama import OllamaDialect
    from privyx.dialects._openai import OpenAIDialect
    from privyx.dialects._responses import ResponsesDialect

__all__ = [
    "AnthropicDialect",
    "Dialect",
    "EmbeddingsDialect",
    "GeminiDialect",
    "OllamaDialect",
    "OpenAIDialect",
    "ResponsesDialect",
]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "openai": "privyx.dialects._openai:OpenAIDialect",
    "anthropic": "privyx.dialects._anthropic:AnthropicDialect",
    "responses": "privyx.dialects._responses:ResponsesDialect",
    "gemini": "privyx.dialects._gemini:GeminiDialect",
    "ollama": "privyx.dialects._ollama:OllamaDialect",
    "embeddings": "privyx.dialects._embeddings:EmbeddingsDialect",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.dialects import GeminiDialect`` imports that module on first use."""
    return lazy_attribute(name, _BUILTINS, __name__)


def builtins() -> dict[str, Dialect]:
    """A new instance of each built-in dialect, by name, for a pipeline built without an app."""
    from privyx.core import imported

    return {name: imported(path)() for name, path in _BUILTINS.items()}


def setup(app: Privyx) -> None:
    """Register the built-in dialects with ``app``."""
    app.registry(Dialect).builtins(_BUILTINS)
