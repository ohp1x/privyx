"""OpenTelemetry spans per proxied request, opt-in (``tracing.enabled``).

Needs the ``otel`` extra (``pip install 'privyx[otel]'``); the exporter is set
by the standard ``OTEL_*`` environment variables (``OTEL_EXPORTER_OTLP_ENDPOINT``,
``OTEL_SERVICE_NAME``, ...).  Until :func:`configure` turns it on, every
function here returns at once and OpenTelemetry is never imported.

A request gets a ``privyx.request`` span, parented on the client's
``traceparent`` when it sends one, with ``privyx.transform``,
``privyx.upstream`` (to the response headers) and ``privyx.restore`` under it.
Attributes are the same PII-safe values the audit trail holds: method, route,
schema, status, counts, and error class names, never payload text.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from privyx.core.errors import ConfigError

_tracer: Any = None
#: Built on first enable and kept, so a reload that turns tracing back on reuses it.
_provider: Any = None


def configure(enabled: bool) -> None:
    """Export spans over OTLP/HTTP when ``enabled``; stop when not.

    Called again on ``privyx proxy --reload``, so turning it off takes effect.

    Raises:
        ConfigError: If it is enabled but the ``otel`` extra is not installed.
    """
    global _tracer, _provider
    if not enabled:
        if _tracer is not None:
            _tracer = None
            _provider.force_flush()
        return
    if _tracer is not None:
        return
    if _provider is not None:
        _tracer = _provider.get_tracer("privyx")
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise ConfigError(
            "tracing.enabled needs OpenTelemetry: pip install 'privyx[otel]'"
        ) from exc
    name = os.environ.get("OTEL_SERVICE_NAME") or "privyx"
    _provider = TracerProvider(resource=Resource.create({"service.name": name}))
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    # Not set as the global provider: that can be set only once per process.
    _tracer = _provider.get_tracer("privyx")


def start(
    name: str,
    parent: Any = None,
    *,
    headers: Mapping[str, str] | None = None,
    **attributes: Any,
) -> Any:
    """Start a span under ``parent`` (or the ``traceparent`` in ``headers``); ``None`` when off."""
    if _tracer is None:
        return None
    from opentelemetry import propagate, trace

    context = None
    if parent is not None:
        context = trace.set_span_in_context(parent)
    elif headers is not None:
        context = propagate.extract(dict(headers))
    return _tracer.start_span(name, context=context, attributes=_clean(attributes))


def annotate(span: Any, **attributes: Any) -> None:
    """Add ``attributes`` to ``span``, if tracing is on."""
    if span is not None:
        span.set_attributes(_clean(attributes))


def fail(span: Any) -> None:
    """Mark ``span`` as failed without an exception (a 5xx reply); it ends later."""
    if span is not None:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR))


def end(span: Any, error: BaseException | None = None) -> None:
    """End ``span``; an ``error`` is recorded by its class name only, never its message."""
    if span is None:
        return
    if error is not None:
        span.set_attribute("error.type", type(error).__name__)
        fail(span)
    span.end()


def _clean(attributes: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attributes.items() if v is not None}
