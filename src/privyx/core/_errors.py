"""Domain exceptions for the Privyx privacy engine."""

from __future__ import annotations


class PrivyxError(Exception):
    """Base exception for all Privyx errors."""


class ConfigError(PrivyxError):
    """Raised when configuration is invalid."""


class VaultError(PrivyxError):
    """Raised when a vault operation fails."""


class SessionNotFoundError(VaultError):
    """Raised when a session does not exist in the vault."""


class SessionBusyError(VaultError):
    """A session's lock was not free in time: another request is using it."""


class DetectorError(PrivyxError):
    """Raised when a detector fails."""


class OperatorError(PrivyxError):
    """Raised when an operator fails."""


class LeakError(PrivyxError):
    """Raised when a masked request still holds a value it masked elsewhere."""
