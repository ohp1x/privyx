"""Tests for TLS/SSL configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from privyx.config.env import env_config
from privyx.config.loader import load_config
from privyx.config.schema import Settings, TLSConfig


def test_tls_config_defaults() -> None:
    tls = TLSConfig()
    assert tls.certfile == ""
    assert tls.keyfile == ""
    assert not tls.enabled

    settings = Settings()
    assert not settings.is_tls


def test_tls_config_enabled_when_both_set() -> None:
    tls = TLSConfig(certfile="/path/to/cert.pem", keyfile="/path/to/key.pem")
    assert tls.enabled

    settings = Settings(tls=tls)
    assert settings.is_tls


def test_tls_env_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVYX_SSL_CERTFILE", "/path/to/cert.pem")
    monkeypatch.setenv("PRIVYX_SSL_KEYFILE", "/path/to/key.pem")
    monkeypatch.setenv("PRIVYX_SSL_KEYFILE_PASSWORD", "secret")
    monkeypatch.setenv("PRIVYX_SSL_CA_CERTS", "/path/to/ca.pem")

    cfg = env_config()
    assert cfg["tls"]["certfile"] == "/path/to/cert.pem"
    assert cfg["tls"]["keyfile"] == "/path/to/key.pem"
    assert cfg["tls"]["keyfile_password"] == "secret"
    assert cfg["tls"]["ca_certs"] == "/path/to/ca.pem"

    settings = load_config()
    assert settings.is_tls
    assert settings.tls.certfile == "/path/to/cert.pem"
    assert settings.tls.keyfile == "/path/to/key.pem"


def test_tls_yaml_config(tmp_path: Path) -> None:
    cfg_file = tmp_path / "privyx_tls.yaml"
    cfg_file.write_text(
        "tls:\n  certfile: /etc/ssl/cert.pem\n  keyfile: /etc/ssl/key.pem\n",
        encoding="utf-8",
    )
    settings = load_config(cfg_file)
    assert settings.is_tls
    assert settings.tls.certfile == "/etc/ssl/cert.pem"
    assert settings.tls.keyfile == "/etc/ssl/key.pem"
