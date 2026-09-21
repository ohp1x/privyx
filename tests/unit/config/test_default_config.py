"""The secret patterns shipped in ``configs/default.yaml`` mask what they claim to."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from privyx.config.loader import load_config
from privyx.core.builder import build_detector_from
from privyx.core.context import Context

CONFIG = Path(__file__).parents[3] / "configs" / "default.yaml"
DETECTOR = build_detector_from(load_config(CONFIG))

# Fake values, assembled here so the source holds no literal key-shaped string.
A = "Zq7Xw2Lp9Rt4Vb6Nm1Kc8Hd3Js5Gf0Ay"
HEX = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
PEM = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 2

# (text, secret that must not survive)
LEAKS = [
    (f"key sk-proj-{A}", A),
    (f"sk-ant-api03-{A}-AAAA", A),
    ("gh" + f"p_{A}ABCD", A),
    ("AKIA" + "IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
    ("AIza" + f"{A}ABC", A),
    ("xo" + f"xb-123456789012-{A}", A),
    ("sk" + f"_live_{A}", A),
    (f"keys:\\nsk-proj-{A}", A),  # right after a JSON-escaped newline
    (f"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.{A}", A),
    (f"-----BEGIN RSA PRIVATE KEY-----\n{PEM}\n-----END RSA PRIVATE KEY-----", PEM),
    (f"-----BEGIN OPENSSH PRIVATE KEY-----\n{PEM}\n[truncated]", PEM),
    (f'curl -H "Authorization: Bearer {HEX}"', HEX),
    ("redis://:R3disPass99@cache:6379/0", "R3disPass99"),
    ("DB_PASSWORD=hunter2rocks", "hunter2rocks"),
    ('MAIL_PASSWORD="correct horse battery"', "horse battery"),
    ("APP_KEY=base64:Q2hhbmdlTWVQbGVhc2U=", "Q2hhbmdlTWVQbGVhc2U="),
    ("  password: yamlSecret42", "yamlSecret42"),
    (json.dumps({"api_key": "json-secret-value-123"}), "json-secret-value-123"),
    ("'password' => 'phpSecretPw',", "phpSecretPw"),
    ("const apiKey = 'camelKey123456';", "camelKey123456"),
    (f"https://api.example.com/v1?api_key={HEX}&q=1", HEX),
    ("mysql --password=cliPass123 -h db", "cliPass123"),
]

KEEP = [
    "task-runner-something-long and scikit-learn",
    "def login(password: str) -> bool:",
    "interface User { password: string; token: string }",
    "max_tokens = 4096",
    "token = get_token()",
    "password: ${{ secrets.DB_PASSWORD }}",
    "The access token expires after an hour.",
]


async def _mask(text: str) -> str:
    detection = (await DETECTOR.detect(text, Context())).merged(text)
    for span in reversed(detection.spans):
        text = text[: span.start] + "<X>" + text[span.end :]
    return text


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "secret"), LEAKS)
async def test_secret_is_masked(text: str, secret: str) -> None:
    assert secret not in await _mask(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", KEEP)
async def test_code_and_prose_are_kept(text: str) -> None:
    assert await _mask(text) == text


@pytest.mark.asyncio
async def test_only_the_value_is_masked() -> None:
    assert await _mask("DB_PASSWORD=hunter2rocks") == "DB_PASSWORD=<X>"
