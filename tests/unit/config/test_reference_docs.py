"""The guide's reference pages name every setting, variable, and CLI option.

Hand-written reference docs drift as options are added.  These fail when a new
one is missing from its page; they cannot tell whether the prose is right.
"""

from __future__ import annotations

import typing
from collections.abc import Iterator
from pathlib import Path

import click
from pydantic import BaseModel

from privyx.app import Privyx
from privyx.cli.main import cli
from privyx.config import ALIASES
from privyx.config.schema import Settings

GUIDE = Path(__file__).parents[3] / "docs" / "guide"

#: New settings, variables, and commands, described with the 0.3 upgrade
#: guide.  Each set only shrinks: a name its page has come to describe fails
#: the test too.
PENDING_SETTINGS: frozenset[str] = frozenset(
    {
        "builtins",
        "detectors",
        "features",
        "file",
        "level",
        "log",
        "profile",
        "providers",
        "secrets",
        "server",
        "workers",
    }
)
#: ``PRIVYX_<PATH>`` stands for the generic names, such as ``PRIVYX_SERVER__PORT``.
PENDING_ENV: frozenset[str] = frozenset({"PRIVYX_<PATH>"})
PENDING_CLI: frozenset[str] = frozenset(
    {
        "privyx audit stats --json",
        "privyx config edit",
        "privyx config edit --project",
        "privyx config explain",
        "privyx config explain --json",
        "privyx config get",
        "privyx config get --json",
        "privyx config lint",
        "privyx config migrate",
        "privyx config migrate --write",
        "privyx config path",
        "privyx config routes",
        "privyx config schema",
        "privyx config set",
        "privyx config set --file",
        "privyx config set --project",
        "privyx config show",
        "privyx config trust",
        "privyx config unset",
        "privyx config unset --file",
        "privyx config unset --project",
        "privyx config validate",
        "privyx detect --entity",
        "privyx detect --pattern",
        "privyx doctor --json",
        "privyx features",
        "privyx features disable",
        "privyx features disable --file",
        "privyx features disable --project",
        "privyx features enable",
        "privyx features enable --file",
        "privyx features enable --project",
        "privyx features list",
        "privyx features list --events",
        "privyx features list --json",
        "privyx hook install",
        "privyx mask --stream",
        "privyx plugin",
        "privyx plugin list",
        "privyx plugin list --json",
        "privyx proxy --workers",
        "privyx run --json",
        "privyx session list --json",
        "privyx session token",
        "privyx unmask --stream",
    }
)


def _setting_names(model: type[BaseModel]) -> Iterator[str]:
    for name, field in model.model_fields.items():
        yield name
        for arg in typing.get_args(field.annotation) or (field.annotation,):
            if hasattr(arg, "model_fields"):  # a nested section, not list[...] or a scalar
                yield from _setting_names(arg)
            for inner in typing.get_args(arg):  # an entry of a mapping of sections
                if hasattr(inner, "model_fields"):
                    yield from _setting_names(inner)


def _option_names() -> Iterator[str]:
    """The options of every built-in component type, which the page names too."""
    for registry in Privyx()._registries.values():
        for name in registry.builtin_names():
            yield from _setting_names(registry.get(name).Options)


def _commands(group: click.Command, path: str) -> Iterator[tuple[str, click.Command]]:
    yield path, group
    if isinstance(group, click.Group):
        context = click.Context(group)
        for name in group.list_commands(context):
            command = group.get_command(context, name)
            if command is not None and not command.hidden:
                yield from _commands(command, f"{path} {name}")


def test_configuration_page_names_every_setting() -> None:
    page = (GUIDE / "configuration.md").read_text()
    names = {*_setting_names(Settings), *_option_names()}
    missing = {name for name in names if f"`{name}`" not in page}
    assert missing == PENDING_SETTINGS


def test_configuration_page_names_every_environment_variable() -> None:
    page = (GUIDE / "configuration.md").read_text()
    # The TLS_ spellings are documented as an alternative prefix, not one by one.
    missing = {
        name
        for name in ALIASES
        if name not in page and not (name.startswith("PRIVYX_TLS_") and "PRIVYX_TLS_" in page)
    }
    if "PRIVYX_SERVER__PORT" not in page:
        missing.add("PRIVYX_<PATH>")
    assert missing == PENDING_ENV


def test_cli_page_names_every_command_and_option() -> None:
    page = (GUIDE / "cli.md").read_text()
    missing = set()
    for path, command in _commands(cli, "privyx"):
        if path not in page:
            missing.add(path)
        for param in command.params:
            if getattr(param, "hidden", False):
                continue  # an earlier version's name, read with a warning
            for flag in getattr(param, "opts", []) + getattr(param, "secondary_opts", []):
                if flag.startswith("--") and flag != "--help" and flag not in page:
                    missing.add(f"{path} {flag}")
    assert missing == PENDING_CLI
