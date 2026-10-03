# Detection

Two settings decide what gets masked: the `detector` finds sensitive values,
and the `policy` decides which of those findings are masked.

Try a config before putting it in front of real traffic. `privyx detect` uses
the same detector and policy as the proxy:

```bash
privyx detect -c my.yaml "ann from acme, ann@acme.example"
privyx detect -c my.yaml --transform "..."   # also print the masked text
privyx detect -c my.yaml --no-policy "..."   # every detection, before the policy
```

## Built-in patterns

With no configuration, the `regex` detector finds:

| Entity | Matches |
|---|---|
| `EMAIL` | Email addresses |
| `PHONE` | Phone numbers of three digit groups (`555-010-4477`), or two with a `+<country>` code, an area code in parentheses, or a leading 0 (`(021) 5550-1234`); without separators, in E.164 form (`+6281234567890`) or starting with 08 |
| `CREDIT_CARD` | Runs of 13 to 16 digits, optionally split by spaces or dashes, that pass the Luhn checksum like every card number |
| `IP_ADDRESS` | IPv4 addresses, private ones included, except loopback (`127.0.0.1`), `0.0.0.0`, and the documentation ranges (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`) |
| `SSN` | US social security numbers (`123-45-6789`) |
| `API_KEY` | Vendor-prefixed keys (OpenAI and Anthropic `sk-`, Stripe, GitHub, GitLab, AWS, Google, Slack, Hugging Face, and more) and webhook URLs |
| `JWT` | JSON Web Tokens |
| `PRIVATE_KEY` | PEM private key blocks, even when cut off before the end |
| `AUTH_TOKEN` | The credential in `Authorization: Bearer …` or `Basic …` |
| `URL_CREDENTIAL` | The password in `scheme://user:password@host` |
| `SECRET` | A value assigned to a secret-looking name (`DB_PASSWORD=…`, `api_key: …`, `"token": "…"`) in `.env`, YAML, JSON, shell, and code |

Names and anything specific to you need more configuration.

## Secrets

Most built-in secret patterns match a recognizable shape, such as a vendor
prefix or a PEM header. A password or key without one, such as
`DB_PASSWORD=hunter2`, is found by its name: `SECRET` masks the value assigned
to a name such as `password`, `token`, `secret`, `api_key`, or an upper-case
`…_KEY`. It leaves alone what cannot be the secret:

- a variable that holds it: `api_key=api_key`, `SECRET_KEY = os.environ[…]`;
- a name that describes it: `TOKEN_URL`, `token_type`, `KEY_FILE`;
- a placeholder or punctuation: `sk-...`, `${API_KEY:-}`, `**Token:**`;
- a sentence after a colon, as in a docstring: `api_key: When set, …`.

## Detector types

| `type` | Finds | Needs |
|---|---|---|
| `regex` (default) | The built-in patterns plus your `patterns` and `terms` | Nothing |
| `yaml` | Only your `patterns` and `terms` | Nothing |
| `presidio` | Names, organizations, locations, and more, with NLP | `privyx[presidio]` and a spaCy model |
| `llm` | Context-dependent PII, by asking a language model | `privyx[providers]` and a provider API key |

A plugin can add more types; see [Plugins](../development/plugins.md).

## Your own patterns

`patterns` maps an entity name to a regular expression. For `regex`, they are
added to the built-in set, and a pattern with a built-in name replaces that
one:

```yaml
detector:
  type: regex
  patterns:
    EMPLOYEE_ID: '\bEMP-\d{6}\b'
    PASSWORD: 'PASSWORD=(?P<value>\S+)'
```

- The entity name becomes the `{type}` in the token (`<PRIVYX_EMPLOYEE_ID_1>`).
  It must start with a letter and contain only letters, digits, and
  underscores, up to 64 characters.
- A pattern that replaces a built-in one is used as written, without the
  built-in's check, such as the Luhn checksum for `CREDIT_CARD`.
- Patterns are case-sensitive. Start one with `(?i)` to ignore case.
- A group named `value` masks only that part of the match: the pattern above
  masks the password and leaves `PASSWORD=` for the model to read.
- In YAML, write patterns in single quotes or a `|` block so backslashes stay
  as they are. Inside double quotes, every backslash must be doubled.

## Word lists

`terms` maps an entity name to plain strings. Privyx escapes them, so no regex
knowledge is needed:

```yaml
detector:
  type: regex
  terms:
    PERSON:       [ann, bob]
    ORGANIZATION: [acme, initech]
    URL:          ["https://git.internal.example/team"]
```

Matching ignores case. Longer terms are tried first, so a compound term wins
over a shorter term inside it. A term only matches as a whole word: `ann` does
not match inside `annual`.

`terms` and `patterns` can be used together. If both name the same entity, the
`patterns` regex wins.

## Presidio

[Presidio](https://microsoft.github.io/presidio/) finds names, locations,
organizations, and other entities with a spaCy language model:

```bash
pip install 'privyx[presidio]'
python -m spacy download en_core_web_sm
```

```yaml
detector:
  type: presidio
  language: en
  model: ""               # empty → en_core_web_sm
  entities: []            # empty → every Presidio recognizer
  score_threshold: 0.35   # drop findings below this confidence
```

If Presidio or the model is missing, Privyx fails at startup and prints the
command that installs it.

## LLM detector

The `llm` detector asks a language model to list the PII in the text. It
catches what patterns cannot, such as a name that is also a common word, at the
cost of a model call per request.

```yaml
detector:
  type: llm
  llm_provider: openai          # or anthropic
  llm_model: ""                 # empty → gpt-4o-mini / claude-haiku-4-5
  llm_timeout: 30               # seconds per scan
  llm_max_chars: 4000           # longer text is scanned in overlapping chunks
  llm_fallback_on_error: false
```

Set `llm_model` to a model your account can use; the built-in default for a
provider can be one that provider has since retired.

The text is sent *unmasked* to that provider, so use one you already trust with
it. The key comes from `llm_api_key`, or else the SDK's own variable
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). The SDK also reads `OPENAI_BASE_URL` /
`ANTHROPIC_BASE_URL` from the proxy's environment, which can point the detector
at a local model. Do not let it point at Privyx itself.

If a scan fails, times out, or the reply has no list of findings, the request
fails with `503` and a `privyx_scan_failed` error rather than going upstream
with a weaker scan. With `llm_fallback_on_error: true` the request is scanned
with the built-in patterns instead, which lets through whatever only the model
would have caught. Every fallback is counted in the audit trail as
`detector_counts.llm_fallbacks`.

## Several detectors at once

Give `detector` a list to run several detectors. Their findings are pooled:

```yaml
detector:
  - type: regex             # structured PII and your terms
    terms:
      PROJECT: [bluebird]
  - type: presidio          # names, organizations, locations
  - type: llm               # what the others miss
```

Each item is a complete detector config. A `regex` item includes the built-in
patterns; a `yaml` item has only its own.

## Cache

Detections are cached by text, so in a multi-turn conversation the earlier
turns are not scanned again. The cache is on by default and holds 10,000
entries:

```yaml
detector:
  type: regex
  cache:
    enabled: true
    max_size: 10000
```

`cache: false` or `PRIVYX_DETECTOR_CACHE=false` turns it off.

## Policy

The policy filters what the detector found.

- `default` masks every detection.
- `strict` masks only the entity types in `allowed`, and leaves the rest in
  the text, built-in secret types included. An empty `allowed` means every
  built-in entity.

```yaml
policy:
  type: strict
  allowed: [EMAIL, PHONE, SSN, CREDIT_CARD, IP_ADDRESS, EMPLOYEE_ID]
```

With `strict`, a custom entity that is missing from `allowed` is detected and
then left in the text. To spot one, compare `privyx detect --transform` with
and without `--no-policy`:

```console
$ privyx detect -c strict.yaml --transform "bluebird mail a@b.io"
    14:20    EMAIL             'a@b.io'

bluebird mail <PRIVYX_EMAIL_1>
$ privyx detect -c strict.yaml --transform --no-policy "bluebird mail a@b.io"
     0:8     PROJECT           'bluebird'
    14:20    EMAIL             'a@b.io'

<PRIVYX_PROJECT_1> mail <PRIVYX_EMAIL_2>
```
