"""Unit tests for CachedDetector and turn detection caching."""

from __future__ import annotations

import json
import tracemalloc

import pytest

from privyx.config.env import env_config
from privyx.config.schema import DetectorConfig, Settings
from privyx.core.builder import build_detector_from
from privyx.core.context import Context
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.detector.cache import CachedDetector
from privyx.privacy.detector.yaml import build_detector


@pytest.mark.asyncio
async def test_cached_detector_hits_and_misses() -> None:
    inner = RegexDetector({"EMAIL": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"})
    cached = CachedDetector(inner, max_size=100)

    ctx = Context()
    text = "reach me at user@example.com for questions"

    # First call: cache miss
    det1 = await cached.detect(text, ctx)
    assert cached.misses == 1
    assert cached.hits == 0
    assert len(det1.spans) == 1
    assert det1.spans[0].text == "user@example.com"

    # Second call: cache hit
    det2 = await cached.detect(text, ctx)
    assert cached.misses == 1
    assert cached.hits == 1
    assert len(det2.spans) == 1
    assert det2.spans[0].text == "user@example.com"
    assert det2.spans[0].start == det1.spans[0].start

    # Mutating returned detection spans must not corrupt cached copy
    det2.spans.clear()
    det3 = await cached.detect(text, ctx)
    assert len(det3.spans) == 1


@pytest.mark.asyncio
async def test_cached_detector_lru_eviction() -> None:
    inner = RegexDetector({"WORD": r"\btest\b"})
    cached = CachedDetector(inner, max_size=2)
    ctx = Context()

    await cached.detect("test 1", ctx)
    await cached.detect("test 2", ctx)
    assert cached.size == 2

    # Access "test 1" to mark it most recently used
    await cached.detect("test 1", ctx)
    assert cached.hits == 1

    # Insert "test 3" -> "test 2" should be evicted
    await cached.detect("test 3", ctx)
    assert cached.size == 2

    # "test 1" should still hit
    await cached.detect("test 1", ctx)
    assert cached.hits == 2

    # "test 2" should miss (evicted)
    await cached.detect("test 2", ctx)
    assert cached.misses == 4  # test 1, test 2, test 3, test 2


def test_cached_detector_sync() -> None:
    inner = RegexDetector({"IP": r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"})
    cached = CachedDetector(inner, max_size=10)
    ctx = Context()

    text = "server at 192.168.1.1"
    det1 = cached.detect_sync(text, ctx)
    assert cached.misses == 1
    assert cached.hits == 0
    assert len(det1.spans) == 1

    det2 = cached.detect_sync(text, ctx)
    assert cached.misses == 1
    assert cached.hits == 1
    assert len(det2.spans) == 1


@pytest.mark.asyncio
async def test_cached_detector_empty_detection_cached() -> None:
    inner = RegexDetector({"EMAIL": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"})
    cached = CachedDetector(inner, max_size=10)
    ctx = Context()

    text = "hello world with no sensitive data here"
    det1 = await cached.detect(text, ctx)
    assert not det1
    assert cached.misses == 1
    assert cached.hits == 0

    det2 = await cached.detect(text, ctx)
    assert not det2
    assert cached.hits == 1


def test_cached_detector_clear_and_properties() -> None:
    inner = RegexDetector({"EMAIL": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"})
    cached = CachedDetector(inner, max_size=50)

    assert cached.inner is inner
    assert cached.unwrap() is inner
    assert cached.name == inner.name
    assert cached.max_size == 50

    cached.detect_sync("alice@example.com", Context())
    assert cached.size == 1
    assert cached.misses == 1

    cached.clear()
    assert cached.size == 0
    assert cached.hits == 0
    assert cached.misses == 0


def test_cache_does_not_keep_the_texts_it_saw() -> None:
    cached = CachedDetector(RegexDetector({"EMAIL": r"u\d+@example\.com"}), max_size=100)
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for i in range(100):
            cached.detect_sync(f"{i} mail u{i}@example.com " + "x" * 50_000, Context())
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()

    assert cached.size == 100
    assert held < 500_000  # the texts alone are 5 MB


def test_cache_takes_text_with_a_lone_surrogate() -> None:
    cached = CachedDetector(RegexDetector({"EMAIL": r"u@example\.com"}), max_size=10)
    text = json.loads('"\\ud800 mail u@example.com"')  # valid JSON, not valid UTF-8

    for _ in range(2):
        assert [s.text for s in cached.detect_sync(text, Context()).spans] == ["u@example.com"]
    assert (cached.misses, cached.hits) == (1, 1)


def test_detector_config_cache_coercion() -> None:
    # Defaults
    cfg1 = DetectorConfig()
    assert cfg1.cache.enabled is True
    assert cfg1.cache.max_size == 10000

    # Boolean false
    cfg2 = DetectorConfig(cache=False)
    assert cfg2.cache.enabled is False

    # Boolean true
    cfg3 = DetectorConfig(cache=True)
    assert cfg3.cache.enabled is True

    # Dict override
    cfg4 = DetectorConfig(cache={"enabled": False, "max_size": 250})
    assert cfg4.cache.enabled is False
    assert cfg4.cache.max_size == 250


def test_build_detector_cache_handling() -> None:
    # Explicit cache=True
    d_cached = build_detector({"type": "regex", "cache": True})
    assert isinstance(d_cached, CachedDetector)
    assert d_cached.max_size == 10000

    # Explicit cache=False
    d_raw = build_detector({"type": "regex", "cache": False})
    assert not isinstance(d_raw, CachedDetector)
    assert isinstance(d_raw, RegexDetector)

    # Explicit cache dict
    d_custom = build_detector({"type": "regex", "cache": {"enabled": True, "max_size": 500}})
    assert isinstance(d_custom, CachedDetector)
    assert d_custom.max_size == 500

    # Explicit cache disabled dict
    d_disabled = build_detector({"type": "regex", "cache": {"enabled": False}})
    assert not isinstance(d_disabled, CachedDetector)

    # Build from settings
    s_enabled = Settings(detector=DetectorConfig(cache=True))
    d_from_s = build_detector_from(s_enabled)
    assert isinstance(d_from_s, CachedDetector)

    s_disabled = Settings(detector=DetectorConfig(cache=False))
    d_from_s_dis = build_detector_from(s_disabled)
    assert not isinstance(d_from_s_dis, CachedDetector)


def test_env_detector_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_DETECTOR_CACHE", "false")
    cfg = env_config()
    assert cfg.get("detector", {}).get("cache") is False

    monkeypatch.setenv("PRIVYX_DETECTOR_CACHE", "1")
    cfg = env_config()
    assert cfg.get("detector", {}).get("cache") is True
