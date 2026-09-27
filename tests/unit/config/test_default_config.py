"""Secret patterns mask what they claim to: the built-ins with no config at all,
and ``SECRET`` from ``configs/default.yaml``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from privyx.config.loader import load_config
from privyx.core.builder import build_detector_from, build_policy_from
from privyx.core.context import Context
from privyx.privacy.detector.base import Detector
from privyx.privacy.policy.base import Policy
from privyx.privacy.policy.strict import StrictPolicy

CONFIGS = Path(__file__).parents[3] / "configs"
NO_CONFIG = build_detector_from(load_config(None))
DETECTOR = build_detector_from(load_config(CONFIGS / "default.yaml"))

# Fake values, assembled here so the source holds no literal key-shaped string.
A = "Zq7Xw2Lp9Rt4Vb6Nm1Kc8Hd3Js5Gf0Ay"
HEX = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
PEM = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 2

# One key per vendor the built-in API_KEY pattern knows.
KEYS = [
    f"sk-proj-{A}",
    f"sk-ant-api03-{A}-AAAA",
    "sk" + f"_live_{A}",
    "rk" + f"_test_{A}",
    "whsec" + f"_{A}",
    "gh" + f"p_{A}ABCD",
    "github" + f"_pat_{A}",
    "gl" + f"pat-{A}",
    "AKIA" + "IOSFODNN7EXAMPLE",
    "AIza" + f"{A}ABC",
    "GOCSPX" + f"-{A}",
    "ya29" + f".{A}",
    "xo" + f"xb-123456789012-{A}",
    "xapp" + f"-1-{A}",
    "hf" + f"_{A}",
    "gsk" + f"_{A}{A}",
    "xai" + f"-{A}{A}",
    "r8" + f"_{A}",
    "pplx" + f"-{A}{A}",
    "nvapi" + f"-{A}{A}",
    "npm" + f"_{A}ABCD",
    "pypi-AgEIcHlwaS5vcmc" + A + A,
    "SG" + f".{A}.{A}",
    "do" + f"p_v1_{HEX}{HEX}",
    "shp" + f"at_{HEX}",
    "ATATT3" + A * 4,
    "lin" + f"_api_{A}ABCDEFGH",
    "sbp" + f"_{HEX}01234567",
    "sntry" + f"s_{A}{A}",
    "AGE-SECRET-" + "KEY-1" + (A.upper() * 2)[:58],
    "PMAK" + f"-{HEX[:24]}-{HEX}ab",
    "123456789" + f":AA{A}x",  # Telegram bot
    "hooks.slack.com" + f"/services/T0/B0/{A}",
    "discord.com" + f"/api/webhooks/123/{A}",
]

# (text, secret that must not survive), caught with no config
BUILTIN_LEAKS = [
    (f"keys:\\nsk-proj-{A}", A),  # right after a JSON-escaped newline
    (f"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.{A}", A),
    (f"-----BEGIN RSA PRIVATE KEY-----\n{PEM}\n-----END RSA PRIVATE KEY-----", PEM),
    (f"-----BEGIN OPENSSH PRIVATE KEY-----\n{PEM}\n[truncated]", PEM),
    (f'curl -H "Authorization: Bearer {HEX}"', HEX),
    ("redis://:R3disPass99@cache:6379/0", "R3disPass99"),
]

# ... and the ones that need `SECRET` from configs/default.yaml
LEAKS = [
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


async def _mask(text: str, detector: Detector = DETECTOR) -> str:
    detection = (await detector.detect(text, Context())).merged(text)
    for span in reversed(detection.spans):
        text = text[: span.start] + "<X>" + text[span.end :]
    return text


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS)
async def test_vendor_key_is_masked_without_config(key: str) -> None:
    assert await _mask(f"key {key} end", NO_CONFIG) == "key <X> end"


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "secret"), BUILTIN_LEAKS)
async def test_builtin_secret_is_masked_without_config(text: str, secret: str) -> None:
    assert secret not in await _mask(text, NO_CONFIG)


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "secret"), LEAKS)
async def test_secret_is_masked(text: str, secret: str) -> None:
    assert secret not in await _mask(text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [build_policy_from(load_config(CONFIGS / "strict.yaml")), StrictPolicy()],
    ids=["strict.yaml", "empty allowed"],
)
async def test_strict_policy_keeps_builtin_secrets(policy: Policy) -> None:
    text = " ".join([f"key sk-proj-{A}", *(t for t, _ in BUILTIN_LEAKS)])
    detection = await NO_CONFIG.detect(text, Context())
    assert "API_KEY" in {s.entity_type for s in detection.spans}
    assert (await policy.decide(detection, Context())).spans == detection.spans


@pytest.mark.asyncio
@pytest.mark.parametrize("text", KEEP)
async def test_code_and_prose_are_kept(text: str) -> None:
    assert await _mask(text) == text


@pytest.mark.asyncio
async def test_only_the_value_is_masked() -> None:
    assert await _mask("DB_PASSWORD=hunter2rocks") == "DB_PASSWORD=<X>"
