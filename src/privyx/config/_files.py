"""The config files read: which ones, whether a local one is trusted, and reading one.

``~/.privyx/config.yaml``, then the working directory's ``privyx.yaml`` and
``.privyx/config.yaml`` once ``privyx trust`` recorded them, or the one file
named with ``--config``.  The files a config file names in ``include`` come
just below it.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from privyx.config.env import discovery_disabled
from privyx.core import ConfigError

_log = logging.getLogger("privyx")


def _home_file(name: str) -> Path | None:
    """``~/.privyx/<name>``, or ``None`` for a user without a home directory.

    Never a path left relative: a project could then supply the file itself.
    """
    try:
        return Path.home() / ".privyx" / name
    except RuntimeError:
        return None


def find_local_configs() -> list[Path]:
    """The working directory's own config files, low → high precedence.

    ``privyx.yaml``, then ``.privyx/config.yaml``.  In the home directory the
    second is ``~/.privyx/config.yaml`` itself, and is left out.
    """
    cwd = Path.cwd()
    home_config = _home_file("config.yaml")
    return [
        path
        for path in (cwd / "privyx.yaml", cwd / ".privyx" / "config.yaml")
        if path.is_file() and not (home_config and path.resolve() == home_config.resolve())
    ]


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror or exc}") from exc


def _fingerprint(path: Path) -> str:
    """SHA-256 of ``path`` and of every file it includes, so trust covers them all.

    A file with no ``include`` hashes to the SHA-256 of its content alone.
    """
    digest = hashlib.sha256(_read_bytes(path))
    try:
        # A key given twice must not drop the includes: the load reports it.
        included = included_files(path, read_yaml(path, loader=yaml.SafeLoader))
    except ConfigError:
        included = []  # a broken file is reported when it is loaded
    for file in included:
        digest.update(b"\0" + str(file).encode() + b"\0" + _read_bytes(file))
    return digest.hexdigest()


def _trusted() -> dict[str, str]:
    """The files ``privyx trust`` recorded: resolved path → SHA-256 of the content."""
    store = _home_file("trusted.json")
    try:
        trusted = json.loads(store.read_text(encoding="utf-8")) if store else {}
    except (OSError, ValueError):
        return {}  # nothing trusted yet, or a record that cannot be read
    return trusted if isinstance(trusted, dict) else {}


def is_trusted(path: Path) -> bool:
    """Whether ``path`` was trusted with the content it has now."""
    return _trusted().get(str(path.resolve())) == _fingerprint(path)


def trust(path: Path) -> None:
    """Record ``path``, with the content it has now, as trusted.

    A config file found in the working directory can load plugins and choose
    the upstream, so one that came with a project is read only on the user's
    word, given again after each change to it.
    """
    store = _home_file("trusted.json")
    if store is None:
        raise ConfigError("cannot trust a config file: no home directory to record it in")
    trusted = {**_trusted(), str(path.resolve()): _fingerprint(path)}
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(trusted, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot write {store}: {exc.strerror or exc}") from exc


def _config_paths(path: str | Path | None) -> tuple[list[Path], list[Path]]:
    """The config files to read, low → high precedence, and the local ones not trusted."""
    named = path or os.environ.get("PRIVYX_CONFIG")
    if named:
        return [Path(named)], []
    if discovery_disabled():
        return [], []
    home_config = _home_file("config.yaml")
    active = [home_config] if home_config and home_config.is_file() else []
    untrusted: list[Path] = []
    for local in find_local_configs():
        (active if is_trusted(local) else untrusted).append(local)
    return active, untrusted


def active_config_paths(path: str | Path | None = None) -> list[Path]:
    """The config files :func:`load_config` reads for ``path``, low → high precedence.

    Each file comes after the files it includes.
    """
    return [file for config in _config_paths(path)[0] for file, _ in _config_layers(config)]


def _config_layers(path: Path, *, warn: bool = False) -> list[tuple[Path, dict[str, Any]]]:
    """``path``'s included files, then ``path`` itself, each as read, low → high precedence.

    An included file cannot include others: one level keeps the merge order
    plain to see, and leaves no include loop to detect.  With ``warn``, each
    file that exposes a credential is reported (:func:`_warn_exposed`).
    """
    data = read_yaml(path)
    layers = []
    for file in included_files(path, data):
        included = read_yaml(file)
        if "include" in included:
            raise ConfigError(f"{file} is included by {path}, so it cannot include files itself")
        if warn:
            _warn_exposed(file, included)
        layers.append((file, included))
    data.pop("include", None)
    if warn:
        _warn_exposed(path, data)
    layers.append((path, data))
    return layers


#: An agent ``env`` name that holds a credential.
_CREDENTIAL = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL", re.IGNORECASE)


def _warn_exposed(path: Path, data: dict[str, Any]) -> None:
    """Warn when other users can read a credential written in ``path``, as ssh does for a key.

    A credential is an agent ``env`` value under a name such as ``*_TOKEN``,
    written as is rather than as a ``${NAME}`` reference.  Only names are
    reported, never values.
    """
    if os.name == "nt":
        return  # no POSIX mode bits to go by
    agents = data.get("agents")
    names = [
        f"agents.{agent}.env.{key}"
        for agent, profile in (agents.items() if isinstance(agents, dict) else [])
        if isinstance(profile, dict) and isinstance(profile.get("env"), dict)
        for key, value in profile["env"].items()
        if _CREDENTIAL.search(str(key)) and isinstance(value, str) and value and "${" not in value
    ]
    try:
        exposed = names and path.stat().st_mode & 0o044
    except OSError:
        return
    if exposed:
        _log.warning(
            "%s holds %s and other users can read it; run `chmod 600 %s`, "
            "or use env_from or ${NAME} instead",
            path,
            ", ".join(names),
            path,
        )


def included_files(path: Path, data: dict[str, Any]) -> list[Path]:
    """The files named by ``path``'s ``include``, in order, relative to its directory.

    A glob's matches are sorted by name, and a glob may match nothing (an empty
    ``privyx.d/``); a plain path must exist.  Paths are taken as written, not
    interpolated, so the files that trust covers are the files that are read.
    """
    entries = data.get("include", [])
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise ConfigError(f"'include' in {path} must be a list of file paths or globs")
    files: list[Path] = []
    for entry in entries:
        target = path.parent / Path(entry).expanduser()
        if glob.escape(entry) != entry:
            files += sorted(Path(m) for m in glob.glob(str(target)) if Path(m).is_file())
        elif target.is_file():
            files.append(target)
        else:
            raise ConfigError(f"config file not found: {target} (included by {path})")
    return files


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]
    """A safe loader that refuses a key given twice in one mapping.

    PyYAML keeps the last one without a word, so an agent profile written
    twice would silently lose its first definition.  Only the key and its line
    are reported, never a value.
    """

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue  # `<<` may be overridden by design
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                continue  # unhashable: the base class reports it
            if duplicate:
                mark = key_node.start_mark
                raise ConfigError(
                    f"{mark.name}, line {mark.line + 1}: key {str(key)!r} is given twice "
                    "in one mapping; remove one of them"
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def read_yaml(path: Path, *, loader: type[yaml.SafeLoader] = _UniqueKeyLoader) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        try:
            data = yaml.load(fh, Loader=loader)  # noqa: S506 - a SafeLoader
        except yaml.YAMLError as exc:
            raise ConfigError(f"config file is not valid YAML: {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file must contain a mapping: {path}")
    return data
