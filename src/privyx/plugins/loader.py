"""Plugin loader — find plugin components and register them.

Components come from three places, all opt-in:

- ``plugins.paths``: directories or ``.py`` files.  Each module is imported
  under a synthetic name and every concrete subclass of a Privyx base class
  *defined* in it is registered.
- ``plugins.modules``: importable modules, such as an installed package.  Each
  is imported and its ``setup(px)`` called with a :class:`~privyx.app.Privyx`,
  which it adds components, routes, and hooks to.
- :meth:`Privyx.add <privyx.app.Privyx.add>` in the user's own code.

There is no entry-point discovery: a package installed in the environment,
a transitive dependency among them, runs in the process that sees raw values
only when a config names it.

Contract for a component class:
    - a class-level ``name``: the value a config ``type`` selects (falls back
      to the class name with the family suffix stripped, if unset);
    - optionally a ``Config``, a :class:`~privyx.plugins.ComponentConfig`
      with the options its section takes, and then
      ``@classmethod from_config(cls, config: Config, ctx: BuildContext)``;
    - without ``Config``, optionally ``@classmethod from_config(cls, config:
      dict) -> Self``; otherwise a no-argument constructor is used.

Contract for lifecycle:
    - module-level ``on_startup()`` / ``on_shutdown()`` (sync or async) in a
      ``plugins.paths`` module, or ``px.on_startup`` / ``px.on_shutdown``, are
      collected into the returned :class:`~privyx.plugins.hooks.HookManager`.
"""

from __future__ import annotations

import difflib
import importlib
import importlib.util
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from privyx.config.schema import (
    AnchorConfig,
    DetectorConfig,
    OperatorConfig,
    PolicyConfig,
    ProviderConfig,
    VaultConfig,
)
from privyx.core.errors import ConfigError, PrivyxError
from privyx.plugins.hooks import HookManager
from privyx.plugins.registry import (
    ANCHOR_KEY,
    CODEC_KEY,
    ID_LENGTH_KEY,
    PLUGINS,
    SET_KEY,
    BuildContext,
    ComponentConfig,
)
from privyx.privacy.anchor.base import BaseAnchor
from privyx.privacy.detector.base import BaseDetector
from privyx.privacy.operator.base import BaseOperator
from privyx.privacy.policy.base import BasePolicy
from privyx.providers.base import BaseProvider
from privyx.vault.base import BaseVault

if TYPE_CHECKING:
    from types import ModuleType

    from privyx.app import Privyx
    from privyx.config.schema import Settings

#: (base class, PluginRegistry attribute, config section, the section's model).
#: The section name is also the suffix stripped from a class name when the
#: class does not set an explicit ``name``.
_BASES: list[tuple[type, str, str, type[BaseModel]]] = [
    (BaseDetector, "detectors", "detector", DetectorConfig),
    (BaseOperator, "operators", "operator", OperatorConfig),
    (BasePolicy, "policies", "policy", PolicyConfig),
    (BaseProvider, "providers", "provider", ProviderConfig),
    (BaseAnchor, "anchors", "anchor", AnchorConfig),
    (BaseVault, "vaults", "vault", VaultConfig),
]

_LIVE_KEYS = frozenset({ANCHOR_KEY, CODEC_KEY, ID_LENGTH_KEY})


def load_plugins(settings: Settings, app: Privyx | None = None) -> HookManager:
    """Register every plugin component the config and ``app`` name.

    Repopulates :data:`PLUGINS` from scratch each call, so a second call in the
    same process (e.g. ``privyx doctor`` then a serve) never sees stale
    registrations.  ``plugins.modules`` get ``app`` in their ``setup``, or a
    new :class:`~privyx.app.Privyx` when there is none; each module is set up
    once per ``app``.  Returns a :class:`HookManager` with every lifecycle hook
    found; the caller fires it around the serving lifecycle.

    Raises:
        ConfigError: If a configured path does not exist, a module fails to
            import or has no ``setup``, or two plugins in the same family claim
            the same ``name``.
    """
    from privyx.app import Privyx

    PLUGINS.clear()
    hooks = HookManager()
    px = app if app is not None else Privyx()

    plugins = settings.plugins
    if plugins.enabled:
        for raw in plugins.paths:
            path = Path(raw).expanduser()
            if not path.exists():
                raise ConfigError(f"plugin path does not exist: {raw}")
            for module in _import_modules(path):
                _register_module(module, hooks)
        for name in plugins.modules:
            px._set_up(name)
    for cls in px._components:
        register(cls, f"{type(px).__name__}.add")
    hooks.extend(px._hooks)
    return hooks


def loaded_options() -> dict[str, dict[str, type[BaseModel] | None]]:
    """Each section with registered plugins: their names, each to its ``Config`` or ``None``."""
    loaded: dict[str, dict[str, type[BaseModel] | None]] = {}
    for _, attr, section, _ in _BASES:
        registry = getattr(PLUGINS, attr)
        for name in registry.names():
            options = getattr(registry.factory(name), "options", None)
            loaded.setdefault(section, {})[name] = options
    return loaded


