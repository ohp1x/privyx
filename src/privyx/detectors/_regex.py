"""The ``regex`` detector: named regular expressions and literal terms."""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from privyx.core import Context, Detection
from privyx.detectors import _catalog as catalog
from privyx.detectors import _decode as decode
from privyx.detectors import _normalize as normalize
from privyx.detectors._base import Detector
from privyx.detectors._pattern import (
    _CHECKS,
    UNDERSCORE_SPLIT,
    _builtin,
    _builtins,
    _check_for,
    _detect,
    _enabled,
    _pattern_options,
    _patterns_from,
    _validate,
    split_underscore,
)


class RegexDetector(Detector):
    """The built-in entity patterns, with the configured patterns and terms over them.

    Adding one pattern never silently disables EMAIL, SSN, and the rest;
    ``disable`` turns a built-in off, and ``builtins: false`` all of them but
    those ``enable`` names.

    ``patterns`` maps an entity to a regex; ``terms`` maps an entity to a list
    of literal strings, compiled into one case-insensitive regex.  Both may be
    set at once: with ``merge_terms`` off, a hand-written ``patterns`` entry
    wins over a ``terms`` entry for the same entity.  ``context`` maps an
    entity to words (lower case, phrases allowed), one of which must be within
    five words of a match for it to count; it replaces the built-in context
    words of that entity.  ``decode`` also finds values split into string
    literals or encoded, ``normalize`` values written with fullwidth or other
    compatibility characters, or with zero-width characters inside.

    Raises:
        ConfigError: If an entity name, a regex, ``enable``, ``disable``, or
            ``context`` is invalid.
    """

    name = "regex"

    class Options(Detector.Options):
        #: The built-in patterns, under the configured ones; ``false`` keeps
        #: only the configured ones and the opt-in built-ins ``enable`` names.
        builtins: bool = True
        patterns: dict[str, str] = Field(default_factory=dict)
        terms: dict[str, list[str]] = Field(default_factory=dict)
        merge_terms: bool = True  # match terms before patterns for the same entity
        term_variants: bool = True  # terms also match acme-corp, acme_corp, … for "Acme Corp"
        # entity -> words, one of which must be near a match
        context: dict[str, list[str]] = Field(default_factory=dict)
        enable: list[str] = Field(default_factory=list)  # opt-in built-in entities
        disable: list[str] = Field(default_factory=list)  # built-in entities to turn off
        entropy_threshold: float = Field(default=3.0, gt=0)  # HIGH_ENTROPY: bits per character
        decode: bool = False  # also scan split strings and Base64/hex/URL-encoded text
        normalize: bool = False  # also scan with fullwidth and zero-width characters folded

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        config = self.options.model_dump()
        combined = {**_enabled(config), **_patterns_from(config)}
        if self.options.builtins:
            combined = {**_builtins(config), **combined}
        _validate(combined)
        settings = _pattern_options(config, combined)
        self._decode = settings["decode"]
        self._normalize = settings["normalize"]
        threshold = settings.get("entropy_threshold", catalog.ENTROPY_THRESHOLD)
        checks = {**_CHECKS, "HIGH_ENTROPY": catalog.high_entropy(threshold)}
        words = {
            entity: frozenset(w.lower() for w in v) for entity, v in settings["context"].items()
        }
        self._compiled = []
        for entity, raw in combined.items():
            builtin = _builtin(entity, raw)
            if settings["underscore"] and builtin and entity in UNDERSCORE_SPLIT:
                raw = split_underscore(raw)
            pattern = re.compile(raw)
            default = catalog.CONTEXT.get(entity, frozenset()) if builtin else None
            self._compiled.append(
                (
                    entity,
                    pattern,
                    _check_for(entity, pattern, checks, builtin),
                    words.get(entity, default),
                )
            )

    def detect_sync(self, text: str, context: Context) -> Detection:
        detection = _detect(self._compiled, text)
        if self._decode:
            detection.spans += decode.hidden(text, lambda t: _detect(self._compiled, t)).spans
        if self._normalize:
            found = normalize.normalized(text, lambda t: _detect(self._compiled, t))
            # What the scan of the text as written found already: counted once.
            seen = {(s.start, s.end, s.entity_type) for s in detection.spans}
            detection.spans += [
                s for s in found.spans if (s.start, s.end, s.entity_type) not in seen
            ]
        return detection
