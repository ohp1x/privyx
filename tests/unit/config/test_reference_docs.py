"""The guide's reference pages name every setting, variable, and CLI option.

Hand-written reference docs drift as options are added.  These fail when a new
one is missing from its page; they cannot tell whether the prose is right.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Iterator
from pathlib import Path

import click
from pydantic import BaseModel

import privyx.config.env
from privyx.cli.main import cli
from privyx.config.schema import Settings

GUIDE = Path(__file__).parents[3] / "docs" / "guide"


def _setting_names(model: type[BaseModel]) -> Iterator[str]:
    for name, field in model.model_fields.items():
        yield name
        for arg in typing.get_args(field.annotation) or (field.annotation,):
            if hasattr(arg, "model_fields"):  # a nested section, not list[...] or a scalar
                yield from _setting_names(arg)


def _commands(group: click.Command, path: str) -> Iterator[tuple[str, click.Command]]:
    yield path, group
    if isinstance(group, click.Group):
        for name, command in group.commands.items():
            yield from _commands(command, f"{path} {name}")


def test_configuration_page_names_every_setting() -> None:
    page = (GUIDE / "configuration.md").read_text()
    missing = {name for name in _setting_names(Settings) if f"`{name}`" not in page}
    assert not missing


def test_configuration_page_names_every_environment_variable() -> None:
    page = (GUIDE / "configuration.md").read_text()
    source = Path(privyx.config.env.__file__).read_text()
    names = set(re.findall(r'_env\("([A-Z_]+)"\)', source))
    # The TLS_ spellings are documented as an alternative prefix, not one by one.
    missing = {
        name
        for name in names
        if f"PRIVYX_{name}" not in page and not (name.startswith("TLS_") and "PRIVYX_TLS_" in page)
    }
    assert names and not missing


def test_cli_page_names_every_command_and_option() -> None:
    page = (GUIDE / "cli.md").read_text()
    missing = set()
    for path, command in _commands(cli, "privyx"):
        if path not in page:
            missing.add(path)
        for param in command.params:
            for flag in getattr(param, "opts", []) + getattr(param, "secondary_opts", []):
                if flag.startswith("--") and flag != "--help" and flag not in page:
                    missing.add(f"{path} {flag}")
    assert not missing
