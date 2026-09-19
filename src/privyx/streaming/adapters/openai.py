"""OpenAI stream adapters: Chat Completions and the Responses API."""

from __future__ import annotations

from privyx.streaming.adapters.generic import SSEStreamAdapter

# OpenAI stream deltas live at choices[0].delta.content
OPENAI_DATA_PATH = ["choices", "0", "delta", "content"]


class OpenAIStreamAdapter(SSEStreamAdapter):
    """Adapter for OpenAI-style SSE streaming."""

    schema_name = "openai"

    def __init__(self) -> None:
        super().__init__(data_path=OPENAI_DATA_PATH)


class ResponsesStreamAdapter(SSEStreamAdapter):
    """Adapter for the OpenAI Responses API (``/v1/responses``) stream.

    Its typed ``response.*`` events carry text at several kinds of ``delta``, so
    :class:`~privyx.proxy.stream_router.StreamRouter` classifies every event
    itself; this adapter only names the schema.
    """

    schema_name = "responses"
