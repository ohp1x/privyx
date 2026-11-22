"""The configuration's own event: a setting written to a file Privyx manages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from privyx.core import Notification


@dataclass(frozen=True, eq=False)
class ConfigChanged(Notification):
    """``privyx config set`` or ``unset`` wrote ``key`` to ``file``; never the value.

    It applies from the next start: a running server keeps the settings it
    started with.
    """

    name: ClassVar[str] = "config.changed"
    key: str
    file: str
    action: Literal["set", "unset"]
