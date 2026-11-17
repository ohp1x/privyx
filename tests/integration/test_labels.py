"""``token.label_format`` through the engine and both proxy modes."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from privyx.config.schema import Settings
from privyx.core.builder import build_engine
from privyx.core.context import Context
from privyx.core.engine import PrivacyEngine
from privyx.core.errors import ConfigError
from privyx.core.result import Detection
from privyx.core.session import Session
from privyx.gateway.server import Gateway
from privyx.gateway.transparent import create_transparent_app
from privyx.privacy.anchor.hmac import HMACAnchor
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.providers.generic import GenericProvider
from privyx.proxy.schemas import directive
from privyx.proxy.transparent import TransparentProxy
from privyx.token.codec import LabelCodec

pytest.importorskip("faker")

LABELED = "<{namespace}_{type}_{id}:{label}>"
NAME = "john smith"
EMAIL = "ana@example.com"
TERMS = {"terms": {"PERSON": [NAME], "PROJECT": ["kestrel"]}}


def _settings(**token: Any) -> Settings:
    return Settings(
        token={"label_format": LABELED, **token},
        detector=TERMS,
        audit={"enabled": False},
        proxy={"directive": True},
        anchor={"secret": ""},  # counter tokens, which the tests name
    )


async def _engine(settings: Settings | None = None) -> PrivacyEngine:
    engine, _ = await build_engine(settings or _settings())
    return engine


async def test_a_labeled_value_restores_and_its_label_alone_does_not() -> None:
    engine = await _engine()
    session = await engine.get_or_create_session()

    masked = await engine.transform(f"{NAME} wrote to {EMAIL} about kestrel", session=session)
    person, email, project = (t.replacement for t in masked.transformations)
    label = person.split(":", 1)[1].rstrip(">")

    assert person.startswith("<PRIVYX_PERSON_1:") and email.startswith("<PRIVYX_EMAIL_2:")
    assert project == "<PRIVYX_PROJECT_3>"  # no fake values for this type
    assert set(session.mapping) == {"<PRIVYX_PERSON_1>", "<PRIVYX_EMAIL_2>", "<PRIVYX_PROJECT_3>"}
    assert NAME not in masked.text and EMAIL not in masked.text and label != NAME

    reply = f"{person} said {label} is fine; {project}"
    restored = await engine.restore(reply, session.session_id)  # loaded from the vault
    assert restored.text == f"{NAME} said {label} is fine; kestrel"


async def test_labels_are_the_same_in_every_request() -> None:
    settings = _settings()
    one, two = await _engine(settings), await _engine(settings)
    first = await one.transform(f"{NAME} and {EMAIL}")
    second = await two.transform(f"{NAME} and {EMAIL}")
    assert first.text == second.text


async def test_without_label_format_nothing_changes() -> None:
    settings = Settings(detector=TERMS, audit={"enabled": False}, anchor={"secret": ""})
    engine = await _engine(settings)
    masked = await engine.transform(f"{NAME} wrote to {EMAIL}")
    assert masked.text == "<PRIVYX_PERSON_1> wrote to <PRIVYX_EMAIL_2>"
    assert "label" not in directive(engine.codec).casefold()


def test_the_directive_shows_a_labeled_placeholder() -> None:
    codec = LabelCodec("<{namespace}_{type}_{id}>", LABELED)
    text = directive(codec)
    assert "<PRIVYX_PERSON_1:Alice Johnson>" in text
    assert "never the name alone" in text


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"token": {"label_format": LABELED, "compact": True}}, "token.compact"),
        ({"token": {"label_format": LABELED}, "operator": {"type": "faker"}}, "operator.type"),
        ({"token": {"label_format": "<{namespace}_{type}_{id}>"}}, r"token.label_format must"),
        ({"token": {"label_format": "<{id}:{label}"}}, r"token.label_format needs a literal"),
        ({"token": {"label_format": "<{id}:{name}>"}}, r"token.label_format: unknown token"),
    ],
)
async def test_an_unusable_label_setting_fails_at_startup(
    override: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        await build_engine(Settings(audit={"enabled": False}, **override))


async def test_an_id_stands_for_one_value_whatever_the_type() -> None:
    # A counter: EMAIL_1 was freed, so the next value would get id 2 beside PERSON_2.
    engine = await _engine(_settings(label_format="<{namespace}_{id}:{label}>"))
    session = await engine.get_or_create_session()
    session.put("<PRIVYX_PERSON_2>", NAME)
    masked = await engine.transform(EMAIL, session=session)
    assert masked.transformations[0].replacement.startswith("<PRIVYX_3:")

    # An anchored id: another value already holds this id under another type.
    anchor = HMACAnchor("a-test-secret")
    codec = LabelCodec("<{namespace}_{type}_{id}>", "<{namespace}_{id}:{label}>")
    operator = PseudonymOperator(codec, anchor, id_length=8, labels=lambda *_: "Fake Name")
    taken = await anchor.anchor(NAME, Context())
    digest = taken.rsplit("_", 1)[-1].upper()
    other = Session()
    other.put(f"<PRIVYX_EMAIL_{digest[:8]}>", EMAIL)
    found = Detection()
    found.add(0, len(NAME), "PERSON", NAME)
    result = await operator.pseudonymize(NAME, found, other, Context())
    assert result.text == f"<PRIVYX_{digest[:9]}:Fake Name>"


async def test_a_label_that_is_the_value_or_shares_its_name_is_not_used() -> None:
    labels = {0: "John Smith", 1: "John Brown", 2: "Mary Brown"}
    codec = LabelCodec("<{namespace}_{type}_{id}>", LABELED)
    operator = PseudonymOperator(codec, labels=lambda _type, _value, salt: labels.get(salt, "x"))
    found = Detection()
    found.add(0, len(NAME), "PERSON", NAME)
    result = await operator.pseudonymize(NAME, found, Session(), Context())
    assert result.text == "<PRIVYX_PERSON_1:Mary Brown>"

    only_shared = PseudonymOperator(codec, labels=lambda *_: "John Brown")
    result = await only_shared.pseudonymize(NAME, found, Session(), Context())
    assert result.text == "<PRIVYX_PERSON_1:John Brown>"  # the best there is, not the name

    only_the_value = PseudonymOperator(codec, labels=lambda *_: "JOHN SMITH")
    result = await only_the_value.pseudonymize(NAME, found, Session(), Context())
    assert result.text == "<PRIVYX_PERSON_1>"


async def test_a_label_comes_from_the_token_never_from_the_value() -> None:
    # Two values behind one counter token get one label: the provider, which
    # sees the token anyway, cannot try candidate values against a label.
    engine = await _engine()
    first = await engine.transform(NAME)
    second = await engine.transform("kestrel", detection=_person("kestrel"))
    assert first.text.split(":")[0] == second.text.split(":")[0] == "<PRIVYX_PERSON_1"
    assert first.text.split(":")[1] == second.text.split(":")[1]


def _person(value: str) -> Detection:
    found = Detection()
    found.add(0, len(value), "PERSON", value)
    return found


async def test_a_label_never_holds_another_value_of_the_session() -> None:
    # `James` is masked too, so `James Wilson` would send it, and fail the DLP check.
    labels = {0: "James Wilson", 1: "Ann Lee"}
    codec = LabelCodec("<{namespace}_{type}_{id}>", LABELED)
    operator = PseudonymOperator(codec, labels=lambda _type, _token, salt: labels[salt % 2])
    text = "James met mary smith"
    found = Detection()
    found.add(0, 5, "PERSON", "James")
    found.add(10, 20, "PERSON", "mary smith")

    result = await operator.pseudonymize(text, found, Session(), Context())

    assert result.text == "<PRIVYX_PERSON_1:Ann Lee> met <PRIVYX_PERSON_2:Ann Lee>"


async def test_labels_follow_the_locale() -> None:
    engine = await _engine(
        Settings(
            token={"label_format": LABELED},
            operator={"locale": "id_ID", "seed": 3},
            detector=TERMS,
            audit={"enabled": False},
        )
    )
    session = await engine.get_or_create_session()
    masked = await engine.transform(NAME, session=session)
    assert (await engine.restore(masked.text, session.session_id)).text == NAME


# --- through the proxy --------------------------------------------------------


def _upstream(seen: list[dict[str, Any]]) -> httpx.AsyncClient:
    """Echo the last message back: as text, as tool arguments, or as a stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        content = body["messages"][-1]["content"]
        call = {"function": {"name": "send", "arguments": json.dumps({"to": content})}}
        if body.get("stream"):
            frames = "".join(
                f"data: {json.dumps({'choices': [{'delta': {'content': content[i : i + 3]}}]})}\n\n"
                for i in range(0, len(content), 3)
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(frames + "data: [DONE]\n\n").encode(),
            )
        message = {"content": content, "tool_calls": [call]}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _apps(seen: list[dict[str, Any]], **proxy: Any) -> list[Any]:
    settings = Settings(
        token={"label_format": LABELED},
        detector=TERMS,
        audit={"enabled": False},
        proxy={"directive": True, **proxy},
    )
    engine = await _engine(settings)
    transparent = TransparentProxy(engine, origin="https://up.test", client=_upstream(seen))
    gateway_settings = settings.model_copy(update={"proxy": settings.proxy.model_copy()})
    provider = GenericProvider(
        base_url="https://up.test/v1/chat/completions", client=_upstream(seen)
    )
    return [
        create_transparent_app(engine, transparent, settings),
        Gateway(engine, provider, gateway_settings).app,
    ]


