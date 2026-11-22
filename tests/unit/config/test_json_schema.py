"""The published JSON Schema matches the settings, and every config example in the docs fits it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from privyx.app._schema import json_schema

jsonschema = pytest.importorskip("jsonschema")

DOCS = Path(__file__).parents[3] / "docs"


def _validator() -> Any:
    return jsonschema.Draft202012Validator(json_schema())


def test_published_schema_is_up_to_date() -> None:
    published = json.loads((DOCS / "schema.json").read_text(encoding="utf-8"))
    assert published == json_schema(), "run: privyx config schema > docs/schema.json"


def _examples() -> list[tuple[str, dict[str, Any]]]:
    found = []
    for page in sorted(DOCS.rglob("*.md")):
        for block in re.findall(r"```ya?ml\n(.*?)```", page.read_text(encoding="utf-8"), re.S):
            data = yaml.safe_load(block)
            if isinstance(data, dict):
                found.append((str(page.relative_to(DOCS)), data))
    return found


def test_every_docs_example_fits_the_schema() -> None:
    examples = _examples()
    validator = _validator()
    failures = [
        f"{page}: {error.message}"
        for page, data in examples
        for error in validator.iter_errors(data)
    ]
    assert examples and not failures


def test_a_misspelled_setting_does_not_fit() -> None:
    assert not _validator().is_valid({"vault": {"type": "memory"}, "sesion": {}})
    assert not _validator().is_valid({"session": {"strategy": "client", "strategey": "x"}})


def test_profile_settings_and_presets_are_checked_like_a_config_file() -> None:
    validator = _validator()
    assert validator.is_valid(
        {
            "agents": {"x": {"privyx": {"session": {"strategy": "client"}, "port": "${PORT}"}}},
            "presets": {"debug": {"log_level": "debug", "vault": {"type": "memory"}}},
        }
    )
    for config in (
        {"agents": {"x": {"privyx": {"sesion": {}}}}},
        {"agents": {"x": {"privyx": {"presets": {}}}}},
        {"agents": {"x": {"privyx": {"include": "more.yaml"}}}},
        {"presets": {"debug": {"log_levle": "debug"}}},
        {"presets": {"debug": {"provider": {"type": "openai"}}}},
        {"presets": {"debug": {"upstream_url": "https://example.com"}}},
    ):
        assert not validator.is_valid(config), config


def test_a_plugin_section_keeps_its_own_options() -> None:
    assert _validator().is_valid({"detector": {"type": "acme", "acme_threshold": 3}})


def test_references_fit_non_string_and_enum_settings() -> None:
    validator = _validator()
    assert validator.is_valid({"port": "${PORT:-8080}", "audit": {"enabled": "${AUDIT}"}})
    assert validator.is_valid({"session": {"strategy": "${STRATEGY:-client}"}})
    assert validator.is_valid({"proxy": {"mode": "${MODE}"}})
    assert not validator.is_valid({"proxy": {"mode": "sideways"}})


def test_id_length_has_the_bounds_the_loader_checks() -> None:
    validator = _validator()
    for kind in ("hash", "pseudonym", "encrypt"):
        for length, valid in ((5, False), (6, True), (64, True), (65, False), ("${N}", True)):
            config = {"operator": {"type": kind, "id_length": length}}
            assert validator.is_valid(config) is valid, (kind, length)
    assert validator.is_valid({"operator": {"type": "my_plugin", "length": 3}})
    # Renamed, and still read with a warning.
    assert validator.is_valid({"operator": {"type": "hash", "length": "${N}"}})
    assert not validator.is_valid({"operator": {"type": "pseudonym", "length": 8}})
