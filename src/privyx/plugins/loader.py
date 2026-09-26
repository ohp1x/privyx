"""Plugin loader — discover local plugins and register them.

A Privyx plugin is an ordinary Python module placed in a directory (or a single
``.py`` file) listed under ``plugins.paths`` in the config.  Discovery is
**local and opt-in**: nothing is loaded unless a path is configured, and there
is no third-party entry-point mechanism.

A plugin module defines one or more concrete subclasses of the Privyx base
classes (:class:`~privyx.privacy.detector.base.BaseDetector`,
:class:`~privyx.privacy.operator.base.BaseOperator`,
:class:`~privyx.privacy.policy.base.BasePolicy`,
:class:`~privyx.providers.base.BaseProvider`,
:class:`~privyx.privacy.anchor.base.BaseAnchor`,
:class:`~privyx.vault.base.BaseVault`).  The loader imports each module, finds
those subclasses, and registers each under its ``name`` into
:data:`~privyx.plugins.registry.PLUGINS`.  The matching ``build_*`` function
then resolves a config ``type`` to the plugin when it is not a built-in.

Contract for a plugin class:
    - a class-level ``name`` — the value a config ``type`` selects (falls back
      to the class name with the family suffix stripped, if unset);
    - optional ``@classmethod from_config(cls, config: dict) -> Self`` for
      config-driven construction; otherwise a no-argument constructor is used.

Contract for lifecycle:
    - module-level ``on_startup()`` / ``on_shutdown()`` (sync or async) are
      collected into the returned :class:`~privyx.plugins.hooks.HookManager`.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.core.errors import ConfigError
from privyx.plugins.hooks import HookManager
from privyx.plugins.registry import PLUGINS
from privyx.privacy.anchor.base import BaseAnchor
from privyx.privacy.detector.base import BaseDetector
from privyx.privacy.operator.base import BaseOperator
from privyx.privacy.policy.base import BasePolicy
from privyx.providers.base import BaseProvider
from privyx.vault.base import BaseVault

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from privyx.config.schema import Settings

#: (base class, PluginRegistry attribute, family suffix stripped from a class
#: name when the class does not set an explicit ``name``).
_BASES: list[tuple[type, str, str]] = [
    (BaseDetector, "detectors", "detector"),
    (BaseOperator, "operators", "operator"),
    (BasePolicy, "policies", "policy"),
    (BaseProvider, "providers", "provider"),
    (BaseAnchor, "anchors", "anchor"),
    (BaseVault, "vaults", "vault"),
]


def load_plugins(settings: Settings) -> HookManager:
    """Discover and register every plugin under ``settings.plugins.paths``.

    Repopulates :data:`PLUGINS` from scratch each call, so a second call in the
    same process (e.g. ``privyx doctor`` then a serve) never sees stale
    registrations.  Returns a :class:`HookManager` carrying any lifecycle hooks
    the plugins defined; the caller fires it around the serving lifecycle.

    Raises:
        ConfigError: If a configured path does not exist, a module fails to
            import, or two plugins in the same family claim the same ``name``.
    """
    PLUGINS.clear()
    hooks = HookManager()

    plugins = settings.plugins
    if not plugins.enabled or not plugins.paths:
        return hooks

    for raw in plugins.paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise ConfigError(f"plugin path does not exist: {raw}")
        for module in _import_modules(path):
            _register_module(module, hooks)
    return hooks


def _import_modules(path: Path) -> list[ModuleType]:
    """Import every plugin module at ``path`` — a single file or a directory tree."""
    if path.is_file():
        files = [path]
    else:
        files = sorted(p for p in path.rglob("*.py") if not p.name.startswith("_"))
    return [_import_file(file) for file in files]


def _import_file(file: Path) -> ModuleType:
    """Import ``file`` under a synthetic, collision-free module name.

    The name is derived from the absolute path so plugins never land on
    ``sys.path`` or clash in ``sys.modules`` with each other or real packages.
    """
    mod_name = "privyx._plugins." + re.sub(r"\W", "_", str(file.resolve()))
    spec = importlib.util.spec_from_file_location(mod_name, file)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot load plugin module: {file}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ConfigError(f"failed to import plugin {file}: {exc}") from exc
    return module


def _register_module(module: ModuleType, hooks: HookManager) -> None:
    """Register every plugin component found in ``module`` and collect its hooks."""
    for attr, name, cls in _discover(module):
        registry = getattr(PLUGINS, attr)
        if name in registry:
            raise ConfigError(
                f"duplicate plugin name {name!r} in family {attr!r} (from {module.__name__})"
            )
        registry.register(name, _make_factory(cls))

    for attr, add in (("on_startup", hooks.add_startup), ("on_shutdown", hooks.add_shutdown)):
        hook = getattr(module, attr, None)
        if callable(hook):
            add(module.__name__, hook)


def _discover(module: ModuleType) -> list[tuple[str, str, type]]:
    """Find concrete Privyx-component subclasses *defined* in ``module``."""
    found: list[tuple[str, str, type]] = []
    for obj in vars(module).values():
        if not inspect.isclass(obj) or inspect.isabstract(obj):
            continue
        # Only classes defined in this module — not the base classes or other
        # components a plugin happens to import.
        if obj.__module__ != module.__name__:
            continue
        for base, attr, suffix in _BASES:
            if issubclass(obj, base) and obj is not base:
                found.append((attr, _resolve_name(obj, suffix), obj))
                break
    return found


def _resolve_name(cls: type, suffix: str) -> str:
    """The registry key for ``cls``: its explicit ``name`` or a derived one."""
    own = cls.__dict__.get("name")
    if isinstance(own, str) and own:
        return own
    derived = cls.__name__.lower().removesuffix(suffix)
    return derived or cls.__name__.lower()


def _make_factory(cls: type) -> Callable[[dict[str, Any]], Any]:
    """Wrap ``cls`` in a factory honouring the optional ``from_config`` contract."""

    def factory(config: dict[str, Any]) -> Any:
        from_config = getattr(cls, "from_config", None)
        if callable(from_config):
            return from_config(config)
        return cls()

    return factory
