// Loaded by `privyx run pi` as `pi -e <this file>`. pi has no base-URL variable
// or flag, so this points one pi provider at the local proxy for this run only,
// leaving the user's models.json untouched. A model whose requests would not
// go through the proxy is refused: at startup, when the model changes, and
// before any prompt is sent with it.

export default function (pi) {
  const provider = process.env.PRIVYX_PI_PROVIDER;
  const baseUrl = process.env.PRIVYX_PI_BASE_URL;
  if (!provider || !baseUrl) {
    throw new Error("PRIVYX_PI_PROVIDER and PRIVYX_PI_BASE_URL must be set");
  }
  pi.registerProvider(provider, { baseUrl });

  const proxied = (model) => model?.baseUrl === baseUrl;
  const reason = (model) =>
    `privyx: ${model.provider}/${model.id} would bypass privyx, which serves ` +
    `only pi's ${provider} provider in this run; choose one of its models, ` +
    `or use a privyx profile for ${model.provider}`;

  pi.on("session_start", (_event, ctx) => {
    if (ctx.model && !proxied(ctx.model)) {
      process.exitCode = 1;
      console.error(reason(ctx.model));
      ctx.shutdown();
    }
  });

  pi.on("model_select", async (event, ctx) => {
    if (proxied(event.model)) return;
    ctx.ui.notify(reason(event.model), "error");
    if (proxied(event.previousModel)) await pi.setModel(event.previousModel);
  });

  // shutdown() does not stop a prompt already queued (pi -p), so block it here.
  pi.on("input", (_event, ctx) => {
    if (!ctx.model || proxied(ctx.model)) return { action: "continue" };
    process.exitCode = 1;
    ctx.ui.notify(reason(ctx.model), "error");
    return { action: "handled" };
  });
}
