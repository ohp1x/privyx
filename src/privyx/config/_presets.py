"""Presets and a profile's ``privyx`` settings: partial configs applied above the files."""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from privyx.config._interpolate import _settle
from privyx.config._unused import _raise_unknown, _unknown_keys
from privyx.config.schema import Settings
from privyx.core import ConfigError

if TYPE_CHECKING:
    from privyx.config._layers import _Types


#: The presets ``--preset`` chose for the running command.  A context variable,
#: not an environment variable: a process the command starts, such as an agent
#: calling ``privyx`` in turn, does not inherit them.
CHOSEN_PRESETS: ContextVar[tuple[str, ...]] = ContextVar("privyx_presets", default=())


def _check_overrides(profiles: dict[str, Any], types: _Types) -> None:
    """Reject a key a profile's ``privyx`` settings do not take, as for a config file."""
    unknown: list[str] = []
    for name, profile in profiles.items():
        override = profile.get("privyx") if isinstance(profile, dict) else None
        if isinstance(override, dict):  # the wrong shape is pydantic's to report
            unknown += _check_patch(
                override,
                f"agents.{name}.privyx.",
                ("agents", "presets"),
                "profiles and presets cannot be set per profile",
                types,
            )
    _raise_unknown(unknown)


#: What a preset cannot set: where requests go and with which credentials stays
#: a profile's, and presets neither nest nor define profiles.
_NOT_IN_PRESET = ("agents", "presets", "include", "upstream_url", "provider")


def _check_presets(merged: dict[str, Any], chosen: Sequence[str], types: _Types) -> None:
    """Check every preset as a config file is checked, and every name that picks one.

    Done whichever profile runs, so a typo shows before the preset is needed.
    Only key paths and preset names are reported, never values.
    """
    defined = merged.get("presets")
    if not isinstance(defined, dict):
        defined = {}  # a wrong shape is pydantic's to report
    unknown: list[str] = []
    for name, preset in defined.items():
        if isinstance(preset, dict):
            unknown += _check_patch(
                preset,
                f"presets.{name}.",
                _NOT_IN_PRESET,
                "a preset cannot set the destination, profiles, or other presets",
                types,
            )
    _raise_unknown(unknown)
    profiles = merged.get("agents")
    uses = [
        (f"in {f'agents.{name}.presets'!r}", profile.get("presets"))
        for name, profile in (profiles.items() if isinstance(profiles, dict) else [])
        if isinstance(profile, dict)
    ]
    for where, names in [*uses, ("in --preset", list(chosen))]:
        for name in names if isinstance(names, list) else []:
            if isinstance(name, str) and name not in defined:
                match = difflib.get_close_matches(name, [str(n) for n in defined], n=1)
                hint = f" (did you mean {match[0]!r}?)" if match else ""
                raise ConfigError(f"unknown preset {name!r}{hint} {where}")


def _preset_layers(merged: dict[str, Any], names: Sequence[str]) -> list[tuple[str, Any]]:
    """The layers that apply the presets ``names``, in order, their references resolved."""
    defined = merged.get("presets") or {}
    return [
        (f"presets.{name}", _settle(defined[name], expand=True))
        for name in names
        if isinstance(defined.get(name), dict)
    ]


def _check_patch(
    patch: dict[str, Any], prefix: str, forbidden: tuple[str, ...], why: str, types: _Types
) -> list[str]:
    """The keys of ``patch``, a partial config, that it cannot take or that are unknown."""
    found = [f"{prefix + key!r} ({why})" for key in forbidden if key in patch]
    allowed = {key: value for key, value in patch.items() if key not in forbidden}
    return found + _unknown_keys(Settings, allowed, types, prefix)
