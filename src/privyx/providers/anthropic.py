"""Anthropic provider adapter.

Uses the generic HTTP passthrough with Anthropic's required headers.
"""

from __future__ import annotations

from privyx.providers.generic import GenericProvider

DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider(GenericProvider):
    """Anthropic messages provider via generic HTTP passthrough."""

    name = "anthropic"

    def __init__(
        self,
        base_url: str = DEFAULT_ANTHROPIC_URL,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> None:
        merged = {"anthropic-version": "2023-06-01", **(headers or {})}
        super().__init__(base_url=base_url, api_key=api_key, headers=merged)
