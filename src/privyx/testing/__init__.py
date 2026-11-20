"""Conformance suites: the checks a component of each kind must pass.

A plugin's tests subclass the suite of its component's kind, and pytest runs
them; Privyx's own components pass the same suites.
"""

from privyx.testing._suites import (
    AnchorSuite,
    DetectorSuite,
    OperatorSuite,
    PolicySuite,
    ProviderSuite,
    VaultSuite,
    opened,
    services,
)

__all__ = [
    "AnchorSuite",
    "DetectorSuite",
    "OperatorSuite",
    "PolicySuite",
    "ProviderSuite",
    "VaultSuite",
    "opened",
    "services",
]
