"""The policy kind: decide which detections to act on."""

from __future__ import annotations

from typing import Any

from privyx.core import Component, Context, Detection


class Policy(Component, kind="policy"):
    """Filter or enrich a detection before the operator replaces its spans.

    A policy overrides :meth:`decide_sync`, or :meth:`decide` for async work.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__ and not _overrides(cls, "decide", "decide_sync"):
            raise TypeError(f"{cls.__name__} must override decide_sync() or decide()")

    async def decide(self, detection: Detection, context: Context) -> Detection:
        """The spans of ``detection`` to replace."""
        return self.decide_sync(detection, context)

    def decide_sync(self, detection: Detection, context: Context) -> Detection:
        """:meth:`decide`, without awaiting anything."""
        raise NotImplementedError

    def reason(self) -> str:
        """Why this policy leaves a detected span unmasked, as ``privyx detect`` says it."""
        return f"the {self.name} policy left it out"


def _overrides(cls: type, *methods: str) -> bool:
    """Whether ``cls`` defines one of ``methods`` below its kind's base class."""
    return any(getattr(cls, method) is not getattr(Policy, method) for method in methods)
