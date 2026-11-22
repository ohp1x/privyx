"""Conformance suites: the checks a component of each kind must pass.

A plugin's tests subclass the suite of its component's kind, and pytest runs
them; Privyx's own components pass the same suites.
"""

from privyx.testing._suites import (
    AgentSuite,
    AnchorSuite,
    DetectorSuite,
    DialectSuite,
    FeatureSuite,
    OperatorSuite,
    PolicySuite,
    ProviderSuite,
    SecretResolverSuite,
    VaultSuite,
    audit_to,
    capture_events,
    listening,
    opened,
    services,
)

__all__ = [
    "AgentSuite",
    "AnchorSuite",
    "DetectorSuite",
    "DialectSuite",
    "FeatureSuite",
    "OperatorSuite",
    "PolicySuite",
    "ProviderSuite",
    "SecretResolverSuite",
    "VaultSuite",
    "audit_to",
    "capture_events",
    "listening",
    "opened",
    "services",
]
