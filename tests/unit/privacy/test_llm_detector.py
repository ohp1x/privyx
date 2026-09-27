"""LLM detector — config wiring, response parsing, and provider selection.

The LLM SDKs themselves are optional extras, so these tests inject a
stand-in client that implements the ``LLMClient`` protocol directly. What is
actually at risk here is not the model's judgment (that is the model's
problem) but the seam around it: whether a config can select it, how a
response is parsed into spans, and that a reply the model botched fails the
scan instead of passing for "no PII found".
"""

from __future__ import annotations

import asyncio
import json
import re

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
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        raise RuntimeError("upstream is down")


class _SlowLLMClient:
    async def complete(self, prompt: str) -> str:
        await asyncio.sleep(5)
        return "[]"


class _NameFindingClient:
    """Answers like a well-behaved model: every "Ana" in the scanned text."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def complete(self, prompt: str) -> str:
        chunk = prompt.split("TEXT:\n", 1)[1]
        self.chunks.append(chunk)
        found = [[m.start(), m.end(), "PERSON", "Ana"] for m in re.finditer("Ana", chunk)]
        return json.dumps(found)


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
        "",
        '[[0, 3, "PERSON", "Ana"]',  # cut off mid-array (out of tokens)
    ],
)
async def test_a_reply_without_a_json_array_fails_the_scan(response: str) -> None:
    detector = LLMDetector(client=_FakeLLMClient(response))

    with pytest.raises(DetectorError) as info:
        await detector.detect("some text", Context())
    # Exactly this: the reply, which may repeat the scanned text, is never quoted.
    assert str(info.value) == "LLM detector failed: reply is not a JSON array of spans"


async def test_a_botched_reply_falls_back_when_allowed() -> None:
    context = Context()
    detector = LLMDetector(client=_FakeLLMClient("Sure! Here you go."), fallback_on_error=True)

    detection = await detector.detect("mail ann@example.com", context)

    assert [s.entity_type for s in detection.spans] == ["EMAIL"]
    assert context.counters["llm_fallbacks"] == 1


@pytest.mark.parametrize("response", ["[1, 2]", '[["not", "an", "int"]]', "[]"])
async def test_malformed_spans_are_dropped_without_failing_the_reply(response: str) -> None:
    detector = LLMDetector(client=_FakeLLMClient(response))

    detection = await detector.detect("some text", Context())

    assert detection.spans == []


@pytest.mark.parametrize(
    "response",
    [
        '```json\n[[0, 3, "PERSON", "Ana"]]\n```',
        'Here are the spans: [[0, 3, "PERSON", "Ana"]] Let me know!',
    ],
)
async def test_an_array_wrapped_in_a_fence_or_prose_is_still_read(response: str) -> None:
    detector = LLMDetector(client=_FakeLLMClient(response))

    detection = await detector.detect("Ana called", Context())

    assert [(s.start, s.text) for s in detection.spans] == [(0, "Ana")]


async def test_spans_follow_the_reported_text_not_the_offsets() -> None:
    """A miscounted offset must not mask the wrong words and leak the name."""
    text = "Call Ana, then Ana again; bananas are fine."
    detector = LLMDetector(client=_FakeLLMClient('[[1, 4, "PERSON", "Ana"]]'))

    detection = await detector.detect(text, Context())

    # Every whole-word occurrence; never inside "bananas".
    assert [(s.start, s.text) for s in detection.spans] == [(5, "Ana"), (15, "Ana")]


async def test_a_name_that_is_also_a_word_masks_only_its_own_case() -> None:
    detector = LLMDetector(client=_FakeLLMClient('[[0, 3, "PERSON", "May"]]'))

    detection = await detector.detect("May said you may go", Context())

    assert [(s.start, s.text) for s in detection.spans] == [(0, "May")]


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("PERSON", "PERSON"),
        ("person", "PERSON"),
        ("phone number", "PHONE_NUMBER"),
        ("E-mail", "E_MAIL"),
        ("", "PII"),
        ("1X", "PII"),
        ("?!", "PII"),
        ("A" * 65, "PII"),
    ],
)
async def test_entity_types_are_normalized_to_token_safe_names(
    reported: str, expected: str
) -> None:
    reply = json.dumps([[0, 3, reported, "Ana"]])
    detector = LLMDetector(client=_FakeLLMClient(reply))

    detection = await detector.detect("Ana called", Context())

    assert [s.entity_type for s in detection.spans] == [expected]


async def test_reported_text_that_does_not_occur_is_dropped() -> None:
    detector = LLMDetector(client=_FakeLLMClient('[[0, 2, "PERSON", "Zed"]]'))

    detection = await detector.detect("hi there", Context())

    assert detection.spans == []


async def test_offsets_are_used_for_a_span_without_text() -> None:
    detector = LLMDetector(client=_FakeLLMClient('[[5, 8, "PERSON"], [0, 999, "PERSON"]]'))

    detection = await detector.detect("Call Ana", Context())

    assert [(s.start, s.text) for s in detection.spans] == [(5, "Ana")]  # out of range dropped


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


def test_config_selects_llm_and_fails_fast_without_the_providers_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting ``llm`` never fails with 'unknown detector type'.

    Whether it succeeds depends on whether the ``providers`` extra (openai/
    anthropic) is installed in this environment; either way it must not reach
    a request before failing.
    """
    # Without a key the SDK refuses to construct a client, so this skipped as
    # "extra not installed" even where it is (CI included).
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    try:
        detector = build_detector({"type": "llm"})
    except ConfigError as exc:
        assert "unknown detector type" not in str(exc)
        pytest.skip(f"providers extra not installed: {exc}")
    else:
        assert detector.name == "llm"


