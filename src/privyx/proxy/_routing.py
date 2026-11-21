"""Which dialect a request path has: the route map of ``proxy.routes``."""

from __future__ import annotations

import re
from functools import lru_cache


def detect_schema(
    path: str, routes: dict[str, str], versionless: frozenset[str] = frozenset()
) -> str | None:
    """Return the name of the dialect configured for ``path``, or ``None``.

    ``path`` is matched with a single leading slash, so both ``v1/messages``
    (as FastAPI hands it to a ``{path:path}`` route) and ``/v1/messages`` resolve
    the same way.

    A path that repeats ``/v1`` (``/v1/v1/messages``, what an Anthropic client
    calls when its base URL ends in a ``/v1`` of its own) gets the schema of the
    route it means: the upstream answers 404 either way, and the body must not
    reach it unmasked.

    A ``*`` in a route matches any characters but ``/``, as the model name in
    ``/v1beta/models/*:generateContent``.  A route without one wins over a
    route with one; between those, the first listed wins.

    A path without its API version (``/models/m:generateContent``, what a
    client calls when its base URL ends in the version, as pi's does pointed
    at Privyx) has the dialect of the route it means too, for the same reason,
    when that dialect is one of ``versionless``; see :func:`unversioned_routes`.
    """
    normalized = "/" + path.lstrip("/")
    schema = routes.get(normalized)
    if schema is None and normalized.startswith("/v1/v1/"):
        schema = routes.get(normalized[3:])
    if schema is None:
        for pattern, route in _route_patterns(tuple(r for r in routes if "*" in r)):
            if pattern.fullmatch(normalized):
                return routes[route]
        unversioned = unversioned_routes(routes, versionless)
        for pattern, route in _route_patterns(tuple(unversioned)):
            if pattern.fullmatch(normalized):
                return unversioned[route]
    return schema


def unversioned_routes(routes: dict[str, str], versionless: frozenset[str]) -> dict[str, str]:
    """Each route of a ``versionless`` dialect without its first segment, the API version.

    Mapped to its dialect's name.
    """
    return {
        "/" + rest: schema
        for route, schema in routes.items()
        if schema in versionless and (rest := route.lstrip("/").partition("/")[2])
    }


@lru_cache(maxsize=16)
def _route_patterns(routes: tuple[str, ...]) -> tuple[tuple[re.Pattern[str], str], ...]:
    """Each route with a ``*`` compiled to a regex, in order."""
    return tuple((re.compile("[^/]+".join(map(re.escape, r.split("*")))), r) for r in routes)
