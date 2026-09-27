"""LLM-assisted detector (optional).

Uses a configured LLM client to detect sensitive spans that a regex or a
NER model would miss (context-dependent PII, domain-specific identifiers,
freeform descriptions of a person or place). Because this adds latency and
cost to every request, it is never the default and should only be enabled
explicitly in configuration — typically layered alongside ``regex`` via a
detector list rather than used alone.

Failure is closed by default: an error, a timeout (``timeout``), or a reply
with no JSON array of spans fails the request with
:class:`~privyx.core.errors.DetectorError`, so no text reaches the upstream
having skipped the scan it was configured for.  ``fallback_on_error`` opts into
scanning with the built-in regex patterns instead, which lets through whatever
only the LLM would have caught; every fallback is logged and counted as
``llm_fallbacks`` on the ``session.transform`` audit event.

Text longer than ``max_chars`` is scanned in overlapping chunks, all at once,
rather than truncated: a truncated tail would never be scanned at all.

``llm`` is an optional extra: selecting it without ``openai`` or
``anthropic`` installed (``pip install privyx[providers]``, or
``uv sync --all-extras``) fails at startup with a
:class:`~privyx.core.errors.ConfigError`, never mid-request — the same
contract ``faker``, ``encrypt``, and ``presidio`` follow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol

from privyx.core.context import Context
from privyx.core.errors import ConfigError, DetectorError
from privyx.core.result import Detection, Span
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.detector.yaml import _bounded

_log = logging.getLogger(__name__)

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
            max_tokens=4096,  # room for a dense chunk's spans; a cut-off reply fails
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
        timeout: Seconds the whole scan (every chunk) may take before it fails.
        max_chars: Longest text sent in one call; longer text is chunked.
        fallback_on_error: On failure, scan with the built-in regex patterns
            instead of raising :class:`~privyx.core.errors.DetectorError`.

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
        timeout: float = 30.0,
        max_chars: int = 4000,
        fallback_on_error: bool = False,
    ) -> None:
        self._client = client if client is not None else _load_client(provider, model, api_key)
        self._instructions = instructions or _DEFAULT_INSTRUCTIONS
        self._timeout = timeout
        self._max_chars = max_chars
        self._fallback = RegexDetector() if fallback_on_error else None

    async def detect(self, text: str, context: Context) -> Detection:
        try:
            # ponytail: every chunk is sent at once; bound it with a semaphore
            # if a provider starts rate-limiting long prompts.
            async with asyncio.timeout(self._timeout), asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(self._scan(offset, chunk, context))
                    for offset, chunk in _chunks(text, self._max_chars)
                ]
        except Exception as exc:
            if isinstance(exc, ExceptionGroup):  # a chunk failed; the rest were cancelled
                exc = exc.exceptions[0]
            if self._fallback is None:
                reason = (
                    f"timed out after {self._timeout}s" if isinstance(exc, TimeoutError) else exc
                )
                raise DetectorError(f"LLM detector failed: {reason}") from exc
            # The class name only: an SDK error message may quote the request.
            _log.warning("LLM detector failed (%s); scanning with regex only", type(exc).__name__)
            context.counters["llm_fallbacks"] += 1
            detection = await self._fallback.detect(text, context)
            detection.cacheable = False  # scan it properly next time
            return detection
        # Chunks overlap, so an entity in the overlap is found twice.
        unique: dict[tuple[int, int, str], Span] = {}
        for task in tasks:
            for span in task.result().spans:
                unique.setdefault((span.start, span.end, span.entity_type), span)
        return Detection(spans=list(unique.values()))

    async def _scan(self, offset: int, chunk: str, context: Context) -> Detection:
        prompt = f"{self._instructions}\n\nTEXT:\n{chunk}"
        response = await self._client.complete(prompt)
        # ponytail: ~4 characters per token, the same for every client; read
        # the SDK's usage fields instead if billing needs exact numbers.
        context.counters["llm_calls"] += 1
        context.counters["llm_input_tokens"] += len(prompt) // 4
        context.counters["llm_output_tokens"] += len(response) // 4
        return _parse_spans(response, chunk, offset)


def _chunks(text: str, size: int) -> list[tuple[int, str]]:
    """Split ``text`` into ``(offset, piece)`` pairs of at most ``size`` characters.

    Pieces overlap by a tenth of ``size``, so an entity cut by one boundary is
    whole in the next piece.
    """
    if len(text) <= size:
        return [(0, text)]
    overlap = size // 10
    return [(i, text[i : i + size]) for i in range(0, len(text) - overlap, size - overlap)]


def _parse_spans(response: str, text: str, offset: int = 0) -> Detection:
    """Parse an LLM reply into a Detection over ``text``.

    A reply with no JSON array in it means the model did not do the scan (it
    chatted, or ran out of tokens mid-array), so it raises rather than report
    "no PII": the caller then fails closed or falls back like any other error.
    A code fence or prose around the array is tolerated.

    Each span is located by the text the model reports, not by its offsets:
    models miscount characters, and a wrong offset inside ``text`` would mask
    the wrong words and let the real value through.  Every whole-word,
    case-insensitive occurrence is masked, and reported text that does not
    occur at all is a hallucination and is dropped.  Offsets are used only for a
    ``[start, end, type]`` span without text, and only when they are in range.
    A malformed span is dropped; it does not fail the reply.  ``offset`` shifts
    the spans of a chunk back to positions in the full text.

    Raises:
        DetectorError: If the reply holds no JSON array.
    """
    start, end = response.find("["), response.rfind("]")
    try:
        payload = json.loads(response[start : end + 1]) if 0 <= start < end else None
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, list):
        # Never quote the reply: it may repeat the scanned text.
        raise DetectorError("reply is not a JSON array of spans")

    detection = Detection()
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        entity = str(item[2])
        reported = item[3].strip() if len(item) > 3 and isinstance(item[3], str) else ""
        if reported:
            for match in re.finditer(_bounded(reported), text, re.IGNORECASE):
                detection.add(match.start() + offset, match.end() + offset, entity, match[0])
            continue
        try:
            first, last = int(item[0]), int(item[1])
        except (TypeError, ValueError):
            continue
        if 0 <= first < last <= len(text):
            detection.add(first + offset, last + offset, entity, text[first:last])
    return detection
