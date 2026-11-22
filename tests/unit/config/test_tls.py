"""Tests for TLS/SSL configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from privyx import load_config
from privyx.app import read_config
from privyx.config.schema import ServerConfig, Settings, TLSConfig


def test_tls_config_defaults() -> None:
    tls = TLSConfig()
    assert tls.certfile == ""
    assert tls.keyfile == ""
    assert not tls.enabled
    assert not Settings().server.tls.enabled


def test_tls_config_enabled_when_both_set() -> None:
    tls = TLSConfig(certfile="/path/to/cert.pem", keyfile="/path/to/key.pem")
    assert tls.enabled
    assert Settings(server=ServerConfig(tls=tls)).server.tls.enabled


@pytest.mark.parametrize("prefix", ["SSL", "TLS", "SERVER__TLS_"])
def test_tls_env_config(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    for name, value in (
        ("CERTFILE", "/path/to/cert.pem"),
        ("KEYFILE", "/path/to/key.pem"),
        ("KEYFILE_PASSWORD", "secret"),
        ("CA_CERTS", "/path/to/ca.pem"),
    ):
        monkeypatch.setenv(f"PRIVYX_{prefix}_{name}", value)

    tls = load_config().server.tls

    assert tls.enabled
    assert (tls.certfile, tls.keyfile) == ("/path/to/cert.pem", "/path/to/key.pem")
    assert (tls.keyfile_password, tls.ca_certs) == ("secret", "/path/to/ca.pem")


def test_tls_yaml_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "privyx_tls.yaml"
    cfg_file.write_text(
        "server:\n  tls:\n    certfile: /etc/ssl/cert.pem\n    keyfile: /etc/ssl/key.pem\n",
        encoding="utf-8",
    )
    tls = load_config(cfg_file).server.tls
    assert tls.enabled
    assert (tls.certfile, tls.keyfile) == ("/etc/ssl/cert.pem", "/etc/ssl/key.pem")


def test_top_level_tls_is_read_with_a_warning(tmp_path: Path) -> None:
    cfg_file = tmp_path / "privyx_tls.yaml"
    cfg_file.write_text(
        "tls:\n  certfile: /etc/ssl/cert.pem\n  keyfile: /etc/ssl/key.pem\n", encoding="utf-8"
    )
    warnings: list[str] = []

    loaded = read_config(cfg_file)
    warnings = loaded.warnings
    tls = loaded.settings.server.tls

    assert tls.certfile == "/etc/ssl/cert.pem"
    assert warnings == ["'tls' is deprecated; write 'server.tls' (removed in 0.4.0)"]
