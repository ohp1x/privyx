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


class DetectorError(PrivyxError):
    """Raised when a detector fails."""


class OperatorError(PrivyxError):
    """Raised when an operator fails."""


class PolicyError(PrivyxError):
    """Raised when a policy is invalid or fails."""


class ProviderError(PrivyxError):
    """Raised when a provider transport fails."""


class StreamError(PrivyxError):
    """Raised when a streaming operation fails."""
