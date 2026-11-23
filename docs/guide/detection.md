---
description: What Privyx detects out of the box, opt-in detectors, word lists and patterns, Presidio and LLM detectors, the cache, and the policy.
---

# Detection

Two settings decide what gets masked: the `detectors` find sensitive values,
and the `policy` decides which of those findings are masked.

Try a config before putting it in front of real traffic. `privyx detect` uses
the same detector and policy as the proxy:

```bash
privyx -c my.yaml detect "ann from acme, ann@acme.example"
privyx -c my.yaml detect --transform "..."   # also print the masked text
privyx -c my.yaml detect --no-policy "..."   # every detection, before the policy
```

## Built-in patterns

With no configuration, the `regex` detector finds:

| Entity | Matches |
|---|---|
| `EMAIL` | Email addresses |
| `PHONE` | Phone numbers of three digit groups (`555-010-4477`), or two with a `+<country>` code, an area code in parentheses, or a leading 0 (`(021) 5550-1234`); without separators, in E.164 form (`+6281234567890`) or starting with 08 |
| `CREDIT_CARD` | Runs of 13 to 16 digits, optionally split by spaces or dashes, that pass the Luhn checksum like every card number and do not start with 1, as a Unix time in milliseconds or microseconds does (only UATP airline cards start with 1) |
| `IP_ADDRESS` | IPv4 addresses, private ones included, except loopback (`127.0.0.1`), `0.0.0.0`, and the documentation ranges (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`) |
| `SSN` | US social security numbers (`123-45-6789`) |
| `API_KEY` | Vendor-prefixed keys (OpenAI and Anthropic `sk-`, Stripe, GitHub, GitLab, AWS, Google, Slack, Hugging Face, and more) and webhook URLs |
| `JWT` | JSON Web Tokens |
| `PRIVATE_KEY` | PEM private key blocks, even when cut off before the end |
| `AUTH_TOKEN` | The credential in `Authorization: Bearer …` or `Basic …` |
| `URL_CREDENTIAL` | The password in `scheme://user:password@host` |
| `SECRET` | A value assigned to a secret-looking name (`DB_PASSWORD=…`, `api_key: …`, `"token": "…"`) in `.env`, YAML, JSON, shell, and code |
| `CRYPTO_WALLET` | Bitcoin addresses (`1…`, `3…`, `bc1…`) with a valid checksum, EVM addresses (`0x` and 40 hex digits), and Solana addresses next to a word such as `wallet` or `solana` |
| `ID_NIK` | Indonesian NIK: 16 digits with a known province code and a real birth date |
| `ID_NPWP` | Indonesian NPWP, written `01.234.567.8-901.000`, or 15 bare digits next to `npwp` |
| `SG_NRIC_FIN` | Singapore NRIC/FIN with a correct check letter (`S1234567D`) |
| `MY_NRIC` | Malaysian MyKad number with a real birth date (`850101-14-5678`) |
| `ES_NIF`, `ES_NIE` | Spanish DNI and NIE with a correct check letter |
| `UK_NHS` | UK NHS numbers with a correct check digit, next to `nhs` |
| `IN_AADHAAR` | Indian Aadhaar numbers with a correct check digit, next to `aadhaar` |

The wallet and ID entities are built in from 0.2: each passes a checksum or
structure check. A `regex` detector with `builtins: false` takes
them with `enable`; `enable` lists them in a `regex` detector that keeps its
built-ins to no effect, so a config written for 0.1 still loads. To turn any of them off, list it under
[`disable`](#turning-a-built-in-off).

Names and anything specific to you need more configuration.

## Opt-in detectors

These are off until a `regex` detector lists them under `enable`,
since each also catches more of what is not sensitive:

```yaml
detectors:
  regex:
    enable: [HIGH_ENTROPY, MRN]
    entropy_threshold: 3.0     # HIGH_ENTROPY: bits per character
```

| Entity | Matches |
|---|---|
| `HIGH_ENTROPY` | Strings of 16 to 512 characters that mix upper case, lower case, and digits at random, such as a key without a known prefix. Hex (git hashes, UUIDs) and identifiers made of words (`OAuth2AuthorizationCode`) are left alone. Raise `entropy_threshold` to mask fewer |
| `ASSIGNED_SECRET` | A value of 12 characters or more with letters and digits or symbols, assigned to any name in `.env`, YAML, JSON, or code (`FOO_BAR="…"`). Not words, numbers, paths, URLs, hosts, dates, versions, variables, or values of names such as `*_id`, `*_url`, `model`, or `version` |
| `MRN` | A medical record number after its label: `MRN`, `medical record number`, `No. RM`, `nomor rekam medis` |
| `ICD10` | ICD-10 diagnosis codes (`E11.9`, `S72.001A`, `U07.1`) next to a word such as `diagnosis`, `dx`, or `icd` |

## Context words

`context` makes an entity count only when one of its words is within five
words of the match. It turns a pattern that would match too much into one you
can use:

```yaml
detectors:
  regex:
    patterns:
      PASSPORT: \b[A-Z]\d{7}\b
    context:
      PASSPORT:
      - passport
      - paspor
      - travel document
```

Words are matched whole and ignoring case; an entry may be a phrase. For a
built-in entity, the list replaces its own words (`UK_NHS`, `IN_AADHAAR`, and
`ICD10` have some) or adds the requirement to one without them. `context` for
an entity the detector does not detect stops Privyx at startup. The `presidio`
detector weighs context words on its own and ignores this setting.

## Hidden values

An agent that is kept from sending a secret may split or encode it. With
`decode: true`, a `regex` detector also looks inside:

- string literals joined by `+`, `.`, or `..`, and `chr(…)` calls:
  `'s' + 'k' + '-' + …`;
- Base64 (standard and URL-safe) and hex, up to two levels deep;
- URL escapes: `ana%40example.com`.

What it finds there masks the whole expression or blob, under the entity found
inside (`API_KEY`, `EMAIL`). URL-escaped text is the exception: only the value
itself is masked. Blobs over 64 KiB are not decoded, and a scan decodes at most
1,000,000 characters in all, so the cost stays bounded; it is still extra work on
every request, which is why it is off by default.

## Look-alike characters

Text copied from a web page, a PDF, or an input method can spell a value with
characters that look the same but match no pattern or term: fullwidth letters
(`ｊｏｈｎ`), ligatures (`ﬁ`), or an invisible zero-width space or soft hyphen
inside a word. With `normalize: true`, a `regex` detector also scans
the text with each character folded to its plain form (Unicode NFKC) and those
invisible format characters dropped. What it finds there is masked as written:
`ｊｏｈｎ` becomes the token, and restoring brings back `ｊｏｈｎ`. A value whose
name or context word is written that way, as in `ｐａｓｓｗｏｒｄ: …`, is found too.

```yaml
detectors:
  regex:
    normalize: true
    terms:
      PERSON:
      - john
```

Accents are not folded (`jóhn` is another name), nor are letters from other
scripts that look Latin, such as a Cyrillic `о`. Text with nothing to fold costs
almost nothing more; text with such characters is scanned twice.

## Secrets

Most built-in secret patterns match a recognizable shape, such as a vendor
prefix or a PEM header. A password or key without one, such as
`DB_PASSWORD=hunter2`, is found by its name: `SECRET` masks the value assigned
to a name such as `password`, `token`, `secret`, `api_key`, or an upper-case
`…_KEY`. It leaves alone what cannot be the secret:

- a variable that holds it: `api_key=api_key`, `SECRET_KEY = os.environ[…]`;
- a name that describes it: `TOKEN_URL`, `token_type`, `KEY_FILE`, or counts
  it: `token_count`, `pass_rate`, `TOKEN_LIMIT`, `session_token_ttl`;
- a placeholder or punctuation: `sk-...`, `${API_KEY:-}`, `**Token:**`;
- a sentence after a colon, as in a docstring: `api_key: When set, …`.

In JSON, such as tool arguments or `privyx mask -f json`, the key is the name:
the string in `{"password": "hunter2"}` is masked too. A number is checked
alone, so `{"password": 123456}` is not.

## Detector types

| `type` | Finds | Needs |
|---|---|---|
| `regex` (default) | The built-in patterns plus your `patterns` and `terms` | Nothing |
| `regex` with `builtins: false` | Only your `patterns` and `terms` | Nothing |
| `presidio` | Names, organizations, locations, and more, with NLP | `privyx[presidio]` and a spaCy model |
| `llm` | Context-dependent PII, by asking a language model | `privyx[providers]` and a provider API key |

A plugin can add more types; see [Plugins](../development/plugins.md).

`patterns`, `terms`, `enable`, and the other pattern settings apply to `regex`
only. A `presidio` or `llm` detector does not use them, and Privyx
refuses them beside its `type`; see
[Settings per type](configuration.md#settings-per-type).

## Your own patterns

`patterns` maps an entity name to a regular expression. For `regex`, they are
added to the built-in set, and a pattern with a built-in name replaces that
one:

```yaml
detectors:
  regex:
    patterns:
      EMPLOYEE_ID: \bEMP-\d{6}\b
      PASSWORD: PASSWORD=(?P<value>\S+)
```

- The entity name becomes the `{type}` in the token (`<PRIVYX_EMPLOYEE_ID_5E47FE8CF7B42F4E>`).
  It must start with a letter and contain only letters, digits, and
  underscores, up to 64 characters.
- A pattern that replaces a built-in one is used as written, without the
  built-in's check, such as the Luhn checksum for `CREDIT_CARD`.
- Patterns are case-sensitive. Start one with `(?i)` to ignore case.
- A group named `value` masks only that part of the match: the pattern above
  masks the password and leaves `PASSWORD=` for the model to read.
- In YAML, write patterns in single quotes or a `|` block so backslashes stay
  as they are. Inside double quotes, every backslash must be doubled.
- A pattern with a repeat inside a repeat, such as `(\d+)+` or `(\w+\s?)*`,
  logs a warning at startup: on a crafted input it can backtrack for minutes
  and stall the proxy. Make the inner quantifier possessive (`(\d++)+`) or
  drop the outer one. The check misses alternatives that overlap, like `(a|a)+`.

## Turning a built-in off

`disable` turns off built-in entities of the `regex` detector. It also accepts
opt-in entity names, so a config can opt out before an upgrade enables one by
default:

```yaml
detectors:
  regex:
    disable:
    - CREDIT_CARD
```

- Only the built-in pattern goes: your own `patterns` and `terms` for the
  entity still apply.
- An opt-in entity that is not enabled is a no-op, such as
  `disable: [HIGH_ENTROPY, MRN]` in a config without `enable`.
- Listing the same entity in `enable` and `disable` stops the config from
  loading. Remove it from `enable` to opt out.
- An unknown name, such as a typo, stops the config from loading.
- Built-in patterns overlap. With `CREDIT_CARD` off, `PHONE` can find part of
  a card number written in groups and send the rest as is:
  `4111 1111 1111 1111` becomes `<PRIVYX_PHONE_80091F7A92974293> 1111`.

## Word lists

`terms` maps an entity name to plain strings. Privyx escapes them, so no regex
knowledge is needed:

```yaml
detectors:
  regex:
    terms:
      PERSON:
      - ann
      - bob
      ORGANIZATION:
      - acme
      - initech
      URL:
      - https://git.internal.example/team
```

Matching ignores case. Longer terms are tried first, so a compound term wins
over a shorter term inside it. A term only matches as a whole word: `ann` does
not match inside `annual`.

A term of several words also matches its other spellings (`term_variants`,
on by default since 0.2): `Acme Corp` or `AcmeCorp` matches `acmecorp`,
`acme-corp`, `acme_corp`, `ACME_CORP`, and `acme.corp`. Set
`term_variants: false` to match only the spelling as written:

```yaml
detectors:
  regex:
    term_variants: false
    terms:
      ORGANIZATION:
      - Acme Corp
```

A word is split at a case change only when each part keeps two letters, so
`iPhone` stays one word. A term with other characters, such as a
URL, matches only as written.

`_` separates words (`underscore_separates_words`, on by default since 0.2):
`acme` matches in `ACME_API_KEY` and `acme_billing`, `john` and `smith` in
`JOHN__SMITH`, and still not in `johnson`. The text itself is not changed:
`ACME_API_KEY` is sent as `<PRIVYX_ORGANIZATION_1B30D33813A652F8>_API_KEY`. The built-in
patterns follow it too, so `host_10.0.0.1` and `my_sk-…` are found, while a
secret assigned to `DB_PASSWORD` keeps its name. Set
`underscore_separates_words: false` to count `_` as part of a word, as 0.1
did, so a term is not found in a name such as `ACME_API_KEY` or `DB_JOHN`:

```yaml
detectors:
  regex:
    underscore_separates_words: false
    terms:
      ORGANIZATION:
      - acme
```

`terms` and `patterns` can be used together, also for the same entity
(`merge_terms`, on by default since 0.2). Terms are tried first, then the
regex; terms keep their case-insensitive word boundaries, `term_variants`, and
`underscore_separates_words` settings. The regex keeps its own flags, anchors,
and `value` group. If both alternatives match at the same position, the term
wins even when the regex could match more text.

```yaml
detectors:
  regex:
    terms:
      PERSON:
      - john smith
    patterns:
      PERSON: \bEMP-\d{6}\b
```

This finds both `JOHN SMITH` and `EMP-123456` as `PERSON`. The option applies
to `regex`, and does not merge custom patterns with built-in
patterns. With `merge_terms: false`, as in 0.1, the `patterns` regex wins and
the terms of that entity are not matched; Privyx warns when it loads such a
config.

To see which spellings of your terms still get through with your settings,
and which of these options masks each, run
[`privyx config lint`](cli.md#privyx-config-lint).

## Presidio

[Presidio](https://microsoft.github.io/presidio/) finds names, locations,
organizations, and other entities with a spaCy language model:

```bash
pip install 'privyx[presidio]'
python -m spacy download en_core_web_sm
```

```yaml
detectors:
  regex: false              # replaces the built-in patterns
  presidio:
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
detectors:
  regex: false                  # replaces the built-in patterns
  llm:
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

List several detectors under `detectors` to run them together. Their findings
are pooled:

```yaml
detectors:
  regex:                    # structured PII and your terms
    terms:
      PROJECT: [bluebird]
  presidio: {}              # names, organizations, locations
  llm: {}                   # what the others miss
```

Each entry is a complete detector config. `regex` includes the built-in
patterns unless it sets `builtins: false`. Entries from a config file and the
file it includes are merged by name, and `<name>: false` in the upper file turns
off the entry from the lower one.

## Cache

Detections are cached by text, so in a multi-turn conversation the earlier
turns are not scanned again. The cache is on by default and holds 10,000
entries:

```yaml
detectors:
  regex:
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
  built-in entity, opt-in ones included.

```yaml
policy:
  type: strict
  allowed: [EMAIL, PHONE, SSN, CREDIT_CARD, IP_ADDRESS, EMPLOYEE_ID]
```

With `strict`, a custom entity that is missing from `allowed` is detected and
then left in the text. To spot one, compare `privyx detect --transform` with
and without `--no-policy`:

```console
$ privyx -c strict.yaml detect --transform "bluebird mail a@b.io"
    14:20    EMAIL             'a@b.io'

bluebird mail <PRIVYX_EMAIL_1D67ED6CD0AAE8C3>
$ privyx -c strict.yaml detect --transform --no-policy "bluebird mail a@b.io"
     0:8     PROJECT           'bluebird'
    14:20    EMAIL             'a@b.io'

<PRIVYX_PROJECT_DBBA12EA1BBFFECF> mail <PRIVYX_EMAIL_1D67ED6CD0AAE8C3>
```
