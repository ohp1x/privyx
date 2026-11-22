"""The events Privyx emits; ``app.on(Event)`` adds a listener, a feature ``@listen``.

Interceptable events happen inside a flow: :class:`AppStarting`,
:class:`RequestReceived`, :class:`RequestMasked`.  Notifications are the audit
vocabulary (:class:`AuditEvent` and its subclasses), :class:`AppStopping`,
and :class:`ConfigChanged`.
"""

from privyx.events._app import AppStarting, AppStopping
from privyx.events._audit import (
    SCHEMA_VERSION,
    AuditEvent,
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
)
from privyx.events._config import ConfigChanged
from privyx.events._request import CREDENTIAL_HEADERS, Headers, RequestMasked, RequestReceived

__all__ = [
    "CREDENTIAL_HEADERS",
    "SCHEMA_VERSION",
    "AppStarting",
    "AppStopping",
    "AuditEvent",
    "ConfigChanged",
    "Headers",
    "ProxyBlocked",
    "ProxyError",
    "ProxyPassthrough",
    "ProxyRequest",
    "ProxyResponse",
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
]
