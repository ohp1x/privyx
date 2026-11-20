"""Session vaults: where a session's mapping is kept.

The built-ins are imported on first use, so ``import privyx.vaults`` needs no
optional extra.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute
from privyx.vaults._base import StoredVault, Vault

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.vaults._memory import MemoryVault
    from privyx.vaults._redis import RedisVault
    from privyx.vaults._sqlite import SQLiteVault

__all__ = ["MemoryVault", "RedisVault", "SQLiteVault", "StoredVault", "Vault"]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "memory": "privyx.vaults._memory:MemoryVault",
    "sqlite": "privyx.vaults._sqlite:SQLiteVault",
    "redis": "privyx.vaults._redis:RedisVault",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.vaults import RedisVault`` imports that module on first use."""
    return lazy_attribute(name, _BUILTINS, __name__)


def setup(app: Privyx) -> None:
    """Register the built-in vaults with ``app``."""
    app.registry(Vault).builtins(_BUILTINS)

