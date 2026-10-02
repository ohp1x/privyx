---
description: Use Azure OpenAI through Privyx, with the v1 API or with deployment URLs. Both need routes, because Azure serves its API under /openai.
---

# Azure OpenAI

Azure OpenAI serves the OpenAI formats under an `/openai` prefix on your
resource's host. Privyx needs a route for each path you use. Without one,
requests are forwarded **unmasked**.

## The v1 API

Azure's v1 API works with the standard OpenAI client and needs no
`api-version`.

```yaml
# azure.yaml
proxy:
  routes:
    /openai/v1/chat/completions: openai
    /openai/v1/responses: responses
```

```bash
privyx proxy -c azure.yaml --upstream https://YOUR-RESOURCE-NAME.openai.azure.com
```

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/openai/v1/",
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
)

response = client.responses.create(
    model="my-deployment",  # your model deployment name
    input="Write a short greeting to alice@example.com",
)
print(response.output_text)
```

## Deployment URLs

The older API puts the deployment name in the path and takes an
`api-version`. Add one route per deployment; the query string is not part of
the route:

```yaml
# azure.yaml
proxy:
  routes:
    /openai/deployments/my-deployment/chat/completions: openai
```

```python
import os

from openai import AzureOpenAI

client = AzureOpenAI(
    azure_endpoint="http://localhost:8000",
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-10-21",
)

reply = client.chat.completions.create(
    model="my-deployment",
    messages=[{"role": "user", "content": "Write a short greeting to alice@example.com"}],
)
print(reply.choices[0].message.content)
```

## Good to know

- **Authentication.** Privyx relays the client's credential as it came: the
  `api-key` header, or an `Authorization` header with a key or a Microsoft
  Entra ID token.
- **Routes are exact paths.** A deployment without a route is forwarded
  unmasked. Set
  [`proxy.passthrough_unknown: false`](../guide/proxy.md#paths-without-a-route)
  so that a missing route shows up as a `403` instead.
- **Content filter results** and the other fields Azure adds to a reply pass
  through; Privyx restores tokens in every string of the reply.
