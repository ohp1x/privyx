"""Resolve agent inheritance without executing tools or reading secret values."""

from __future__ import annotations

import re
from typing import Any

from privyx.core.errors import ConfigError


def resolve_profiles(
    profiles: dict[str, Any], sources: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve final parents first, retaining the origin of inherited fields.

    An iterative walk handles long chains without Python recursion limits.
    Scalars and lists replace; env mappings merge, with a child's destination
    overriding its parent's even when it changes between env and env_from;
    ``budget`` and ``privyx`` merge deeply.
    """
    from privyx.config.loader import _deep_merge, _record_sources

    resolved: dict[str, Any] = {}
    origins: dict[str, str] = {}
    profile_origins: dict[str, dict[str, str]] = {}
    for name in profiles:
        chain: list[str] = []
        seen: set[str] = set()
        current = name
        while current not in resolved:
            if current in seen:
                raise ConfigError(f"agent inheritance cycle: {' -> '.join([*chain, current])}")
            if current not in profiles:
                raise ConfigError(f"agent {chain[-1]!r} extends unknown profile {current!r}")
            seen.add(current)
            chain.append(current)
            profile = profiles[current]
            parent = profile.get("extends") if isinstance(profile, dict) else None
            if not isinstance(parent, str) or not parent:
                break  # Schema validation reports malformed profiles/extends.
            current = parent

        for child_name in reversed(chain):
            child = profiles[child_name]
            if not isinstance(child, dict):
                resolved[child_name] = child
                continue
            parent_name = child.get("extends")
            parent = resolved.get(parent_name, {}) if isinstance(parent_name, str) else {}
            if not isinstance(parent, dict):
                parent = {}  # The parent's schema error is reported by Settings.
            effective = {**parent, **child}
            for section in ("budget", "privyx"):
                own, inherited = child.get(section), parent.get(section)
                if isinstance(own, dict) and isinstance(inherited, dict):
                    effective[section] = _deep_merge(inherited, own)
            prefix = f"agents.{child_name}."
            inherited_origins = (
                profile_origins.get(parent_name, {}) if isinstance(parent_name, str) else {}
            )
            own_origins = dict(inherited_origins)
            _record_sources(child, "", own_origins, base=parent)
            for path, origin in own_origins.items():
                if not origin:
                    # A mapping override of an inherited detector list has no index
                    # in the child's original source paths.
                    unindexed = re.sub(r"(^|\.)detector\.\d+\.", r"\1detector.", path)
                    own_origins[path] = sources.get(
                        prefix + path, sources.get(prefix + unindexed, "defaults")
                    )
            for field in ("env", "env_from"):
                other = "env_from" if field == "env" else "env"
                own = child.get(field, {})
                inherited = parent.get(field, {})
                opposite = child.get(other, {})
                if isinstance(own, dict) and isinstance(inherited, dict):
                    mapping = {**inherited, **own}
                    if isinstance(opposite, dict):
                        for key in opposite:
                            if key not in own:
                                mapping.pop(key, None)
                                own_origins.pop(field + "." + key, None)
                    effective[field] = mapping
            resolved[child_name] = effective
            profile_origins[child_name] = own_origins
            origins.update({prefix + path: source for path, source in own_origins.items()})
    return resolved, origins
