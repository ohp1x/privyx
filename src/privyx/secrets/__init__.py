"""Secret resolvers: read a credential a config names, such as ``op://vault/item/field``.

A resolver's type is its scheme: ``op`` resolves ``op://...``, and a plugin
adds another, with its options under ``secrets.<scheme>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute
from privyx.secrets._base import RESERVED_SCHEMES, SecretResolver
from privyx.secrets._resolve import resolve_references as resolve_references

if TYPE_CHECKING:
    from privyx import Privyx
    from privyx.secrets._keychain import KeychainResolver
    from privyx.secrets._op import OnePasswordResolver

__all__ = ["RESERVED_SCHEMES", "KeychainResolver", "OnePasswordResolver", "SecretResolver"]

#: The built-ins by type name, imported only when chosen.
_BUILTINS = {
    "op": "privyx.secrets._op:OnePasswordResolver",
    "keychain": "privyx.secrets._keychain:KeychainResolver",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.secrets import KeychainResolver`` imports it on first use."""
    return lazy_attribute(name, _BUILTINS, __name__)


def setup(app: Privyx) -> None:
    """Register the built-in secret resolvers with ``app``."""
    app.registry(SecretResolver).builtins(_BUILTINS)
