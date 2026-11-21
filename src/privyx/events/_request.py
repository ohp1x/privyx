"""The events of one exchange in the pipeline: received, and masked."""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from privyx.core import Interceptable

if TYPE_CHECKING:
    from privyx.dialects import Dialect

#: Request headers that carry a credential.  Listeners cannot write them: a
#: value written there would pass the leak check, which does not look in them
#: (a key a user pasted into a prompt is sent there too), and reach the
#: upstream beside the credential the provider's definition holds.
CREDENTIAL_HEADERS: frozenset[str] = frozenset(
    {"authorization", "x-api-key", "api-key", "x-goog-api-key", "proxy-authorization"}
)


class Headers(MutableMapping[str, str]):
    """A request's headers, by lowercase name, whose credential headers cannot be written."""

    def __init__(self, data: MutableMapping[str, str]) -> None:
        self._data = data  # by lowercase name, as the pipeline keeps them

    def __getitem__(self, name: str) -> str:
        return self._data[name.lower()]

    def __setitem__(self, name: str, value: str) -> None:
        self._data[self._writable(name)] = value

    def __delitem__(self, name: str) -> None:
        del self._data[self._writable(name)]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"Headers({sorted(self._data)})"  # names only: values may be credentials

    @staticmethod
    def _writable(name: str) -> str:
        lower = name.lower()
        if lower in CREDENTIAL_HEADERS:
            raise PermissionError(f"a listener cannot write the credential header {lower!r}")
        return lower


@dataclass(eq=False)
class _Exchange(Interceptable):
    method: str
    #: The path the client called, from its leading ``/``.
    path: str
    headers: Headers
    #: The request body, parsed JSON.
    body: dict[str, Any]
    #: The API format of the route.
    dialect: Dialect
    session_id: str
    #: Correlates the exchange's audit notifications, ``proxy.response`` among them.
    request_id: str


@dataclass(eq=False)
class RequestReceived(_Exchange):
    """A request on a masking route, parsed, before it is masked.

    ``body`` and ``headers`` are the client's: a listener may change them,
    refuse the request, or answer it, its answer sent as it is.
    """

    name: ClassVar[str] = "request.received"


@dataclass(eq=False)
class RequestMasked(_Exchange):
    """A request masked, before the final leak check and before it goes upstream.

    ``body`` is masked, and ``headers`` are the ones to send: a listener may
    change them, and what it adds is checked for leaks too.  It may refuse
    the request, or answer it, its answer restored as an upstream reply is.
    """

    name: ClassVar[str] = "request.masked"
