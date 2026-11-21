"""Configuration: the settings, and the files, environment, and layers they are read from.

The loader is imported on first use (PEP 562), so a module that needs only the
settings model does not load YAML.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute

if TYPE_CHECKING:
    from privyx.config._files import (
        active_config_paths,
        find_local_configs,
        included_files,
        is_trusted,
        read_yaml,
        trust,
    )
    from privyx.config._layers import load_config
    from privyx.config._presets import CHOSEN_PRESETS

__all__ = [
    "CHOSEN_PRESETS",
    "active_config_paths",
    "find_local_configs",
    "included_files",
    "is_trusted",
    "load_config",
    "read_yaml",
    "trust",
]

_LAZY = {
    "active_config_paths": "privyx.config._files:active_config_paths",
    "find_local_configs": "privyx.config._files:find_local_configs",
    "included_files": "privyx.config._files:included_files",
    "is_trusted": "privyx.config._files:is_trusted",
    "read_yaml": "privyx.config._files:read_yaml",
    "trust": "privyx.config._files:trust",
    "load_config": "privyx.config._layers:load_config",
    "CHOSEN_PRESETS": "privyx.config._presets:CHOSEN_PRESETS",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.config import load_config`` imports the loader on first use."""
    return lazy_attribute(name, _LAZY, __name__)
