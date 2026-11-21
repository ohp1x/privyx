"""``keychain://service/account``: the system keychain, through ``keyring``."""

from __future__ import annotations

from privyx.core import ConfigError
from privyx.secrets._base import SecretResolver


class KeychainResolver(SecretResolver):
    """Reads ``keychain://service/account`` with ``keyring`` (``privyx[keychain]``)."""

    name = "keychain"

    def resolve(self, reference: str, path: str) -> str:
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
            raise ConfigError(
                f"cannot read the keychain for {path}: {type(exc).__name__}"
            ) from None
        if found is None:
            raise ConfigError(
                f"{path}: no keychain entry for service {service!r}, account {account!r}"
            )
        return str(found)