def family(cls: object) -> tuple[str, str, type[BaseModel]]:
    """``(registry attribute, section, section model)`` of a component class.

    Raises:
        ConfigError: If ``cls`` is not a concrete subclass of a base class.
    """
    if inspect.isclass(cls) and not inspect.isabstract(cls):
        for base, attr, section, model in _BASES:
            if issubclass(cls, base) and cls is not base:
                return attr, section, model
    names = ", ".join(base.__name__ for base, *_ in _BASES)
    raise ConfigError(f"{cls!r} is not a concrete subclass of one of {names}")


def register(cls: type, source: str) -> None:
    """Register component ``cls`` under its name, in its family.

    Raises:
        ConfigError: If its family already has a plugin of that name, or it
            has a ``Config`` but no ``from_config``.
    """
    attr, section, model = family(cls)
    name = _resolve_name(cls, section)
    registry = getattr(PLUGINS, attr)
    if name in registry:
        raise ConfigError(f"duplicate plugin name {name!r} in family {attr!r} (from {source})")
    factory = _Factory(cls, section, model)
    if factory.options is not None and not callable(getattr(cls, "from_config", None)):
        raise ConfigError(f"plugin {name!r} has a Config but no from_config(config, ctx)")
    registry.register(name, factory)


def set_up_module(name: str, px: Privyx) -> None:
    """Import module ``name`` and call its ``setup(px)``.

    Raises:
        ConfigError: If it cannot be imported, has no ``setup``, or its
            ``setup`` fails.
    """
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        raise ConfigError(f"failed to import plugin module {name!r}: {exc}") from exc
    setup = getattr(module, "setup", None)
    if not callable(setup):
        raise ConfigError(f"plugin module {name!r} has no setup(px) function")
    try:
        setup(px)
    except PrivyxError:
        raise
    except Exception as exc:
        raise ConfigError(f"setup of plugin module {name!r} failed: {exc}") from exc


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
    for cls in _discover(module):
        register(cls, module.__name__)

    for attr, add in (("on_startup", hooks.add_startup), ("on_shutdown", hooks.add_shutdown)):
        hook = getattr(module, attr, None)
        if callable(hook):
            add(module.__name__, hook)


def _discover(module: ModuleType) -> list[type]:
    """Find concrete Privyx-component subclasses *defined* in ``module``."""
    found: list[type] = []
    for obj in vars(module).values():
        if not inspect.isclass(obj) or inspect.isabstract(obj):
            continue
        # Only classes defined in this module — not the base classes or other
        # components a plugin happens to import.
        if obj.__module__ != module.__name__:
            continue
        if any(issubclass(obj, base) and obj is not base for base, *_ in _BASES):
            found.append(obj)
    return found


def _resolve_name(cls: type, suffix: str) -> str:
    """The registry key for ``cls``: its explicit ``name`` or a derived one."""
    own = cls.__dict__.get("name")
    if isinstance(own, str) and own:
        return own
    derived = cls.__name__.lower().removesuffix(suffix)
    return derived or cls.__name__.lower()


@dataclass(frozen=True, slots=True)
class _Factory:
    """Builds ``cls`` from its section, honouring the ``from_config`` contracts."""

    cls: type
    section: str
    model: type[BaseModel]

    @property
    def options(self) -> type[ComponentConfig] | None:
        """The component's ``Config``, if it has one."""
        options = getattr(self.cls, "Config", None)
        if isinstance(options, type) and issubclass(options, ComponentConfig):
            return options
        return None

    def __call__(self, config: dict[str, Any]) -> Any:
        from_config = getattr(self.cls, "from_config", None)
        options = self.options
        explicit = config.get(SET_KEY)
        config = {key: value for key, value in config.items() if key != SET_KEY}
        if options is None:
            return from_config(config) if callable(from_config) else self.cls()
        # Its own options, and the section's fields its Config declares and
        # the config sets: not a default the section has for the built-in
        # types (vault.dsn, say), nor one Privyx left unset (no provider key).
        # The Config's own default applies then.
        fields = self.model.model_fields
        given = {
            key: value
            for key, value in config.items()
            if key != "type"
            and key not in _LIVE_KEYS
            and (
                key not in fields
                or (
                    key in options.model_fields
                    and value is not None
                    and (explicit is None or key in explicit)
                )
            )
        }
        ctx = BuildContext(
            codec=config.get(CODEC_KEY),
            anchor=config.get(ANCHOR_KEY),
            id_length=config.get(ID_LENGTH_KEY),
        )
        assert callable(from_config)  # checked when registered
        return from_config(self._validate(options, given, config.get("type")), ctx)

    def _validate(
        self, options: type[ComponentConfig], given: dict[str, Any], kind: object
    ) -> ComponentConfig:
        """``given`` as ``options``; an error names each key, never a value."""
        try:
            return options.model_validate(given)
        except ValidationError as exc:
            problems = []
            for error in exc.errors():
                path = ".".join(str(part) for part in (self.section, *error["loc"]))
                if error["type"] == "extra_forbidden":
                    key = str(error["loc"][-1])
                    match = difflib.get_close_matches(key, list(options.model_fields), n=1)
                    hint = f" (did you mean {match[0]!r}?)" if match else ""
                    problems.append(f"unknown setting {path!r}{hint}")
                else:
                    problems.append(f"invalid setting {path!r}: {error['msg']}")
            raise ConfigError(f"{', '.join(problems)} for {self.section} type {kind!r}") from None
