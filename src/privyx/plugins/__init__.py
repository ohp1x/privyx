"""The module to write a Privyx component with.

Everything a plugin needs is imported from here: the base class of each
family, the result and session types, and :func:`restore`.  Those names
are loaded on first use, so reading :data:`PLUGINS` (which every ``build_*``
function does) does not import every component base class.  Other import paths
work too, but may move between releases.

:data:`PLUGINS` is the process-wide registry the ``build_*`` functions consult;
:func:`privyx.plugins.loader.load_plugins` populates it.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from privyx.plugins.hooks import HookManager
from privyx.plugins.registry import PLUGINS, PluginRegistry, Registry

if TYPE_CHECKING:
    from privyx.core.context import Context
    from privyx.core.errors import ConfigError
    from privyx.core.result import Detection, TransformResult
    from privyx.core.session import Session
    from privyx.privacy.anchor.base import BaseAnchor
    from privyx.privacy.detector.base import BaseDetector
    from privyx.privacy.operator.base import BaseOperator, restore
    from privyx.privacy.policy.base import BasePolicy
    from privyx.providers.base import BaseProvider
    from privyx.token.model import LogicalToken
    from privyx.vault.base import BaseVault

#: Name -> the module it is loaded from on first use.
_LAZY = {
    "BaseAnchor": "privyx.privacy.anchor.base",
    "BaseDetector": "privyx.privacy.detector.base",
    "BaseOperator": "privyx.privacy.operator.base",
    "BasePolicy": "privyx.privacy.policy.base",
    "BaseProvider": "privyx.providers.base",
    "BaseVault": "privyx.vault.base",
    "ConfigError": "privyx.core.errors",
    "Context": "privyx.core.context",
    "Detection": "privyx.core.result",
    "LogicalToken": "privyx.token.model",
    "Session": "privyx.core.session",
    "TransformResult": "privyx.core.result",
    "restore": "privyx.privacy.operator.base",
}

__all__ = [
    "PLUGINS",
    "BaseAnchor",
    "BaseDetector",
    "BaseOperator",
    "BasePolicy",
    "BaseProvider",
    "BaseVault",
    "ConfigError",
    "Context",
    "Detection",
    "HookManager",
    "LogicalToken",
    "PluginRegistry",
    "Registry",
    "Session",
    "TransformResult",
    "restore",
]


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
