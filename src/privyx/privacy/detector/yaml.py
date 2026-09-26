"""Detector factory that builds detectors from YAML configuration."""

from __future__ import annotations

import re
from typing import Any

from privyx.core.errors import ConfigError
from privyx.plugins.registry import PLUGINS
from privyx.privacy.detector.base import Detector
from privyx.privacy.detector.builtin import (
    DEFAULT_PATTERNS,
    CompositeDetector,
    RegexDetector,
    YamlDetector,
)
from privyx.privacy.detector.cache import CachedDetector

#: Entity names must round-trip through a token, so they follow the codec's
#: ``type`` grammar (see :mod:`privyx.token.codec`).
_ENTITY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def build_detector(config: dict[str, Any] | list[dict[str, Any]]) -> Detector:
    """Build a detector from a config dict.

    Supports:
        - ``{"type": "regex", "patterns": {...}, "terms": {...}}``
        - ``{"type": "yaml", "patterns": {...}, "terms": {...}}``
        - ``{"type": "presidio", ...}`` or a plugin detector type
        - a list of such configs → each is built on its own and run together
          (see :class:`~privyx.privacy.detector.builtin.CompositeDetector`).

    ``patterns`` maps an entity to a regex; ``terms`` maps an entity to a list
    of literal strings, compiled here into one case-insensitive regex.  Both may
    be set at once — see :func:`_patterns_from` for how they combine.

    A ``regex`` detector layers the configured patterns *over* the built-in
    set, so adding one custom pattern never silently disables EMAIL/SSN/etc.
    A ``yaml`` detector uses only the patterns given — use it when you want
    full control over which entities are detected.

    Raises:
        ConfigError: If the config shape, detector type, or any regex is invalid.
    """
    if isinstance(config, list):
        if not config:
            # No detector means nothing is ever pseudonymized — fail loudly.
            raise ConfigError("detector list must not be empty")
        detectors = []
        for item in config:
            if not isinstance(item, dict):
                raise ConfigError(f"invalid detector config: {item!r}")
            detectors.append(build_detector(item))
        return detectors[0] if len(detectors) == 1 else CompositeDetector(detectors)

    if not isinstance(config, dict):
        raise ConfigError(f"invalid detector config: {config!r}")

    dtype = config.get("type", "regex")
    patterns = _patterns_from(config)
    detector: Detector
    if dtype == "regex":
        combined = {**DEFAULT_PATTERNS, **patterns}
        _validate(combined)
        detector = RegexDetector(combined)
    elif dtype == "yaml":
        _validate(patterns)
        detector = YamlDetector(patterns)
    elif dtype == "presidio":
        from privyx.privacy.detector.presidio import PresidioDetector

        detector = PresidioDetector(
            language=config.get("language", "en"),
            model=config.get("model", ""),
            entities=list(config.get("entities", [])) or None,
            score_threshold=float(config.get("score_threshold", 0.35)),
        )
    elif dtype in PLUGINS.detectors:
        detector = PLUGINS.detectors.build(config)
    else:
        raise ConfigError(f"unknown detector type: {dtype}")

    cache = config.get("cache")
    if cache is not None:
        enabled = cache if isinstance(cache, bool) else cache.get("enabled", True)
        if enabled:
            max_size = 10000 if isinstance(cache, bool) else cache.get("max_size", 10000)
            return CachedDetector(detector, max_size=max_size)
    return detector


def _validate(patterns: dict[str, str]) -> None:
    """Fail fast on an invalid entity name or regex rather than at first request.

    The name check mirrors the token codec's ``type`` grammar
    (:data:`privyx.token.codec._DEFAULT_GRAMMARS`): an entity that cannot be
    serialized into a token would otherwise only blow up mid-request, once
    something is actually detected.
    """
    for entity, pattern in patterns.items():
        if not _ENTITY_NAME.fullmatch(entity):
            raise ConfigError(
                f"invalid entity name {entity!r}: must start with a letter and "
                "contain only letters, digits, or underscores (max 64 chars)"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"invalid regex for entity {entity!r}: {exc}") from exc


def _patterns_from(config: dict[str, Any]) -> dict[str, str]:
    """Collect ``terms`` and ``patterns`` into a single entity -> regex map.

    A hand-written ``patterns`` entry wins over a ``terms`` entry for the same
    entity: writing the regex yourself is the deliberate choice.  Entities that
    appear in only one of the two are all kept.
    """
    patterns = config.get("patterns", {})
    if not isinstance(patterns, dict):
        raise ConfigError("detector 'patterns' must be a mapping of entity -> regex")
    compiled = {str(k): str(v) for k, v in patterns.items()}
    return {**_terms_to_patterns(config.get("terms", {})), **compiled}


def _terms_to_patterns(terms: Any) -> dict[str, str]:
    """Compile literal term lists into one case-insensitive regex per entity.

    Matching is case-insensitive via an inline ``(?i:...)`` group rather than a
    compile flag, so the detectors stay untouched and a hand-written ``patterns``
    regex keeps its own (case-sensitive) semantics.

    Terms are ordered longest-first so a compound term wins over a substring of
    itself, and each one gets word boundaries only on the sides where it ends in
    a word character — that keeps ``ann`` out of ``annual`` while still matching
    a URL that ends in ``/``.
    """
    if not isinstance(terms, dict):
        raise ConfigError("detector 'terms' must be a mapping of entity -> list of strings")

    patterns: dict[str, str] = {}
    for entity, values in terms.items():
        if isinstance(values, str) or not isinstance(values, (list, tuple)):
            raise ConfigError(
                f"detector 'terms' for entity {str(entity)!r} must be a list of strings, "
                f"got {type(values).__name__}"
            )
        cleaned = {str(v).strip() for v in values}
        cleaned.discard("")
        if not cleaned:
            # An empty alternation would match the empty string everywhere.
            continue
        # Longest first; the alphabetical tie-break keeps the pattern stable
        # across runs, since `cleaned` is a set.
        ordered = sorted(cleaned, key=lambda term: (-len(term), term))
        alternatives = [_bounded(term) for term in ordered]
        patterns[str(entity)] = "(?i:" + "|".join(alternatives) + ")"
    return patterns


def _bounded(term: str) -> str:
    """Escape ``term`` and guard the sides that would otherwise match mid-word."""
    prefix = r"(?<!\w)" if _is_word_char(term[0]) else ""
    suffix = r"(?!\w)" if _is_word_char(term[-1]) else ""
    return f"{prefix}{re.escape(term)}{suffix}"


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"
