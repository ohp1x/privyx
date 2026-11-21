"""The ``cache_control`` feature: mark a large prompt prefix for caching."""

from __future__ import annotations

from privyx.core import listen
from privyx.events import RequestMasked
from privyx.features._base import Feature


class CacheControl(Feature):
    """Marks a request's cacheable prefix, where its dialect has one (Anthropic's).

    Only a request that marks nothing itself is changed.  It runs after the
    directive, so the prefix it marks is the one sent.
    """

    name = "cache_control"

    @listen(RequestMasked, priority=-10)
    def mark(self, event: RequestMasked) -> None:
        event.dialect.add_cache_control(event.body)
