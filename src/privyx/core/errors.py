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
    """Raised when a provider transport fails.

    When the upstream answered with an error status, ``status_code``, ``body``,
    and ``content_type`` carry that response so a gateway can relay it instead
    of a bare 500.  They stay ``None``/empty for transport failures.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: bytes = b"",
        content_type: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.content_type = content_type


class StreamError(PrivyxError):
    """Raised when a streaming operation fails."""
