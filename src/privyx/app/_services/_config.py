"""``app.config``: read, explain, change, check, and migrate the configuration (experimental).

``privyx config`` calls it.  A change is written to a file Privyx manages,
``settings.yaml`` in the scope's directory, which wins over the files written
by hand there.  Those are written only when named, with ``--file`` or to
``migrate --write``, and are kept as a backup first.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml  # type: ignore[import-untyped]

from privyx.core import ConfigError

if TYPE_CHECKING:
    from privyx.app._privyx import Privyx
    from privyx.config import Loaded

#: The layers below the files: what a key is set to when no file writes it.
_BUILT_IN = ("defaults", "plugin defaults", "run defaults")

#: The first line of a file ``privyx config set`` wrote, which needs no backup.
_MARK = "# Written by `privyx config set`"
_HEADER = _MARK + "; it wins over the config files beside it.\n"


@dataclass(frozen=True)
class Explained:
    """One setting: its value, where it comes from, and how else to set it."""

    key: str
    value: Any
    #: The layer whose value is used: ``defaults``, a file, ``environment``, ``CLI``.
    source: str
    #: The value with no config file and no environment, or ``None`` without one.
    default: Any
    #: The ``PRIVYX_<PATH>`` variable that sets it, if its names can be written in one.
    env: str | None
    #: The variables of earlier versions that set it too.
    aliases: tuple[str, ...]
    #: Every layer that writes it, lowest first, a file with the line, if known.
    written: tuple[str, ...]


@dataclass(frozen=True)
class Changed:
    """What ``set`` or ``unset`` wrote, and the other layers that write the key."""

    key: str
    file: Path
    #: The files below it that write the key too, which it wins over.
    below: tuple[str, ...]
    #: The layers above it that write the key, whose value is used instead.
    above: tuple[str, ...]
    #: Where a file written by hand was kept as it was, with its comments.
    backup: Path | None = None


@dataclass(frozen=True)
class Migrated:
    """A config file written the 0.3.0 way."""

    file: Path
    text: str
    #: Each key that was renamed, as the loader warns about it; none: nothing to do.
    renamed: list[str]
    #: Where the file was kept as it was, when it was rewritten.
    backup: Path | None = None
    #: The old operator keys left as written: moving them could change a profile or preset.
    left: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Routes:
    """The request paths the proxy masks, and what it does with the others."""

    mode: str
    upstream: str
    routes: dict[str, str]
    other: str


class Config:
    """The configuration of one app: its files, its settings, and changes to them."""

    def __init__(self, app: Privyx) -> None:
        self._app = app

    def files(self) -> list[Path]:
        """The config files read, low → high precedence, each after the files it includes."""
        from privyx.config import active_config_paths

        return active_config_paths(self._app._config)

    def show(self) -> Loaded:
        """The settings, read again, with the layer of each key.

        Raises:
            ConfigError: If they cannot be read.
        """
        return self._app._read(self._app._config).log_warnings()

    def get(self, key: str, *, masked: bool = False) -> Any:
        """The value of ``key``, such as ``server.port``; secret references as written.

        With ``masked``, as ``privyx config show`` prints it: the credentials
        and the values detectors look for are masked, whichever key names them.

        Raises:
            ConfigError: If the settings cannot be read, or have no ``key``.
        """
        from privyx.security import redact

        data = self._app._load(self._app._config, secrets=False).model_dump(mode="json")
        parts = split(key, data)
        return _lookup(redact(data) if masked else data, parts, key)

    def explain(self, key: str) -> Explained:
        """``key``'s value, masked, the layer it comes from, its default, and its variables.

        Raises:
            ConfigError: If the settings cannot be read, or have no ``key``.
        """
        from privyx.config import ALIASES, env_name, resolve
        from privyx.security import redact

        loaded = self._app._read(self._app._config, secrets=False).log_warnings()
        sources, written = loaded.sources, loaded.written
        data = loaded.settings.model_dump(mode="json")
        parts = split(key, data)
        value = _lookup(redact(data), parts, key)
        dotted = ".".join(parts)
        under = sorted({s for path, s in sources.items() if _within(path, dotted)})
        layers = [s for path, found in written.items() if _within(path, dotted) for s in found]
        defaults = self._app._load(defaults_only=True, secrets=False).model_dump(mode="json")
        try:
            default = _lookup(redact(defaults), parts, key)
        except ConfigError:
            default = None
        settable = resolve(parts, data, self._app._types(), existing=True)
        aliases = tuple(
            name
            for name, path in ALIASES.items()
            if path == dotted
            or (path == "detectors.*.cache" and parts[0] == "detectors" and parts[2:] == ["cache"])
        )
        return Explained(
            key=dotted,
            value=value,
            source=", ".join(under) if len(under) > 1 else sources.get(dotted, "defaults"),
            default=default,
            env=None if isinstance(settable, str) else env_name(parts),
            aliases=aliases,
            written=tuple(_where(s, parts) for s in dict.fromkeys(layers) if s not in _BUILT_IN),
        )

    def set(
        self,
        key: str,
        value: Any,
        *,
        scope: Literal["user", "project"] = "user",
        file: Path | None = None,
    ) -> Changed:
        """Write ``key`` with ``value`` to the managed file of ``scope``, or to ``file``.

        A value given as text is read by the type of the setting, as from the
        environment: ``8001`` for a port is a number, ``[a, b]`` for a list
        is a list.  The settings are read again with it before it is kept;
        it applies from the next start.

        Raises:
            ConfigError: If ``key`` is no setting, or the settings with the
                value are not valid; the file is left as it was.
        """
        from privyx.config import resolve, typed

        target = self._target(scope, file)
        data = self._app._load(self._app._config, secrets=False).model_dump(mode="json")
        parts = split(key, data)
        found = resolve(parts, data, self._app._types(), existing=False)
        if isinstance(found, str):
            raise ConfigError(found)
        if found.path[0] == "upstream_url":
            raise ConfigError(
                "'upstream_url' is where a profile's upstream goes; set provider.base_url"
            )
        if isinstance(value, str):
            value = typed(value, found.annotation, found.dotted)
        content = _read(target)
        path = list(found.path)
        chosen = content.get("provider")
        if path[0] == "provider" and len(path) > 1 and isinstance(chosen, str):
            path = ["providers", chosen, *path[1:]]  # the file picks it by name: keep the pick
        _put(content, path, value)
        return self._write(target, content, ".".join(path), "set")

    def unset(
        self,
        key: str,
        *,
        scope: Literal["user", "project"] = "user",
        file: Path | None = None,
    ) -> Changed:
        """Remove ``key`` from the managed file of ``scope``, or from ``file``.

        Raises:
            ConfigError: If the file does not set ``key``, or the settings
                without it are not valid; the file is left as it was.
        """
        target = self._target(scope, file)
        content = _read(target)
        parts = split(key, content)
        if not _drop(content, parts):
            raise ConfigError(f"{target} does not set {key!r}")
        return self._write(target, content, ".".join(parts), "unset")

    def validate(self, path: Path | None = None) -> list[str]:
        """Read the settings, of ``path`` alone when given; the warnings they give.

        Raises:
            ConfigError: If they are not valid.
        """
        return self._app._read(path or self._app._config, secrets=False).warnings

    def migrate(self, path: Path, *, write: bool = False) -> Migrated:
        """Config file ``path`` written the 0.3.0 way; with ``write``, also to the file.

        The file is kept first as ``<name>.bak``, or a numbered one when that
        exists, with its permissions.  Project files trusted before stay
        trusted.  Comments are not kept.  A file with nothing to rename is
        left as it is, as is an old operator key a profile or a preset would
        read differently once moved (``left``).

        Raises:
            ConfigError: If the file cannot be read or written.
        """
        from privyx.config import detectors_of, env_operator_types, migrate, read_yaml, trust

        found: list[str] = []
        left: list[str] = []
        written = read_yaml(path)
        below, above = self._around(path)
        others = [*below, *above, env_operator_types()]
        data = migrate(written, found, below=detectors_of(below), left=left, others=others)
        # `operator.type: hash` warns but has no new spelling: alone it renames nothing.
        renamed = list(dict.fromkeys(found)) if data != written else []
        if not renamed:
            # As it is, comments too: printed, it can stand in for the file.
            return Migrated(path, path.read_text(encoding="utf-8"), renamed, left=left)
        text = _dump(data) if data else ""
        if not write:
            return Migrated(path, text, renamed, left=left)
        trusted = _covering(path)
        real = path.resolve()
        try:
            mode = stat.S_IMODE(real.stat().st_mode)
            backup = _keep(real, real.read_bytes(), mode)
            _replace(real, text.encode("utf-8"), mode)
        except OSError as exc:
            raise ConfigError(f"cannot write {path}: {exc.strerror or exc}") from exc
        for file in trusted:
            trust(file)
        return Migrated(path, text, renamed, backup, left)

    def _around(self, path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """What the files read below ``path`` and above it hold, each lowest first.

        Those of the config in use when ``path`` is one of its files, else
        the files ``path`` itself includes, with none above.
        """
        from privyx.config import active_config_paths, read_yaml

        active = active_config_paths(self._app._config)
        names = [file.resolve() for file in active]
        real = path.resolve()
        if real not in names:
            return [read_yaml(file) for file in active_config_paths(path)[:-1]], []
        at = names.index(real)
        return [read_yaml(file) for file in active[:at]], [
            read_yaml(file) for file in active[at + 1 :]
        ]

    def schema(self) -> dict[str, Any]:
        """The config file's JSON Schema, with the types of the plugins the config loads.

        Without plugins, the published ``docs/schema.json``; also when the
        config cannot be read, since the schema is what helps to fix it.
        Secret references are not resolved.
        """
        from privyx.app._plugins import load_plugins
        from privyx.app._schema import json_schema
        from privyx.core import PrivyxError

        try:
            settings = self._app._load(self._app._config, secrets=False)
            plugins = settings.plugins
            if not (plugins.enabled and (plugins.paths or plugins.modules)):
                return json_schema()
            app = load_plugins(settings)
        except PrivyxError:
            return json_schema()
        return json_schema(app)

    def trust(self) -> list[Path]:
        """Trust the working directory's config files with their content now.

        Raises:
            ConfigError: If there is none, or the record cannot be written.
        """
        from privyx.config import find_local_configs, trust

        paths = find_local_configs()
        if not paths:
            raise ConfigError(
                "no privyx.yaml, .privyx/config.yaml, or .privyx/settings.yaml to trust here"
            )
        for path in paths:
            trust(path)
        return paths

    def routes(self) -> Routes:
        """The request paths masked, with the wire schema of each.

        Raises:
            ConfigError: If the settings cannot be read, a route names no
                dialect, or the gateway's provider type is unknown.
        """
        from privyx.app._serve import masked_routes
        from privyx.providers import resolve_base_url

        settings = self._app.settings
        proxy = settings.proxy
        table = masked_routes(self._app, settings)
        if proxy.mode == "gateway":
            other = "404"
        elif proxy.passthrough_unknown:
            other = "forwarded unchanged, without masking"
        else:
            other = "refused with 403 (proxy.passthrough_unknown is off)"
        return Routes(proxy.mode, resolve_base_url(settings), table, other)

    def _target(self, scope: str, file: Path | None) -> Path:
        """The file a change goes to.

        A project's file is written only if the user has vouched for it: a
        link it brings could point anywhere, and a file not trusted is not
        read, so a change to it would do nothing.

        Raises:
            ConfigError: If only a named file is read and ``file`` is not
                given, or a project's file is a link or is not trusted.
        """
        from privyx.config import discovery_disabled, is_trusted, managed_file

        if file is not None:
            target = file
        elif self._app._config is not None or os.environ.get("PRIVYX_CONFIG"):
            raise ConfigError(
                "with --config, only that file is read: name the file to write with --file"
            )
        elif discovery_disabled():
            raise ConfigError(
                "with PRIVYX_NO_DISCOVERY, no settings file is read: "
                "name the file to write with --file"
            )
        else:
            target = managed_file(scope)
        if _in_project(target):
            if target.is_symlink() or target.parent.is_symlink():
                raise ConfigError(f"{target} is a symbolic link; Privyx does not write through it")
            if target.exists() and not is_trusted(target):
                raise ConfigError(
                    f"{target} is not trusted; review it and run `privyx config trust` first"
                )
        return target

    def _write(
        self, target: Path, content: dict[str, Any], key: str, action: Literal["set", "unset"]
    ) -> Changed:
        """Write ``content`` to ``target`` if the settings are valid with it; else leave it.

        The file is written whole.  A managed or new file is owner-only, since
        it may hold a credential; one written by hand keeps its permissions
        and is kept as a backup first, since its comments are not.  The
        project files whose trust covers it stay trusted, and a new one of
        the project is, since the user just wrote it.  A file outside the
        config in use is checked alone, without loading the plugins it names.
        """
        from privyx.config import active_config_paths, read_config, trust
        from privyx.core import PrivyxError
        from privyx.events import ConfigChanged

        real = target.resolve()
        before = real.read_bytes() if real.exists() else None
        mode = stat.S_IMODE(real.stat().st_mode) if before is not None else 0o600
        managed = _managed(target)
        read = real in {p.resolve() for p in active_config_paths(self._app._config)}
        trusted = _covering(target)
        new_in_project = before is None and _in_project(target)
        written: dict[str, list[str]] = {}
        backup: Path | None = None

        def vouch() -> None:
            for file in trusted:
                if file.exists():
                    trust(file)

        try:
            real.parent.mkdir(parents=True, exist_ok=True)
            if before is not None and not before.startswith(_MARK.encode()):
                backup = _keep(real, before, mode)
            if content:
                header = _HEADER if managed else _MARK + ".\n"
                data = (header + _dump(content)).encode("utf-8")
                _replace(real, data, 0o600 if managed else mode)
            else:
                real.unlink(missing_ok=True)
            vouch()
            if new_in_project and content:
                trust(target)
            read = read or real in {p.resolve() for p in active_config_paths(self._app._config)}
            if read or not content:
                written = self._app._read(self._app._config, secrets=False).written
            else:
                written = read_config(target, types=self._app._registries).written
        except (PrivyxError, OSError) as exc:
            if before is None:
                real.unlink(missing_ok=True)
            else:
                _replace(real, before, mode)
            if backup is not None:
                backup.unlink(missing_ok=True)
            vouch()
            if isinstance(exc, OSError):
                raise ConfigError(f"cannot write {target}: {exc.strerror or exc}") from exc
            raise
        events = self._app._events
        if events.wants(ConfigChanged):
            events.notify(ConfigChanged(key=key, file=str(target), action=action))
        layers = [s for path, found in written.items() if _within(path, key) for s in found]
        layers = [s for s in dict.fromkeys(layers) if s not in _BUILT_IN]
        mine = str(target.resolve())
        parts = key.split(".")
        at = layers.index(mine) if mine in layers else len(layers)
        return Changed(
            key=key,
            file=target,
            below=tuple(_where(s, parts) for s in layers[:at]),
            above=tuple(_where(s, parts) for s in layers[at + 1 :]),
            backup=backup,
        )


def split(key: str, data: Any) -> list[str]:
    """``key``'s names: an entry whose name has a dot (``ollama:llama3.2``) is one name."""
    parts = key.split(".")
    out: list[str] = []
    i = 0
    while i < len(parts):
        if isinstance(data, dict):
            for j in range(len(parts), i, -1):
                name = ".".join(parts[i:j])
                if name in data:
                    out.append(name)
                    data, i = data[name], j
                    break
            else:
                out.append(parts[i])
                data, i = None, i + 1
        else:
            out.append(parts[i])
            data, i = None, i + 1
    return out


