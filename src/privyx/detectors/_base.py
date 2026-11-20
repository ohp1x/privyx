"""The detector kind: find the spans of a text to protect."""

from __future__ import annotations

from typing import Any

from privyx.config.schema import SharedDetectorOptions
from privyx.core import Component, Context, Detection


class Detector(Component, kind="detector"):
    """Find the sensitive spans of a text.

    A detector overrides :meth:`detect_sync`, or :meth:`detect` for async
    work.  ``cache`` and ``underscore_separates_words``, which every detector
    takes, are applied by Privyx: the first wraps the detector, the second
    also changes the leak check.  A pattern detector follows the second too.
    """

    class Options(Component.Options, SharedDetectorOptions):
        """``cache`` and ``underscore_separates_words``, which every detector takes."""

    options: Options

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__ and not _overrides(cls, "detect", "detect_sync"):
            raise TypeError(f"{cls.__name__} must override detect_sync() or detect()")

    async def detect(self, text: str, context: Context) -> Detection:
        """The spans of ``text`` to protect."""
        return self.detect_sync(text, context)

    def detect_sync(self, text: str, context: Context) -> Detection:
        """:meth:`detect`, without awaiting anything."""
        raise NotImplementedError


def _overrides(cls: type, *methods: str) -> bool:
    """Whether ``cls`` defines one of ``methods`` below its kind's base class."""
    return any(getattr(cls, method) is not getattr(Detector, method) for method in methods)
