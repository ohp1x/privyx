"""Plugin loader: import the plugins a config names, and set each up.

Components come from three places, all opt-in:

- ``plugins.paths``: directories or ``.py`` files.  Each module is imported
  under a synthetic name, and its ``setup(app)`` called.
- ``plugins.modules``: importable modules, such as an installed package.  Each
  is imported and its ``setup(app)`` called.
- :meth:`Privyx.add <privyx.app.Privyx.add>` in the user's own code.

A ``setup(app)`` adds components, routes, and listeners to the
:class:`~privyx.app.Privyx` it is given.  There is no entry-point discovery: a
package installed in the environment, a transitive dependency among them,
runs in the process that sees raw values only when a config names it.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.core import ConfigError, PrivyxError

if TYPE_CHECKING:
    from types import ModuleType

    from privyx.app import Privyx
    from privyx.config.schema import PluginsConfig, Settings


def load_plugins(settings: Settings, app: Privyx | None = None) -> Privyx:
    """Set up every plugin the config names, in ``app`` or a new :class:`~privyx.app.Privyx`.

    Each file and module is set up once per app, so loading again, as a
    second build does, adds nothing.

    Returns:
        The app the plugins were set up in.

    Raises:
        ConfigError: If a configured path does not exist, a plugin fails to
            import, has no ``setup``, or its ``setup`` fails, or two
            components of one kind take the same name.
    """
    from privyx.app._privyx import Privyx

    px = app if app is not None else Privyx()
    set_up_plugins(settings.plugins, px)
    return px


def set_up_plugins(plugins: PluginsConfig, px: Privyx) -> None:
    """Set up the plugins of a ``plugins`` section in ``px``, each once.

    Raises:
        ConfigError: As :func:`load_plugins`.
    """
    if plugins.enabled:
        for raw in plugins.paths:
            path = Path(raw).expanduser()
            if not path.exists():
                raise ConfigError(f"plugin path does not exist: {raw}")
            for file in _files(path):
                resolved = file.resolve()
                if resolved not in px._files:
                    _set_up(_import_file(file), f"plugin {file}", px)
                    px._files.add(resolved)
        for name in plugins.modules:
            px.use(name)


def set_up_module(name: str, px: Privyx) -> None:
    """Import module ``name`` and call its ``setup(app)``.

    Raises:
        ConfigError: If it cannot be imported, has no ``setup``, or its
            ``setup`` fails.
    """
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        raise ConfigError(f"failed to import plugin module {name!r}: {exc}") from exc
    _set_up(module, f"plugin module {name!r}", px)


def _set_up(module: ModuleType, what: str, px: Privyx) -> None:
    """Call ``module``'s ``setup(app)`` with ``px``; ``what`` names it in errors."""
    setup: Any = getattr(module, "setup", None)
    if not callable(setup):
        raise ConfigError(f"{what} has no setup(app) function")
    px._setting_up, before = what, px._setting_up
    try:
        setup(px)
    except PrivyxError:
        raise
    except Exception as exc:
        raise ConfigError(f"setup of {what} failed: {exc}") from exc
    finally:
        px._setting_up = before


def _files(path: Path) -> list[Path]:
    """The plugin files at ``path``: the file itself, or a directory tree's ``.py`` files."""
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*.py") if not p.name.startswith("_"))


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