def _lookup(data: Any, parts: list[str], key: str) -> Any:
    for part in parts:
        if not isinstance(data, dict) or part not in data:
            raise ConfigError(f"no setting {key!r}")
        data = data[part]
    return data


def _within(path: str, key: str) -> bool:
    return path == key or path.startswith(key + ".")


def _where(source: str, parts: list[str]) -> str:
    """``source``, with the line that writes the key when it is a file."""
    from privyx.config import key_line

    path = Path(source)
    if not path.is_absolute():
        return source
    line = key_line(path, parts)
    return f"{source}:{line}" if line else source


def _read(target: Path) -> dict[str, Any]:
    from privyx.config import read_yaml

    return read_yaml(target) if target.exists() else {}


def _put(content: dict[str, Any], path: list[str], value: Any) -> None:
    for name in path[:-1]:
        nested = content.get(name)
        if not isinstance(nested, dict):
            # `directive: false` with an option set under it stays off.
            nested = content[name] = {"enabled": nested} if isinstance(nested, bool) else {}
        content = nested
    content[path[-1]] = value


def _drop(content: dict[str, Any], path: list[str]) -> bool:
    """Remove ``path`` from ``content``, and the mappings it leaves empty; whether it was there."""
    if not path or not isinstance(content, dict) or path[0] not in content:
        return False
    if len(path) == 1:
        del content[path[0]]
        return True
    nested = content[path[0]]
    if not _drop(nested, path[1:]):
        return False
    if isinstance(nested, dict) and not nested:
        del content[path[0]]
    return True