async def _post(app: Any, payload: dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/v1/chat/completions", json=payload)


async def test_both_modes_send_labels_and_restore_text_tool_arguments_and_streams() -> None:
    seen: list[dict[str, Any]] = []
    text = f"tell {NAME} at {EMAIL}"
    for app in await _apps(seen):
        chat = {"messages": [{"role": "user", "content": text}]}
        reply = (await _post(app, chat)).json()["choices"][0]["message"]
        assert reply["content"] == text
        assert json.loads(reply["tool_calls"][0]["function"]["arguments"]) == {"to": text}

        streamed = await _post(app, {**chat, "stream": True})
        deltas = [
            json.loads(line[6:])["choices"][0]["delta"]["content"]
            for line in streamed.text.splitlines()
            if line.startswith("data: {")
        ]
        assert "".join(deltas) == text

    for body in seen:
        sent = json.dumps(body)
        assert NAME not in sent and EMAIL not in sent
        assert "<PRIVYX_PERSON_1:" in sent
        assert "<PRIVYX_PERSON_1:Alice Johnson>" in body["messages"][0]["content"]  # directive


async def test_dlp_check_still_finds_a_masked_value() -> None:
    seen: list[dict[str, Any]] = []
    app = (await _apps(seen, dlp_check=True))[0]
    # The id is masked in the message; the same value sits unmasked in a key.
    payload = {"messages": [{"role": "user", "content": f"hi {NAME}"}], NAME: 1}
    response = await _post(app, payload)
    assert response.status_code == 422
    assert not seen
