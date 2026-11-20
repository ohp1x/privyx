"""The app's own events: starting and stopping."""

from __future__ import annotations

from typing import ClassVar

from privyx.core import Interceptable, Notification


class AppStarting(Interceptable):
    """The components are open and the app is about to take requests.

    A listener that fails stops the app from starting.
    """

    name: ClassVar[str] = "app.starting"


class AppStopping(Notification):
    """The app has stopped taking requests and is about to close its components."""

    name: ClassVar[str] = "app.stopping"
