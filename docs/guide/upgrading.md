---
description: Versioning, deprecation, and how to try or opt out of the defaults planned for Privyx 0.2.
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

## Preparing for 0.2

**0.2 is not released yet.** The settings below remain opt-in in `0.1.x`.
Only candidates that pass compatibility checks will become defaults. This
page will be updated with the final changes before the release.

| Setting | Available since | Try the planned behavior in 0.1 | Keep the 0.1 behavior after the change |
|---|---|---|---|
| `detector.underscore_separates_words` | 0.1.33 | `true` finds terms and built-ins inside names separated by `_` | Set `false` |
| `detector.term_variants` | 0.1.26 | `true` finds alternate spellings of multiword terms | Set `false` |
| `anchor.secret` | 0.1.35 for `auto` on all commands | `auto` reuses a private per-user key for stable tokens | Set `""` explicitly to use no anchor |
| Checksum/structure-based detectors | 0.1.26 | List the entities below in `detector.enable` | List unwanted entities in `detector.disable` |
| `proxy.dlp_check` | 0.1.27 | `true` rejects requests that still contain values masked elsewhere in the request | Set `false` |

`run` already uses `anchor.secret: auto` unless an anchor choice is explicit.
For commands that currently use counters, choosing `auto` changes the token
IDs. Keep the same key to keep those IDs stable; a different key changes them.
An explicit empty secret, including `PRIVYX_ANCHOR_SECRET=""`, disables the
anchor. See [Anchors](masking.md#anchors).

The detector candidates are `CRYPTO_WALLET`, `ID_NIK`, `ID_NPWP`,
`SG_NRIC_FIN`, `MY_NRIC`, `ES_NIF`, `ES_NIE`, `UK_NHS`, and `IN_AADHAAR`.
`HIGH_ENTROPY`, `ASSIGNED_SECRET`, `MRN`, and `ICD10` remain opt-in. Starting
with 0.1.36, `disable` accepts inactive opt-in entities, so an opt-out config
can be prepared before their defaults change. An entity cannot be in both
`enable` and `disable`; remove it from `enable` when opting out.

Try the compatible detection and anchor candidates together:

```yaml
detector:
  underscore_separates_words: true
  term_variants: true
  enable: [CRYPTO_WALLET, ID_NIK, ID_NPWP, SG_NRIC_FIN, MY_NRIC, ES_NIF, ES_NIE, UK_NHS, IN_AADHAAR]
anchor:
  secret: auto
```

Test `proxy.dlp_check: true` separately before using it with an agent. Claude
can echo signed thinking containing a value the request masks elsewhere; DLP
then rejects the request, while changing that thinking would invalidate its
signature. This compatibility issue must be resolved before DLP becomes a
default. See the [agent tests](../development/testing.md#end-to-end-a-real-agent-make-e2e).

The default operator and PASP are not finalized. `detector.merge_terms: true`
is available in 0.1.36, but making it a default is also undecided. Keep these
separate from the settings above; see [Detection](detection.md) and
[Masking](masking.md).

Duplicate YAML keys have warned since 0.1.29 and are planned to become an
error in 0.2. Keep a single occurrence of each key before upgrading. The
warning includes the file and line; it is distinct from an intentional
override between files in an `include` chain.
