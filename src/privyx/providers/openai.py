"""OpenAI provider adapter.

Uses the generic HTTP passthrough; the SDK is optional and only used for
richer typing/integration when installed.
"""

from __future__ import annotations

from privyx.providers.generic import GenericProvider

DEFAULT_OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(GenericProvider):
    """OpenAI chat-completions provider via generic HTTP passthrough."""

    name = "openai"

    def __init__(
        self,
        base_url: str = DEFAULT_OPENAI_URL,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, headers=headers)
