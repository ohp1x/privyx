"""Config precedence, focused on ``base_extra`` (:func:`privyx.config.loader.load_config`).

Precedence, low → high: defaults < base_extra < YAML < env < extra.  ``base_extra``
is the channel ``privyx run`` uses for defaults the user must still be able to
override (e.g. a per-conversation session).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from privyx.config.loader import load_config
from privyx.core.builder import build_detector_from
from privyx.core.context import Context
from privyx.core.errors import ConfigError

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


async def test_yaml_detector_sees_only_its_own_patterns(tmp_path: Path) -> None:
    # The built-in patterns in the defaults used to be merged into it.
    cfg = tmp_path / "c.yaml"
    cfg.write_text("detector:\n  type: yaml\n  patterns: {CODE: 'X\\d+'}\n", encoding="utf-8")
    detector = build_detector_from(load_config(cfg))
    detection = await detector.detect("X12 mail a@b.io", Context())
    assert [span.entity_type for span in detection.spans] == ["CODE"]


async def test_regex_detector_keeps_the_builtin_patterns(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("detector:\n  type: regex\n  patterns: {CODE: 'X\\d+'}\n", encoding="utf-8")
    detector = build_detector_from(load_config(cfg))
    detection = await detector.detect("X12 mail a@b.io", Context())
    assert sorted(span.entity_type for span in detection.spans) == ["CODE", "EMAIL"]


def test_env_detector_cache_applies_to_each_listed_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It used to replace the whole list with one default detector, so a
    # configured LLM or Presidio detector silently stopped scanning.
    monkeypatch.setenv("PRIVYX_DETECTOR_CACHE", "false")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "detector:\n"
        "  - type: regex\n    terms: {PROJECT: [bluebird]}\n"
        "  - type: yaml\n    patterns: {CODE: 'X\\d+'}\n",
        encoding="utf-8",
    )
    settings = load_config(cfg)
    assert isinstance(settings.detector, list)
    assert settings.detector_type == "regex+yaml"
    assert settings.detector[0].terms == {"PROJECT": ["bluebird"]}
    assert [d.cache.enabled for d in settings.detector] == [False, False]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("detector:\n  term: {PERSON: [Zed Quill]}\n", "'detector.term' (did you mean 'terms'?)"),
        ("detectors:\n  - type: regex\n", "'detectors' (did you mean 'detector'?)"),
        (
            "detector:\n  - type: regex\n  - type: yaml\n    patern: {CODE: X}\n",
            "'detector[1].patern' (did you mean 'patterns'?)",
        ),
        (
            "detector:\n  cache: {max_sise: 5}\n",
            "'detector.cache.max_sise' (did you mean 'max_size'?)",
        ),
        ("vault:\n  type: sqlite\n  path: x.db\n", "unknown setting 'vault.path'"),
    ],
    ids=["terms", "detector", "list-item", "cache", "vault"],
)
def test_an_unknown_setting_fails_to_load(tmp_path: Path, text: str, message: str) -> None:
    # Each used to load with the key dropped, so the first masked no PERSON
    # while `privyx doctor` called it valid.
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=re.escape(message)) as caught:
        load_config(cfg)
    assert "Zed" not in str(caught.value)  # key paths only, never values
