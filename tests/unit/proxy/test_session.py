"""Tests for session-id resolution (:mod:`privyx.proxy.session`)."""

from __future__ import annotations

import re

from privyx.proxy.session import resolve_session_id

_SES = re.compile(r"^ses_[0-9a-f]{16}$")

BEARER = {"authorization": "Bearer sk-abc"}
XKEY = {"x-api-key": "anthropic-key"}


def _convo(role: str, text: str) -> dict[str, object]:
    return {"messages": [{"role": role, "content": text}]}


# -- header always wins ------------------------------------------------------


def test_explicit_header_beats_every_strategy() -> None:
    for strategy in ("ephemeral", "client", "conversation"):
        sid, source = resolve_session_id(
            strategy,
            header_value="ses_fromclient",
            headers=BEARER,
            payload=_convo("user", "mail a@b.com"),
            schema="openai",
        )
        assert sid == "ses_fromclient"
        assert source == "header"


# -- ephemeral ---------------------------------------------------------------


def test_ephemeral_returns_none() -> None:
    sid, source = resolve_session_id(
        "ephemeral",
        header_value=None,
        headers=BEARER,
        payload=_convo("user", "hi"),
        schema="openai",
    )
    assert sid is None
    assert source == "ephemeral"


# -- client ------------------------------------------------------------------


def test_client_is_stable_and_credential_shaped() -> None:
    kwargs = dict(header_value=None, headers=BEARER, payload=None, schema=None)
    a, source = resolve_session_id("client", **kwargs)
    b, _ = resolve_session_id("client", **kwargs)
    assert source == "client"
    assert a == b and _SES.match(a)


def test_client_differs_by_credential() -> None:
    a, _ = resolve_session_id(
        "client", header_value=None, headers=BEARER, payload=None, schema=None
    )
    b, _ = resolve_session_id("client", header_value=None, headers=XKEY, payload=None, schema=None)
    assert a != b


def test_client_without_credential_falls_back_to_ephemeral() -> None:
    sid, source = resolve_session_id(
        "client", header_value=None, headers={}, payload=None, schema=None
    )
    assert sid is None
    assert source == "ephemeral"


# -- conversation ------------------------------------------------------------


def test_conversation_stable_as_history_grows() -> None:
    """The id keys off the first user message, so later turns resolve the same."""
    turn1 = _convo("user", "mail alice@example.com")
    turn2 = {
        "messages": [
            {"role": "user", "content": "mail alice@example.com"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "and bob@example.org"},
        ]
    }
    a, source = resolve_session_id(
        "conversation", header_value=None, headers=BEARER, payload=turn1, schema="openai"
    )
    b, _ = resolve_session_id(
        "conversation", header_value=None, headers=BEARER, payload=turn2, schema="openai"
    )
    assert source == "conversation"
    assert a == b and _SES.match(a)


def test_conversation_isolates_distinct_conversations() -> None:
    a, _ = resolve_session_id(
        "conversation",
        header_value=None,
        headers=BEARER,
        payload=_convo("user", "topic one"),
        schema="openai",
    )
    b, _ = resolve_session_id(
        "conversation",
        header_value=None,
        headers=BEARER,
        payload=_convo("user", "topic two"),
        schema="openai",
    )
    assert a != b


def test_conversation_no_cross_user_collision() -> None:
    """Same first message, different credential → different session (no shared map)."""
    same = _convo("user", "identical opening line")
    a, _ = resolve_session_id(
        "conversation", header_value=None, headers=BEARER, payload=same, schema="openai"
    )
    b, _ = resolve_session_id(
        "conversation", header_value=None, headers=XKEY, payload=same, schema="openai"
    )
    assert a != b


def test_conversation_without_any_signal_is_ephemeral() -> None:
    sid, source = resolve_session_id(
        "conversation", header_value=None, headers={}, payload=None, schema=None
    )
    assert sid is None
    assert source == "ephemeral"


def test_conversation_ignores_body_on_unknown_schema() -> None:
    """A non-chat path is not fingerprinted; it degrades to a per-credential id."""
    with_body, _ = resolve_session_id(
        "conversation",
        header_value=None,
        headers=BEARER,
        payload=_convo("user", "hi"),
        schema=None,
    )
    no_body, _ = resolve_session_id(
        "conversation", header_value=None, headers=BEARER, payload=None, schema=None
    )
    assert with_body == no_body  # body ignored → credential-only fingerprint


# -- first-user-message extraction ------------------------------------------


def test_first_user_message_prefers_first_user_over_system() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "first user line"},
            {"role": "user", "content": "second user line"},
        ]
    }
    keyed_on_first, _ = resolve_session_id(
        "conversation", header_value=None, headers=BEARER, payload=payload, schema="openai"
    )
    same_first_different_tail = {
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "first user line"},
        ]
    }
    b, _ = resolve_session_id(
        "conversation",
        header_value=None,
        headers=BEARER,
        payload=same_first_different_tail,
        schema="openai",
    )
    assert keyed_on_first == b


def test_first_user_message_handles_content_block_lists() -> None:
    """Anthropic-style content parts are flattened the same as a plain string."""
    blocks = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hello world"}]}]}
    plain = _convo("user", "hello world")
    a, _ = resolve_session_id(
        "conversation", header_value=None, headers=XKEY, payload=blocks, schema="anthropic"
    )
    b, _ = resolve_session_id(
        "conversation", header_value=None, headers=XKEY, payload=plain, schema="anthropic"
    )
    assert a == b


def test_first_user_message_reads_responses_input() -> None:
    """A Responses body fingerprints on ``input``: an item list or the bare string."""
    items = {
        "input": [
            {"role": "developer", "content": "be brief"},
            {"role": "user", "content": [{"type": "input_text", "text": "hi Alice"}]},
        ]
    }
    later = {"input": [*items["input"], {"role": "assistant", "content": "hello"}]}

    def sid(payload: object) -> str | None:
        return resolve_session_id(
            "conversation", header_value=None, headers=BEARER, payload=payload, schema="responses"
        )[0]

    assert sid(items) == sid(later) == sid({"input": "hi Alice"})
    assert sid(items) != sid({"input": "hi Bob"})
