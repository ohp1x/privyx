"""Header manipulation utilities for the transparent proxy.

Forwarding HTTP faithfully means dropping the headers that describe *this* hop
rather than the message: connection management, the request body length (the
body is rewritten, so its length changes), and content compression (we hand the
client identity-encoded bytes after transforming them).  Everything else is
relayed so upstream rate-limit, request-id, and provider-specific headers reach
the client.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from privyx.dialects import Dialect

#: Headers that describe a single transport hop and must never be forwarded.
#: ``content-length`` is here because the proxy rewrites the body; the ASGI
#: server recomputes it from the bytes actually sent.
HOP_BY_HOP: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

#: Response headers dropped in addition to :data:`HOP_BY_HOP`.  ``content-encoding``
#: goes because the client receives the decoded (identity) bytes we transformed;
#: ``set-cookie`` because a mapping folds several into one line no client can
#: read, so the reply carries each on its own.
_RESPONSE_DROP: frozenset[str] = HOP_BY_HOP | {"content-encoding", "set-cookie"}

#: The client's credential headers.
_CLIENT_AUTH: frozenset[str] = frozenset({"authorization", "x-api-key", "x-goog-api-key"})

#: Sent by ``privyx run pi`` with every request: the run's secret, and a JSON
#: object mapping each wire schema of the model's provider to its base URL.
RUN_TOKEN_HEADER = "x-privyx-run-token"
UPSTREAMS_HEADER = "x-privyx-upstreams"

#: Internal request headers that must not leak upstream.
_REQUEST_DROP_EXTRA: frozenset[str] = frozenset(
    {"accept-encoding", "x-privyx-session", RUN_TOKEN_HEADER, UPSTREAMS_HEADER}
)


def filter_request_headers(
    headers: Mapping[str, str],
    *,
    dialect: Dialect | None = None,
    forward_client_auth: bool = True,
    api_key: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the header set to send upstream.

    Args:
        headers: Incoming client headers (case-insensitive mapping).
        dialect: The dialect of the target path, which names the credential
            header and the headers its format needs (``anthropic-version``).
        forward_client_auth: When false, the client's ``Authorization`` /
            ``x-api-key`` / ``x-goog-api-key`` are dropped (rely on the
            configured ``api_key``).
        api_key: When set, overrides the upstream credential with this key,
            using the header the target provider expects.
        extra: Static headers merged in last (e.g. configured provider headers).

    Returns:
        A plain ``dict`` with lowercased header names.
    """
    out: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP or lower in _REQUEST_DROP_EXTRA:
            continue
        if not forward_client_auth and lower in _CLIENT_AUTH:
            continue
        out[lower] = value

    if extra:
        for key, value in extra.items():
            out[key.lower()] = value

    if api_key:
        style = dialect.credential_header if dialect is not None else "bearer"
        if style == "bearer":
            out["authorization"] = f"Bearer {api_key}"
        else:
            out[style] = api_key
            out.pop("authorization", None)

    for key, value in (dialect.request_headers if dialect is not None else {}).items():
        out.setdefault(key, value)

    return out


def filter_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Build the header set to return to the client.

    Drops hop-by-hop headers plus ``content-encoding`` (the body is handed back
    decoded), ``content-length`` (recomputed by the server), and ``set-cookie``
    (:attr:`~privyx.proxy._pipeline.Reply.set_cookies` carries each one).
    """
    return {
        key.lower(): value for key, value in headers.items() if key.lower() not in _RESPONSE_DROP
    }
