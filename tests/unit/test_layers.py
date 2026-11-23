"""Each module imports only its own layer and those below, as the 0.3.0 layout has it.

A module is placed in the package it belongs to in that layout, wherever it
lives today: ``privyx.observability.usage`` is a dialect's.  The layers, lowest first:

0. ``core``
1. ``events``, ``token``, ``security``, ``streaming``
2. ``config``
3. the kinds: ``detectors``, ``policies``, ``operators``, ``anchors``,
   ``vaults``, ``providers``, ``secrets``, ``dialects``, ``agents``,
   ``features``; none of them imports another
4. ``engine``
5. ``proxy``
6. ``workspace``
7. ``app``
8. ``mcp``, ``cli``, ``testing``

Imports under ``if TYPE_CHECKING:`` do not count, imports inside functions do.
The imports that break the layers today are in :data:`ALLOWED`, each with its
reason.  The list only shrinks: an entry no longer needed fails the test too.

A module whose name starts with ``_`` is private to its package: another
package imports what the package's ``__init__`` exports, except ``app``, which
assembles them all.  The exceptions today are in :data:`ALLOWED_PRIVATE`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

KINDS = frozenset(
    {
        "detectors",
        "policies",
        "operators",
        "anchors",
        "vaults",
        "providers",
        "secrets",
        "dialects",
        "agents",
        "features",
    }
)

LAYERS: dict[str, int] = {
    "core": 0,
    **dict.fromkeys(("events", "token", "security", "streaming"), 1),
    "config": 2,
    **dict.fromkeys(KINDS, 3),
    "engine": 4,
    "proxy": 5,
    "workspace": 6,
    "app": 7,
    **dict.fromkeys(("mcp", "cli", "testing"), 8),
}

#: The package each module belongs to, by its longest dotted prefix here.
#: ``privyx`` itself, the public API, is placed with ``app``.
PLACES: dict[str, str] = {
    "privyx.agents": "agents",
    "privyx.anchors": "anchors",
    "privyx.app": "app",
    "privyx.cli": "cli",
    "privyx.config": "config",
    "privyx.core": "core",
    "privyx.detectors": "detectors",
    "privyx.dialects": "dialects",
    "privyx.engine": "engine",
    "privyx.events": "events",
    "privyx.features": "features",
    # The package only re-exports; its modules go their own ways.
    "privyx.observability": "app",
    "privyx.observability.audit": "events",
    "privyx.observability.logging": "app",
    "privyx.observability.tracing": "features",
    "privyx.observability.usage": "dialects",
    "privyx.mcp": "mcp",
    "privyx.operators": "operators",
    "privyx.policies": "policies",
    "privyx.providers": "providers",
    "privyx.proxy": "proxy",
    "privyx.secrets": "secrets",
    "privyx.security": "security",
    "privyx.streaming": "streaming",
    "privyx.testing": "testing",
    "privyx.token": "token",
    "privyx.vaults": "vaults",
    "privyx.workspace": "workspace",
    # A leaf, importable from every layer.
    "privyx.version": "core",
}

#: (importer, imported) pairs that break the layers today.
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        # Telling a model name from PII goes with the cost report into app.
        ("privyx.observability.usage", "privyx.detectors"),
    }
)


#: (importer, imported) pairs where a module imports a private module of another package.
ALLOWED_PRIVATE: frozenset[tuple[str, str]] = frozenset()


def _modules() -> dict[str, Path]:
    found = {}
    for path in sorted((SRC / "privyx").rglob("*.py")):
        parts = path.relative_to(SRC).with_suffix("").parts
        found[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = path
    return found


MODULES = _modules()


def _place(module: str) -> str:
    if module == "privyx":
        return "app"
    name = module
    while name not in PLACES:
        name = name.rpartition(".")[0]
        if name == "privyx":
            raise AssertionError(f"{module} has no package in PLACES")
    return PLACES[name]


def _resolve(name: str) -> str:
    """The module an import binds: ``name`` or the package holding it."""
    while name not in MODULES:
        name = name.rpartition(".")[0]
    return name


def _imports(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Each Privyx module imported at run time, with its line."""
    typing_only = {
        id(node)
        for block in ast.walk(tree)
        if isinstance(block, ast.If)
        and isinstance(block.test, ast.Name)
        and block.test.id == "TYPE_CHECKING"
        for statement in block.body
        for node in ast.walk(statement)
    }
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.level == 0, "relative imports are not used"
            names = [f"{node.module}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in names:
            if name.split(".")[0] == "privyx":
                yield node.lineno, _resolve(name)


def _violations() -> dict[tuple[str, str], list[str]]:
    found: dict[tuple[str, str], list[str]] = {}
    for module, path in MODULES.items():
        source = _place(module)
        for line, target in _imports(ast.parse(path.read_text(), str(path))):
            imported = _place(target)
            upward = LAYERS[imported] > LAYERS[source]
            across = source in KINDS and imported in KINDS and source != imported
            if upward or across:
                where = f"{path.relative_to(SRC.parent)}:{line} ({source} -> {imported})"
                found.setdefault((module, target), []).append(where)
    return found


def test_each_place_is_a_package_of_the_layout() -> None:
    assert set(PLACES.values()) <= LAYERS.keys()
    assert [prefix for prefix in PLACES if prefix not in MODULES] == []


def test_imports_follow_the_layers() -> None:
    found = _violations()
    new = [where for pair, places in found.items() if pair not in ALLOWED for where in places]
    assert new == [], "imports against the layers:\n" + "\n".join(new)
    fixed = sorted(ALLOWED - found.keys())
    assert fixed == [], "no longer needed in ALLOWED: " + ", ".join(map(str, fixed))


def _package(module: str) -> str:
    """The package a module is in on disk: ``privyx.cli.commands.run`` is in ``cli``."""
    return module.split(".")[1] if "." in module else ""


def _private(module: str) -> bool:
    return any(part.startswith("_") for part in module.split(".")[1:])


def test_private_modules_stay_in_their_package() -> None:
    found: dict[tuple[str, str], list[str]] = {}
    for module, path in MODULES.items():
        if _package(module) == "app":
            continue  # it assembles every package
        for line, target in _imports(ast.parse(path.read_text(), str(path))):
            if _private(target) and _package(target) != _package(module):
                where = f"{path.relative_to(SRC.parent)}:{line} ({module} -> {target})"
                found.setdefault((module, target), []).append(where)
    new = [
        where for pair, places in found.items() if pair not in ALLOWED_PRIVATE for where in places
    ]
    assert new == [], "private modules imported from another package:\n" + "\n".join(new)
    fixed = sorted(ALLOWED_PRIVATE - found.keys())
    assert fixed == [], "no longer needed in ALLOWED_PRIVATE: " + ", ".join(map(str, fixed))


def test_only_a_kind_branches_on_the_name_of_a_built_in_type() -> None:
    from privyx.app import Privyx

    names = {name for kind in Privyx()._registries.values() for name in kind.builtin_names()}
    found = []
    for module, path in MODULES.items():
        if _place(module) in KINDS:
            continue
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Attribute)
                and node.left.attr == "type"
                and any(isinstance(c, ast.Constant) and c.value in names for c in node.comparators)
            ):
                found.append(f"{path.relative_to(SRC.parent)}:{node.lineno}")
    assert found == [], "ask the component's class instead:\n" + "\n".join(found)


def test_the_mcp_server_uses_nothing_of_the_cli() -> None:
    found = [
        f"{path.relative_to(SRC.parent)}:{line} -> {target}"
        for module, path in MODULES.items()
        if _package(module) == "mcp"
        for line, target in _imports(ast.parse(path.read_text(), str(path)))
        if _package(target) == "cli"
    ]
    assert found == []
