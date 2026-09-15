"""Local plugin system — discover and register components from configured paths.

:data:`PLUGINS` is the process-wide registry the ``build_*`` functions consult;
:func:`privyx.plugins.loader.load_plugins` populates it.  ``load_plugins`` is
imported from :mod:`privyx.plugins.loader` directly (not re-exported here) so
that merely reading :data:`PLUGINS` does not pull in every component base class.
"""

from privyx.plugins.hooks import HookManager
from privyx.plugins.registry import PLUGINS, PluginRegistry, Registry

__all__ = ["PLUGINS", "HookManager", "PluginRegistry", "Registry"]
