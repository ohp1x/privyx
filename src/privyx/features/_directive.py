"""The ``directive`` feature: tell the model to keep placeholders as written."""

from __future__ import annotations

from typing import Any

from privyx.core import Services, listen
from privyx.events import RequestMasked
from privyx.features._base import Feature
from privyx.token import FormatCodec
from privyx.token.codec import LabelCodec
from privyx.token.model import LogicalToken


class Directive(Feature):
    """Adds an instruction to a chat request's system prompt, where its dialect keeps it put.

    The text is added to the masked request, so the final leak check reads it
    too.  It names a placeholder of the configured syntax.
    """

    name = "directive"

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.text = directive(FormatCodec.default())

    async def open(self, services: Services) -> None:
        self.text = directive(services.codec)

    @listen(RequestMasked)
    def add(self, event: RequestMasked) -> None:
        event.dialect.add_directive(event.body, self.text, event.path)


def directive(codec: FormatCodec) -> str:
    """The instruction the feature adds: keep placeholders exactly as written.

    With ``token.label_format``, the example is a labeled placeholder, and a
    sentence says that its label only stands in for the value.
    """
    example = codec.encode(LogicalToken(namespace=codec.namespace, type="EMAIL", identifier="1"))
    labeled = None
    if isinstance(codec, LabelCodec):
        person = LogicalToken(namespace=codec.namespace, type="PERSON", identifier="1")
        labeled = codec.label(person, "Alice Johnson")
    text = (
        "Privacy notice from the Privyx proxy: some values in this conversation are "
        f"replaced by placeholders such as {labeled or example}. Treat each placeholder as "
        "the value itself and copy it exactly as written, brackets included, wherever that "
        "value is needed: in replies, code, commands, file contents, and tool calls. Do not "
        "change, split, encode, translate, or guess what is behind a placeholder."
    )
    if labeled:
        text += (
            " The name inside a placeholder only stands in for the value: write the whole "
            "placeholder wherever the value is meant, never the name alone."
        )
    return text
