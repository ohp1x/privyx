"""The components of one kind, by type name: :class:`Registry`."""

from __future__ import annotations

import difflib
import importlib
import inspect
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from privyx.core._component import Component, Options
from privyx.core._errors import ConfigError


class Registry[C: Component]:
    """The components of one kind an app builds, by type name.

    Built-ins are registered by import path and imported only when chosen;
    their names are reserved.  A plugin is registered by its class.  Every
    error names the setting, the type, or the class, never a value.

    Args:
        base: The kind's base class, such as ``Vault``.
    """

    def __init__(self, base: type[C]) -> None:
        self.base = base
        self.kind = base.kind
        self._builtins: dict[str, str] = {}
        self._classes: dict[str, type[C]] = {}

    def builtins(self, paths: Mapping[str, str]) -> None:
        """Register built-ins as ``{name: "module:Class"}``; each is imported on first use."""
        self._builtins.update(paths)

    def add(self, cls: type[C]) -> type[C]:
        """Register component ``cls`` under its own ``name``; returns ``cls``.

        Raises:
            ConfigError: If ``cls`` is not a component of this kind, is
                abstract, has no name of its own, takes a built-in's name, or
                another component has its name.
        """
        if not (inspect.isclass(cls) and issubclass(cls, self.base)):
            raise ConfigError(f"{cls!r} is not a {self.kind} component")
        if inspect.isabstract(cls):
            raise ConfigError(f"{cls.__name__} is abstract; it cannot be a {self.kind} type")
        name = cls.__dict__.get("name")
        if name is None:
            owner = next((c for c in cls.__mro__ if "name" in c.__dict__), None)
            inherited = f"; {owner.__dict__['name']!r} is {owner.__name__}'s" if owner else ""
            raise ConfigError(f"{cls.__name__} must set its own name{inherited}")
        if name in self._builtins:
            raise ConfigError(
                f"{self.kind} type {name!r} is built in; give {cls.__name__} a name of its own"
            )
        if name in self._classes:
            raise ConfigError(
                f"two {self.kind} plugins are named {name!r}: "
                f"{_where(self._classes[name])} and {_where(cls)}"
            )
        self._classes[name] = cls
        return cls

    def get(self, name: str) -> type[C]:
        """The class of type ``name``, importing a built-in on first use.

        Raises:
            ConfigError: If no component has that name.
        """
        cls = self._classes.get(name)
        if cls is not None:
            return cls
        path = self._builtins.get(name)
        if path is None:
            match = difflib.get_close_matches(name, self.names(), n=1)
            hint = f" (did you mean {match[0]!r}?)" if match else ""
            raise ConfigError(f"unknown {self.kind} type {name!r}{hint}")
        loaded: type[C] = imported(path)
        self._classes[name] = loaded
        return loaded

    def names(self) -> list[str]:
        """Every registered type name, sorted."""
        return sorted(self._builtins.keys() | self._classes.keys())

    def extends(self, name: str) -> str | None:
        """The registered type that type ``name`` extends, if any, read from its classes."""
        for parent in self.get(name).__mro__[1:]:
            own = parent.__dict__.get("name")
            if isinstance(own, str) and own in self:
                return own
        return None

    def builtin_names(self) -> list[str]:
        """The built-in type names, in the order they were registered."""
        return list(self._builtins)

    def is_builtin(self, name: object) -> bool:
        """Whether ``name`` is a built-in type, whose name a plugin cannot take."""
        return name in self._builtins

    def validate(
        self,
        name: str,
        options: Mapping[str, Any],
        *,
        setting: str | None = None,
        origin: Callable[[str], str] | None = None,
    ) -> Options:
        """``options`` checked as type ``name``'s, with its defaults filled in.

        Args:
            name: The type.
            options: The options its section sets.
            setting: Where the section is in the config, for errors; the kind
                by default (``vault``).
            origin: What an error writes after an invalid setting, given its
                path: where its value was given, such as `` (config.yaml:3)``.

        Raises:
            ConfigError: If the type is unknown, or an option is unknown or
                invalid.  Each error names the setting and the type, never a
                value.
        """
        cls = self.get(name)
        try:
            return cls.Options.model_validate(dict(options))
        except ValidationError as exc:
            problems = []
            for error in exc.errors():
                path = ".".join(str(part) for part in (setting or self.kind, *error["loc"]))
                if error["type"] == "extra_forbidden":
                    key = str(error["loc"][-1])
                    match = difflib.get_close_matches(key, list(cls.Options.model_fields), n=1)
                    hint = f" (did you mean {match[0]!r}?)" if match else ""
                    problems.append(f"unknown setting {path!r}{hint}")
                else:
                    given = origin(path) if origin is not None else ""
                    problems.append(f"invalid setting {path!r}{given}: {error['msg']}")
            raise ConfigError(f"{', '.join(problems)} for {self.kind} type {name!r}") from None

    def build(self, name: str, options: Mapping[str, Any], *, setting: str | None = None) -> C:
        """An instance of type ``name`` with ``options`` from a config.

        Raises:
            ConfigError: As :meth:`validate`.
        """
        self.validate(name, options, setting=setting)
        return self.get(name)(**options)

    def __contains__(self, name: object) -> bool:
        return name in self._builtins or name in self._classes


def imported(path: str) -> Any:
    """The object at ``path``, ``"module:name"``, importing its module."""
    module, _, attr = path.partition(":")
    return getattr(importlib.import_module(module), attr)


def lazy_attribute(name: str, paths: Mapping[str, str], package: str) -> Any:
    """A package's attribute ``name`` from the ``"module:name"`` paths it lists (PEP 562).

    Raises:
        AttributeError: If no path names ``name``.
    """
    for path in paths.values():
        if path.rpartition(":")[2] == name:
            return imported(path)
    raise AttributeError(f"module {package!r} has no attribute {name!r}")


def _where(cls: type) -> str:
    """Where ``cls`` comes from: its module and name."""
    return f"{cls.__module__}.{cls.__qualname__}"
