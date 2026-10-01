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

One departure is tolerated: a model sometimes drops the outer delimiters, most
often in a tool argument or a title, and writes `PRIVYX_EMAIL_1`. That bare form
is restored too when the session issued the token and it stands between word
boundaries (`PRIVYX_EMAIL_1` is not found inside `PRIVYX_EMAIL_12` or
`xPRIVYX_EMAIL_1`). It needs a format that starts with `{namespace}` inside its
outer delimiters, as the three above do. Any other change to a token, such as
its id alone, is left as written.

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
`~/.config/privyx/anchor.key` on first use; pass `--no-anchor` to skip that.
