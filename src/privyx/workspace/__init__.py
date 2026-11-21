"""The working directory: what it tells about its people and secrets, and its git hooks.

``privyx scan`` and ``privyx run --harvest`` read it; ``privyx hook`` checks
what is committed to it.  Commands reach this package through the app's
workspace service.
"""

from privyx.workspace._githook import Installed, added_lines, findings, install_hooks, staged_diff
from privyx.workspace._harvest import Found, harvest
from privyx.workspace._scan import TERMS_FILE, Added, Scan, add, env_pattern, is_env, scan

__all__ = [
    "TERMS_FILE",
    "Added",
    "Found",
    "Installed",
    "Scan",
    "add",
    "added_lines",
    "env_pattern",
    "findings",
    "harvest",
    "install_hooks",
    "is_env",
    "scan",
    "staged_diff",
]
