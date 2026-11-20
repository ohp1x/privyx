"""Detectors: find the spans of a text to protect."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute
from privyx.detectors._base import Detector

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.detectors._llm import LLMDetector
    from privyx.detectors._presidio import PresidioDetector
    from privyx.detectors._regex import RegexDetector, YamlDetector

__all__ = ["Detector", "LLMDetector", "PresidioDetector", "RegexDetector", "YamlDetector"]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "regex": "privyx.detectors._regex:RegexDetector",
    "yaml": "privyx.detectors._regex:YamlDetector",
    "presidio": "privyx.detectors._presidio:PresidioDetector",
    "llm": "privyx.detectors._llm:LLMDetector",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.detectors import RegexDetector`` imports it on first use."""
    return lazy_attribute(name, _BUILTINS, __name__)


def setup(app: Privyx) -> None:
    """Register the built-in detectors with ``app``."""
    app.registry(Detector).builtins(_BUILTINS)
