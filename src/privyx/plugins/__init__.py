"""The module to write a Privyx component with.

Everything a plugin needs is imported from here: the base class of each
family, :class:`ComponentConfig` and :class:`BuildContext` for a component
with options, the result and session types, and :func:`restore`.  Other import
paths work too, but may move between releases.

:data:`PLUGINS` is the process-wide registry the ``build_*`` functions consult;
:func:`privyx.app._plugins.load_plugins` populates it.
"""

from __future__ import annotations

from privyx.core import ConfigError, Context, Detection, Session, TransformResult
from privyx.plugins.registry import (
    PLUGINS,
    BuildContext,
    ComponentConfig,
    PluginRegistry,
    Registry,
)
from privyx.privacy.anchor.base import BaseAnchor
from privyx.privacy.detector.base import BaseDetector
from privyx.privacy.operator.base import BaseOperator, restore
from privyx.privacy.policy.base import BasePolicy
from privyx.providers.base import BaseProvider
from privyx.token.model import LogicalToken
from privyx.vault.base import BaseVault

__all__ = [
    "PLUGINS",
    "BaseAnchor",
    "BaseDetector",
    "BaseOperator",
    "BasePolicy",
    "BaseProvider",
    "BaseVault",
    "BuildContext",
    "ComponentConfig",
    "ConfigError",
    "Context",
    "Detection",
    "LogicalToken",
    "PluginRegistry",
    "Registry",
    "Session",
    "TransformResult",
    "restore",
]

