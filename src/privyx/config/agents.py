"""Resolve agent inheritance without executing tools or reading secret values."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from privyx.config._migrate import without_options_of_another_type
from privyx.config.schema import AgentConfig
from privyx.core import ConfigError

if TYPE_CHECKING:
    from privyx.config._layers import _Types


def resolve_profiles(
    profiles: dict[str, Any], sources: dict[str, str], types: _Types
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve final parents first, retaining the origin of inherited fields.

    An iterative walk handles long chains without Python recursion limits.
    Scalars and lists replace; env mappings merge, with a child's destination
    overriding its parent's even when it changes between env and env_from;
    ``budget`` and ``privyx`` merge deeply.
    """
    from privyx.config._layers import _deep_merge, _record_sources

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
            if child.get("type", parent.get("type")) != parent.get("type"):
                # Another type: the parent's options are its type's, not this one's.
                parent = {k: v for k, v in parent.items() if k in AgentConfig.model_fields}
            effective = {**parent, **child}
            for section in ("budget", "privyx"):
                own, inherited = child.get(section), parent.get(section)
                if isinstance(own, dict) and isinstance(inherited, dict):
                    operator = own.get("operator") if section == "privyx" else None
                    picked = operator.get("type") if isinstance(operator, dict) else None
                    if isinstance(picked, str):
                        inherited = without_options_of_another_type(
                            inherited, picked, partial(types.builtin, "operator")
                        )
                    effective[section] = _deep_merge(inherited, own, types)
            prefix = f"agents.{child_name}."
            inherited_origins = (
                profile_origins.get(parent_name, {}) if isinstance(parent_name, str) else {}
            )
            own_origins = dict(inherited_origins)
            _record_sources(child, "", own_origins, base=parent)
            for path, origin in own_origins.items():
                if not origin:
                    own_origins[path] = sources.get(prefix + path, "defaults")
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


def profile_type(name: str, profile: Mapping[str, Any], types: Collection[str]) -> str:
    """The agent type of profile ``name``: its ``type``, or one of ``types`` it is named like.

    Unset, it is the type named like the profile, else like its command's
    file, else ``env``.  ``extends`` has already given a profile its parent's.
    """
    kind = profile.get("type")
    if isinstance(kind, str) and kind:
        return kind
    command = profile.get("command")
    for candidate in (name, Path(command).name if isinstance(command, str) else ""):
        if candidate in types:
            return candidate
    return "env"
