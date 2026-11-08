"""Resolve secret references in credential settings.

``op://vault/item/field`` is read with the 1Password CLI (``op read``) and
``keychain://service/account`` from the system keychain through ``keyring``
(``privyx[keychain]``), so a config file can name a credential without holding it.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from privyx.config.redact import _MASK_VALUES, _sensitive
from privyx.core.errors import ConfigError

_SCHEMES = ("op://", "keychain://")


def resolve_secrets(value: Any, path: str = "", name: str = "", masked: bool = False) -> Any:
    """Replace each secret reference in ``value`` with the secret it names.

    A reference resolves only where :func:`privyx.config.redact.redact` masks
    the value, so a resolved secret is never shown by ``privyx config``: a
    field whose own name is a credential's (``api_key``, ``secret``, ...), and
    every value of a section masked in full (``headers``, ``env``, ...).  A
    reference anywhere else is an error.  ``name`` is the field's own key, and
    ``masked`` marks a value inside a section masked in full.
    """
    masked = masked or name in _MASK_VALUES
    if isinstance(value, dict):
        return {
            key: resolve_secrets(item, f"{path}.{key}" if path else str(key), str(key), masked)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_secrets(item, path, name, masked) for item in value]
    if not isinstance(value, str) or not value.startswith(_SCHEMES):
        return value
    if not (masked or _sensitive(name)):
        raise ConfigError(
            f"{path} holds a secret reference; only credential settings such as "
            "api_key, secret, password, headers, and env resolve one"
        )
    if value.startswith("op://"):
        return _op_read(value, path)
    return _keychain(value, path)


def _op_read(reference: str, path: str) -> str:
    if shutil.which("op") is None:
        raise ConfigError(f"{path} names a 1Password secret, but the `op` CLI is not installed")
    try:
        done = subprocess.run(
            ["op", "read", "--no-newline", reference],
            capture_output=True,
            text=True,
            timeout=120,  # long enough to unlock the app
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ConfigError(f"`op read` timed out for {path}") from None
    if done.returncode != 0:
        reason = done.stderr.strip().splitlines()[-1:] or ["no output"]
        raise ConfigError(f"`op read` failed for {path}: {reason[0]}")
    return done.stdout


def _keychain(reference: str, path: str) -> str:
    service, _, account = reference.removeprefix("keychain://").partition("/")
    if not service or not account:
        raise ConfigError(f"{path}: a keychain reference is keychain://<service>/<account>")
    try:
        import keyring
    except ImportError:
        raise ConfigError(
            f"{path} names a keychain secret; install it with: pip install 'privyx[keychain]'"
        ) from None
    try:
        found = keyring.get_password(service, account)
    except Exception as exc:  # any backend failure: locked, no backend, denied
        raise ConfigError(f"cannot read the keychain for {path}: {type(exc).__name__}") from None
    if found is None:
        raise ConfigError(f"{path}: no keychain entry for service {service!r}, account {account!r}")
    return str(found)
