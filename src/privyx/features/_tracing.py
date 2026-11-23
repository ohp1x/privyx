"""The ``tracing`` feature: OpenTelemetry spans per request (``pip install 'privyx[otel]'``).

The exporter is set by the standard ``OTEL_*`` environment variables.  The
spans themselves are the pipeline's own instrumentation, which this switches on.
"""

from __future__ import annotations

from typing import ClassVar

from privyx.core import Services
from privyx.features._base import Feature


class Tracing(Feature):
    """Turns on the pipeline's OpenTelemetry spans while it is on."""

    name = "tracing"

    #: How many are open: the switch is global, and a replacement opens first.
    _open: ClassVar[int] = 0

    async def open(self, services: Services) -> None:
        from privyx.observability import tracing

        tracing.configure(True)
        Tracing._open += 1

    async def close(self) -> None:
        from privyx.observability import tracing

        Tracing._open = max(Tracing._open - 1, 0)
        if not Tracing._open:
            tracing.configure(False)
