// Loaded by `privyx run pi` as `pi -e <this file>`. pi has no base-URL variable
// or flag, so this points pi's providers at the local proxy for this run only,
// leaving the user's models.json untouched. With PRIVYX_PI_PROVIDER, only that
// provider goes through the proxy. Otherwise every provider does, and each
// request names its provider's base URL for each API, which the proxy forwards
// to. A model whose requests would not go through the proxy is refused: at
// startup, when the model changes, and before any prompt is sent with it.

// pi's APIs whose requests privyx can mask, and the proxy's name for each.
// Mistral's is OpenAI's Chat Completions. Any other is refused: its paths are
// not proxy routes, or it does not send to the model's base URL at all (Azure's
// endpoint variables win over it).
const SCHEMAS = {
  "anthropic-messages": "anthropic",
  "openai-completions": "openai",
  "openai-responses": "responses",
  "google-generative-ai": "gemini",
  "mistral-conversations": "openai",
};

export default function (pi) {
  const provider = process.env.PRIVYX_PI_PROVIDER;
  const baseUrl = process.env.PRIVYX_PI_BASE_URL;
  // pi loads this file afresh for each new, resumed, or reloaded session, so
  // what the first load learned is kept on the process.
  const kept = (globalThis[Symbol.for("privyx.pi")] ??= {});
  // The commands pi runs inherit its environment; they need no token.
  const token = (kept.token ??= process.env.PRIVYX_PI_TOKEN);
  delete process.env.PRIVYX_PI_TOKEN;
  if (!baseUrl || !(provider || token)) {
    throw new Error("PRIVYX_PI_BASE_URL and PRIVYX_PI_PROVIDER or PRIVYX_PI_TOKEN must be set");
  }
  if (provider) pi.registerProvider(provider, { baseUrl });

  // provider -> schema -> its base URL; null where two differ. Taken from the
  // first session only: on a reload, pi's models already point at the proxy.
  let bases;
  const follow = (registry) => {
    bases = kept.bases;
    if (!bases) {
      bases = kept.bases = {};
      for (const model of registry.getAll()) {
        const schema = SCHEMAS[model.api];
        if (!schema || model.baseUrl === baseUrl) continue;
        const own = (bases[model.provider] ??= {});
        own[schema] = [undefined, model.baseUrl].includes(own[schema]) ? model.baseUrl : null;
      }
    }
    for (const [name, own] of Object.entries(bases)) {
      const upstreams = Object.fromEntries(Object.entries(own).filter(([, url]) => url));
      pi.registerProvider(name, {
        baseUrl,
        headers: { "x-privyx-run-token": token, "x-privyx-upstreams": JSON.stringify(upstreams) },
      });
    }
  };

  // The model as registered now, past the override above.
  const current = (model, registry) =>
    model && (registry.find(model.provider, model.id) ?? model);
  // Why the model's requests would not go through privyx, or undefined.
  const refusal = async (model, registry) => {
    const name = `privyx: ${model.provider}/${model.id}`;
    const choose = provider
      ? `choose one of its models, or use a privyx profile for ${model.provider}`
      : "choose another model";
    if (!SCHEMAS[model.api]) {
      return `${name} uses pi's ${model.api} API, which privyx cannot mask; ${choose}`;
    }
    const own = bases?.[model.provider]?.[SCHEMAS[model.api]];
    if (!provider && own === null) {
      return (
        `${name} cannot go through privyx: pi's ${model.provider} provider ` +
        `serves its ${model.api} API from more than one base URL; ${choose}`
      );
    }
    if (model.baseUrl !== baseUrl || (!provider && !own)) {
      const serves = provider ? `pi's ${provider} provider` : "the models pi had at startup";
      return `${name} would bypass privyx, which serves only ${serves} in this run; ${choose}`;
    }
    // A login can carry a base URL of its own (GitHub Copilot's), which pi
    // then sends to instead.
    const auth = await registry.getApiKeyAndHeaders(model);
    if (auth.ok && auth.baseUrl && auth.baseUrl !== baseUrl) {
      return `${name} would bypass privyx: its login sends to a base URL of its own; ${choose}`;
    }
  };

  pi.on("session_start", async (_event, ctx) => {
    if (!provider && !bases) follow(ctx.modelRegistry);
    const model = current(ctx.model, ctx.modelRegistry);
    const message = model && (await refusal(model, ctx.modelRegistry));
    if (!message) return;
    // pi's screen hides what is written while it runs, and pi exits 0 once
    // shut down: report the refusal after it has exited, as a failure.
    process.once("exit", () => {
      console.error(message);
      process.exitCode = 1;
    });
    ctx.shutdown();
  });

  pi.on("model_select", async (event, ctx) => {
    const message = await refusal(current(event.model, ctx.modelRegistry), ctx.modelRegistry);
    if (!message) return;
    ctx.ui.notify(message, "error");
    const previous = current(event.previousModel, ctx.modelRegistry);
    if (previous && !(await refusal(previous, ctx.modelRegistry))) await pi.setModel(previous);
  });

  // shutdown() does not stop a prompt already queued (pi -p), so block it here.
  pi.on("input", async (_event, ctx) => {
    const model = current(ctx.model, ctx.modelRegistry);
    const message = model && (await refusal(model, ctx.modelRegistry));
    if (!message) return { action: "continue" };
    process.exitCode = 1;
    ctx.ui.notify(message, "error");
    return { action: "handled" };
  });
}