class _Dumper(yaml.SafeDumper):  # type: ignore[misc]
    """Writes a value used twice out twice: a file is read by people, without ``&id001``."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _dump(content: dict[str, Any]) -> str:
    text: str = yaml.dump(content, Dumper=_Dumper, sort_keys=False, allow_unicode=True)
    return text


def _home_settings() -> str | None:
    """The user's managed file, if the user has a home directory."""
    from privyx.config import managed_file

    with contextlib.suppress(ConfigError):
        return os.path.abspath(managed_file("user"))
    return None


def _managed(target: Path) -> bool:
    """Whether ``target`` is the managed file of the user or of the working directory."""
    from privyx.config import managed_file

    path = os.path.abspath(target)
    return path in {_home_settings(), os.path.abspath(managed_file("project"))}


def _in_project(target: Path) -> bool:
    """Whether ``target`` is one of the working directory's config files."""
    from privyx.config import find_local_configs, managed_file

    path = os.path.abspath(target)
    if path in {os.path.abspath(p) for p in find_local_configs()}:
        return True
    # In the home directory, the managed file is the user's.
    return path == os.path.abspath(managed_file("project")) and path != _home_settings()


def _covering(target: Path) -> list[Path]:
    """The working directory's trusted config files whose trust covers ``target``.

    ``target`` itself, and the files that include it: a change to it would
    otherwise end their trust too.
    """
    from privyx.config import find_local_configs, included_files, is_trusted, read_yaml

    real = target.resolve()
    found: list[Path] = []
    for file in find_local_configs():
        if not is_trusted(file):
            continue
        try:
            included = included_files(file, read_yaml(file))
        except ConfigError:
            included = []  # a broken file is reported when it is loaded
        if real in {file.resolve(), *(p.resolve() for p in included)}:
            found.append(file)
    return found


def _replace(path: Path, data: bytes, mode: int) -> None:
    """Write ``path`` whole or not at all, with ``mode``."""
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def _keep(path: Path, data: bytes, mode: int) -> Path:
    """Save ``data``, the content of ``path``, in a new backup beside it, with ``mode``."""
    backup, n = path.with_name(path.name + ".bak"), 0
    while True:
        try:
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            break
        except FileExistsError:
            n += 1
            backup = path.with_name(f"{path.name}.bak.{n}")
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
    return backup
