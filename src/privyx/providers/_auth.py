"""``ApiKey``: the built-in providers' credential."""

from __future__ import annotations

from collections.abc import Generator

import httpx


class ApiKey(httpx.Auth):
    """A key sent in one header, unless the request has that header already.

    Args:
        key: The key.
        header: ``authorization``, sent as ``Bearer <key>``, or a header that
            takes the key as it is, such as ``x-api-key``.
    """

    def __init__(self, key: str, header: str = "authorization") -> None:
        self.key = key
        self.header = header

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if self.header not in request.headers:
            bearer = self.header.lower() == "authorization"
            request.headers[self.header] = f"Bearer {self.key}" if bearer else self.key
        yield request
