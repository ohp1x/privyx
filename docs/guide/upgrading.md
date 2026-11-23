---
description: Versioning, deprecation, and what changes between Privyx 0.1, 0.2, and 0.3, with the new names of settings, commands, and Python APIs.
---

# Upgrading

Read the [changelog](https://github.com/ohp1x/privyx/blob/main/CHANGELOG.md)
before upgrading, then follow the [installation commands](installation.md#upgrade).
Try your config with `privyx -c your.yaml doctor` and representative fake data
with `privyx -c your.yaml detect --transform "..."` before using it with traffic.

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

## From 0.2 to 0.3

0.3 gives settings, commands, and the Python API new names. The token format,
stored data, and default masking output do not change, so a vault or map file
from 0.2 keeps restoring.

Old config keys and CLI commands keep working in 0.3.x with a warning, and are
removed in 0.4.0. The experimental Python API and plugin interface changed at
once in 0.3.0: their old import paths are gone.

### Migrate your config

Print each config file in use with the new keys, then write them:

```bash
privyx config migrate
privyx config migrate --write
```

Give a path to migrate one file: `privyx config migrate your.yaml --write`.
Comments are not kept. The old file is kept beside it as `<name>.bak`, or a
numbered backup when that exists, with its permissions.

`config migrate` leaves `operator.length`, `operator.token`, or
`token.id_length` as written when moving it could change what a profile or
preset uses, and names the key on stderr: a profile or preset in the same file
picks an operator type that would read the new key differently, or
`token.id_length` sits beside a type that does not read it. Write the value in
each profile or preset that uses it.

`operator.type: hash` has no new spelling, so `config migrate` leaves it. It
warns because anyone who guesses a value can match its token. Its replacement
is `pseudonym` with an explicit `anchor.secret`, which writes different tokens.

### Config keys

| Old key | New key |
|---|---|
| `host`, `port`, `tls.*` | `server.host`, `server.port`, `server.tls.*` |
| `log_level`, `logging`, `log_file` | `log.level`, `log.format`, `log.file` |
| `detector` | `detectors.<name>` |
| `detector.terms`, `detector.patterns` | `detectors.<name>.terms`, `detectors.<name>.patterns` |
| detector type `yaml` | `detectors.regex.builtins: false` |
| `proxy.directive`, `proxy.cache_control` | `features.directive`, `features.cache_control` |
| `audit.enabled`, `audit.path`, `audit.prices` | `features.audit` (`false` turns it off), `features.audit.path`, `features.audit.prices` |
| `audit.tap`, `audit.tap_dir` | `proxy.tap`, `proxy.tap_dir` |
| `tracing.enabled` | `features.tracing` |
| `agents.<profile>.adapter` | `agents.<profile>.type`; for `auto`, leave out `type` |
| `upstream_url` | `provider.base_url` |
| `proxy.forward_client_auth`, `path_mapping`, `timeout`, `connect_timeout`, `max_connections` | the same key in `provider` or `providers.<name>` |
| `provider.openai_api_key`, `anthropic_api_key`, `google_api_key` | `providers.<type>.api_key` |
| `token.id_length`, `operator.length` | `operator.id_length` |
| `operator.token` (`redact`) | `operator.text` |

### CLI commands

| Old command | New command |
|---|---|
| `privyx trust` | `privyx config trust` |
| `privyx lint` | `privyx config lint` |
| `privyx install-hook` | `privyx hook install` |
| `privyx inspect routes` | `privyx config routes` |
| `privyx inspect session` | `privyx session show` |
| `privyx inspect token` | `privyx session token` |
| `privyx inspect regex NAME` | `privyx detect --entity NAME`, or `privyx detect --pattern REGEX` |
| `privyx pipe` | `privyx mask --stream` |
| `privyx pipe --restore` | `privyx unmask --stream` |
| `privyx config --show` | `privyx config show` |
| `privyx config --sources` | `privyx config show --sources` |
| `privyx config --schema` | `privyx config schema` |
| `privyx config --path` | `privyx config path` |
| `privyx COMMAND -c FILE` | `privyx -c FILE COMMAND` |
| `privyx COMMAND --preset NAME` | `privyx --preset NAME COMMAND` |

### Environment

Every setting has a variable: `PRIVYX_` and its path, with `__` between the
names, such as `PRIVYX_SERVER__PORT`. The older names, such as `PRIVYX_PORT`
and `PRIVYX_API_KEY`, stay as aliases: they draw no warning and are not removed
in 0.4.0. Setting one value both ways with different values is an error.

### Python API and plugins

| Old | New |
|---|---|
| `privyx.plugins.BaseDetector` | `privyx.detectors.Detector` |
| `privyx.plugins.BasePolicy`, `_decide_sync` | `privyx.policies.Policy`, `decide_sync` |
| `privyx.plugins.BaseOperator`, `restore` | `privyx.operators.Operator`, `privyx.operators.restore` |
| `privyx.plugins.BaseAnchor`, `deanchor` | `privyx.anchors.Anchor`; `deanchor` is gone |
| `privyx.plugins.BaseVault`, `connect` | `privyx.vaults.Vault` or `StoredVault`, `open` |
| `privyx.plugins.BaseProvider`, `send`, `stream` | `privyx.providers.Provider`, `url` and `auth` |
| `ComponentConfig`, a class's `Config` | `privyx.core.Options`, a class's `Options` |
| `BuildContext` | `privyx.core.Services`, given to `open(services)` |
| `from_config(config, ctx)`, `from_config(config)` | `__init__(**options)` and `open(services)` |
| `privyx.plugins.Context`, `Detection`, `Session`, `TransformResult`, `ConfigError` | `privyx.core` |
| `privyx.plugins.LogicalToken` | `privyx.token.LogicalToken` |
| `privyx.plugins.PLUGINS` | `app.registry(kind)` |
| A class found in a plugin file | `app.add(cls)` in the file's required `setup(app)` |
| A `name` derived from the class name | `name`, required |
| `on_startup()`, `on_shutdown()`, `px.on_startup`, `px.on_shutdown` | `app.on(AppStarting)`, `app.on(AppStopping)` |
| `Privyx.run()` | `Privyx.serve()` |
| `privyx.config.loader.load_config` | `privyx.load_config` |
| `privyx.config.schema.Settings` | `privyx.Settings` |
| `privyx.core.builder.build_engine` | `async with Privyx(...).engine() as engine` |
| `privyx.core.engine.PrivacyEngine` | `privyx.PrivacyEngine` |
| `privyx.core.errors.PrivyxError` | `privyx.PrivyxError` |
| `privyx.privacy.detector.yaml.build_detector` | `app.registry(Detector)` |
| `privyx.privacy.detector.builtin.RegexDetector` | `privyx.detectors.RegexDetector` |
| `privyx.privacy.detector.builtin.YamlDetector` | `privyx.detectors.RegexDetector(builtins=False)` |
| `privyx.privacy.operator.pseudonym.PseudonymOperator` | `privyx.operators.PseudonymOperator` |
| `privyx.privacy.policy.default.DefaultPolicy` | `privyx.policies.DefaultPolicy` |
| `privyx.vault.memory.MemoryVault` | `privyx.vaults.MemoryVault` |

See [Plugins](../development/plugins.md) for each kind, and the
[Python API](../development/python-api.md) for `Privyx` and its services.

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
