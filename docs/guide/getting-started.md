# Getting started

This page takes you from install to a client talking to a provider through
Privyx, with personal data masked on the way out and restored on the way back.

## Install

```bash
pip install privyx
# or
uv add privyx
```

That installs the proxy, the privacy engine, and the CLI. Extras switch on
optional components:

| Extra | Installs | Needed for |
|---|---|---|
| `sqlite` | aiosqlite | `vault.type: sqlite` |
| `redis` | redis | `vault.type: redis` |
| `providers` | OpenAI and Anthropic SDKs | the `llm` detector |
| `presidio` | Presidio, spaCy | the `presidio` detector |
| `faker` | Faker | the `faker` operator |
| `crypto` | cryptography | the `encrypt` operator |

Combine them as needed: `pip install 'privyx[sqlite,faker]'`. To run Privyx
in a container instead, see [Docker](../docker.md).

## See what gets masked

`privyx detect` runs the same detector the proxy uses, on a string:

```console
$ privyx detect --transform "mail alice@example.com or call +1 555 123 4567"
     5:22    EMAIL             'alice@example.com'
    31:46    PHONE             '+1 555 123 4567'

mail <PRIVYX_EMAIL_1> or call <PRIVYX_PHONE_2>
```

The second part is what the provider would receive.

## Start the proxy

```bash
privyx proxy --upstream https://api.openai.com
```

Then point a client at Privyx instead of the provider. Only the base URL
changes; the client keeps its own API key, which Privyx relays upstream:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1
```

For an Anthropic client, start the proxy with
`--upstream https://api.anthropic.com` and set
`ANTHROPIC_BASE_URL=http://localhost:8000`. One proxy forwards to one
upstream, so serving both providers at once takes two proxies on different
ports (`--port`).

A quick check with `curl`:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o-mini",
       "messages": [{"role": "user", "content": "Write a short greeting to alice@example.com"}]}'
```

The provider sees `<PRIVYX_EMAIL_1>`; the reply you get back has
`alice@example.com` in it again. `privyx audit tail` shows the exchange,
recorded without any of the content:

```bash
privyx audit tail --no-follow
```

The proxy has no authentication of its own. Keep it on `127.0.0.1` (the
default) unless something in front of it authenticates callers.

## Or wrap a tool with `privyx run`

`privyx run` starts a proxy on a free local port, launches a tool with its
base-URL variable pointed at it, and stops the proxy when the tool exits:

```bash
privyx run claude            # Claude Code → Privyx → Anthropic
privyx run codex             # Codex → Privyx → OpenAI
privyx run --list            # the tools it knows
```

For a tool it does not know, name the variable and the upstream yourself:

```bash
privyx run -u https://api.example.com --env-var MY_TOOL_BASE_URL -- my-tool --flag
```

`privyx run` also picks defaults suited to a chat tool: one session per
conversation, and an anchor secret in `~/.config/privyx/anchor.key` so
pseudonyms stay the same across turns and restarts. See
[Sessions and vault](sessions.md).

## Mask secrets as well

With no config file, Privyx detects five kinds of values: email addresses,
phone numbers, credit card numbers, IP addresses, and US social security
numbers. It does not detect API keys, passwords, or names.

The repository's
[`configs/default.yaml`](https://github.com/ohp1x/privyx/blob/main/configs/default.yaml)
adds patterns for secrets: vendor API keys, bearer tokens, JWTs, private keys,
passwords in URLs, and values assigned to secret-looking names. Download it and
pass it with `-c`:

```bash
privyx proxy -c default.yaml --upstream https://api.openai.com
```

Names, organizations, and project terms need a word list or an NLP/LLM
detector; see [Detection](detection.md).

## Check your setup

```console
$ privyx config
Privyx 0.1.5
  Host: 127.0.0.1:8000
  Upstream: http://localhost:20128
  Provider: generic
  Vault: memory
  Session: ephemeral
  Detector: regex
  Policy: default
  Operator: pseudonym
```

`privyx config --show` prints every setting with credentials masked, and
`privyx doctor` exercises the configured detector, vault, and proxy end to end.

## Next steps

- [Configuration](configuration.md): the config file, environment variables,
  and every setting.
- [Detection](detection.md): custom patterns, word lists, Presidio, and the
  LLM detector.
- [Masking](masking.md): what a detected value becomes, and how tokens look.
- [CLI reference](cli.md): every command and option.
