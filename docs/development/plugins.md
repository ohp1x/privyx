---
description: Extend Privyx with your own detectors, policies, operators, anchors, vaults, and providers, from local paths or installed packages.
---

# Plugins

Privyx loads plugins from **local paths** and **modules** you configure, and
from your own code through the [Python API](python-api.md). Loading is
**opt-in**: nothing loads unless the config names it, and an installed package
is never loaded on its own. A plugin defines a concrete subclass of a Privyx
component base class, and Privyx registers it under its `name`.

Import everything a plugin needs from `privyx.plugins`. Other import paths
work too, but may move between releases.

!!! note "Experimental"

    `Config`, `BuildContext`, `setup(px)`, and the `privyx.plugins` module may
    change in a `0.x` release. A plugin package should pin the versions it
    supports, for example `privyx>=0.1.38,<0.2`.

## What can be plugged in

| Family | Base class | Methods to implement | Selected by |
|---|---|---|---|
| Detector | `BaseDetector` | `detect_sync` | `detector.type` |
| Policy | `BasePolicy` | `_decide_sync` | `policy.type` |
| Operator | `BaseOperator` | `pseudonymize`, `deanonymize` | `operator.type` |
| Anchor | `BaseAnchor` | `anchor`, `deanchor` | `anchor.type` |
| Vault | `BaseVault` | `create`, `get`, `save`, `delete` (opt. `connect`/`close`, and `list_sessions` for `privyx session list`/`prune`) | `vault.type` |
| Provider | `BaseProvider` | `send`, `stream`, `close` | `provider.type` |

All are in `privyx.plugins`, with `ComponentConfig`, `BuildContext`,
`Detection`, `TransformResult`, `Context`, `Session`, `LogicalToken`,
`ConfigError`, and `restore`, the routine the built-in operators restore
tokens with.

## The contract

- **Subclass** one of the base classes above with a concrete implementation.
- Set a class-level **`name`**: the value a config `type` selects. If omitted,
  it is derived from the class name with the family suffix stripped
  (`LicensePlateDetector` → `licenseplate`); setting `name` explicitly is
  recommended.
