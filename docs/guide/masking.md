# Masking

Once a value is detected, three settings shape what replaces it:

- the **operator** decides what the value becomes and whether it can be
  restored;
- the **token format** decides how a placeholder is written;
- the **anchor** decides whether the same value gets the same placeholder in
  every session.

## Operators

| `operator.type` | `alice@example.com` becomes | Restored in replies | Vault stores | Needs |
|---|---|---|---|---|
| `pseudonym` (default) | `<PRIVYX_EMAIL_1>` | Yes | The original | Nothing |
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
and rejects `<PRIVYX_EMAIL_1>` where it expects an email address:

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
  format: "<{namespace}_{type}_{id}>"     # <PRIVYX_EMAIL_1>
  # format: "[[{namespace}:{type}:{id}]]" # [[PRIVYX:EMAIL:1]]
  # format: "<{namespace}:{type}:{id}>"   # <PRIVYX:EMAIL:1>
```

- `format` must contain `{type}` and `{id}`. `{namespace}` is optional.
- Two placeholders need a delimiter between them.
- An invalid format fails at startup.

A token only comes back if the model writes it exactly, so pick a syntax your
model reproduces reliably. `PRIVYX_TOKEN_FORMAT` and `PRIVYX_TOKEN_NAMESPACE`
set these from the environment.

Two departures are tolerated. A model sometimes drops the outer delimiters, most
often in a tool argument or a title, and writes `PRIVYX_EMAIL_1`. That bare form
is restored too when the session issued the token and it stands between word
boundaries (`PRIVYX_EMAIL_1` is not found inside `PRIVYX_EMAIL_12` or
`xPRIVYX_EMAIL_1`). It needs a format that starts with `{namespace}` inside its
outer delimiters, as the three above do.

A model may also keep only the id, and write `247102A91C49C99E` for
`<PRIVYX_EMAIL_247102A91C49C99E>`. The id alone is restored when it is at least
12 characters long, stands as a whole word, and belongs to exactly one token of
the session. The ids of an [anchor](#anchors), of `hash` at its default length,
and of `encrypt` are that long. A counter (`1`) is not: it is ordinary text, so
it is never looked for on its own.

Any other change to a token is left as written.

### Compact tokens

In a long conversation the same placeholders come back on every turn. Set
`token.compact` and they are issued short:

```yaml
token:
  compact: true             # <E~1> instead of <PRIVYX_EMAIL_1>
```

The entity type is cut to the initials of its words (`EMAIL` is `E`,
`CREDIT_CARD` is `CC`) and the id stays. Tokens in `token.format` are still
restored, so a session that issued them before the switch keeps working. A
compact token has no bare form; its id alone is restored under the same rules
as above.

## Anchors

By default a `pseudonym` token is a counter within its session:
`<PRIVYX_EMAIL_1>` is the first email address that session saw. In another
session the same address can get a different number, and `<PRIVYX_EMAIL_1>`
can stand for a different address.

Set an anchor secret and the id is derived from the value instead, so the same
value gets the same token in every session and after a restart:

```bash
export PRIVYX_ANCHOR_SECRET=$(openssl rand -hex 32)
```

```console
$ privyx detect --transform "mail alice@example.com"
...
mail <PRIVYX_EMAIL_247102A91C49C99E>
```

The id is an HMAC of the value under the secret. Anyone holding the secret can
check a guessed value against a token, so keep it as private as an API key.
With an empty secret there is no anchoring at all, rather than anchoring with a
guessable key.

Anchoring applies to the `pseudonym` operator. `privyx run` creates a secret in
`~/.privyx/anchor.key` on first use; pass `--no-anchor` to skip that.

## Leak check

A value is masked where it is detected. The same value can also reach the
provider somewhere Privyx does not mask: a field outside the conversation, a
URL, a message `name`, a request header, or a thinking block sent back with its
signature (which must stay as the provider wrote it). With `proxy.dlp_check`
set, the proxy looks for every value a request masked in all of that request as
it will be sent, and refuses the request with `422` if one is still there:

```yaml
proxy:
  dlp_check: true
```

The check covers values that were detected somewhere in the request, not
values no detector found. The credential headers (`Authorization`,
`x-api-key`, `api-key`, `x-goog-api-key`) are not checked. A value that starts or ends with a letter or digit
must stand as a whole word to count (`Bob` is not found in `Bobby`).
