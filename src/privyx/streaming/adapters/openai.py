"""OpenAI chat-completions stream adapter."""

from __future__ import annotations

from privyx.streaming.adapters.generic import SSEStreamAdapter

# OpenAI stream deltas live at choices[0].delta.content
OPENAI_DATA_PATH = ["choices", "0", "delta", "content"]


class OpenAIStreamAdapter(SSEStreamAdapter):
    """Adapter for OpenAI-style SSE streaming."""

    def __init__(self) -> None:
        super().__init__(data_path=OPENAI_DATA_PATH)