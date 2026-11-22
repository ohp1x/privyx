"""The upstream: where a request goes, and the client that takes it there.

Both proxy modes send through one client per provider
(:func:`upstream_client`).  Where a request goes is the mode's routing rule:

- **transparent**: the request path joined to a base URL, the upstream's
  origin or, with a path prefix, its URL (:func:`joined_path`); for
  ``privyx run pi`` the base URL each request names (:func:`named_upstream`);
- **gateway**: each route the gateway serves (:func:`served_routes`) posted to
  the provider's endpoint for it, ``Provider.url``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from privyx.proxy._headers import RUN_TOKEN_HEADER, UPSTREAMS_HEADER
from privyx.proxy._routing import detect_schema

if TYPE_CHECKING:
    from privyx.dialects import Dialect

_log = logging.getLogger("privyx.proxy")

#: Token-counting and compaction endpoints.  The gateway posts every path to
#: one chat endpoint, which would turn a token count into a billed completion,
#: so these stay unserved (404) and clients fall back as they would upstream.
_AUXILIARY_ROUTES = frozenset(
    {
        "/v1/messages/count_tokens",
        "/v1/responses/input_tokens",
        "/v1/responses/compact",
        "/responses/input_tokens",
        "/responses/compact",
        # Ollama's API, served by the transparent proxy only: posted to one
        # chat endpoint, an embedding request would be a wrong-format reply.
        "/api/embed",
        "/api/embeddings",
    }
)


def served_routes(
    routes: dict[str, str], dialects: Mapping[str, Dialect | type[Dialect]], *, google: bool
) -> dict[str, str]:
    """The ``routes`` the gateway serves, with or without the Google provider.

    Not the auxiliary ones, nor those of a dialect it does not serve, such as
    Ollama's API, which the transparent proxy serves: its streams are JSON
    lines, and an Ollama server has no base path or key to hold.  A dialect
    whose requests each name their endpoint (Gemini's model and method) only
    with the Google provider (``google``): it alone posts each request to the
    path called, and posted to one fixed endpoint, a ``countTokens`` call could
    become a billed reply.
    """

    def served(name: str) -> bool:
        dialect = dialects.get(name)
        return dialect is not None and dialect.gateway and (google or not dialect.own_endpoint)

    return {p: s for p, s in routes.items() if p not in _AUXILIARY_ROUTES and served(s)}


def joined_path(path: str, raw_path: str | None, prefix: str | None) -> str | None:
    """The path to append to the upstream URL, or ``None`` to refuse the request.

    Without a prefix, the whole path.  With one, the rest after it, cut at a
    segment boundary (``/v1`` takes ``/v1/messages``, not ``/v10/messages``).
    A dot segment, backslash, or percent sign (once decoded), or an encoded
    ``/``, is refused rather than normalized: the joined URL could then leave
    the upstream's own path.
    """
    path = "/" + path.lstrip("/")
    if prefix is None:
        return path
    if (
        any(c in path for c in "\\%?#")
        or any(segment in (".", "..") for segment in path.split("/"))
        or (raw_path is not None and "%2f" in raw_path.lower())
    ):
        return None
    if path != prefix and not path.startswith(prefix + "/"):
        return None
    return path[len(prefix) :]


def named_upstream(
    headers: Mapping[str, str],
    path: str,
    routes: dict[str, str],
    token: str,
    versionless: frozenset[str] = frozenset(),
) -> str | None:
    """The base URL a ``privyx run pi`` request names for its route.

    Each base URL is listed under a wire schema; the one for the schema of the
    request path's route is used.  Only an http(s) base URL without
    credentials is.  ``None`` when the token does not match or no base URL fits.
    """
    sent = header(headers, RUN_TOKEN_HEADER)
    if not hmac.compare_digest(sent.encode(), token.encode()):
        return None
    try:
        bases = json.loads(header(headers, UPSTREAMS_HEADER))
    except ValueError:
        return None
    schema = detect_schema(path, routes, versionless)
    if not (schema and isinstance(bases, dict) and isinstance(bases.get(schema), str)):
        return None
    try:
        url = urlsplit(bases[schema])
        _ = url.port
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.hostname or "@" in url.netloc:
        return None
    # Rebuilt from its parts: a stray `?` or `#` would swallow the path.
    return f"{url.scheme}://{url.netloc}{url.path.rstrip('/')}"


def header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup that works for dicts and Headers objects."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


#: Seconds to wait before each retry of a connection that could not be opened.
_CONNECT_RETRY_DELAYS = (0.5, 1.0)


class _UpstreamClient(httpx.AsyncClient):
    """Retries a request whose connection could not be opened.

    Only :class:`httpx.ConnectError` (refused, DNS, TLS handshake): nothing has
    been sent, so even a billed POST is safe to repeat.  A connect timeout is not
    retried, since every attempt would wait ``connect_timeout`` again.  Done here
    rather than with ``AsyncHTTPTransport(retries=...)``: a client given its own
    transport ignores ``HTTPS_PROXY`` and ``NO_PROXY``.
    """

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        for delay in _CONNECT_RETRY_DELAYS:
            try:
                return await super().send(request, **kwargs)
            except httpx.ConnectError:
                await asyncio.sleep(delay)
        return await super().send(request, **kwargs)


def upstream_client(
    *, timeout: float = 300.0, connect_timeout: float = 10.0, max_connections: int | None = None
) -> httpx.AsyncClient:
    """The httpx client both proxy modes reach the upstream with.

    ``timeout`` bounds each read and write, not the whole exchange: an upstream
    can take minutes to send its first byte, and a stream lasts as long as bytes
    keep coming.  ``max_connections`` of ``None`` sets no limit, so the proxy is
    never narrower than its clients; httpx's default of 100 made request 101
    onward wait for a free connection and fail.  A connection that cannot be
    opened is retried twice; see :class:`_UpstreamClient`.

    Its cookie jar accepts nothing: one client serves every caller, so a cookie
    kept from one reply would go out on another caller's request.
    """
    return _UpstreamClient(
        timeout=httpx.Timeout(connect=connect_timeout, read=timeout, write=timeout, pool=10.0),
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=20),
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
    )


def upstream_failure(exc: BaseException) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Status, headers, and JSON body for an upstream call that got no response.

    Also logs it: the class at WARNING and the traceback only at DEBUG, since a
    dead or slow upstream is an expected condition, not a bug in Privyx.
    """
    name = type(exc).__name__
    _log.warning("no response from the upstream: %s", name)
    _log.debug("upstream request failed", exc_info=exc)
    headers: dict[str, str] = {}
    if isinstance(exc, httpx.PoolTimeout):
        status, kind, message = 503, "busy", "privyx is at provider.max_connections; retry later"
        headers["retry-after"] = "10"  # the time it already waited for a free connection
    elif isinstance(exc, httpx.TimeoutException):
        status, kind, message = 504, "timeout", "the upstream did not respond in time"
    else:
        status, kind, message = 502, "unreachable", "privyx could not reach the upstream"
    body = {"error": {"type": f"privyx_upstream_{kind}", "message": f"{message} ({name})"}}
    return status, headers, body
