"""``Registry``: a kind's components by name, and the errors config mistakes get."""

from __future__ import annotations

import sys
from abc import abstractmethod
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import Field

from privyx.core import Component, ConfigError, Registry


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


class Detector(Component, kind="detector"):
    pass


LAZY = f"""
from {__name__} import Vault


class LazyVault(Vault):
    name = "lazy"

    async def get(self, key):
        return "lazy"
"""


@pytest.fixture
def registry() -> Registry[Vault]:
    registry: Registry[Vault] = Registry(Vault)
    registry.builtins({"memory": f"{__name__}:MemoryVault"})
    return registry


@pytest.fixture
def lazy_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    (tmp_path / "privyx_lazy_vault.py").write_text(LAZY, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "privyx_lazy_vault"
    sys.modules.pop("privyx_lazy_vault", None)


def test_a_builtin_is_imported_only_when_chosen(
    registry: Registry[Vault], lazy_module: str
) -> None:
    registry.builtins({"lazy": f"{lazy_module}:LazyVault"})
    assert registry.names() == ["lazy", "memory"]
    assert lazy_module not in sys.modules

    assert registry.get("lazy").__name__ == "LazyVault"
    assert lazy_module in sys.modules


def test_a_plugin_is_added_by_class(registry: Registry[Vault]) -> None:
    assert registry.add(AuditedVault) is AuditedVault
    assert registry.get("audited-memory") is AuditedVault
    assert registry.names() == ["audited-memory", "memory"]
    assert "audited-memory" in registry


def test_a_plugin_sets_its_own_name(registry: Registry[Vault]) -> None:
    class NoName(MemoryVault):
        pass

    with pytest.raises(
        ConfigError, match="^NoName must set its own name; 'memory' is MemoryVault's$"
    ):
        registry.add(NoName)


def test_a_builtin_name_is_reserved(registry: Registry[Vault]) -> None:
    class Shadow(MemoryVault):
        name = "memory"

    with pytest.raises(
        ConfigError, match="^vault type 'memory' is built in; give Shadow a name of its own$"
    ):
        registry.add(Shadow)


def test_two_plugins_with_one_name_are_both_named(registry: Registry[Vault]) -> None:
    class A(MemoryVault):
        name = "x"

    class B(MemoryVault):
        name = "x"

    registry.add(A)
    with pytest.raises(ConfigError, match=r"^two vault plugins are named 'x': \S+\.A and \S+\.B$"):
        registry.add(B)


def test_an_abstract_class_is_not_a_type(registry: Registry[Vault]) -> None:
    class Half(Vault):
        name = "half"

    with pytest.raises(ConfigError, match="^Half is abstract; it cannot be a vault type$"):
        registry.add(Half)


def test_a_component_of_another_kind_is_refused(registry: Registry[Vault]) -> None:
    class Other(Detector):
        name = "other"

    with pytest.raises(ConfigError, match="is not a vault component$"):
        registry.add(Other)  # type: ignore[arg-type]


def test_a_misspelled_type_gets_a_suggestion(registry: Registry[Vault]) -> None:
    with pytest.raises(
        ConfigError, match=r"^unknown vault type 'memroy' \(did you mean 'memory'\?\)$"
    ):
        registry.get("memroy")
    with pytest.raises(ConfigError, match="^unknown vault type 'postgres'$"):
        registry.get("postgres")


def test_build_checks_the_options_of_the_type(registry: Registry[Vault]) -> None:
    registry.add(AuditedVault)
    vault = registry.build("audited-memory", {"ttl": 60, "log_every": 5})
    assert isinstance(vault, AuditedVault)
    assert (vault.options.ttl, vault.options.log_every) == (60, 5)  # type: ignore[attr-defined]


def test_a_misspelled_option_names_the_setting_and_type(registry: Registry[Vault]) -> None:
    registry.add(AuditedVault)
    expected = (
        r"^unknown setting 'vault\.log_evry' \(did you mean 'log_every'\?\) "
        r"for vault type 'audited-memory'$"
    )
    with pytest.raises(ConfigError, match=expected):
        registry.build("audited-memory", {"log_evry": 5})


def test_an_invalid_option_names_the_setting_never_the_value(registry: Registry[Vault]) -> None:
    with pytest.raises(ConfigError) as error:
        registry.build("memory", {"ttl": -7341}, setting="vaults.main")
    assert str(error.value) == (
        "invalid setting 'vaults.main.ttl': Input should be greater than 0 for vault type 'memory'"
    )
    assert "7341" not in str(error.value)


def test_extends_is_read_from_the_classes(registry: Registry[Vault]) -> None:
    class Deeper(AuditedVault):
        name = "deeper"

    registry.add(AuditedVault)
    registry.add(Deeper)
    assert registry.extends("audited-memory") == "memory"
    assert registry.extends("deeper") == "audited-memory"
    assert registry.extends("memory") is None
