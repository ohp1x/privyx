"""The base of everything Privyx builds by name from config: :class:`Component`.

A kind, such as a vault, is a base class that declares itself once with
``class Vault(Component, kind="vault")``.  A component is a concrete subclass
with a ``name`` of its own, the ``type`` a config selects, and ``Options``
checked when it is built.  The rules a subclass breaks fail when the class is
created; the registry checks the rest when the component is added.
"""

from __future__ import annotations

import re
from abc import ABC
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # types only: core imports no other Privyx package at run time
    from privyx.anchors import Anchor
    from privyx.core._events import EventBus
    from privyx.token import FormatCodec


@dataclass(frozen=True, slots=True)
class Services:
    """What the app hands a component in :meth:`Component.open`: never a service locator."""

    #: The token codec of ``token.*``.
    codec: FormatCodec
    #: The anchor, or ``None`` when anchoring is off.
    anchor: Anchor | None
    #: The app's event bus, to emit notifications, which never carry values.
    events: EventBus


#: A type name: lowercase words joined by ``-`` or ``_``.
_NAME = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*")


class Component(ABC):
    """Something Privyx builds by name from config: a vault, a detector, an agent, ...

    Built directly, a component checks its options at once and works with its
    defaults: ``MemoryVault(ttl=60)``.  :meth:`open` gives it what the app
    shares, and :meth:`close` releases what it opened.
    """

    #: The kind, declared once by the kind's base class.
    kind: ClassVar[str]
    #: The type a config selects; a component sets its own, never inherits one.
    name: ClassVar[str]

    class Options(BaseModel):
        """A component's options: unknown keys are refused, values never shown in errors."""

        model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    def __init_subclass__(cls, *, kind: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in cls.__bases__:
            if base.__dict__.get("__final__", False):
                raise TypeError(f"{cls.__name__} cannot extend {base.__name__}: it is final")
        if kind is not None:
            owner = next((c for c in cls.__mro__[1:] if "kind" in c.__dict__), None)
            if owner is not None:
                raise TypeError(
                    f"{cls.__name__} cannot declare kind {kind!r}: "
                    f"{owner.__name__} already declares {owner.__dict__['kind']!r}"
                )
            cls.kind = kind
        name = cls.__dict__.get("name")
        if name is not None and not (isinstance(name, str) and _NAME.fullmatch(name)):
            raise TypeError(
                f"{cls.__name__}.name {name!r} must be lowercase words joined by - or _"
            )
        options = cls.__dict__.get("Options")
        if options is not None:
            for base in cls.__bases__:
                parent = getattr(base, "Options", None)
                if parent is not None and not (
                    isinstance(options, type) and issubclass(options, parent)
                ):
                    raise TypeError(f"{cls.__name__}.Options must subclass {base.__name__}.Options")

    def __init__(self, **options: Any) -> None:
        self.options = self.Options(**options)

    # Not abstract: a component that opens nothing leaves both out.
    async def open(self, services: Services) -> None:  # noqa: B027
        """Take what the app shares and open resources, such as connections or models.

        Optional: a component built directly works with its defaults, and
        ``open`` replaces them.
        """

    async def close(self) -> None:  # noqa: B027
        """Release what :meth:`open` opened; the app closes components in reverse order."""


#: The base of every component's options (:attr:`Component.Options`).
Options = Component.Options
