"""Configuration: the settings, and the files, environment, and layers they are read from.

The loader is imported on first use (PEP 562), so a module that needs only the
settings model does not load YAML.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from privyx.core import lazy_attribute

if TYPE_CHECKING:
    from privyx.config._env import ALIASES, discovery_disabled, env_operator_types
    from privyx.config._files import (
        active_config_paths,
        find_local_configs,
        included_files,
        is_trusted,
        key_line,
        managed_file,
        read_yaml,
        trust,
    )
    from privyx.config._keys import Key, env_name, parse, resolve, typed
    from privyx.config._layers import Loaded, load_config, read_config
    from privyx.config._migrate import detectors_of, migrate
    from privyx.config._presets import CHOSEN_PRESETS

__all__ = [
    "ALIASES",
    "CHOSEN_PRESETS",
    "Key",
    "Loaded",
    "active_config_paths",
    "detectors_of",
    "discovery_disabled",
    "env_name",
    "env_operator_types",
    "find_local_configs",
    "included_files",
    "is_trusted",
    "key_line",
    "load_config",
    "managed_file",
    "migrate",
    "parse",
    "read_config",
    "read_yaml",
    "resolve",
    "trust",
    "typed",
]

_LAZY = {
    "ALIASES": "privyx.config._env:ALIASES",
    "discovery_disabled": "privyx.config._env:discovery_disabled",
    "env_operator_types": "privyx.config._env:env_operator_types",
    "active_config_paths": "privyx.config._files:active_config_paths",
    "find_local_configs": "privyx.config._files:find_local_configs",
    "included_files": "privyx.config._files:included_files",
    "is_trusted": "privyx.config._files:is_trusted",
    "key_line": "privyx.config._files:key_line",
    "managed_file": "privyx.config._files:managed_file",
    "read_yaml": "privyx.config._files:read_yaml",
    "trust": "privyx.config._files:trust",
    "Key": "privyx.config._keys:Key",
    "env_name": "privyx.config._keys:env_name",
    "parse": "privyx.config._keys:parse",
    "resolve": "privyx.config._keys:resolve",
    "typed": "privyx.config._keys:typed",
    "Loaded": "privyx.config._layers:Loaded",
    "load_config": "privyx.config._layers:load_config",
    "read_config": "privyx.config._layers:read_config",
    "migrate": "privyx.config._migrate:migrate",
    "detectors_of": "privyx.config._migrate:detectors_of",
    "CHOSEN_PRESETS": "privyx.config._presets:CHOSEN_PRESETS",
}


def __getattr__(name: str) -> Any:
    """PEP 562: ``from privyx.config import load_config`` imports the loader on first use."""
    return lazy_attribute(name, _LAZY, __name__)
