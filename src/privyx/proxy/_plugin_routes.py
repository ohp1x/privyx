"""Plugin routes beside the proxy's: served, but never on a masking path."""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from privyx.core import ConfigError

if TYPE_CHECKING:
    from fastapi import APIRouter


def include_router(app: Any, router: APIRouter | None, routes: dict[str, str]) -> None:
    """Serve ``router``'s routes in ``app``.

    Called after ``/health`` and ``/metrics`` and before the proxy's own
    routes, so those two stay Privyx's and the rest are matched first.  The
    routes are taken as they are now: one added to ``router`` later is not
    served.

    Each route is served behind :class:`_Unmasked`, so a request on a masking
    path never reaches it, whatever its converters.  A route that plainly
    overlaps one also stops startup, to say so at once.

    Raises:
        ConfigError: If a route would take a path of ``routes``, the masking
            routes: requests there would reach the plugin instead of masking.
    """
    if router is None:
        return
    from starlette.routing import Match

    from privyx.proxy.schemas import detect_schema, unversioned_routes

    taken = list(router.routes)
    # Every path the proxy masks, as detect_schema reads it: /v1/v1/... and
    # Gemini paths without their version too.
    masking = [
        *routes,
        *(f"/v1{route}" for route in routes if route.startswith("/v1/")),
        *unversioned_routes(routes),
    ]
    for route in taken:
        # Both ways: each masking path against the route, with the matcher that
        # serves it, and a path the route serves, its parameters filled in,
        # against the masking routes.  Each `*` of a masking route is filled in
        # with `x`, an API version, or a segment of the route itself:
        # `/v1beta/models/{name}` takes `/v1beta/models/x:generateContent`.
        path = getattr(route, "path", None)
        fillers = {"x", "v1", "v1beta", "v1alpha"}
        if isinstance(path, str):
            fillers.update(filter(None, re.split(r"[/:]", _PARAM.sub("", path))))
        examples = (example for pattern in masking for example in _filled(pattern, fillers))
        overlaps = any(route.matches(_scope(example))[0] != Match.NONE for example in examples)
        if overlaps or (isinstance(path, str) and detect_schema(_PARAM.sub("x", path), routes)):
            name = repr(path) if isinstance(path, str) else type(route).__name__
            raise ConfigError(f"plugin route {name} overlaps a masking route in proxy.routes")
    app.router.routes.extend(_Unmasked(route, routes) for route in taken)


class _Unmasked:
    """A plugin route that never takes a request on a masking path.

    The check at startup tries sample paths, which a route's converter
    (``{item:int}`` against ``/api/7*``) can miss; this one asks
    ``detect_schema`` about the very path requested.
    """

    def __init__(self, route: Any, routes: dict[str, str]) -> None:
        self._route = route
        self._routes = routes

    def matches(self, scope: Any) -> tuple[Any, Any]:
        from starlette.routing import Match

        from privyx.proxy.schemas import detect_schema

        if scope.get("type") == "http" and detect_schema(scope.get("path", ""), self._routes):
            return Match.NONE, {}
        match: tuple[Any, Any] = self._route.matches(scope)
        return match

    async def handle(self, scope: Any, receive: Any, send: Any) -> None:
        await self._route.handle(scope, receive, send)

    def url_path_for(self, name: str, /, **params: Any) -> Any:
        return self._route.url_path_for(name, **params)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._route(scope, receive, send)


def _scope(path: str) -> dict[str, Any]:
    """The ASGI scope of a ``POST`` to ``path``, as a route's matcher reads it."""
    return {"type": "http", "method": "POST", "path": path, "root_path": "", "headers": []}


def _filled(route: str, fillers: set[str]) -> Iterator[str]:
    """``route`` with each ``*`` replaced by each of ``fillers``."""
    pieces = route.split("*")
    for chosen in itertools.product(sorted(fillers), repeat=len(pieces) - 1):
        yield pieces[0] + "".join(f + piece for f, piece in zip(chosen, pieces[1:], strict=True))


#: A path parameter in a route, ``{name}`` or ``{name:path}``.
_PARAM = re.compile(r"\{[^}]*\}")
