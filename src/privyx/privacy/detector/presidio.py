"""Presidio-backed detector — NER for the entities a regex cannot catch.

The built-in ``regex`` detector is reliable for *structured* PII (email, SSN,
credit card) and blind to everything else.  Presidio adds names, organizations,
locations, and the rest by running spaCy over the text.

Presidio (and the spaCy model it needs) is an optional dependency: selecting
``detector.type: presidio`` without it installed fails at startup with a
:class:`~privyx.core.errors.ConfigError`, never mid-request — the same contract
the ``faker`` and ``encrypt`` operators follow.
"""

from __future__ import annotations

import asyncio
from typing import Any

from privyx.core.context import Context
from privyx.core.errors import ConfigError
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector

#: Presidio entity names → the names the rest of Privyx speaks (the built-in
#: patterns, ``ENTITY_PRIORITY``, and the ``strict`` policy's allow-list).
#: Without this, ``policy: strict`` would drop every Presidio span — its
#: allow-list holds ``EMAIL``, not ``EMAIL_ADDRESS`` — and forward the PII
#: untouched.  Unlisted types pass through under their Presidio name.
ENTITY_ALIASES: dict[str, str] = {
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "US_SSN": "SSN",
    "US_BANK_NUMBER": "BANK_ACCOUNT",
    "LOCATION": "LOCATION",
    "ORGANIZATION": "ORG",
}

#: Analyzers keyed by ``(language, model)``.  Loading spaCy costs seconds, and
#: ``privyx doctor`` alone builds several engines; the reference implementation
#: this was taken from cached for the same reason.
_ANALYZERS: dict[tuple[str, str], Any] = {}


def _load_analyzer(language: str, model: str, kwargs: dict[str, Any]) -> Any:
    """Build (or reuse) a Presidio analyzer backed by an explicit spaCy model.

    The NLP engine is configured explicitly rather than left to Presidio's
    default, which quietly expects ``en_core_web_lg`` to be present.

    Raises:
        ConfigError: If Presidio is not installed, or the spaCy model is
            missing — both at startup, so a request never fails halfway.
    """
    cached = _ANALYZERS.get((language, model))
    if cached is not None:
        return cached

    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ConfigError(
            "detector type 'presidio' requires the 'presidio' extra; install with "
            "`pip install privyx[presidio]` (or `uv sync --all-extras`)"
        ) from exc

    try:
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": language, "model_name": model}],
            }
        )
        analyzer = AnalyzerEngine(
            nlp_engine=provider.create_engine(),
            supported_languages=[language],
            **kwargs,
        )
    except OSError as exc:  # pragma: no cover - depends on the local model set
        raise ConfigError(
            f"spaCy model {model!r} is not installed; run `python -m spacy download {model}`"
        ) from exc

    _ANALYZERS[(language, model)] = analyzer
    return analyzer


class PresidioDetector(BaseDetector):
    """Detector backed by the Microsoft Presidio analyzer.

    Args:
        language: ISO language code passed to Presidio and spaCy.
        model: spaCy model name; defaults to ``{language}_core_web_sm``.
        entities: Entity types to detect.  Empty means *every* recognizer
            Presidio has — the safer default for a privacy tool, since an
            unlisted type is an undetected one.
        score_threshold: Minimum confidence to keep.  Presidio scores NER hits
            well below 1.0, so 0.0 would flood the output with guesses.
        analyzer: A ready-made analyzer, for tests and advanced wiring.  When
            given, Presidio is never imported.
        **kwargs: Forwarded to ``AnalyzerEngine``.

    Raises:
        ConfigError: If Presidio or the spaCy model is missing.
    """

    name = "presidio"

    def __init__(
        self,
        language: str = "en",
        model: str = "",
        entities: list[str] | None = None,
        score_threshold: float = 0.35,
        analyzer: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self._language = language
        self._entities = list(entities) if entities else None
        self._score_threshold = float(score_threshold)
        self._analyzer = (
            analyzer
            if analyzer is not None
            else _load_analyzer(language, model or f"{language}_core_web_sm", kwargs)
        )

    def detect_sync(self, text: str, context: Context) -> Detection:
        results = self._analyzer.analyze(
            text=text,
            language=self._language,
            entities=self._entities,
            score_threshold=self._score_threshold,
        )
        detection = Detection()
        for r in results:
            entity_type = ENTITY_ALIASES.get(r.entity_type, r.entity_type)
            detection.add(r.start, r.end, entity_type, text[r.start : r.end])
        return detection

    async def detect(self, text: str, context: Context) -> Detection:
        """Run Presidio in a thread pool — its analyzer is synchronous."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.detect_sync, text, context)
