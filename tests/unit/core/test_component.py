"""``Component`` and ``Options``: the rules a subclass is held to when it is created."""

from __future__ import annotations

from abc import abstractmethod
from typing import final

import pytest
from pydantic import Field, ValidationError

from privyx.core import Component, Options


class Vault(Component, kind="vault"):
    @abstractmethod
    async def get(self, key: str) -> str | None: ...


class MemoryVault(Vault):
    name = "memory"

    class Options(Vault.Options):
        ttl: int | None = Field(default=None, gt=0)

    async def get(self, key: str) -> str | None:
        return None


class AuditedVault(MemoryVault):
    name = "audited-memory"

    class Options(MemoryVault.Options):
        log_every: int = 100


def test_a_kind_is_declared_once_by_its_base_class() -> None:
    assert (Vault.kind, MemoryVault.kind) == ("vault", "vault")
    with pytest.raises(
        TypeError, match="^Weird cannot declare kind 'store': Vault already declares 'vault'$"
    ):

        class Weird(MemoryVault, kind="store"):
            name = "weird"


def test_a_final_component_cannot_be_extended() -> None:
    @final
    class SealedVault(MemoryVault):
        name = "sealed"

    with pytest.raises(TypeError, match="^Child cannot extend SealedVault: it is final$"):

        class Child(SealedVault):
            name = "child"


@pytest.mark.parametrize("name", ["Corp Vault", "corp vault", "-corp", "corp_", "1corp", "Corp"])
def test_a_name_is_lowercase_words_joined_by_a_dash_or_underscore(name: str) -> None:
    with pytest.raises(
        TypeError, match=r"^Upper\.name .* must be lowercase words joined by - or _$"
    ):
        type("Upper", (MemoryVault,), {"name": name})


@pytest.mark.parametrize("name", ["corp", "corp-vault", "corp_vault", "v2", "acme-id-2"])
def test_a_valid_name_is_kept(name: str) -> None:
    assert type("Named", (MemoryVault,), {"name": name}).name == name


def test_options_extend_the_parents_options() -> None:
    with pytest.raises(TypeError, match=r"^Broken\.Options must subclass MemoryVault\.Options$"):

        class Broken(MemoryVault):
            name = "broken"

            class Options(Options):
                ttl: str = "never"


def test_options_are_checked_when_built_and_frozen() -> None:
    vault = MemoryVault(ttl=60)
    assert vault.options == MemoryVault.Options(ttl=60)
    with pytest.raises(ValidationError):
        vault.options.ttl = 5  # type: ignore[misc]
    with pytest.raises(ValidationError):
        MemoryVault(ttl=0)


def test_an_unknown_option_is_refused_without_showing_its_value() -> None:
    with pytest.raises(ValidationError) as error:
        MemoryVault(tll="hunter2")
    assert "tll" in str(error.value)
    assert "hunter2" not in str(error.value)


def test_an_extension_keeps_the_options_and_behavior_of_what_it_extends() -> None:
    vault = AuditedVault(ttl=5, log_every=1)
    assert (vault.options.ttl, vault.options.log_every) == (5, 1)  # type: ignore[attr-defined]
    assert isinstance(vault, MemoryVault)


async def test_a_component_works_without_open() -> None:
    vault = MemoryVault()
    assert await vault.get("x") is None
    await vault.close()