async def test_a_slow_client_times_out_as_a_detector_error() -> None:
    detector = LLMDetector(client=_SlowLLMClient(), timeout=0.05)

    with pytest.raises(DetectorError, match="timed out after 0.05s"):
        await detector.detect("hello", Context())


async def test_fallback_scans_with_regex_and_is_counted() -> None:
    context = Context()
    detector = LLMDetector(client=_FailingLLMClient(), fallback_on_error=True)

    detection = await detector.detect("mail ann@example.com", context)

    assert [(s.entity_type, s.text) for s in detection.spans] == [("EMAIL", "ann@example.com")]
    assert detection.cacheable is False
    assert context.counters["llm_fallbacks"] == 1


def test_a_fallback_result_is_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built from config with the default cache, a fallback is retried next turn."""
    client = _FailingLLMClient()
    monkeypatch.setattr("privyx.privacy.detector.llm._load_client", lambda *_: client)
    detector = build_detector({"type": "llm", "llm_fallback_on_error": True, "cache": True})

    for _ in range(2):
        asyncio.run(detector.detect("mail ann@example.com", Context()))

    assert client.calls == 2


async def test_long_text_is_scanned_in_overlapping_chunks() -> None:
    """Nothing past ``max_chars`` goes unscanned, and spans map back to the full text."""
    text = "Ana " + "x" * 300 + " Ana " + "y" * 300 + " Ana"
    client = _NameFindingClient()
    context = Context()
    detector = LLMDetector(client=client, max_chars=200)

    detection = await detector.detect(text, context)

    assert len(client.chunks) > 1
    assert all(len(chunk) <= 200 for chunk in client.chunks)
    starts = sorted(s.start for s in detection.spans)
    assert starts == [m.start() for m in re.finditer("Ana", text)]  # once each, overlap deduped
    assert all(text[s.start : s.end] == "Ana" for s in detection.spans)
    assert context.counters["llm_calls"] == len(client.chunks)
    assert context.counters["llm_input_tokens"] > 0


async def test_one_failing_chunk_fails_the_whole_scan() -> None:
    detector = LLMDetector(client=_FailingLLMClient(), max_chars=10)

    with pytest.raises(DetectorError, match="upstream is down"):
        await detector.detect("a" * 50, Context())
