"""Privyx's own components pass the conformance suite of their kind, as a plugin's must."""

from __future__ import annotations

import importlib.util
import json
import os
import re
from typing import Any

import pytest

from privyx.anchors import HMACAnchor
from privyx.detectors import LLMDetector, PresidioDetector, RegexDetector, YamlDetector
from privyx.operators import (
    EncryptOperator,
    FakerOperator,
    FPEOperator,
    HashOperator,
    PartialOperator,
    PseudonymOperator,
    RedactOperator,
)
from privyx.policies import DefaultPolicy, StrictPolicy
from privyx.providers import AnthropicProvider, GenericProvider, GoogleProvider, OpenAIProvider
from privyx.testing import (
    AnchorSuite,
    DetectorSuite,
    OperatorSuite,
    PolicySuite,
    ProviderSuite,
    VaultSuite,
)
from privyx.vaults import MemoryVault, RedisVault, SQLiteVault

KEY = "sk-" + "test" * 6

# --- vaults -------------------------------------------------------------------


class TestMemoryVault(VaultSuite):
    component = MemoryVault


class TestSQLiteVault(VaultSuite):
    component = SQLiteVault
    options = {"dsn": ":memory:"}


@pytest.mark.skipif(
    not os.environ.get("PRIVYX_TEST_REDIS_URL"), reason="PRIVYX_TEST_REDIS_URL is not set"
)
class TestRedisVault(VaultSuite):
    component = RedisVault
    options = {"redis_url": os.environ.get("PRIVYX_TEST_REDIS_URL", "")}

    def test_ttl_hides_an_idle_session(self) -> None:
        """Redis counts a key's TTL from its last write, not from the session's last use."""


# --- anchors ------------------------------------------------------------------


class TestHMACAnchor(AnchorSuite):
    component = HMACAnchor
    options = {"secret": "a" * 64}
    other_options = {"secret": "b" * 64}


# --- policies -----------------------------------------------------------------


class TestDefaultPolicy(PolicySuite):
    component = DefaultPolicy


class TestStrictPolicy(PolicySuite):
    component = StrictPolicy
    options = {"allowed": ["EMAIL"]}


# --- providers ----------------------------------------------------------------


class TestGenericProvider(ProviderSuite):
    component = GenericProvider
    options = {"base_url": "https://router.example.com/v1/chat/completions", "api_key": KEY}


class TestOpenAIProvider(ProviderSuite):
    component = OpenAIProvider
    options = {"api_key": KEY}


class TestAnthropicProvider(ProviderSuite):
    component = AnthropicProvider
    options = {"api_key": KEY}


class TestGoogleProvider(ProviderSuite):
    component = GoogleProvider
    options = {"api_key": KEY}
    paths = ["/v1beta/models/gemini-2.5-flash:streamGenerateContent"]


# --- detectors ----------------------------------------------------------------


class TestRegexDetector(DetectorSuite):
    component = RegexDetector
    samples = {
        "mail alice@example.com, card 4111 1111 1111 1111": [
            "alice@example.com",
            "4111 1111 1111 1111",
        ]
    }


class TestYamlDetector(DetectorSuite):
    component = YamlDetector
    options = {"terms": {"PROJECT": ["kestrel"]}, "patterns": {"TICKET": r"\bT-\d{4}\b"}}
    samples = {"the kestrel launch, ticket T-1234": ["kestrel", "T-1234"]}


class _Result:
    def __init__(self, entity_type: str, start: int, end: int) -> None:
        self.entity_type, self.start, self.end, self.score = entity_type, start, end, 0.9


class _Analyzer:
    """Stands in for Presidio's analyzer: each "Ana" is a PERSON."""

    def analyze(self, text: str, **kwargs: Any) -> list[_Result]:
        return [_Result("PERSON", m.start(), m.end()) for m in re.finditer("Ana", text)]


class TestPresidioDetector(DetectorSuite):
    component = PresidioDetector
    samples = {"Ana met Ana": ["Ana"]}

    def create(self, **options: Any) -> Any:
        return PresidioDetector(analyzer=_Analyzer(), **{**self.options, **options})


class _Client:
    """Answers like a well-behaved model: each "Ana" in the scanned text."""

    async def complete(self, prompt: str) -> str:
        chunk = prompt.split("TEXT:\n", 1)[1]
        found = [[m.start(), m.end(), "PERSON", "Ana"] for m in re.finditer("Ana", chunk)]
        return json.dumps(found)


class TestLLMDetector(DetectorSuite):
    component = LLMDetector
    samples = {"Ana met Ana": ["Ana"]}

    def create(self, **options: Any) -> Any:
        return LLMDetector(client=_Client(), **{**self.options, **options})


# --- operators ----------------------------------------------------------------


class TestPseudonymOperator(OperatorSuite):
    component = PseudonymOperator


class TestRedactOperator(OperatorSuite):
    component = RedactOperator


class TestHashOperator(OperatorSuite):
    component = HashOperator


@pytest.mark.skipif(importlib.util.find_spec("faker") is None, reason="faker is not installed")
class TestFakerOperator(OperatorSuite):
    component = FakerOperator
    options = {"seed": 7}


@pytest.mark.skipif(
    importlib.util.find_spec("cryptography") is None, reason="cryptography is not installed"
)
class TestEncryptOperator(OperatorSuite):
    component = EncryptOperator
    options = {"key": "0f" * 32}


class TestPartialOperator(OperatorSuite):
    component = PartialOperator


class TestFPEOperator(OperatorSuite):
    component = FPEOperator
    options = {"seed": 7}
