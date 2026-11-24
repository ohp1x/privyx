"""The events Privyx emits; ``app.on(Event)`` adds a listener, a feature ``@listen``.

Interceptable events happen inside a flow: :class:`AppStarting`,
:class:`RequestReceived`, :class:`RequestMasked`.  Notifications are the audit
vocabulary (:class:`AuditEvent` and its subclasses), :class:`AuditWriteFailed`,
:class:`AppStopping`, :class:`ConfigChanged`, and :class:`RequestEnded`.
"""

from privyx.events._app import AppStarting, AppStopping
from privyx.events._audit import (
    SCHEMA_VERSION,
    AuditEvent,
    AuditWriteFailed,
    ProxyBlocked,
    ProxyError,
    ProxyPassthrough,
    ProxyRequest,
    ProxyResponse,
    RunBudgetExceeded,
    RunUnproxied,
    SessionCreated,
    SessionDeleted,
    SessionRestore,
    SessionTransform,
    breakdown,
    local_time,
    record_line,
    utc_time,
)
from privyx.events._config import ConfigChanged
from privyx.events._request import (
    CREDENTIAL_HEADERS,
    Headers,
    RequestEnded,
    RequestMasked,
    RequestReceived,
)

__all__ = [
    "CREDENTIAL_HEADERS",
    "SCHEMA_VERSION",
    "AppStarting",
    "AppStopping",
    "AuditEvent",
    "AuditWriteFailed",
    "ConfigChanged",
    "Headers",
    "ProxyBlocked",
    "ProxyError",
    "ProxyPassthrough",
    "ProxyRequest",
    "ProxyResponse",
    "RequestEnded",
    "RequestMasked",
    "RequestReceived",
    "RunBudgetExceeded",
    "RunUnproxied",
    "SessionCreated",
    "SessionDeleted",
    "SessionRestore",
    "SessionTransform",
    "breakdown",
    "local_time",
    "record_line",
    "utc_time",
]
