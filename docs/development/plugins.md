# Plugins

Privyx loads plugins from **local paths** you configure — there is no
third-party entry-point mechanism. Discovery is **opt-in** (nothing loads unless
you list a path) and works by **subclass**: a plugin module defines a concrete
subclass of a Privyx component base class, and Privyx registers it under its
`name`.

## What Can Be Plugged In

| Family | Base class | Methods to implement | Selected by |
|---|---|---|---|
| Detector | `privyx.privacy.detector.base.BaseDetector` | `detect_sync` | `detector.type` |
| Policy | `privyx.privacy.policy.base.BasePolicy` | `_decide_sync` | `policy.type` |
| Operator | `privyx.privacy.operator.base.BaseOperator` | `pseudonymize`, `deanonymize` | `operator.type` |
| Anchor | `privyx.privacy.anchor.base.BaseAnchor` | `anchor`, `deanchor` | `anchor.type` |
| Vault | `privyx.vault.base.BaseVault` | `create`, `get`, `save`, `delete` (opt. `connect`/`close`, and `list_sessions` for `privyx session list`/`prune`) | `vault.type` |
| Provider | `privyx.providers.base.BaseProvider` | `send`, `stream`, `close` | `provider.type` |

## The Contract

- **Subclass** one of the base classes above with a concrete implementation.
- Set a class-level **`name`** — the value a config `type` selects. If omitted,
  it is derived from the class name with the family suffix stripped
  (`LicensePlateDetector` → `licenseplate`); setting `name` explicitly is
  recommended.
- Optionally add **`@classmethod from_config(cls, config: dict) -> Self`** to
  construct from the component's config section. Without it, Privyx calls the
  no-argument constructor `cls()`. The section may carry options of your own
  (`detector: {type: license_plate, region: EU}` passes `region`); Privyx
  rejects such unknown keys only under a built-in type, where they are typos.
- Optionally define module-level **`on_startup()`** / **`on_shutdown()`**
  (sync or async) for lifecycle work (see below).

A plugin never shadows a built-in: built-in types (`regex`, `pseudonym`,
`hmac`, `memory`, ...) always win, and a plugin is consulted only when the
configured `type` is not a built-in.

## Enabling

List a directory or a single `.py` file under `plugins.paths`. Each directory
is walked recursively; files whose names start with `_` are skipped.

```yaml
plugins:
  enabled: true          # set false to skip loading even when paths are set
  paths:
    - ./plugins          # the conventional in-repo location
    - /opt/privyx/ext
detector:
  type: license_plate    # a name a plugin registered
```

Paths may also be set via `PRIVYX_PLUGIN_PATHS` (comma-separated).

`privyx doctor` reports what each family loaded, so you can confirm a plugin was
discovered before serving.

## Writing a Detector

```python
from privyx.core.context import Context
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector

class LicensePlateDetector(BaseDetector):
    name = "license_plate"

    def detect_sync(self, text: str, context: Context) -> Detection:
        d = Detection()
        # ... find spans, d.add(start, end, "LICENSE_PLATE", text[start:end])
        return d
```

A ready-to-run copy lives in `plugins/detectors/license_plate.py`. A second
example, `plugins/detectors/iban.py`, validates a checksum and takes an
option; the tutorial [A detector plugin](../tutorials/detector-plugin.md)
builds it step by step.

A detector that calls a paid or remote service can report what it did with
`context.counters["my_calls"] += 1`. The counts land in the audit trail as
`session.transform` `detector_counts` and on `/metrics`, so they must be counts,
never text. Set `detection.cacheable = False` on a degraded result (a fallback)
so the detection cache does not keep it in place of a real scan.

## Writing an Operator

```python
from privyx.core.result import TransformResult
from privyx.privacy.operator.base import BaseOperator

class MyOperator(BaseOperator):
    name = "myop"

    async def pseudonymize(self, text, detection, session, context) -> TransformResult:
        ...

    async def deanonymize(self, text, session, context) -> TransformResult:
        ...
```

Operators that emit tokens or anchor values receive the token codec and anchor
through their config; read them in `from_config` if needed.

## Lifecycle Hooks

A plugin module may define module-level hooks that run around the serving
lifecycle:

```python
async def on_startup() -> None:
    ...  # open a connection, warm a cache

def on_shutdown() -> None:
    ...  # release resources
```

Startup hooks run before the server accepts traffic; a failing startup hook
aborts the process. Shutdown hooks run during teardown and are best-effort
(errors are logged, not raised).

## How Discovery Works

`privyx.plugins.loader.load_plugins(settings)` clears the plugin registry, then
imports every module under `plugins.paths` (under a synthetic module name, so
plugins never touch `sys.path`). For each module it registers every concrete
`Base*` subclass *defined in that module*, keyed by `name`, into
`privyx.plugins.registry.PLUGINS`. Two plugins claiming the same name in the
same family, a missing path, or a module that fails to import all raise
`ConfigError` at startup rather than failing on the first request.
