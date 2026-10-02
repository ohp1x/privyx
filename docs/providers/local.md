---
description: Use Ollama and other local OpenAI-compatible servers behind Privyx, and use a local model as Privyx's own LLM detector.
---

# Ollama and local servers

A model server on your own machine speaks, in most cases, the OpenAI format:
Ollama, vLLM, llama.cpp's server, LM Studio. Privyx works with them like with
any OpenAI-compatible provider.

If the model runs on your own machine, your text does not leave it, and
masking adds little. Two cases remain where Privyx is useful with a local
server: an inference server shared with other people, and a local model that
does the *detecting* for requests to a cloud provider.

## In front of a local server

Ollama listens on port 11434 and serves the OpenAI format under `/v1`:

```bash
privyx proxy --upstream http://localhost:11434
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="ollama")  # a key is required but ignored

reply = client.chat.completions.create(
    model="llama3.2",
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(reply.choices[0].message.content)
```

For another server, replace the upstream with its address.

Masked: `/v1/chat/completions` and `/v1/responses`. Forwarded as sent:
`/v1/embeddings`, the legacy `/v1/completions`, and Ollama's native API under
`/api/`. A client that uses the native API, as the `ollama` command itself
does, is not masked.

## A local model as the detector

The [`llm` detector](../guide/detection.md#llm-detector) sends the text it
scans, unmasked, to a model. Pointing it at a local model keeps that text on
your machine while the chat itself goes to a cloud provider:

```yaml
# privyx.yaml
detector:
  - type: regex
  - type: llm
    llm_provider: openai
    llm_model: llama3.2            # a model your local server has
```

```bash
export OPENAI_BASE_URL=http://localhost:11434/v1   # read by the detector's SDK
export OPENAI_API_KEY=ollama
privyx proxy -c privyx.yaml --upstream https://api.anthropic.com
```

The two variables are read by the detector, in the proxy's environment. Your
client's own base URL still points at Privyx.

How well this works depends on the model: it must answer with a JSON list of
what it found. If it does not, the scan fails and the request is not
forwarded. Try it first:

```bash
privyx detect -c privyx.yaml --transform "Bob Marsh called about the Bluebird contract"
```
