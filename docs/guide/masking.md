---
description: "What a detected value becomes: the operators, the token format, compact tokens, id length, anchors for stable tokens, the placeholder directive, and the leak check."
---

# Masking

Once a value is detected, three settings shape what replaces it:

- the **operator** decides what the value becomes and whether it can be
  restored;
- the **token format** decides how a placeholder is written;
- the **anchor** decides whether the same value gets the same placeholder in
  every session.

Two proxy settings complete them: the [placeholder directive](#placeholder-directive)
tells the model how to treat placeholders, and the [leak check](#leak-check)
refuses a request that still holds a value masked elsewhere in it.

## Operators

| `operator.type` | `alice@example.com` becomes | Restored in replies | Vault stores | Needs |
|---|---|---|---|---|
| `pseudonym` (default) | `<PRIVYX_EMAIL_4FF5EF825E496E1A>` | Yes | The original | Nothing |
| `hash` | `<PRIVYX_EMAIL_ff8d9819fc0e>` | Yes | The original | Nothing |
| `encrypt` | `<PRIVYX_EMAIL_AAE5ECF9F12ED145>` | Yes | Ciphertext only | `privyx[crypto]` and a key |
| `faker` | `curtisjohnson@cummings.net` | Yes | The original | `privyx[faker]` |
| `fpe` | `user-38bc14ee@example.invalid` | Yes | The original | Nothing |
| `partial` | `a****@example.com` | Yes | The original | Nothing |
| `redact` | `[REDACTED]` | No | Nothing to restore | Nothing |

### `pseudonym`

Replaces each value with a numbered token. Within a session, the same value
always gets the same token. This is the default and the right choice unless you
need one of the properties below.

### `hash`

The token's id is a SHA-256 digest of the value, cut to `length` hex
characters (default `12`). The same value gets the same token in every
session, without a secret.

```yaml
operator:
  type: hash
  length: 12
```

`length` is between 6 and 64; another value is a configuration error. When two values in
a session would get the same id, the later one gets a longer id from the same
digest, so a collision never restores the wrong value.

A short unkeyed hash can be matched by anyone who guesses the value. Prefer
`pseudonym` with an [anchor](#anchors) when that matters.

### `encrypt`

The vault stores AES-256-GCM ciphertext instead of the original value, so a
leaked or shared vault exposes no personal data. The token's id is a keyed
digest of the value.

```bash
pip install 'privyx[crypto]'
export PRIVYX_ENCRYPT_KEY=$(openssl rand -hex 32)
```

```yaml
operator:
  type: encrypt             # key from PRIVYX_ENCRYPT_KEY, or `key:` here
```

Without the package or the key, Privyx fails at startup. Losing the key means
the stored sessions can no longer be restored. See
[Cryptography](../security/cryptography.md).

### `faker`

Replaces a value with a realistic fake of the same kind, so the model reads a
plausible email or name rather than a placeholder.

```yaml
operator:
  type: faker
  locale: en_US             # optional
  seed: 1234                # optional: the same value fakes the same way across runs
```

Fakes are restored by matching the exact fake strings. If the model happens to
write one of them on its own, it is restored too. Use `pseudonym` when exact
reversal matters more than realism.

### `fpe`

Replaces a value with one of the same format that passes validation, from a
range no real person or system uses. Use it when a tool checks its arguments
and rejects `<PRIVYX_EMAIL_4FF5EF825E496E1A>` where it expects an email address:

| Value | Becomes |
|---|---|
| Email address | `user-38bc14ee@example.invalid` (`.invalid` is reserved, RFC 2606) |
| IPv4 / IPv6 address | `10.147.79.228` (RFC 1918) / an `fd00::/8` address |
| UUID | A random version 4 UUID |
| Card number (`CREDIT_CARD`) | `4000 0088 1099 1617`: the `400000` prefix of payment test cards, Luhn-valid, same layout |
| Anything else | A random letter for each letter and digit for each digit; punctuation and length stay |

```yaml
operator:
  type: fpe
  seed: 1234                # optional: another seed gives other substitutes
```

The same value gets the same substitute in every session. Like `faker`, a
reply is restored by matching the exact substitutes, so one the model writes on
its own is restored too, and a substitute that looks real (a name, a phone
number) can be taken for real data by whoever reads the masked text. Use
`pseudonym` when that matters.

### `partial`

Hides most of a value and keeps a little context, so the model can still tell
which card or which mail domain a sentence is about:

| Value | Becomes |
|---|---|
| `alice@example.com` | `a****@example.com` |
| `4111 1111 1111 1234` | `**** **** **** 1234` |
| `+62 812-3456-7890` | `+** ***-****-7890` |

An email address keeps the first character and the domain. Any other value
keeps its punctuation and its last four letters and digits, never more than a
third of them. Two values that would look the same get one more `*` in front.

```yaml
operator:
  type: partial
```

What stays visible goes to the provider: the domain of an address, the end of
a card or key. The vault keeps the original, and a reply is restored by
matching the exact masked strings.

### `redact`

Replaces the value with fixed text and keeps nothing, so the reply cannot get
it back:

```yaml
operator:
  type: redact
  token: "[REDACTED]"
```

## Token format

The token operators (`pseudonym`, `hash`, `encrypt`) write placeholders from
`token.format`:

```yaml
token:
  namespace: PRIVYX
  format: "<{namespace}_{type}_{id}>"     # <PRIVYX_EMAIL_4FF5EF825E496E1A>
  # format: "[[{namespace}:{type}:{id}]]" # [[PRIVYX:EMAIL:4FF5EF825E496E1A]]
  # format: "<{namespace}:{type}:{id}>"   # <PRIVYX:EMAIL:4FF5EF825E496E1A>
```

- `format` must contain `{type}` and `{id}`. `{namespace}` is optional.
- Two placeholders need a delimiter between them.
- An invalid format fails at startup.

A token only comes back if the model writes it exactly, so pick a syntax your
model reproduces reliably. `PRIVYX_TOKEN_FORMAT` and `PRIVYX_TOKEN_NAMESPACE`
set these from the environment.

Some departures are tolerated. A model sometimes drops the outer delimiters, most
often in a tool argument or a title, and writes `PRIVYX_EMAIL_1`. That bare form
is restored too when the session issued the token and it stands between word
boundaries (`PRIVYX_EMAIL_1` is not found inside `PRIVYX_EMAIL_12` or
`xPRIVYX_EMAIL_1`). So is a token with only one of its delimiters
(`<PRIVYX_EMAIL_1`, `PRIVYX_EMAIL_1>`), the delimiter with it. These need a
format that starts with `{namespace}` inside its outer delimiters, as the three
above do. A token in another case (`<privyx_email_1>`) is restored when exactly
one token of the session matches it.

A model may also keep only the id, and write `4FF5EF825E496E1A` for
`<PRIVYX_EMAIL_4FF5EF825E496E1A>`. The id alone is restored when it is at least
12 characters long, stands as a whole word, and belongs to exactly one token of
the session. The ids of an [anchor](#anchors) and of `encrypt` at the default
[id length](#id-length), and of `hash` at its default length, are that long. A counter (`1`) is not: it is ordinary text, so
it is never looked for on its own.

Any other change to a token is left as written. A token the session did not
issue is never restored, whatever its form.

### Compact tokens

In a long conversation the same placeholders come back on every turn. Set
`token.compact` and they are issued short:

```yaml
token:
  compact: true             # <E~4FF5EF825E496E1A> instead of <PRIVYX_EMAIL_4FF5EF825E496E1A>
```

The entity type is cut to the initials of its words (`EMAIL` is `E`,
`CREDIT_CARD` is `CC`) and the id stays. Tokens in `token.format` are still
restored, so a session that issued them before the switch keeps working. A
compact token has no bare form; its id alone is restored under the same rules
as above.

### Id length

The id of an [anchored](#anchors) `pseudonym` token and of an `encrypt` token
is 16 hex characters. `token.id_length` sets it, from 6 to 64:

```yaml
token:
  id_length: 8              # <PRIVYX_EMAIL_4FF5EF82>
```

Counter ids and `hash` (`operator.length`) do not change. A new length changes
every token, as a new secret or key does: tokens issued before no longer match
the same value. Below 12, an id the model writes without the rest of its token
is not restored, as with a counter.

A short id makes two values of a session more likely to share it. The chance
for one entity type in a session is about 0.03% at 100 values with 6
characters, 2.9% at 1,000, and 52% at 5,000; with 8 characters it is 0.01% at
1,000, and from 12 it is negligible. A collision only gives the later value a
longer id. A token pasted from another session is more likely to match a
different value of this one when ids are short.

To shorten tokens without shortening ids, use [compact tokens](#compact-tokens)
instead; both can be combined (`<E~4FF5EF>`).

### Labels

A placeholder tells the model nothing about the value it stands for. With
`token.label_format`, a `pseudonym` token also carries a label, a fake value
of the same kind, for the model to read and reason with:

```yaml
token:
  label_format: "<{namespace}_{type}_{id}:{label}>"   # <PRIVYX_PERSON_A81B3C9AEE08DE11:Alice Johnson>
proxy:
  directive: true           # recommended with labels
```

`john smith` is sent as `<PRIVYX_PERSON_A81B3C9AEE08DE11:Alice Johnson>`, and the placeholder
comes back as `john smith`. A reply that only says `Alice Johnson`, or `Alice
in Wonderland`, is left as written: the session maps the token, never the
label, so text that happens to hold the label is never restored. That is the
difference from the [`faker`](#faker) operator.

- The placeholder restores by its id, with its type and namespace when the
  format holds them; the label is ignored, so one the model changed still
  restores. A placeholder whose fields point to no token of the session, or to
  two, is left as written; in another case (`<privyx_person_a81b3c9aee08de11:Alice>`), it is
  restored when exactly one token of the session matches it. Without its
  closing delimiter, only the token part is restored, as for a
  [half edge](#token-format).
- Tokens in `token.format`, and every form of them above, are still restored.
  If the model drops the label, the placeholder is a plain token and still
  comes back.
- Labels come from Faker, for the types it has fakes for: `PERSON`, `EMAIL`,
  `PHONE`, `LOCATION`, `ORGANIZATION`, `ADDRESS`, `URL`, `IP_ADDRESS`,
  `CREDIT_CARD`, `SSN`, and a few more. Other types, such as an API key or a
  `PROJECT` of your own, get a plain token. `operator.locale` sets the
  language of the labels and `operator.seed` varies them.
- A label comes from the token, never from the value: the provider sees the
  token anyway, so the label tells it nothing more, and trying candidate
  values cannot find one. A token gets the same label in every request, so the
  provider's prompt cache keeps working.
- A label never holds the value, nor another value masked in the session (a
  fake `James Wilson` while `James` is masked too). One that shares a word with
  the value, such as the real first name, is avoided.
- An id stands for one value whatever its type, so a format without `{type}`
  still points to one value.

`label_format` must contain `{id}` and `{label}`, with a literal before and
after `{label}`: a label may hold spaces, and only those literals bound it.
A label never holds the character after it, a line break, `"`, or `\`. The
recommended form is `token.format` with `:{label}` before the closing
delimiter: if the model drops the label, the rest is a token it knows. A
shorter form such as `<{namespace}_{id}:{label}>` costs fewer tokens, but
without its label it is no token at all.

It works with the `pseudonym` operator only, not with `token.compact`, and
needs `pip install 'privyx[faker]'`; otherwise Privyx stops at startup with
the setting named. Off by default.

The limits: a model that writes only the label shows the user the label. The
model may also pass a label on its own as a tool argument (a fake email,
say), and the tool then gets the fake value, as with `faker`. Each placeholder
costs as many more tokens as its label.

## Anchors

A `pseudonym` token's id is derived from the value under an anchor secret, so
the same value gets the same token in every session and after a restart:

```console
$ privyx detect --transform "mail alice@example.com"
...
mail <PRIVYX_EMAIL_4FF5EF825E496E1A>
```

The id is an HMAC of the value under the secret. Anyone holding the secret can
check a guessed value against a token, so keep it as private as an API key.

The default secret, since 0.2, is `auto`: every command that uses HMAC
anchoring, including `proxy`, `mask`, `pipe`, `mcp wrap`, and `run`, shares
the per-user key in `~/.privyx/anchor.key` (`PRIVYX_ANCHOR_KEY_PATH` sets
another path). It is created with owner-only
permissions on first use, and concurrent processes use the same complete key.
`config --show`, `config --sources`, and `run --dry-run` do not create it.
Different users or machines need the same explicit secret to share tokens:

```bash
export PRIVYX_ANCHOR_SECRET=$(openssl rand -hex 32)
```

An empty secret turns anchoring off, rather than anchoring with a guessable
key. A token is then a counter within its session, as by default in 0.1:
`<PRIVYX_EMAIL_1>` is the first email address that session saw. In another
session the same address can get a different number, and `<PRIVYX_EMAIL_1>`
can stand for a different address:

```yaml
anchor:
  secret: ""
```

`PRIVYX_ANCHOR_SECRET=""` does the same. `privyx run --no-anchor` uses no
anchor unless the config or environment sets one. Anchoring applies to the
`pseudonym` operator.

If the automatic key cannot be read or saved, the command fails instead of
using a temporary secret that would change tokens on the next invocation.
An existing empty key file is also an error; remove it to generate a new key.
Do not delete a nonempty key file: replacing it changes every anchored token.

Without an anchor, a counter token only means something in its own session.
Text masked in one session and pasted into another, such as the output of
`privyx mask` pasted into a conversation that goes through the proxy, is
restored there as that session's value: `<PRIVYX_EMAIL_1>` from the first comes
back as the first email of the second, with no warning. Set an anchor whenever
output from several sessions is mixed: different values then get different
tokens, and a token a session did not make is left as it is.

## Placeholder directive

`proxy.directive` adds a short instruction to each masked chat request: some
values are placeholders such as `<PRIVYX_EMAIL_4FF5EF825E496E1A>`, to be copied exactly as
written wherever the value is needed, and never changed, split, encoded, or
guessed at. It helps a model that otherwise treats a placeholder as a typo to
fix, or rebuilds one in code.

```yaml
proxy:
  directive: true
```

With [labels](#labels), the example in the instruction is a labeled
placeholder, and one more sentence says that the name in a placeholder only
stands in for the value. Turn both on together.

It goes after Anthropic's `system`, after the leading system messages of a Chat
Completions request, first in a Responses `input` (in `instructions` when
the request continues a stored response), and last in Gemini's
`systemInstruction`. Ollama's API gets none: a system prompt there replaces
the one the model was made with. The text is the same on every
request, so the provider's prompt cache keeps working; it adds about 80 tokens
to each request. Off by default.

## Leak check

A value is masked where it is detected. The same value can also reach the
provider somewhere Privyx does not mask: a field outside the conversation, a
URL, a message `name`, or a request header. With `proxy.dlp_check`, on by
default since 0.2, the proxy looks for every value a request masked in all of
that request as it will be sent, and refuses the request with `422` if one is
still there. Set it to `false` to forward such a request, as 0.1 did:

```yaml
proxy:
  dlp_check: false
```

The check covers values that were detected somewhere in the request, not
values no detector found, and not the placeholders that replaced them. The credential headers (`Authorization`,
`x-api-key`, `api-key`, `x-goog-api-key`) are not checked, nor are the tool
names the request declares, where a tool is named or called: they go out as
declared, so `acme` in `mcp__acme__search` does not refuse the request. A value that starts or ends with a letter or digit
must stand as a whole word to count (`Bob` is not found in `Bobby`). When
`detector.underscore_separates_words` is on, as by default, `_` separates words
here too (`Bob` is found in `DB_Bob`).

Echoed Anthropic thinking is an exception only when Privyx remembers its
signature from an upstream response. After the DLP check, Privyx replaces the
thinking text with that exact upstream text to preserve the signature, even if
the model wrote a value that is masked elsewhere in the request. Client edits
to that text are discarded; other fields in the block and the rest of the
request remain checked. An unknown or evicted signature grants no exception:
the thinking is masked normally. The signature cache is bounded and per
process, so a restart or another worker can lose it.
