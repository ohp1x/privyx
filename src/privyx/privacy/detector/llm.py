"""LLM-assisted detector (optional).

Uses a configured LLM client to detect sensitive spans that a regex or a
NER model would miss (context-dependent PII, domain-specific identifiers,
freeform descriptions of a person or place). Because this adds latency and
cost to every request, it is never the default and should only be enabled
explicitly in configuration — typically layered alongside ``regex`` via a
detector list rather than used alone.

``llm`` is an optional extra: selecting it without ``openai`` or
``anthropic`` installed (``pip install privyx[providers]``, or
``uv sync --all-extras``) fails at startup with a
:class:`~privyx.core.errors.ConfigError`, never mid-request — the same
contract ``faker``, ``encrypt``, and ``presidio`` follow.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from privyx.core.context import Context
from privyx.core.errors import ConfigError, DetectorError
from privyx.core.result import Detection

#: Default instructions appended before the text to scan. Kept strict about
#: the output shape (JSON array only) since the response is parsed, not read.
_DEFAULT_INSTRUCTIONS = (
    "List every span of personal or sensitive data in the text below. "
    "Respond with ONLY a JSON array of [start, end, entity_type, text] tuples "
    "(character offsets into the text, entity_type in upper snake case, e.g. "
    'PERSON, LOCATION, ORGANIZATION). Respond with "[]" if none are found. '
    "Do not include any other text in the response."
)


class LLMClient(Protocol):
    async def complete(self, prompt: str) -> str: ...


class _OpenAIClient:
    """Adapts an OpenAI chat-completions client to :class:`LLMClient`."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    async def complete(self, prompt: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return response.choices[0].message.content or ""


class _AnthropicClient:
    """Adapts an Anthropic messages client to :class:`LLMClient`."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    async def complete(self, prompt: str) -> str:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


#: Provider name -> (import path, SDK client attr, adapter factory, default model).
#: The default model is deliberately small/cheap: this detector runs on every
#: request when enabled, so cost and latency compound quickly.
_ProviderEntry = tuple[str, str, Callable[[Any, str], LLMClient], str]
_PROVIDER_DEFAULTS: dict[str, _ProviderEntry] = {
    "openai": ("openai", "AsyncOpenAI", _OpenAIClient, "gpt-4o-mini"),
    "anthropic": ("anthropic", "AsyncAnthropic", _AnthropicClient, "claude-3-5-haiku-latest"),
}


def _load_client(provider: str, model: str, api_key: str) -> LLMClient:
    """Build an :class:`LLMClient` backed by the named provider's SDK.

    Raises:
        ConfigError: If ``provider`` is unknown, its SDK package is not
            installed, or the SDK itself refuses to construct a client (most
            commonly: no API key found, neither passed nor in the SDK's usual
            environment variable) — all at startup, so a request never fails
            mid-flight, matching the ``faker``/``encrypt``/``presidio`` contract.
    """
    try:
        module_name, client_attr, adapter, default_model = _PROVIDER_DEFAULTS[provider]
    except KeyError:
        known = ", ".join(sorted(_PROVIDER_DEFAULTS))
        raise ConfigError(
            f"detector type 'llm' has no built-in support for provider {provider!r}; "
            f"known providers: {known}. Pass a ready-made 'client' for anything else."
        ) from None

    try:
        import importlib

        sdk = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ConfigError(
            "detector type 'llm' requires the 'providers' extra; install with "
            "`pip install privyx[providers]` (or `uv sync --all-extras`)"
        ) from exc

    sdk_client_cls = getattr(sdk, client_attr)
    try:
        sdk_client = sdk_client_cls(api_key=api_key or None)
    except Exception as exc:  # noqa: BLE001 - any SDK constructor failure, not just its own error type
        raise ConfigError(
            f"detector type 'llm' could not construct a {provider!r} client: {exc}. "
            f"Pass 'llm_api_key' / set the SDK's usual API key environment variable, "
            f"or pass a ready-made 'client'."
        ) from exc
    return adapter(sdk_client, model or default_model)


class LLMDetector:
    """Detect sensitive spans by asking an LLM.

    Implements the :class:`~privyx.privacy.detector.base.Detector` protocol
    directly rather than extending ``BaseDetector``: every LLM SDK call is
    inherently async, so there is no meaningful ``detect_sync`` to provide (the
    same reasoning behind :class:`~privyx.privacy.detector.builtin.CompositeDetector`
    not extending it either).

    Args:
        provider: Built-in SDK to use (``openai`` or ``anthropic``) when
            ``client`` is not given.
        model: Model name for ``provider``. Defaults to a small, cheap model
            for that provider (this runs on every request when enabled).
        api_key: API key forwarded to the provider's SDK. Empty defers to the
            SDK's own environment-variable lookup (``OPENAI_API_KEY``, etc.).
        instructions: Prompt instructions prepended to the scanned text.
        client: A ready-made :class:`LLMClient`, for tests and advanced wiring
            (a custom endpoint, a different provider, request-level options).
            When given, ``provider``/``model``/``api_key`` are ignored and no
            SDK is imported.

    Raises:
        ConfigError: If ``client`` is omitted and ``provider``'s SDK is not
            installed, or ``provider`` is not one Privyx builds in.
    """

    name = "llm"

    def __init__(
        self,
        provider: str = "openai",
        model: str = "",
        api_key: str = "",
        instructions: str | None = None,
        client: LLMClient | None = None,
    ) -> None:
        self._client = client if client is not None else _load_client(provider, model, api_key)
        self._instructions = instructions or _DEFAULT_INSTRUCTIONS

    async def detect(self, text: str, context: Context) -> Detection:
        try:
            response = await self._client.complete(f"{self._instructions}\n\nTEXT:\n{text}")
        except Exception as exc:  # pragma: no cover - external dependency
            raise DetectorError(f"LLM detector failed: {exc}") from exc
        return _parse_spans(response, text)


def _parse_spans(response: str, text: str) -> Detection:
    """Best-effort parse of an LLM JSON response into a Detection.

    An LLM can hallucinate offsets that do not exist in ``text`` (negative,
    reversed, or past the end). Python slicing does not raise for those — it
    silently clamps — so bounds are checked explicitly rather than trusting
    the slice to fail. A span that fails the check is dropped rather than
    raising: one bad tuple in the response should not fail the whole request.
    """
    import json

    detection = Detection()
    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        return detection
    if not isinstance(payload, list):
        return detection
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        try:
            start, end, entity = int(item[0]), int(item[1]), str(item[2])
        except (TypeError, ValueError):
            continue
        if not (0 <= start < end <= len(text)):
            continue
        detection.add(start, end, entity, text[start:end])
    return detection
