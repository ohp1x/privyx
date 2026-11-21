"""Privyx's own components pass the conformance suite of their kind, as a plugin's must."""

from __future__ import annotations

import importlib.util
import json
import os
import re
from typing import Any

import pytest

from privyx.agents import ClaudeAgent, CodexAgent, EnvAgent, OllamaAgent, PiAgent
from privyx.anchors import HMACAnchor
from privyx.detectors import LLMDetector, PresidioDetector, RegexDetector, YamlDetector
from privyx.dialects import (
    AnthropicDialect,
    EmbeddingsDialect,
    GeminiDialect,
    OllamaDialect,
    OpenAIDialect,
    ResponsesDialect,
)
from privyx.features import AuditTrail, Budget, CacheControl, Directive, Metrics, Tracing
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
from privyx.secrets import KeychainResolver, OnePasswordResolver
from privyx.testing import (
    AgentSuite,
    AnchorSuite,
    DetectorSuite,
    DialectSuite,
    FeatureSuite,
    OperatorSuite,
    PolicySuite,
    ProviderSuite,
    SecretResolverSuite,
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


# --- secret resolvers ---------------------------------------------------------


class TestOnePasswordResolver(SecretResolverSuite):
    component = OnePasswordResolver
    reference = "op://dev/acme/credential"
    secret = KEY
    broken = "op://dev/acme/missing"

    @pytest.fixture(autouse=True)
    def _op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        from privyx.secrets import _op

        def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            if argv[-1] == self.reference:
                return subprocess.CompletedProcess(argv, 0, KEY, "")
            return subprocess.CompletedProcess(argv, 1, "", "[ERROR] item not found\n")

        monkeypatch.setattr(_op.shutil, "which", lambda _: "/usr/bin/op")
        monkeypatch.setattr(_op.subprocess, "run", run)


class TestKeychainResolver(SecretResolverSuite):
    component = KeychainResolver
    reference = "keychain://acme/alice"
    secret = KEY
    broken = "keychain://acme/bob"

    @pytest.fixture(autouse=True)
    def _keyring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys
        import types

        module = types.ModuleType("keyring")
        module.get_password = lambda service, account: (  # type: ignore[attr-defined]
            KEY if (service, account) == ("acme", "alice") else None
        )
        monkeypatch.setitem(sys.modules, "keyring", module)


# --- agents -------------------------------------------------------------------

#: A home with no settings in it, and the tools' own endpoints set.
_TOOL_ENVIRONMENT = {
    "HOME": "/nonexistent/privyx-suite",
    "CODEX_HOME": "/nonexistent/privyx-suite/.codex",
    "CLAUDE_CONFIG_DIR": "/nonexistent/privyx-suite/.claude",
    "ANTHROPIC_BASE_URL": "https://anthropic.example.com",
    "OPENAI_BASE_URL": "https://openai.example.com",
    "OLLAMA_HOST": "ollama.example.com",
}


class TestEnvAgent(AgentSuite):
    component = EnvAgent
    options = {"endpoint_env": ["OPENAI_BASE_URL"]}
    environment = _TOOL_ENVIRONMENT


class TestClaudeAgent(AgentSuite):
    component = ClaudeAgent
    environment = _TOOL_ENVIRONMENT


class TestCodexAgent(AgentSuite):
    component = CodexAgent
    environment = _TOOL_ENVIRONMENT


class TestPiAgent(AgentSuite):
    component = PiAgent
    environment = _TOOL_ENVIRONMENT


class TestOllamaAgent(AgentSuite):
    component = OllamaAgent
    environment = _TOOL_ENVIRONMENT


# --- dialects -----------------------------------------------------------------

_TOOL_CALL = {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}


class TestOpenAIDialect(DialectSuite):
    component = OpenAIDialect
    requests = [
        {
            "model": "gpt-x",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "tool_calls": [_TOOL_CALL]},
                {"role": "tool", "tool_call_id": "call_1", "content": "done"},
            ],
            "prediction": {"type": "content", "content": "draft"},
        },
        {"model": "gpt-x", "prompt": "complete this", "suffix": "end"},
    ]
    stream = (
        'data: {"choices":[{"index":0,"delta":{"content":"mail {token} now"}}]}\n\ndata: [DONE]\n\n'
    )


class TestAnthropicDialect(DialectSuite):
    component = AnthropicDialect
    requests = [
        {
            "model": "claude-x",
            "system": [{"type": "text", "text": "be brief"}],
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": "f", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "done"},
                        {"type": "document", "source": {"type": "text", "data": "a note"}},
                    ],
                },
            ],
        }
    ]
    stream = (
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"mail {token} now"}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )


class TestResponsesDialect(DialectSuite):
    component = ResponsesDialect
    requests = [
        {
            "model": "gpt-x",
            "instructions": "be brief",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
                {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
            ],
        }
    ]
    stream = (
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,'
        '"content_index":0,"delta":"mail {token} now"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp_1"}}\n\n'
    )


class TestGeminiDialect(DialectSuite):
    component = GeminiDialect
    requests = [
        {
            "systemInstruction": {"parts": [{"text": "be brief"}]},
            "contents": [
                {"role": "user", "parts": [{"text": "hi"}]},
                {"role": "model", "parts": [{"functionCall": {"name": "f", "args": {}}}]},
                {
                    "role": "user",
                    "parts": [{"functionResponse": {"name": "f", "response": {"r": "done"}}}],
                },
            ],
        },
        {"generateContentRequest": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}},
    ]
    stream = (
        'data: {"candidates":[{"index":0,"content":{"role":"model",'
        '"parts":[{"text":"mail {token} now"}]}}]}\n\n'
    )


class TestOllamaDialect(DialectSuite):
    component = OllamaDialect
    requests = [
        {"model": "llama", "messages": [{"role": "user", "content": "hi", "images": ["aGk="]}]},
        {"model": "llama", "system": "be brief", "prompt": "hi"},
    ]
    stream = (
        '{"model":"m","message":{"role":"assistant","content":"mail {token} now"},"done":false}\n'
        '{"model":"m","done":true}\n'
    )


class TestEmbeddingsDialect(DialectSuite):
    component = EmbeddingsDialect
    requests = [{"model": "embed-x", "input": ["first text", "second text"]}]


# --- features -----------------------------------------------------------------


class TestAuditTrail(FeatureSuite):
    component = AuditTrail

    @pytest.fixture(autouse=True)
    def _trail(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(type(self), "options", {"path": str(tmp_path / "audit.log")})


class TestMetrics(FeatureSuite):
    component = Metrics


class TestTracing(FeatureSuite):
    component = Tracing

    @pytest.fixture(autouse=True)
    def _provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sdk = pytest.importorskip("opentelemetry.sdk.trace")
        from privyx.observability import tracing

        monkeypatch.setattr(tracing, "_provider", sdk.TracerProvider())  # exports nowhere
        monkeypatch.setattr(tracing, "_tracer", None)


class TestDirective(FeatureSuite):
    component = Directive


class TestCacheControl(FeatureSuite):
    component = CacheControl


class TestBudget(FeatureSuite):
    component = Budget
    options = {"max_turns": 3, "max_tokens": 1000, "profile": "claude"}
