"""Detector factory that builds detectors from YAML configuration."""

from __future__ import annotations

import re
from typing import Any

from privyx.core.errors import ConfigError
from privyx.plugins.registry import PLUGINS
from privyx.privacy.detector.base import Detector
from privyx.privacy.detector.builtin import DEFAULT_PATTERNS, RegexDetector, YamlDetector


def build_detector(config: dict[str, Any]) -> Detector:
    """Build a detector from a config dict.

    Supports:
        - ``{"type": "regex", "patterns": {...}}``
        - ``{"type": "yaml", "patterns": {...}}``
        - a list of such configs → merged into a single detector.

    A ``regex`` detector layers the configured patterns *over* the built-in
    set, so adding one custom pattern never silently disables EMAIL/SSN/etc.
    A ``yaml`` detector uses only the patterns given — use it when you want
    full control over which entities are detected.

    Raises:
        ConfigError: If the config shape, detector type, or any regex is invalid.
    """
    if isinstance(config, list):
        merged: dict[str, str] = {}
        dtype = "yaml"
        for item in config:
            if not isinstance(item, dict):
                raise ConfigError(f"invalid detector config: {item!r}")
            if item.get("type") == "regex":
                dtype = "regex"
            merged.update(_patterns_from(item))
        patterns = {**DEFAULT_PATTERNS, **merged} if dtype == "regex" else merged
        _validate(patterns)
        return YamlDetector(patterns)

    if not isinstance(config, dict):
        raise ConfigError(f"invalid detector config: {config!r}")

    dtype = config.get("type", "regex")
    patterns = _patterns_from(config)
    if dtype == "regex":
        combined = {**DEFAULT_PATTERNS, **patterns}
        _validate(combined)
        return RegexDetector(combined)
    if dtype == "yaml":
        _validate(patterns)
        return YamlDetector(patterns)
    if dtype == "presidio":
        from privyx.privacy.detector.presidio import PresidioDetector

        return PresidioDetector(
            language=config.get("language", "en"),
            model=config.get("model", ""),
            entities=list(config.get("entities", [])) or None,
            score_threshold=float(config.get("score_threshold", 0.35)),
        )
    if dtype in PLUGINS.detectors:
        return PLUGINS.detectors.build(config)
    raise ConfigError(f"unknown detector type: {dtype}")


def _validate(patterns: dict[str, str]) -> None:
    """Fail fast on an invalid regex rather than at first request."""
    for entity, pattern in patterns.items():
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"invalid regex for entity {entity!r}: {exc}") from exc


def _patterns_from(config: dict[str, Any]) -> dict[str, str]:
    patterns = config.get("patterns", {})
    if not isinstance(patterns, dict):
        raise ConfigError("detector 'patterns' must be a mapping of entity -> regex")
    return {str(k): str(v) for k, v in patterns.items()}
