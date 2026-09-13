"""Tests for detector components."""

from __future__ import annotations

import pytest

from privyx.core.context import Context
from privyx.privacy.detector.builtin import RegexDetector, YamlDetector


@pytest.mark.asyncio
async def test_regex_detector_finds_email() -> None:
    detector = RegexDetector()
    detection = await detector.detect("Email: alice@example.com", Context())
    assert len(detection.spans) == 1
    span = detection.spans[0]
    assert span.entity_type == "EMAIL"
    assert span.text == "alice@example.com"


@pytest.mark.asyncio
async def test_regex_detector_finds_multiple_entities() -> None:
    detector = RegexDetector()
    text = "alice@example.com +1 (555) 123-4567 192.168.0.1"
    detection = await detector.detect(text, Context())
    types = {s.entity_type for s in detection.spans}
    assert "EMAIL" in types
    assert "PHONE" in types
    assert "IP_ADDRESS" in types


@pytest.mark.asyncio
async def test_yaml_detector_custom_rules() -> None:
    detector = YamlDetector({"CODE": r"\b[A-Z]{3}\d{2}\b"})
    detection = await detector.detect("code is ABC12", Context())
    assert len(detection.spans) == 1
    assert detection.spans[0].entity_type == "CODE"
    assert detection.spans[0].text == "ABC12"


@pytest.mark.asyncio
async def test_no_false_positive_on_plain_text() -> None:
    detector = RegexDetector()
    detection = await detector.detect("hello world, nothing sensitive here", Context())
    assert not detection