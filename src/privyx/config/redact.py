"""Mask credentials and detector config before settings or URLs are printed."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

_MASK = "***"

#: Fields whose every value is masked, keys kept: header values carry
#: credentials, and detector config spells out the very values Privyx hides.
_MASK_VALUES = frozenset({"headers", "terms", "patterns", "llm_instructions"})


def redact(value: Any, name: str = "") -> Any:
    """Mask sensitive values in a settings dump, keeping empty ones visible as unset.

    Matched by field name, so a new ``*_api_key`` / ``*_password`` field is
    covered without a list to update: keys, secrets, passwords, the fields in
    :data:`_MASK_VALUES`, and the password in a URL field (``*_url``, ``dsn``).
    """
    if name in _MASK_VALUES:
        return _mask_all(value)
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, name) for v in value]
    if not isinstance(value, str) or not value:
        return value
    if name == "key" or name.endswith(("api_key", "secret", "password")):
        return _MASK
    if name.endswith(("url", "dsn")):
        parts = urlsplit(value)
        if parts.password:
            netloc = parts.netloc.replace(f":{parts.password}@", f":{_MASK}@", 1)
            return urlunsplit(parts._replace(netloc=netloc))
    return value


def _mask_all(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _mask_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_all(v) for v in value]
    return _MASK if value else value