- To take options, give the class a **`Config`** and a
  **`from_config(cls, config, ctx)`** ([Options](#options)). Or, as before,
  add **`@classmethod from_config(cls, config: dict) -> Self`**: it gets the
  whole section, your keys included
  (`detector: {type: license_plate, region: EU}` passes `region`), unchecked.
  Without either, Privyx calls the no-argument constructor `cls()`.
- Optionally define module-level **`on_startup()`** / **`on_shutdown()`**
  (sync or async) for lifecycle work (see below).

A plugin never shadows a built-in: built-in types (`regex`, `pseudonym`,
`hmac`, `memory`, ...) always win, and a plugin is consulted only when the
configured `type` is not a built-in.

## Options

A `Config` declares the options of the component's section as a pydantic
model. Privyx checks the section against it when it builds the component,
before it serves anything, and hands the result to `from_config`:

```python
from typing import Literal

from privyx.plugins import BaseDetector, BuildContext, ComponentConfig, Context, Detection


class AcmeIdDetector(BaseDetector):
    name = "acme_id"

    class Config(ComponentConfig):
        region: Literal["EU", "US"] = "EU"

    def __init__(self, region: str) -> None:
        super().__init__()
        self.region = region

    @classmethod
    def from_config(cls, config: Config, ctx: BuildContext) -> "AcmeIdDetector":
        return cls(config.region)

    def detect_sync(self, text: str, context: Context) -> Detection:
        ...
```

```yaml
detector:
  type: acme_id
  regoin: US
```

```console
$ privyx proxy
Error: unknown setting 'detector.regoin' (did you mean 'region'?) for detector type 'acme_id'
```

An error names the key, never its value. `Config` gets your own options and
those of the section's fields it declares; the other fields of the section,
such as `vault.dsn`, which has a default for the built-in vaults, are left
out. Fields Privyx applies whatever the type still work: `detector.cache` and
`detector.underscore_separates_words`, and the `provider` URL, key, and
headers, which the transparent proxy uses.

`ctx`, a `BuildContext`, holds the live objects an operator needs: the token
`codec`, the `anchor` (`None` when anchoring is off), and `id_length`. For
other families it is empty.

A config layer that sets another `type` drops the options of the plugin set
below it, so `detector: {type: acme_id, region: EU}` in
`~/.privyx/config.yaml` does not break a project that sets
`detector: {type: regex}`. The section's own fields, such as `terms`, are
merged as before.

With a config that loads the plugin, `privyx config --schema` adds its options
to the [editor schema](../guide/configuration.md#editor-support).

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

## Plugins from a package

A package of components, routes, and hooks is named under `plugins.modules`
(or `PRIVYX_PLUGIN_MODULES`, comma-separated). Privyx imports each module and
calls its `setup` with a [`Privyx`](python-api.md):

```yaml
plugins:
  modules: [privyx_acme]
```

```python
# privyx_acme/__init__.py
from fastapi import Request

from .detector import AcmeIdDetector


def setup(px) -> None:
    px.add(AcmeIdDetector)

    @px.router.get("/acme/status")
    async def status(request: Request) -> dict[str, str]:
        settings = request.app.state.settings
        return {"detector": settings.detector_type}

    @px.on_startup
    async def connect() -> None:
        ...
```

- Components work in every command. Routes and hooks work in `privyx proxy`
  and `privyx run`; the handlers find the engine at `request.app.state.engine`
  and the settings at `request.app.state.settings`.
- A module without `setup`, one that fails to import, and two plugins with the
  same name, from any of `paths`, `modules`, and `px.add`, stop startup with
  an error.
- A route that would take a masking route of `proxy.routes` stops startup
  too; otherwise it could turn masking off for `/v1/messages`. The `*` in a
  Gemini route stands for any segment, so `/acme/{item}` would also take
  `/acme/cachedContents`; use `/acme/items/{item}`. `/health` and `/metrics`
  stay Privyx's.
- Privyx serves plugin routes on its own port, without authentication, like
  `/health`. A route that returns a session's mapping returns the original
  values.
- `--reload` reads the config again, not a module already imported: restart
  after changing a plugin's code.

The package must be installed where Privyx runs. With `uv`, add it to the
Privyx tool environment:

```bash
uv tool install privyx --with privyx-acme
```

For Docker, build an image on top of Privyx's; see
[Installation](../guide/installation.md#plugin-packages).

A config file found in the working directory names `modules` only once you
have trusted it, as for `paths`. Trust covers the config file, not the code
of the modules it names.

## Writing a detector

```python
from privyx.plugins import BaseDetector, Context, Detection

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

## Writing an operator

```python
from privyx.plugins import BaseOperator, TransformResult

class MyOperator(BaseOperator):
    name = "myop"

    async def pseudonymize(self, text, detection, session, context) -> TransformResult:
        ...

    async def deanonymize(self, text, session, context) -> TransformResult:
        ...
```

An operator that emits tokens or anchor values gets the token codec and the
anchor in `BuildContext` when it has a `Config`. One without a `Config` finds
them in its config dict under `__codec__` and `__anchor__`, as before.

## Lifecycle hooks

A plugin module may define module-level hooks that run around the serving
lifecycle:

```python
async def on_startup() -> None:
    ...  # open a connection, warm a cache

def on_shutdown() -> None:
    ...  # release resources
```

A module from `plugins.modules` registers its hooks with `px.on_startup` and
`px.on_shutdown` instead.

Startup hooks run before the server accepts traffic; a failing startup hook
aborts the process. Shutdown hooks run during teardown and are best-effort
(errors are logged, not raised).

## How discovery works

`privyx.plugins.loader.load_plugins(settings)` clears the plugin registry, then
imports every module under `plugins.paths` (under a synthetic module name, so
plugins never touch `sys.path`). For each module it registers every concrete
`Base*` subclass *defined in that module*, keyed by `name`, into
`privyx.plugins.PLUGINS`. Next it imports each of `plugins.modules` and calls
its `setup`, then registers what `px.add` added. Two plugins claiming the same
name in the same family, a missing path, or a module that fails to import all
raise `ConfigError` at startup rather than failing on the first request.
