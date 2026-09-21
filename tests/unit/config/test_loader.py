"""Config precedence, focused on ``base_extra`` (:func:`privyx.config.loader.load_config`).

Precedence, low → high: defaults < base_extra < YAML < env < extra.  ``base_extra``
is the channel ``privyx run`` uses for defaults the user must still be able to
override (e.g. a per-conversation session).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from privyx.config.loader import load_config

_CONVO = {"session": {"strategy": "conversation"}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the developer's own PRIVYX_* shell vars out of these precedence tests.
    monkeypatch.delenv("PRIVYX_SESSION_STRATEGY", raising=False)
    monkeypatch.delenv("PRIVYX_CONFIG", raising=False)


def test_base_extra_applies_when_nothing_overrides() -> None:
    assert load_config(base_extra=_CONVO).session.strategy == "conversation"


def test_yaml_overrides_base_extra(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("session:\n  strategy: client\n", encoding="utf-8")
    assert load_config(cfg, base_extra=_CONVO).session.strategy == "client"


def test_env_overrides_base_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_SESSION_STRATEGY", "client")
    assert load_config(base_extra=_CONVO).session.strategy == "client"


def test_extra_overrides_base_extra() -> None:
    settings = load_config(extra={"session": {"strategy": "ephemeral"}}, base_extra=_CONVO)
    assert settings.session.strategy == "ephemeral"


def test_base_extra_leaves_unrelated_defaults_intact(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("policy:\n  type: strict\n", encoding="utf-8")
    settings = load_config(cfg, base_extra=_CONVO)
    assert settings.session.strategy == "conversation"
    assert settings.policy.type == "strict"
    assert settings.operator.type == "pseudonym"  # untouched default


def test_detector_accepts_a_list(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "detector:\n  - type: regex\n  - type: yaml\n    patterns: {CODE: 'X\\d+'}\n",
        encoding="utf-8",
    )
    settings = load_config(cfg)
    assert isinstance(settings.detector, list)
    assert settings.detector_type == "regex+yaml"
