---
description: Use the Vercel AI SDK with Privyx by creating the OpenAI or Anthropic provider with a baseURL.
---

# Vercel AI SDK

The AI SDK's providers take a `baseURL`. Create the provider with Privyx's
address, and `generateText`, `streamText`, and tool calls go through it.

## OpenAI

```bash
privyx proxy --upstream https://api.openai.com
```

```js
import { createOpenAI } from "@ai-sdk/openai";
import { generateText } from "ai";

const openai = createOpenAI({ baseURL: "http://localhost:8000/v1" });

const { text } = await generateText({
  model: openai("gpt-4o-mini"),
  prompt: "Write a short greeting to alice@example.com",
});
console.log(text);
```

The provider calls OpenAI's Responses API for this model, on `/v1/responses`,
which Privyx masks like Chat Completions.

Streaming:

```js
import { createOpenAI } from "@ai-sdk/openai";
import { streamText } from "ai";

const openai = createOpenAI({ baseURL: "http://localhost:8000/v1" });

const result = streamText({
  model: openai("gpt-4o-mini"),
  prompt: "Write a short greeting to alice@example.com",
});
for await (const delta of result.textStream) {
  process.stdout.write(delta);
}
console.log();
```

## Anthropic

```bash
privyx proxy --upstream https://api.anthropic.com
```

```js
import { createAnthropic } from "@ai-sdk/anthropic";
import { generateText } from "ai";

const anthropic = createAnthropic({ baseURL: "http://localhost:8000/v1" });

const { text } = await generateText({
  model: anthropic("claude-opus-5-5"),
  prompt: "Write a short greeting to alice@example.com",
});
console.log(text);
```

Unlike the Anthropic SDK itself, this provider's `baseURL` includes `/v1`.

## Without changing code

The default `openai` provider reads `OPENAI_BASE_URL`:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1
```

## Good to know

- **Run Privyx next to your server code.** The base URL above is reached from
  where the AI SDK runs: your route handlers or server actions, not the
  browser. In production, use the address of a
  [shared gateway](../tutorials/team-gateway.md).
- **Embeddings are not masked.** `embed` and `embedMany` call
  `/v1/embeddings`, which Privyx forwards as sent.
