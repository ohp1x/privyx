<p align="center">
  <img src="https://raw.githubusercontent.com/ohp1x/privyx/main/docs/assets/logo.svg" width="88" height="88" alt="Privyx logo">
</p>

<h1 align="center">Privyx</h1>

<p align="center"><strong>Keep secrets and personal data out of LLM prompts.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/privyx/"><img src="https://img.shields.io/pypi/v/privyx" alt="PyPI"></a>
  <a href="https://pypi.org/project/privyx/"><img src="https://img.shields.io/pypi/pyversions/privyx" alt="Python versions"></a>
  <a href="https://github.com/ohp1x/privyx/actions/workflows/ci.yml"><img src="https://github.com/ohp1x/privyx/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://hub.docker.com/r/ohp1x/privyx"><img src="https://img.shields.io/docker/pulls/ohp1x/privyx" alt="Docker pulls"></a>
  <a href="https://github.com/ohp1x/privyx/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/privyx" alt="License"></a>
</p>

<p align="center">
  <a href="https://ohp1x.github.io/privyx/">Documentation</a> ·
  <a href="https://ohp1x.github.io/privyx/guide/getting-started/">Quickstart</a> ·
  <a href="https://ohp1x.github.io/privyx/tutorials/">Tutorials</a> ·
  <a href="https://ohp1x.github.io/privyx/integrations/">Integrations</a>
</p>

Privyx is a proxy between your tools and an AI provider. It replaces the API
keys, passwords, email addresses, and other sensitive values it detects in a
request with placeholders, and puts the originals back into the reply. The
only thing that changes in your client is its base URL.

