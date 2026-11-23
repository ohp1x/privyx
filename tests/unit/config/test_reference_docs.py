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
    assert missing == set()


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
    assert missing == set()


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
    assert missing == set()
