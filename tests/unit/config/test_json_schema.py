"""The published JSON Schema matches the settings, and every config example in the docs fits it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from privyx.config.schema import json_schema

jsonschema = pytest.importorskip("jsonschema")

DOCS = Path(__file__).parents[3] / "docs"


def _validator() -> Any:
    return jsonschema.Draft202012Validator(json_schema())


def test_published_schema_is_up_to_date() -> None:
    published = json.loads((DOCS / "schema.json").read_text(encoding="utf-8"))
    assert published == json_schema(), "run: privyx config --schema > docs/schema.json"


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


def test_a_plugin_section_keeps_its_own_options() -> None:
    assert _validator().is_valid({"detector": {"type": "acme", "acme_threshold": 3}})


def test_references_fit_non_string_and_enum_settings() -> None:
    validator = _validator()
    assert validator.is_valid({"port": "${PORT:-8080}", "audit": {"enabled": "${AUDIT}"}})
    assert validator.is_valid({"session": {"strategy": "${STRATEGY:-client}"}})
    assert validator.is_valid({"proxy": {"mode": "${MODE}"}})
    assert not validator.is_valid({"proxy": {"mode": "sideways"}})
