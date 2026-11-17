"""Each built-in type has settings of its own, checked in the schema and warned about at load."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from privyx.cli.main import cli
from privyx.config.loader import load_config
from privyx.config.schema import (
    AnchorConfig,
    DetectorConfig,
    OperatorConfig,
    PolicyConfig,
    ProviderConfig,
    VaultConfig,
    json_schema,
)
from privyx.core.builder import ANCHORS, OPERATORS, VAULTS
from privyx.core.errors import ConfigError
from privyx.privacy.detector.yaml import DETECTORS
from privyx.privacy.policy.loader import POLICIES
from privyx.providers.registry import default_registry


def test_every_builtin_type_has_settings_and_a_registry_entry() -> None:
    assert set(DetectorConfig.builtin_types) == set(DETECTORS.names())
    assert set(PolicyConfig.builtin_types) == set(POLICIES.names())
    assert set(OperatorConfig.builtin_types) == set(OPERATORS.names())
    assert set(AnchorConfig.builtin_types) == set(ANCHORS.names())
    assert set(VaultConfig.builtin_types) == set(VAULTS.names())
    assert set(ProviderConfig.builtin_types) == set(default_registry().names())


def test_the_section_is_the_union_of_its_types_settings() -> None:
    sections = (DetectorConfig, PolicyConfig, OperatorConfig, AnchorConfig, VaultConfig)
    for model in (*sections, ProviderConfig):
        used = {"type", *model.shared_fields}
        for kind in model.builtin_types:
            used |= model.type_fields(kind)
        assert used == set(model.model_fields), model.__name__


jsonschema = pytest.importorskip("jsonschema")


@pytest.mark.parametrize(
    ("config", "valid"),
    [
        ({"detector": {"type": "presidio", "language": "id"}}, True),
        ({"detector": {"type": "presidio", "terms": {"PROJECT": ["acme"]}}}, False),
        ({"detector": {"type": "regex", "language": "id"}}, False),
        ({"detector": {"language": "id"}}, False),  # the default type is regex
        ({"detector": {"terms": {"PROJECT": ["acme"]}, "cache": False}}, True),
        ({"detector": [{"type": "regex", "terms": {}}, {"type": "llm", "decode": True}]}, False),
        ({"detector": {"type": "${DETECTOR}", "language": "id"}}, True),
        ({"vault": {"redis_url": "redis://localhost:6379/0"}}, False),
        ({"vault": {"type": "redis", "redis_url": "redis://localhost:6379/0", "ttl": 60}}, True),
        ({"operator": {"type": "hash", "seed": 3}}, False),
        ({"operator": {"type": "fpe", "seed": 3}}, True),
        ({"policy": {"type": "strict", "allowed": ["EMAIL"]}}, True),
        ({"policy": {"allowed": ["EMAIL"]}}, False),
        ({"provider": {"type": "anthropic", "openai_api_key": "x", "base_url": "http://h"}}, True),
    ],
)
def test_the_schema_takes_only_the_settings_of_the_type(config: dict, valid: bool) -> None:
    assert jsonschema.Draft202012Validator(json_schema()).is_valid(config) is valid


def _load(tmp_path: Path, **files: str) -> list[str]:
    for name, text in files.items():
        (tmp_path / f"{name}.yaml").write_text(text)
    warnings: list[str] = []
    load_config(tmp_path / "top.yaml", warnings=warnings)
    return [w.replace(str(tmp_path.resolve()) + "/", "") for w in warnings]


GLOBAL_TERMS = "detector:\n  type: regex\n  terms: {PROJECT: [kestrel]}\n  language: id\n"


def test_a_key_of_another_type_in_the_same_layer_warns(tmp_path: Path) -> None:
    warnings = _load(
        tmp_path,
        top="detector: {type: presidio, terms: {PROJECT: [kestrel]}, decode: true}\n"
        "vault: {redis_url: 'redis://localhost:6379/0'}\n",
    )

    assert warnings == [
        "'vault.redis_url' from top.yaml is not used by vault type 'memory'; "
        "a later release will refuse it",
        "'detector.terms' from top.yaml is not used by detector type 'presidio'; "
        "a later release will refuse it",
        "'detector.decode' from top.yaml is not used by detector type 'presidio'; "
        "a later release will refuse it",
    ]
    assert "kestrel" not in str(warnings)


def test_global_terms_under_another_type_above_warn(tmp_path: Path) -> None:
    warnings = _load(
        tmp_path, base=GLOBAL_TERMS, top="include: base.yaml\ndetector: {type: presidio}\n"
    )

    assert warnings == [
        "'PROJECT' in 'detector.terms' from base.yaml reaches no detector that uses it, "
        "so it is not masked: detector type 'presidio' from top.yaml does not use it"
    ]


def test_a_detector_list_above_without_the_terms_warns(tmp_path: Path) -> None:
    warnings = _load(
        tmp_path,
        base=GLOBAL_TERMS + "  enable: [IBAN]\n",
        top="include: base.yaml\ndetector: [{type: regex}, {type: presidio}]\n",
    )

    replaced = "the detector list from top.yaml replaces it"
    assert warnings == [
        "'PROJECT' in 'detector.terms' from base.yaml reaches no detector that uses it, "
        f"so it is not masked: {replaced}",
        "'IBAN' in 'detector.enable' from base.yaml reaches no detector that uses it, "
        f"so it is not masked: {replaced}",
    ]


def test_terms_for_the_same_entity_above_do_not_warn(tmp_path: Path) -> None:
    top = "include: base.yaml\ndetector:\n  - {type: regex, terms: {PROJECT: [falcon]}}\n"
    top += "  - {type: presidio}\n"

    assert _load(tmp_path, base=GLOBAL_TERMS, top=top) == []


@pytest.mark.parametrize(
    "item",
    ["{type: regex, enable: [CRYPTO_WALLET]}", "{type: regex, patterns: {CRYPTO_WALLET: x}}"],
)
def test_terms_replaced_by_another_setting_of_the_entity_warn(tmp_path: Path, item: str) -> None:
    base = "detector: {type: regex, terms: {CRYPTO_WALLET: [kestrel]}}\n"
    top = f"include: base.yaml\ndetector: [{item}]\n"

    assert _load(tmp_path, base=base, top=top) == [
        "'CRYPTO_WALLET' in 'detector.terms' from base.yaml reaches no detector that uses "
        "it, so it is not masked: the detector list from top.yaml replaces it"
    ]


def test_a_malformed_type_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / "top.yaml").write_text("detector: {type: [regex], terms: {PROJECT: [kestrel]}}\n")

    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(tmp_path / "top.yaml")


def test_emptied_terms_above_do_not_warn(tmp_path: Path) -> None:
    top = "include: base.yaml\ndetector: {type: regex, terms: {PROJECT: []}}\n"

    assert _load(tmp_path, base=GLOBAL_TERMS, top=top) == []


def test_other_keys_from_a_layer_below_are_dropped_without_a_word(tmp_path: Path) -> None:
    base = "detector: {type: presidio, language: id}\nvault: {type: sqlite, dsn: /tmp/x.db}\n"
    top = "include: base.yaml\ndetector: {type: regex}\nvault: {type: memory}\n"

    assert _load(tmp_path, base=base, top=top) == []


def test_load_logs_each_warning_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "top.yaml"
    path.write_text("detector: {type: llm, normalize: true}\n")

    with caplog.at_level(logging.WARNING, logger="privyx"):
        load_config(path)
        load_config(path)

    assert [r.getMessage() for r in caplog.records] == [
        f"'detector.normalize' from {path.resolve()} is not used by detector type 'llm'; "
        "a later release will refuse it"
    ]


def test_lint_reports_the_warnings(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text(GLOBAL_TERMS)
    (tmp_path / "top.yaml").write_text("include: base.yaml\ndetector: {type: presidio}\n")

    result = CliRunner().invoke(cli, ["lint", "-c", str(tmp_path / "top.yaml")])

    assert result.output.splitlines()[0] == (
        f"warning: 'PROJECT' in 'detector.terms' from {(tmp_path / 'base.yaml').resolve()} "
        "reaches no detector that uses it, so it is not masked: "
        f"detector type 'presidio' from {(tmp_path / 'top.yaml').resolve()} does not use it"
    )
