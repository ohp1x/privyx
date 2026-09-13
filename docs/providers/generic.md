# Generic Provider

Privyx is a transparent proxy: it does not need to know the upstream schema.

```yaml
provider:
  type: generic
  base_url: http://localhost:20128
  api_key: ""
  headers: {}
```

Any HTTP client that speaks JSON (and SSE for streaming) can be pointed at
the gateway. The privacy engine transforms text leaves in the payload;
everything else is relayed as-is.

## Custom Providers

Providers are registered in `providers/registry.py`. To add a provider:

1. Subclass `BaseProvider` (or reuse `GenericProvider`).
2. Register a factory in `default_registry()`.
3. If streaming differs from SSE, add a stream adapter in
   `streaming/adapters/`.
