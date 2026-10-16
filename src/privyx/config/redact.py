"""Mask credentials and detector config before settings or URLs are printed."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_MASK = "***"

#: Fields whose every value is masked, keys kept: header values carry
#: credentials, and detector config spells out the very values Privyx hides.
_MASK_VALUES = frozenset({"headers", "terms", "patterns", "llm_instructions", "env"})


def redact(value: Any, name: str = "") -> Any:
    """Mask sensitive values in a settings dump, keeping empty ones visible as unset.

    Matched by field name, so a new ``*_api_key`` / ``*_password`` field is
    covered without a list to update: keys, secrets, passwords, the fields in
    :data:`_MASK_VALUES`, and the password in a URL field (``*_url``, ``dsn``).
    """
    if name in _MASK_VALUES:
        return _mask_all(value)
    if name == "args" and isinstance(value, list) and all(isinstance(v, str) for v in value):
        return redact_argv(value)
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, name) for v in value]
    if not isinstance(value, str) or not value:
        return value
    if _sensitive(name):
        return _MASK
    if name == "upstream" or name.endswith(("url", "dsn")):
        try:
            parts = urlsplit(value)
        except ValueError:
            return _MASK
        if parts.username is not None or parts.password is not None:
            user = _MASK if parts.username else ""
            password = ":" + _MASK if parts.password is not None else ""
            netloc = user + password + "@" + parts.netloc.rsplit("@", 1)[-1]
            parts = parts._replace(netloc=netloc)
        # Routing is inspectable without printing URL query credentials.
        return urlunsplit(
            parts._replace(
                query=_MASK if parts.query else "", fragment=_MASK if parts.fragment else ""
            )
        )
    return value


def _sensitive(name: str) -> bool:
    name = name.lower().lstrip("-").replace("-", "_").rsplit(".", 1)[-1]
    return name in {"key", "authorization", "credentials"} or name.endswith(
        ("api_key", "secret", "password", "token", "credential")
    )


def redact_argv(argv: list[str]) -> list[str]:
    """Sanitize credential options, config assignments, JSON and URLs for display.

    The actual argv is untouched. Arbitrary prose is not a configuration dump
    and is not scanned for personal data here.
    """
    result: list[str] = []
    mask_next = False
    for arg in argv:
        if mask_next:
            result.append(_MASK)
            mask_next = False
            continue
        if arg.startswith(("{", "[", "http://", "https://")):
            result.append(_redact_arg(arg))
            continue
        key, sep, value = arg.partition("=")
        if sep:
            if _sensitive(key):
                result.append(key + "=" + _MASK)
                continue
            value = _redact_arg(value)
            result.append(key + "=" + value)
        else:
            result.append(_redact_arg(arg))
            mask_next = arg.startswith("-") and _sensitive(arg)
    return result


def _redact_arg(value: str) -> str:
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return json.dumps(redact(parsed))
    if isinstance(parsed, str) and "://" in parsed:
        return json.dumps(redact(parsed, "url"))
    if "://" in value:
        return str(redact(value, "url"))
    return value


def _mask_all(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _mask_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_all(v) for v in value]
    return _MASK if value else value
