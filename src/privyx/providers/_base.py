"""The provider kind: an upstream connection.

A provider says where a request goes and how it authenticates; sending it,
and streaming the reply, is the proxy's.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from privyx.config.schema import SharedProviderOptions
from privyx.core import Component

if TYPE_CHECKING:
    import httpx


class Provider(Component, kind="provider"):
    """Where a request goes upstream, and the credential it carries."""

    class Options(Component.Options, SharedProviderOptions):
        """``base_url``, ``api_key``, and ``headers``, which every provider takes."""

    options: Options

    @abstractmethod
    def url(self, path: str, *, stream: bool) -> str:
        """The upstream URL of a request the client sent to ``path``.

        Args:
            path: The client's path, such as ``/v1/chat/completions``.
            stream: Whether the client asked for a streamed reply.
        """

    def headers(self) -> dict[str, str]:
        """The headers sent with every request: ``headers``, and any of the type's own."""
        return dict(self.options.headers)

    def auth(self) -> httpx.Auth | None:
        """The credential added to each request; ``None``, the default, sends none."""
        return None
