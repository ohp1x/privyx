---
description: Export an OpenTelemetry span per proxied request, with masking, upstream, and restore timings, and no payload text in any attribute.
---

# OpenTelemetry tracing

Privyx can export a span per proxied request to any OpenTelemetry backend
(Jaeger, Grafana Tempo, Honeycomb, Datadog, ...). It needs the `otel` extra and
is off until you turn it on:

```bash
pip install 'privyx[otel]'
```

```yaml
# privyx.yaml
tracing:
  enabled: true
```

Spans are sent over OTLP/HTTP, configured by the standard environment
variables, for example:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=privyx   # the default
```

Without `tracing.enabled`, Privyx never imports the OpenTelemetry SDK. With it
and without the extra, `privyx proxy` and `privyx run` stop at startup and say
what to install.

## Spans

Each request on a masked route gets:

| Span | Covers |
|---|---|
| `privyx.request` | The whole exchange, until the last byte of the reply. |
| `privyx.transform` | Masking the request. |
| `privyx.upstream` | Sending it, until the upstream's response headers. |
| `privyx.restore` | Restoring the reply, batch or streamed. |

When the client sends a W3C `traceparent` header, `privyx.request` joins its
trace.

Attributes hold what the [audit trail](audit-events.md) holds, never payload
text: `http.request.method`, `http.route` (masked routes only),
`http.response.status_code`, `privyx.schema`, `privyx.stream`,
`privyx.restored`, `privyx.aborted`, and `error.type` (an exception's class
name, never its message).

Tracing covers transparent mode, the default and the mode `privyx run` uses.
