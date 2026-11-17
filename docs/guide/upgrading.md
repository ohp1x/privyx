---
description: Versioning, deprecation, and what changes from Privyx 0.1 to 0.2, with how to keep the 0.1 behavior.
---

# Upgrading

Read the [changelog](https://github.com/ohp1x/privyx/blob/main/CHANGELOG.md)
before upgrading, then follow the [installation commands](installation.md#upgrade).
Try your config with `privyx doctor -c your.yaml` and representative fake data
with `privyx detect -c your.yaml --transform "..."` before using it with traffic.

## Version policy

In the `0.x` series, patch releases add features and fixes while keeping
existing configs working, default masking behavior, and the default token
format. Features that change default masking are introduced as opt-in first.
Optional extras may add dependencies; a patch does not add a required dependency.

A minor release batches breaking changes. A new default must have shipped as
opt-in in at least one earlier release, with a documented way to keep the
previous behavior. Enabling an opt-in is a choice to use that behavior now;
it is not a deprecation warning for an older option.

Version 1.0 will establish a stability contract for the documented config,
CLI, token format, and public interfaces. After 1.0, breaking changes require
a new major version.

## Deprecation

An option scheduled for removal keeps working with a warning for at least one
minor-version increment after that warning first ships. The warning and
changelog must name its replacement, if any, and the earliest removal version.
It is not removed in a patch release.

For example, a replacement can ship as opt-in in `0.1`, become the default in
`0.2`, and an old option scheduled for removal can first warn in `0.2`. That
option can be removed no earlier than `0.3`. The opt-in period does not count
as deprecation. An option first deprecated in `0.1` can be removed no earlier
than `0.2`.

Changing a default does not require removing the old behavior. A supported
opt-out can remain without a deprecation warning. Tightening validation of
previously accepted input follows the same warning period: an input that
warns in `0.1` can first become an error in `0.2`.

## From 0.1 to 0.2

0.2 turns behavior that was opt-in in 0.1 into the default, and makes three
inputs that warned in 0.1 an error. Each new default has an opt-out that
already works in 0.1.x, so one config behaves the same on both.

### New defaults

| Setting | Opt-in since | New default | Keep the 0.1 behavior |
|---|---|---|---|
| `detector.underscore_separates_words` | 0.1.33 | `true`: terms and built-ins are found inside names such as `ACME_API_KEY` or `host_10.0.0.1` | `false` |
| `detector.term_variants` | 0.1.26 | `true`: a term of several words also matches `acme-corp` or `acme_corp` for `Acme Corp` | `false` |
| `detector.merge_terms` | 0.1.36 | `true`: `terms` and `patterns` of the same entity are both matched, instead of the regex replacing the terms | `false` |
| Checksum- and structure-validated entities | 0.1.26, with `enable` | built into the `regex` detector | list them in `detector.disable` |
| `proxy.dlp_check` | 0.1.27 | `true`: a masked request that still holds a value it masked elsewhere is refused with `422` | `false` |
| `anchor.secret` | 0.1.35, `auto` on every command | `auto`: a per-user key in `~/.privyx/anchor.key` | `""` |

The entities are `CRYPTO_WALLET`, `ID_NIK`, `ID_NPWP`, `SG_NRIC_FIN`,
`MY_NRIC`, `ES_NIF`, `ES_NIE`, `UK_NHS`, and `IN_AADHAAR`. `enable` still
accepts their names, so a 0.1 config that opted in keeps loading; a `yaml`
detector, which has no built-ins, still takes them with `enable`.
`HIGH_ENTROPY`, `ASSIGNED_SECRET`, `MRN`, and `ICD10` stay opt-in.

To keep all of the 0.1 behavior:

```yaml
detector:
  underscore_separates_words: false
  term_variants: false
  merge_terms: false
  disable: [CRYPTO_WALLET, ID_NIK, ID_NPWP, SG_NRIC_FIN, MY_NRIC, ES_NIF, ES_NIE, UK_NHS, IN_AADHAAR]
proxy:
  dlp_check: false
anchor:
  secret: ""
```

In a `detector` list, the detector keys go on each `regex` item. An entity
cannot be in both `enable` and `disable`; remove it from `enable` to opt out.
Set only the keys you want to keep: each one is independent. `privyx run`
anchored with the per-user key in 0.1 already, so leave `anchor` out for it:
`secret: ""` would switch it to counters.

### What changes for you

**Tokens.** A `pseudonym` token's id is derived from the value with the
anchor key instead of counted, so `<PRIVYX_EMAIL_1>` becomes a token such as
`<PRIVYX_EMAIL_4FF5EF825E496E1A>`, the same for that value in every session
and command. `privyx run` already worked this way, and its tokens do not
change. A map file or vault session from 0.1 keeps its counter tokens and
still restores them; a value it already holds keeps its token, and a new value
gets an anchored one. Keep `~/.privyx/anchor.key`: a new key changes every
anchored token. In the Docker image the key is in `/data/anchor.key`
(`PRIVYX_ANCHOR_KEY_PATH`), so it lasts as long as the `/data` volume. Machines or users that must share
tokens need the same `PRIVYX_ANCHOR_SECRET`. `PRIVYX_ANCHOR_SECRET=""` turns
anchoring off, and `privyx run --no-anchor` uses no anchor unless the config
or environment sets one. See [Anchors](masking.md#anchors).

**More is masked.** With `_` as a word separator, term variants, merged terms,
and the new built-in entities, values that 0.1 sent as is are now masked.
Run `privyx lint -c your.yaml` and `privyx detect -c your.yaml --transform
"..."` on representative fake data to see what your config now finds. Cold
detection of a large text takes longer; cached text does not.

**Refused requests.** With the leak check on, a request whose masked value
also appears where it was not masked, such as in a URL or a header other
than the credential, answers `422` and is not forwarded. Echoed Claude
thinking is exempt only as the exact text remembered from the upstream
response. See the [leak check](masking.md#leak-check).

**Python library.** `load_config`, `build_engine`, and `privyx.app` take the
new defaults. Components built by hand, or `build_detector` with a plain dict,
take only what their arguments say, as before. See the
[Python library](../development/python-library.md).

### New errors

- A key given twice in one mapping of a config file warned from 0.1.29 and
  now stops startup, with the file and line in the error. Keep one of them.
  An override between files in an `include` chain is not affected.
- A setting its section's `type` does not use, written beside that `type` or
  with no `type` chosen, such as `terms` under `detector.type: presidio` or
  `vault.redis_url` without `type: redis`, warned from 0.1.38 and is now an
  error. Remove it or set the type that uses it. `terms`, `patterns`, or
  `enable` from another file that no detector uses still only warn, and a
  setting from an environment variable is still ignored. See
  [Settings per type](configuration.md#settings-per-type).
- `operator.length`, the id length of `hash` tokens, warned outside 6 to 64
  from 0.1.37 and is now an error there. See [`hash`](masking.md#hash).

The default operator does not change: `fpe` stays opt-in. No option is
removed in 0.2.
