"""The feature kind: optional behavior that listens to events, switched on and off."""

from __future__ import annotations

from typing import ClassVar

from privyx.core import Component


class Feature(Component, kind="feature"):
    """Optional behavior that listens to events: the unit that is switched on and off.

    Its methods marked :func:`~privyx.core.listen` are added to the app's event
    bus when it is switched on, and removed when off.  A listener of an
    interceptable event that fails refuses the exchange; one of a notification
    is logged.
    """

    #: Whether the feature is on without a setting that switches it on.
    enabled_by_default: ClassVar[bool] = False
    #: Whether it keeps state in its instance, which each server process
    #: would have its own of: ``server.workers`` above 1 refuses it.
    single_process: ClassVar[bool] = False
