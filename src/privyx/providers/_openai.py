"""The OpenAI provider: the generic one, aimed at the OpenAI API."""

from __future__ import annotations

from typing import ClassVar

from privyx.providers._generic import GenericProvider
from privyx.providers._urls import DEFAULT_BASE_URLS


class OpenAIProvider(GenericProvider):
    """Chat completions at ``api.openai.com`` by default, with a bearer key."""

    name = "openai"
    default_url: ClassVar[str] = DEFAULT_BASE_URLS["openai"]
