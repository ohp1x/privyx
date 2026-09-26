"""LLM detector — config wiring, response parsing, and provider selection.

The LLM SDKs themselves are optional extras, so these tests inject a
stand-in client that implements the ``LLMClient`` protocol directly. What is
actually at risk here is not the model's judgment (that is the model's
problem) but the seam around it: whether a config can select it, how a
response is parsed into spans, and that a malformed/empty response never
raises instead of yielding no spans.
"""

from __future__ import annotations

import pytest

from privyx.core.context import Context
from privyx.core.errors import ConfigError, DetectorError
from privyx.privacy.detector.llm import LLMDetector
from privyx.privacy.detector.yaml import build_detector


class _FakeLLMClient:
    """Stands in for an SDK-backed client, recording the prompt it received."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response


class _FailingLLMClient:
    async def complete(self, prompt: str) -> str:
        raise RuntimeError("upstream is down")


async def test_parses_a_well_formed_response_into_spans() -> None:
    text = "mail <PRDE_EMAIL_3AC4F8DAD86FFF16>"
    start = text.index("<PRDE_EMAIL_3AC4F8DAD86FFF16>")
    end = start + len("<PRDE_EMAIL_3AC4F8DAD86FFF16>")
    client = _FakeLLMClient(f'[[{start}, {end}, "EMAIL", "<PRDE_EMAIL_3AC4F8DAD86FFF16>"]]')
    detector = LLMDetector(client=client)

    detection = await detector.detect(text, Context())

    spans = [(s.entity_type, s.text) for s in detection.spans]
    assert spans == [("EMAIL", "<PRDE_EMAIL_3AC4F8DAD86FFF16>")]


async def test_the_scanned_text_reaches_the_client_prompt() -> None:
    client = _FakeLLMClient("[]")
    detector = LLMDetector(client=client)

    await detector.detect("Ana works at Acme", Context())

    assert "Ana works at Acme" in client.prompts[0]


async def test_custom_instructions_replace_the_default_prompt() -> None:
    client = _FakeLLMClient("[]")
    detector = LLMDetector(client=client, instructions="Find only phone numbers.")

    await detector.detect("hello", Context())

    assert client.prompts[0].startswith("Find only phone numbers.")


@pytest.mark.parametrize(
    "response",
    [
        "not json at all",
        "{}",
        "null",
        "[1, 2]",
        '[["not", "an", "int"]]',
        "",
    ],
)
async def test_malformed_or_empty_responses_yield_no_spans_without_raising(response: str) -> None:
    detector = LLMDetector(client=_FakeLLMClient(response))

    detection = await detector.detect("some text", Context())

    assert detection.spans == []


async def test_an_out_of_range_offset_is_dropped_rather_than_raising() -> None:
    """A span whose offsets do not land on real text contributes nothing."""
    client = _FakeLLMClient('[[0, 999, "PERSON", "whatever"]]')
    detector = LLMDetector(client=client)

    detection = await detector.detect("hi", Context())

    assert detection.spans == []


async def test_client_failure_is_wrapped_in_a_detector_error() -> None:
    detector = LLMDetector(client=_FailingLLMClient())

    with pytest.raises(DetectorError, match="upstream is down"):
        await detector.detect("hello", Context())


def test_config_can_select_llm_with_an_unknown_provider() -> None:
    """``detector.type: llm`` must be a known type, distinct from an unknown one.

    An unsupported ``llm_provider`` value should fail with a message naming the
    problem, never with the generic "unknown detector type".
    """
    with pytest.raises(ConfigError, match="no built-in support for provider"):
        build_detector({"type": "llm", "llm_provider": "does-not-exist"})


def test_config_selects_llm_and_fails_fast_without_the_providers_extra() -> None:
    """Selecting ``llm`` never fails with 'unknown detector type'.

    Whether it succeeds depends on whether the ``providers`` extra (openai/
    anthropic) is installed in this environment; either way it must not reach
    a request before failing.
    """
    try:
        detector = build_detector({"type": "llm"})
    except ConfigError as exc:
        assert "unknown detector type" not in str(exc)
        pytest.skip(f"providers extra not installed: {exc}")
    else:
        assert detector.name == "llm"
