"""The configs shipped in ``configs/`` load, and copying one breaks nothing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from privyx.config.loader import load_config
from privyx.config.schema import Settings
from privyx.core.builder import build_detector_from, build_policy_from
from privyx.core.context import Context

CONFIGS = Path(__file__).parents[3] / "configs"
DEFAULT = CONFIGS / "default.yaml"
STRICT = CONFIGS / "strict.yaml"
#: ``privyx run``'s own defaults, which a config file must not undo by accident.
RUN = {"session": {"strategy": "conversation"}, "anchor": {"secret": "auto"}}
#: A commented-out setting in ``default.yaml``: ``# key: value``, nested by two spaces.
SETTING = re.compile(r"^# ((?:  )*)([a-z_]+):(.*)$")


def _defaults(model: BaseModel, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    out: dict[tuple[str, ...], Any] = {}
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            out |= _defaults(value, (*prefix, name))
        else:
            out[(*prefix, name)] = value
    return out


def _documented() -> dict[tuple[str, ...], str]:
    out: dict[tuple[str, ...], str] = {}
    stack: list[str] = []
    for line in DEFAULT.read_text(encoding="utf-8").splitlines():
        if match := SETTING.match(line):
            stack = [*stack[: len(match[1]) // 2], match[2]]
            out[tuple(stack)] = match[3]
    return out


@pytest.mark.parametrize("path", sorted(CONFIGS.rglob("*.yaml")), ids=lambda p: p.name)
def test_every_shipped_config_loads(path: Path) -> None:
    load_config(path)


def test_default_yaml_sets_nothing() -> None:
    assert yaml.safe_load(DEFAULT.read_text(encoding="utf-8")) is None


def test_default_yaml_lists_every_setting_with_its_default() -> None:
    defaults = _defaults(Settings())
    documented = _documented()
    sections = {path[:i] for path in defaults for i in range(1, len(path))}

    assert defaults.keys() - documented.keys() == set(), "add these to configs/default.yaml"
    assert documented.keys() - defaults.keys() - sections == {("include",)}, "not settings"
    for path, value in documented.items():
        # A map or list given with no value, such as `routes` or `agents`.
        if path in defaults and value.strip():
            assert yaml.safe_load(value) == defaults[path], ".".join(path)


def test_strict_yaml_keeps_the_destination_and_run_defaults() -> None:
    plain = load_config(base_extra=RUN, agent="claude")
    strict = load_config(STRICT, base_extra=RUN, agent="claude")

    for name in ("upstream_url", "provider", "anchor", "session", "agents"):
        assert getattr(strict, name) == getattr(plain, name), name
    assert strict.proxy.passthrough_unknown is False


@pytest.mark.asyncio
async def test_strict_yaml_masks_your_own_terms() -> None:
    settings = load_config(STRICT, extra={"detector": {"terms": {"PERSON": ["ann"]}}})
    detection = await build_detector_from(settings).detect("ask ann", Context())
    decision = await build_policy_from(settings).decide(detection, Context())

    assert [s.entity_type for s in decision.spans] == ["PERSON"]
