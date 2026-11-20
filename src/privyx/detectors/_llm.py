"""LLM-assisted detector (optional).

Uses a configured LLM client to detect sensitive spans that a regex or a
NER model would miss (context-dependent PII, domain-specific identifiers,
freeform descriptions of a person or place). Because this adds latency and
cost to every request, it is never the default and should only be enabled
explicitly in configuration — typically layered alongside ``regex`` via a
detector list rather than used alone.

Failure is closed by default: an error, a timeout (``timeout``), or a reply
with no JSON array of spans fails the request with
:class:`~privyx.core.DetectorError`, so no text reaches the upstream
having skipped the scan it was configured for.  ``fallback_on_error`` opts into
scanning with the built-in regex patterns instead, which lets through whatever
only the LLM would have caught; every fallback is logged and counted as
``llm_fallbacks`` on the ``session.transform`` audit event.

Text longer than ``max_chars`` is scanned in overlapping chunks, all at once,
rather than truncated: a truncated tail would never be scanned at all.

``llm`` is an optional extra: selecting it without ``openai`` or
``anthropic`` installed (``pip install privyx[providers]``, or
``uv sync --all-extras``) fails at startup with a
:class:`~privyx.core.ConfigError`, never mid-request — the same
contract ``faker``, ``encrypt``, and ``presidio`` follow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import Field

from privyx.core import ConfigError, Context, Detection, DetectorError, Services, Span
from privyx.detectors._base import Detector
from privyx.detectors._pattern import _ENTITY_NAME, _bounded
from privyx.detectors._regex import RegexDetector

_log = logging.getLogger(__name__)
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]+")

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
    "anthropic": ("anthropic", "AsyncAnthropic", _AnthropicClient, "claude-haiku-4-5"),
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


class LLMDetector(Detector):
    """Detect sensitive spans by asking an LLM.

    Every LLM SDK call is async, so this detector overrides :meth:`detect`
    only.  ``llm_provider`` names the built-in SDK (``openai`` or
    ``anthropic``) and ``llm_model`` its model, a small, cheap one when empty
    (this runs on every request when enabled).  ``llm_api_key`` is forwarded
    to the SDK; empty defers to the SDK's own environment variable
    (``OPENAI_API_KEY``, etc.).  ``llm_instructions`` replaces the prompt
    instructions.  The whole scan, every chunk, may take ``llm_timeout``
    seconds, and text longer than ``llm_max_chars`` is chunked.  With
    ``llm_fallback_on_error``, a failure scans with the built-in regex
    patterns instead of raising :class:`~privyx.core.DetectorError`.
    ``underscore_separates_words`` applies when a reported value is looked up
    (``JOHN`` in ``DB_JOHN``), and to the fallback's patterns.  The SDK
    client is built by :meth:`open`, or on first use.

    Args:
        client: A ready-made :class:`LLMClient`, for tests and advanced wiring
            (a custom endpoint, a different provider, request-level options).
            When given, no SDK is imported.
        **options: The detector's :class:`Options`.

    Raises:
        ConfigError: From :meth:`open`, if the provider's SDK is not
            installed, or the provider is not one Privyx builds in.
    """

    name = "llm"

    class Options(Detector.Options):
        """Prefixed, unlike presidio's, so they read apart from the upstream provider's."""

        llm_provider: str = "openai"  # built-in SDK ("openai" or "anthropic")
        llm_model: str = ""  # model name (empty → a small default per provider)
        llm_api_key: str = ""  # empty defers to the SDK's own env lookup
        llm_instructions: str = ""  # prompt instructions (empty → built-in default)
        llm_timeout: float = Field(default=30.0, gt=0)  # seconds before a scan fails
        llm_max_chars: int = Field(default=4000, gt=0)  # longer text is chunked
        llm_fallback_on_error: bool = False  # regex-only scan instead of failing

    options: Options

    def __init__(self, *, client: LLMClient | None = None, **options: Any) -> None:
        super().__init__(**options)
        self._client = client
        self._instructions = self.options.llm_instructions or _DEFAULT_INSTRUCTIONS
        self._timeout = self.options.llm_timeout
        self._max_chars = self.options.llm_max_chars
        self._underscore = self.options.underscore_separates_words
        self._fallback = (
            RegexDetector(underscore_separates_words=self._underscore)
            if self.options.llm_fallback_on_error
            else None
        )

    async def open(self, services: Services) -> None:
        """Build the SDK client."""
        self._loaded()

    def _loaded(self) -> LLMClient:
        if self._client is None:
            options = self.options
            self._client = _load_client(
                options.llm_provider, options.llm_model, options.llm_api_key
            )
        return self._client

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
        response = await self._loaded().complete(prompt)
        # ponytail: ~4 characters per token, the same for every client; read
        # the SDK's usage fields instead if billing needs exact numbers.
        context.counters["llm_calls"] += 1
        context.counters["llm_input_tokens"] += len(prompt) // 4
        context.counters["llm_output_tokens"] += len(response) // 4
        return _parse_spans(response, chunk, offset, underscore=self._underscore)


def _chunks(text: str, size: int) -> list[tuple[int, str]]:
    """Split ``text`` into ``(offset, piece)`` pairs of at most ``size`` characters.

    Pieces overlap by a tenth of ``size``, so an entity cut by one boundary is
    whole in the next piece.
    """
    if len(text) <= size:
        return [(0, text)]
    overlap = size // 10
    return [(i, text[i : i + size]) for i in range(0, len(text) - overlap, size - overlap)]


def _parse_spans(
    response: str, text: str, offset: int = 0, *, underscore: bool = False
) -> Detection:
    """Parse an LLM reply into a Detection over ``text``.

    A reply with no JSON array in it means the model did not do the scan (it
    chatted, or ran out of tokens mid-array), so it raises rather than report
    "no PII": the caller then fails closed or falls back like any other error.
    A code fence or prose around the array is tolerated.

    Each span is located by the text the model reports, not by its offsets:
    models miscount characters, and a wrong offset inside ``text`` would mask
    the wrong words and let the real value through.  Every whole-word occurrence
    with the same case is masked (case-insensitive would turn every "may" into
    a token once "May" is a name).  The model's offsets count too where the
    reported text is there as reported, whole word or not (``JOHN`` in
    ``JOHN_SMITH``), and then so does every occurrence ``_`` joins on;
    reported text found in neither way is a hallucination or a miscount, and
    is dropped.  Offsets alone are used for a ``[start, end,
    type]`` span without text, when they are in range.  With ``underscore``,
    ``_`` separates words.
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
        entity = _entity_type(item[2])
        reported = item[3].strip() if len(item) > 3 and isinstance(item[3], str) else ""
        found: set[tuple[int, int]] = set()
        for match in (
            re.finditer(_bounded(reported, underscore=underscore), text) if reported else ()
        ):
            found.add(match.span())
        try:
            first, last = int(item[0]), int(item[1])
        except (TypeError, ValueError):
            first = last = -1
        if 0 <= first < last <= len(text) and text[first:last] == (reported or text[first:last]):
            if reported and (first, last) not in found:
                # Inside a longer word where the model points, so also wherever
                # `_` joins it on: JOHN_SMITH twice, and the check never sees one.
                pattern = _bounded(reported, underscore=True)
                found.update(match.span() for match in re.finditer(pattern, text))
            found.add((first, last))
        if reported and not found:
            # Never the value: logs leave the machine more often than requests.
            _log.debug("LLM detector: a reported %s value is not in the text; dropped", entity)
        for start, end in sorted(found):
            detection.add(start + offset, end + offset, entity, text[start:end])
    return detection


def _entity_type(value: Any) -> str:
    """The model's entity type as a name a token can carry, or ``PII``.

    ``phone number`` becomes ``PHONE_NUMBER`` so it shares tokens with the
    regex detector's types; anything that still is not a valid name (empty,
    leading digit, over 64 characters) becomes ``PII`` rather than crash the
    token codec mid-request with the model's text in the error.
    """
    name = _NOT_ALNUM.sub("_", str(value)).strip("_").upper()
    return name if _ENTITY_NAME.fullmatch(name) else "PII"
