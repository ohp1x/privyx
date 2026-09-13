# Proxy Architecture

## Layers

The proxy is deliberately thin. It:

1. Decodes incoming HTTP requests (JSON body).
2. Hands the text to the privacy engine (pseudonymize).
3. Forwards to the upstream provider.
4. Handles the response — batch or streaming.
5. Deanonymizes before returning to the client.

## Streaming Path

```text
Provider
    ↓
SSE event
    ↓
OpenAI adapter
    ↓
text delta
    ↓
Privacy stream engine
    ↓
deanonymized delta
    ↓
OpenAI adapter
    ↓
SSE event
```

## Why SSE Envelope ≠ Text Stream

SSE events carry metadata (`event:`, `id:`, `retry:`), multi-line `data:`,
and JSON payloads that embed text at different paths (e.g.
`choices[0].delta.content` for OpenAI, `delta.text` for Anthropic).

If the proxy treated raw SSE bytes as text, it would corrupt envelopes and
transform metadata it should leave alone. The adapter layer separates
envelope handling from text transformation.
