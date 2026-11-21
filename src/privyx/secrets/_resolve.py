"""Resolve the secret references in a config's credential settings.

A reference is ``<scheme>://...`` for a scheme a resolver is registered
under; any other value, a URL included, is taken as written.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from privyx.core import ConfigError
from privyx.security import MASKED_FIELDS, is_secret_name

if TYPE_CHECKING:
    from privyx.secrets._base import SecretResolver

_SCHEME = re.compile(r"([a-z][a-z0-9+.-]*)://")

#: Sections read before the secrets are: a reference there could not be resolved.
_UNRESOLVED = frozenset({"plugins", "secrets"})


def resolve_references(
    value: Any,
    resolver: Callable[[str], SecretResolver | None],
    path: str = "",
    name: str = "",
    masked: bool = False,
) -> Any:
    """Replace each secret reference in ``value`` with the secret it names.

    A reference resolves only where :func:`privyx.security.redact` masks the
    value, so a resolved secret is never shown by ``privyx config``: a field
    whose own name is a credential's (``api_key``, ``secret``, ...), and every
    value of a section masked in full (``headers``, ``env``, ...).

    Args:
        value: A config, or a part of one.
        resolver: The resolver of a scheme, or ``None`` when none is registered.
        path: Where ``value`` is, for errors.
        name: The field's own key.
        masked: Whether ``value`` is inside a section masked in full.

    Raises:
        ConfigError: If a reference is outside a credential setting, in
            ``plugins`` or ``secrets``, or cannot be read.
    """
    masked = masked or name in MASKED_FIELDS
    if isinstance(value, dict):
        return {
            key: resolve_references(
                item, resolver, f"{path}.{key}" if path else str(key), str(key), masked
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_references(item, resolver, path, name, masked) for item in value]
    if not isinstance(value, str) or (found := _SCHEME.match(value)) is None:
        return value
    resolve = resolver(found[1])
    if resolve is None:
        return value
    if path.split(".", 1)[0] in _UNRESOLVED:
        raise ConfigError(f"{path} holds a secret reference; write ${{NAME}} there instead")
    if not (masked or is_secret_name(name)):
        raise ConfigError(
            f"{path} holds a secret reference; only credential settings such as "
            "api_key, secret, password, headers, and env resolve one"
        )
    return resolve.resolve(value, path)
