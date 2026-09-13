"""Stream adapter registry — resolve a provider name to its SSE adapter.

Providers differ in where the text delta lives inside an SSE ``data`` payload:
OpenAI puts it at ``choices[0].delta.content``, Anthropic emits
``content_block_delta`` events.  Keeping that knowledge in adapters — selected
by name here — is what stops the proxy from growing per-provider branches.

Unknown names fall back to the plain-SSE adapter rather than raising: a custom
or self-hosted endpoint that speaks ordinary SSE should just work.
"""

from __future__ import annotations

from privyx.streaming.adapters.anthropic import AnthropicStreamAdapter
from privyx.streaming.adapters.generic import SSEStreamAdapter
from privyx.streaming.adapters.openai import OpenAIStreamAdapter

_ADAPTERS: dict[str, type[SSEStreamAdapter]] = {
    "openai": OpenAIStreamAdapter,
    "anthropic": AnthropicStreamAdapter,
    "generic": SSEStreamAdapter,
}


def build_stream_adapter(provider_type: str) -> SSEStreamAdapter:
    """Return the stream adapter for ``provider_type``.

    Args:
        provider_type: Provider name (``openai``, ``anthropic``, ``generic``, ...).

    Returns:
        The matching adapter, or :class:`SSEStreamAdapter` for unknown names.
    """
    return _ADAPTERS.get(provider_type, SSEStreamAdapter)()


def adapter_names() -> list[str]:
    """Return the registered adapter names, sorted."""
    return sorted(_ADAPTERS)