![A terminal: privyx mask replaces a secret key and an email address in a prompt with tokens, and privyx unmask puts the email address back into the model's reply.](https://raw.githubusercontent.com/ohp1x/privyx/main/docs/assets/demo.gif)

## Quick start

Privyx needs Python 3.12 or newer.

```bash
pip install privyx
```

**With a coding agent.** `privyx run` starts a proxy, launches the tool through
it, and stops the proxy when the tool exits:

```bash
privyx run claude        # Claude Code; also: codex, aider
```

**With an application or any other client.** Start the proxy, then change the
client's base URL. The client keeps its own API key, which Privyx relays:

```bash
privyx proxy --upstream https://api.openai.com
export OPENAI_BASE_URL=http://localhost:8000/v1
```

For Anthropic: `--upstream https://api.anthropic.com` and
`ANTHROPIC_BASE_URL=http://localhost:8000`.

**Without installing.** See what would be masked in a text. Nothing is sent
anywhere:

```console
$ uvx privyx detect --transform "DB_PASSWORD=hunter2 deploy to 10.0.4.17, cc dana@acme.example"
    12:19    SECRET            'hunter2'
    30:39    IP_ADDRESS        '10.0.4.17'
    44:61    EMAIL             'dana@acme.example'

DB_PASSWORD=<PRIVYX_SECRET_1> deploy to <PRIVYX_IP_ADDRESS_2>, cc <PRIVYX_EMAIL_3>
```

The [Quickstart](https://ohp1x.github.io/privyx/guide/getting-started/) walks
through each of these, and there is a Docker image:
[`ohp1x/privyx`](https://ohp1x.github.io/privyx/docker/).

## How it works

![Your tool sends a request holding an email address and a password. Privyx forwards it with a placeholder in place of each and keeps the mapping in its session vault. The provider answers using the placeholder, and your tool gets the reply with the address back in it.](https://raw.githubusercontent.com/ohp1x/privyx/main/docs/assets/how-it-works.svg)

The model works with `<PRIVYX_EMAIL_1>`: it can reason about it, repeat it,
and hand it to a tool. The mapping back to the real value stays with Privyx.
More in [How it works](https://ohp1x.github.io/privyx/guide/how-it-works/).

## Who it is for

- **You use a coding agent.** Claude Code, Codex, or aider reads your `.env`,
  your logs, and your git history, and sends them to a provider.
  [Coding agents](https://ohp1x.github.io/privyx/tutorials/coding-agents/)
- **You build an application.** Your prompts carry your users' data. Keep it
  out of them with the OpenAI or Anthropic SDK, LangChain, LlamaIndex,
  LiteLLM, or the Vercel AI SDK.
  [An app on the OpenAI or Anthropic SDK](https://ohp1x.github.io/privyx/tutorials/sdk-app/)
- **You run a gateway for a team.** One proxy for everyone, with a shared
  vault, TLS, and an audit trail.
  [A shared gateway for a team](https://ohp1x.github.io/privyx/tutorials/team-gateway/)
- **You want to mask files.** Logs, JSON, and datasets, in a script or in CI,
  without a proxy.
  [Files and logs in a pipeline](https://ohp1x.github.io/privyx/tutorials/mask-files/)

## What it does

- **Masks secrets and personal data.** Built-in patterns cover email
  addresses, phone numbers, card numbers, IP addresses, API keys, tokens,
  private keys, and passwords. Add your own
  [word lists and patterns](https://ohp1x.github.io/privyx/tutorials/custom-terms/),
  or a detector that understands names: Presidio or an LLM.
- **Restores the reply.** In batch and streaming responses, in reasoning text,
  and in tool-call arguments, so your tools receive real values.
- **Drops in.** It speaks OpenAI Chat Completions and Responses and Anthropic
  Messages, so SDKs, frameworks, and coding tools only need a base URL.
- **Keeps sessions your way.** One mapping per request, per client, or per
  conversation, in memory, SQLite, or Redis.
- **Shows what it did.** A PII-safe audit trail and Prometheus metrics count
  what was masked, without recording any of it.
- **Fails closed.** A request it cannot mask is not forwarded.
- **Extends.** Plugins add detectors, operators, policies, vaults, and
  providers.

## What it does not do

Privyx reduces what a provider sees. It does not make a prompt safe by itself:

- Names, organizations, and project terms are only masked once you configure
  a word list or a detector for them.
- Only chat requests are masked. Other API paths, such as embeddings, are
  forwarded as the client sent them, unless you tell Privyx to refuse them.
- Images, audio, and other binary content are not inspected.
- The proxy has no authentication of its own. Keep it on `127.0.0.1`, or put
  something in front of it that authenticates callers.

[Limitations](https://ohp1x.github.io/privyx/security/limitations/) has the full
list, and the [threat model](https://ohp1x.github.io/privyx/security/threat-model/)
says what Privyx protects against.

## Documentation

The documentation is at [ohp1x.github.io/privyx](https://ohp1x.github.io/privyx/):

- [Quickstart](https://ohp1x.github.io/privyx/guide/getting-started/) and
  [tutorials](https://ohp1x.github.io/privyx/tutorials/)
- Guides to [detection](https://ohp1x.github.io/privyx/guide/detection/),
  [masking](https://ohp1x.github.io/privyx/guide/masking/),
  [sessions](https://ohp1x.github.io/privyx/guide/sessions/), and
  [deployment](https://ohp1x.github.io/privyx/guide/deployment/)
- [Integrations](https://ohp1x.github.io/privyx/integrations/) with providers
  and frameworks
- Reference for [configuration](https://ohp1x.github.io/privyx/guide/configuration/),
  the [CLI](https://ohp1x.github.io/privyx/guide/cli/), and
  [errors](https://ohp1x.github.io/privyx/reference/errors/)
- [Troubleshooting](https://ohp1x.github.io/privyx/guide/troubleshooting/) and
  the [FAQ](https://ohp1x.github.io/privyx/guide/faq/)

Its source is in [`docs/`](https://github.com/ohp1x/privyx/tree/main/docs).

## Project

Privyx is in its `0.1.x` series; the
[changelog](https://github.com/ohp1x/privyx/blob/main/CHANGELOG.md) lists what
each release changed.

- Questions and ideas: [Discussions](https://github.com/ohp1x/privyx/discussions)
- Bugs: [Issues](https://github.com/ohp1x/privyx/issues)
- Security: [report privately](https://github.com/ohp1x/privyx/security/advisories/new);
  see the [security policy](https://github.com/ohp1x/privyx/blob/main/SECURITY.md)
- Contributing: [CONTRIBUTING.md](https://github.com/ohp1x/privyx/blob/main/CONTRIBUTING.md)

## License

[MIT](https://github.com/ohp1x/privyx/blob/main/LICENSE)
