"""Gemini stream adapter (``streamGenerateContent?alt=sse``)."""

from __future__ import annotations

from privyx.streaming.adapters.generic import SSEStreamAdapter


class GeminiStreamAdapter(SSEStreamAdapter):
    """Adapter for the Gemini API stream.

    Each event is a whole ``GenerateContentResponse`` chunk, restored by
    :class:`~privyx.proxy.object_stream.GeminiStream`; this adapter only names
    the schema.
    """

    schema_name = "gemini"
