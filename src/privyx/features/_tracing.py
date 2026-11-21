"""The ``tracing`` feature: OpenTelemetry spans per request (``pip install 'privyx[otel]'``).

The exporter is set by the standard ``OTEL_*`` environment variables.  The
spans themselves are the pipeline's own instrumentation, which this switches on.
"""

from __future__ import annotations

from privyx.core import Services
from privyx.features._base import Feature


class Tracing(Feature):
    """Turns on the pipeline's OpenTelemetry spans while it is on."""

    name = "tracing"

    async def open(self, services: Services) -> None:
        from privyx.observability import tracing

        tracing.configure(True)

    async def close(self) -> None:
        from privyx.observability import tracing

        tracing.configure(False)
