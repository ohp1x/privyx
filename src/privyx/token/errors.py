"""Errors raised by the token subsystem."""

from __future__ import annotations

from privyx.core.errors import ConfigError


class TokenFormatError(ConfigError):
    """Raised when a token format string is unusable or a field violates its grammar.

    A subclass of :class:`~privyx.core.errors.ConfigError` because an unusable
    ``token.format`` is a configuration problem that should fail at startup, not
    a runtime surprise on the first request.
    """
