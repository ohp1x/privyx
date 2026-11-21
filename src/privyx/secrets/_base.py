"""The secret resolver kind: read the secret a reference names."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from privyx.core import Component

#: Schemes a resolver cannot take: a URL in a credential setting is a value.
RESERVED_SCHEMES = frozenset({"http", "https", "file", "ftp", "ws", "wss", "data", "mailto"})


class SecretResolver(Component, kind="secret"):
    """Read the secret a ``<name>://...`` reference names; ``name`` is the scheme.

    It is called while the config is read, before any component opens, so it
    reads the secret itself.  Its errors name the setting, never a value.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("name") in RESERVED_SCHEMES:
            raise TypeError(f"{cls.__name__}.name {cls.__dict__['name']!r} is a URL scheme")

    @abstractmethod
    def resolve(self, reference: str, path: str) -> str:
        """The secret ``reference`` names; ``path`` is the setting, for errors.

        Raises:
            ConfigError: If it cannot be read.
        """
