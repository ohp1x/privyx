"""Each module imports only its own layer and those below, as the 0.3.0 layout has it.

A module is placed in the package it belongs to in that layout, wherever it
lives today: ``privyx.cli.launch`` is agent code, ``privyx.core.builder``
assembles the app.  The layers, lowest first:

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
    "privyx.app": "app",
    "privyx.cli": "cli",
    "privyx.cli.budget": "features",
    "privyx.cli.claude": "agents",
    "privyx.cli.codex": "agents",
    "privyx.cli.commands.mcp": "mcp",
    "privyx.cli.commands.mcp_serve": "mcp",
    "privyx.cli.launch": "agents",
    "privyx.cli.native": "agents",
    "privyx.cli.workspace": "workspace",
    "privyx.config": "config",
    "privyx.config.redact": "security",
    "privyx.core": "core",
    "privyx.core.builder": "app",
    "privyx.engine": "engine",
    "privyx.events": "events",
    "privyx.gateway": "proxy",
    # The package only re-exports; its modules go their own ways.
    "privyx.observability": "app",
    "privyx.observability.audit": "features",
    "privyx.observability.logging": "app",
    "privyx.observability.metrics": "features",
    "privyx.observability.tap": "proxy",
    "privyx.observability.tracing": "features",
    "privyx.observability.usage": "dialects",
    "privyx.plugins": "app",
    "privyx.plugins.registry": "core",
    "privyx.privacy": "core",
    "privyx.privacy.anchor": "anchors",
    "privyx.privacy.detector": "detectors",
    "privyx.privacy.operator": "operators",
    "privyx.privacy.policy": "policies",
    "privyx.privacy.policy.loader": "app",
    "privyx.providers": "providers",
    "privyx.proxy": "proxy",
    "privyx.proxy.object_stream": "dialects",
    "privyx.proxy.stream_router": "streaming",
    "privyx.security": "security",
    "privyx.security.secrets": "secrets",
    "privyx.streaming": "streaming",
    "privyx.streaming.adapters.anthropic": "dialects",
    "privyx.streaming.adapters.gemini": "dialects",
    "privyx.streaming.adapters.openai": "dialects",
    "privyx.token": "token",
    "privyx.vault": "vaults",
    # A leaf, importable from every layer.
    "privyx.version": "core",
}

#: (importer, imported) pairs that break the layers today.
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        # Privyx.run serves with the CLI's server loop: serving moves into app.
        ("privyx.app._privyx", "privyx.cli.commands.proxy"),
        # The budget parses usage from response bytes; it moves onto events.
        ("privyx.cli.budget", "privyx.observability.usage"),
        ("privyx.cli.budget", "privyx.proxy.schemas"),
        # Planning a launch resolves upstream URLs with the providers' helpers.
        ("privyx.cli.launch", "privyx.providers.registry"),
        # Loading config resolves secret references: resolving moves into app.
        ("privyx.config.loader", "privyx.security.secrets"),
        # Telling a model name from PII goes with the cost report into app.
        ("privyx.observability.usage", "privyx.privacy.detector.builtin"),
        # The pseudonym operator takes an anchor: imported for typing only.
        ("privyx.privacy.operator.pseudonym", "privyx.privacy.anchor.base"),
        # The strict policy reads the built-in entity names: they move into core.
        ("privyx.privacy.policy.strict", "privyx.privacy.detector.builtin"),
        # The JSON walk moves into core, and each format's stream into its dialect.
        ("privyx.proxy.object_stream", "privyx.proxy.schemas"),
        ("privyx.proxy.stream_router", "privyx.proxy.object_stream"),
        ("privyx.proxy.stream_router", "privyx.proxy.schemas"),
        ("privyx.streaming.adapters.registry", "privyx.streaming.adapters.anthropic"),
        ("privyx.streaming.adapters.registry", "privyx.streaming.adapters.gemini"),
        ("privyx.streaming.adapters.registry", "privyx.streaming.adapters.openai"),
    }
)


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
