---
description: Extend Privyx with your own detectors, policies, operators, anchors, vaults, providers, secret resolvers, agents, dialects, and features.
---

# Plugins

Privyx loads plugins from **local paths** and **modules** you configure, and
from your own code through the [Python API](python-api.md). Loading is
**opt-in**: nothing loads unless the config names it, and an installed package
is never loaded on its own. A plugin is a module with a `setup(app)` function,
which adds components, listeners, and routes to the app it is given.

!!! note "Experimental"

    The Python API and the plugin interface may change in a `0.x` release. A
    plugin package should pin the versions it supports, for example
    `privyx>=0.3,<0.4`.

## What can be plugged in

| Kind | Base class | Methods to implement | Selected by | Conformance suite |
|---|---|---|---|---|
| [Detector](#detector) | `privyx.detectors.Detector` | `detect_sync` or `detect` | `detectors.<name>` | `DetectorSuite` |
| [Policy](#policy) | `privyx.policies.Policy` | `decide_sync` or `decide` | `policy.type` | `PolicySuite` |
| [Operator](#operator) | `privyx.operators.Operator` | `pseudonymize`, `deanonymize` | `operator.type` | `OperatorSuite` |
| [Anchor](#anchor) | `privyx.anchors.Anchor` | `anchor` | `anchor.type` | `AnchorSuite` |
| [Vault](#vault) | `privyx.vaults.Vault`, `StoredVault` | `create`, `get`, `save`, `delete`, or `read`, `write`, `remove`, `keys` | `vault.type` | `VaultSuite` |
| [Provider](#provider) | `privyx.providers.Provider` | `url` | `provider.type` | `ProviderSuite` |
| [Secret](#secret) | `privyx.secrets.SecretResolver` | `resolve` | the scheme of a reference | `SecretResolverSuite` |
| [Agent](#agent) | `privyx.agents.Agent` | none: override what the tool needs | `agents.<profile>.type` | `AgentSuite` |
| [Dialect](#dialect) | `privyx.dialects.Dialect` | none: set `content_keys` | a value of `proxy.routes` | `DialectSuite` |
| [Feature](#feature) | `privyx.features.Feature` | none: mark listeners with `listen` | `features.<name>` | `FeatureSuite` |

The values components share, such as `Context`, `Detection`, `Session`,
`TransformResult`, and the errors, are in `privyx.core`. The suites are in
`privyx.testing`.

## The contract

- **Subclass** the base class of a kind.
- Set a class-level **`name`**: lowercase words joined by `-` or `_`, the
  value a config selects. Each class sets its own; it is never derived from
  the class name or inherited.
- Declare options in a nested **`Options`** class ([Options](#options)).
- Take what the app shares in **`open(services)`**, and release what you opened
  in **`close()`**. Both are optional. `services` holds the token `codec`, the
  `anchor` (`None` when anchoring is off), and the `events` bus.
- Add the class in **`setup(app)`** with `app.add`.

A plugin never shadows a built-in: a name that a built-in of the same kind
has, or that another plugin took, stops startup with an error.

```python
# privyx_acme/__init__.py
from fastapi import Request

from privyx import Privyx
from privyx.events import AppStarting, RequestMasked

from .detector import AcmeIdDetector


def setup(app: Privyx) -> None:
    app.add(AcmeIdDetector)

    @app.on(AppStarting)
    async def warm(event: AppStarting) -> None:
        ...  # open a connection, warm a cache

    @app.on(RequestMasked)
    def tag(event: RequestMasked) -> None:
        event.headers["x-team"] = "platform"

    @app.router.get("/acme/status")
    async def status(request: Request) -> dict[str, list[str]]:
        return {"detectors": list(request.app.state.settings.detectors)}
```

| Call | Does |
|---|---|
| `app.add(cls)` | Adds a component; its kind follows from its base class. Returns `cls`, so it also works as a class decorator. |
| `app.use(module)` | Imports another plugin module and calls its `setup(app)`, once per app. |
| `app.on(Event, priority=0)` | Decorator: calls the function, sync or async, on each event. The highest priority runs first. See the [event catalog](#events). |
| `app.registry(kind)` | The registry of a kind, such as `app.registry(Detector)`: `names()`, `get(name)`, `is_builtin(name)`. |
| `app.defaults(agents=..., presets=...)` | Adds agent profiles and presets just above Privyx's defaults; every config file still overrides them. |
| `app.add_middleware(factory, priority=0, **options)` | Serves every request through ASGI middleware, before the routes. |
| `app.router` | A FastAPI `APIRouter` served beside the proxy's routes. |

A listener of `AppStarting` that fails stops the server from starting; one of
`AppStopping` that fails is logged. Both are emitted while the app is served,
not by `app.engine()`.

## Options

`Options` declares a component's options as a pydantic model, extending the
`Options` of its base class. Privyx checks the section against it when it
builds the component, before it serves anything, and the component reads the
result from `self.options`:

```python
# privyx_acme/detector.py
import re
from typing import Literal

from privyx.core import Context, Detection
from privyx.detectors import Detector


class AcmeIdDetector(Detector):
    name = "acme_id"

    class Options(Detector.Options):
        region: Literal["EU", "US"] = "EU"

    options: Options

    def detect_sync(self, text: str, context: Context) -> Detection:
        found = Detection()
        for match in re.finditer(rf"\bACME-{self.options.region}-\d{{6}}\b", text):
            found.add(match.start(), match.end(), "ACME_ID", match.group())
        return found
```

```yaml
plugins:
  modules: [privyx_acme]
detectors:
  acme_id:
    regoin: US
```

```console
$ privyx detect "ACME-US-123456"
Error: unknown setting 'detectors.acme_id.regoin' (did you mean 'region'?) for detector type 'acme_id'
```

An error names the key, never its value. A component built directly checks
its options the same way: `AcmeIdDetector(region="US")`.

A config layer that sets another `type` drops the options of the plugin set
below it, so `vault: {type: acme, bucket: b}` in `~/.privyx/config.yaml` does
not break a project that sets `vault: {type: sqlite}`.

With a config that loads the plugin, `privyx config schema` adds its options
to the [editor schema](../guide/configuration.md#editor-support).

## Enabling

List a directory or a single `.py` file under `plugins.paths`. Each directory
is walked recursively; files whose names start with `_` are skipped. Each file
must define `setup(app)`.

```yaml
plugins:
  enabled: true          # set false to skip loading even when paths are set
  paths:
    - ./plugins          # the conventional in-repo location
    - /opt/privyx/ext
detectors:
  license_plate: {}      # runs after the built-in regex detector
```

Paths may also be set via `PRIVYX_PLUGIN_PATHS` (comma-separated).

`privyx plugin list` shows what each plugin added, and `privyx doctor` reports
it too, so you can confirm a plugin was found before serving.

## Plugins from a package

A package is named under `plugins.modules` (or `PRIVYX_PLUGIN_MODULES`,
comma-separated). Privyx imports each module and calls its `setup(app)`:

```yaml
plugins:
  modules: [privyx_acme]
```

- Components work in every command. Routes and listeners of `AppStarting` and
  `AppStopping` work in `privyx proxy` and `privyx run`; a handler finds the
  engine at `request.app.state.engine` and the settings at
  `request.app.state.settings`.
- A module without `setup`, one that fails to import, and two plugins with the
  same name, from any of `paths`, `modules`, and `app.add`, stop startup with
  an error.
- A route that would take a masking route of `proxy.routes` stops startup
  too; otherwise it could turn masking off for `/v1/messages`. A request on a
  masking path never reaches a plugin route in any case, also where startup
  could not tell the two apart (a route's converter, say). The `*` in a
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

A package can declare an entry point, so that `privyx plugin list` shows it as
installed:

```toml
[project.entry-points."privyx.plugins"]
acme = "privyx_acme"
```

```console
$ privyx plugin list
plugins: none loaded
installed, not loaded: acme (privyx_acme)
```

An entry point is only read, never loaded: the package runs once the config
names it in `plugins.modules`.

A config file found in the working directory names `paths` and `modules` only
once you have trusted it with `privyx config trust`. Trust covers the config
file, not the code of the modules it names.

## Testing a plugin

Each kind has a conformance suite in `privyx.testing`. Subclass it in a test
module, name the component and its options, and pytest runs every check. The
checks need no async plugin for pytest.

```python
# tests/test_detector.py
from privyx.testing import DetectorSuite
from privyx_acme.detector import AcmeIdDetector


class TestAcmeIdDetector(DetectorSuite):
    component = AcmeIdDetector
    options = {"region": "EU"}
    samples = {"order ACME-EU-123456 shipped": ["ACME-EU-123456"]}
```

## Detector

Finds the sensitive spans of a text. Override `detect_sync`, or the async
`detect` for I/O. The [Options](#options) section above has a complete one, and
`DetectorSuite` takes `samples`: texts with the values each must yield.

```yaml
detectors:
  acme_id:             # runs after the built-in regex detector
    region: EU
```

Ready-to-run copies are in `plugins/detectors/`: `license_plate.py`, and
`iban.py`, which validates a checksum and takes an option. The tutorial
[A detector plugin](../tutorials/detector-plugin.md) builds the second step by
step.

A detector that calls a paid or remote service can report what it did with
`context.counters["my_calls"] += 1`. The counts land in the audit trail as
`session.transform` `detector_counts` and on `/metrics`, so they must be counts,
never text. Set `detection.cacheable = False` on a degraded result (a fallback)
so the detection cache does not keep it in place of a real scan.

## Policy

Decides which detected spans are replaced. Override `decide_sync`, or the async
`decide`. It may only keep or drop spans: `PolicySuite` fails a policy that
adds one.

```python
# privyx_acme/policy.py
from privyx.core import Context, Detection
from privyx.policies import Policy


class KeepPublicPolicy(Policy):
    """Leaves the entity types of `public` unmasked."""

    name = "keep_public"

    class Options(Policy.Options):
        public: list[str] = []

    options: Options

    def decide_sync(self, detection: Detection, context: Context) -> Detection:
        kept = [s for s in detection.spans if s.entity_type not in self.options.public]
        return Detection(kept, detection.cacheable)
```

```python
# tests/test_policy.py
from privyx.testing import PolicySuite
from privyx_acme.policy import KeepPublicPolicy


class TestKeepPublicPolicy(PolicySuite):
    component = KeepPublicPolicy
    options = {"public": ["ORGANIZATION"]}
```

```yaml
policy:
  type: keep_public
  public: [ORGANIZATION]
```

## Operator

Replaces the spans of a detection, and restores them. Implement `pseudonymize`
and `deanonymize`.

```python
# privyx_acme/operator.py
from privyx.core import Context, Detection, Session, Transformation, TransformResult
from privyx.operators import Operator


class HiddenOperator(Operator):
    """Writes one marker for every value; nothing can be restored."""

    name = "hidden"
    uses_anchor = False
    fixed_text = "[HIDDEN]"

    async def pseudonymize(
        self, text: str, detection: Detection, session: Session, context: Context
    ) -> TransformResult:
        changes = []
        for span in reversed(detection.merged(text).spans):
            text = text[: span.start] + self.fixed_text + text[span.end :]
            changes.append(
                Transformation(span.start, span.end, span.text, self.fixed_text, span.entity_type)
            )
        return TransformResult(text=text, transformations=changes[::-1])

    async def deanonymize(self, text: str, session: Session, context: Context) -> TransformResult:
        return TransformResult(text=text, transformations=[])
```

```python
# tests/test_operator.py
from privyx.testing import OperatorSuite
from privyx_acme.operator import HiddenOperator


class TestHiddenOperator(OperatorSuite):
    component = HiddenOperator
```

```yaml
operator:
  type: hidden
```

- An operator that writes the same text for every value sets **`fixed_text`**
  to it, so the [leak check](../guide/masking.md#leak-check) does not count a
  masked value that is part of that text.
- **`uses_anchor = False`** says it never uses an anchor, so Privyx does not
  read the per-user key for it.
- A reversible operator keeps each original in the `session`. One that writes
  tokens takes `services.codec` and `services.anchor` in `open` (call
  `super().open(services)` if you override it) and restores with
  `privyx.operators.restore`.
- One whose substitutes are not tokens sets **`stream_restore = "literal"`**,
  so a streamed reply is restored by the exact values.

## Anchor

Derives a keyed, deterministic string from a value, so one value gets one token
in every session that shares the key. Implement `anchor`. The `pseudonym`
operator takes the text after the last `_` of the result, uppercased, and uses
its first `operator.id_length` characters as the token id, so return at least
that many.

```python
# privyx_acme/anchor.py
import hashlib

from privyx.anchors import Anchor
from privyx.core import Context


class Blake2Anchor(Anchor):
    name = "blake2"

    class Options(Anchor.Options):
        secret: str

    options: Options

    async def anchor(self, text: str, context: Context) -> str:
        key = self.options.secret.encode()
        return hashlib.blake2b(text.encode(), key=key[:64], digest_size=32).hexdigest()
```

```python
# tests/test_anchor.py
from privyx.testing import AnchorSuite
from privyx_acme.anchor import Blake2Anchor


class TestBlake2Anchor(AnchorSuite):
    component = Blake2Anchor
    options = {"secret": "a" * 32}
    other_options = {"secret": "b" * 32}
```

```yaml
anchor:
  type: blake2
  secret: ${ACME_ANCHOR_SECRET}
```

An anchor gets `anchor.secret` only if its `Options` declares `secret`, and as
written: reading the per-user key for `auto` is the built-in `hmac` anchor's.
An empty `secret` turns anchoring off.

## Vault

Keeps sessions. Extend `Vault` to store `Session` objects (`create`, `get`,
`save`, `delete`, and optionally `list_sessions` for `privyx session list` and
`prune`). For a store of bytes, extend `StoredVault` and implement `read`,
`write`, `remove`, and `keys`: it writes the record format and the session
methods for you.

```python
# privyx_acme/vault.py
import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from privyx.core import SessionBusyError
from privyx.vaults import StoredVault


class DictVault(StoredVault):
    """Keeps each session's record in a dict: a stand-in for a real store."""

    name = "dict_store"
    persistent = False

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._records: dict[str, bytes] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, str] = {}

    async def read(self, key: str) -> bytes | None:
        return self._records.get(key)

    async def write(
        self,
        key: str,
        value: bytes,
        *,
        updated_at: float,
        new: bool = False,
        lock: str | None = None,
    ) -> bool:
        if (new and key in self._records) or (lock and self._holders.get(key) != lock):
            return False
        self._records[key] = value
        return True

    async def remove(self, key: str) -> None:
        self._records.pop(key, None)

    async def keys(self) -> AsyncIterator[str]:
        for key in list(self._records):
            yield key

    @asynccontextmanager
    async def lock(self, session_id: str) -> AsyncIterator[str]:
        held = self._locks.setdefault(session_id, asyncio.Lock())
        try:
            await asyncio.wait_for(held.acquire(), timeout=5)
        except TimeoutError:
            raise SessionBusyError(f"session is busy: {session_id}") from None
        token = self._holders[session_id] = secrets.token_hex(8)
        try:
            yield token
        finally:
            del self._holders[session_id]
            held.release()
```

```python
# tests/test_vault.py
from privyx.testing import VaultSuite
from privyx_acme.vault import DictVault


class TestDictVault(VaultSuite):
    component = DictVault
```

```yaml
vault:
  type: dict_store
```

- **`persistent`** says whether a session outlives the process. Set it to
  `False` for a store in memory.
- **`lock(session_id)`** holds a session across the processes that share the
  store, since a save writes the whole session. It returns an async context
  manager that yields a token, which `save` or `write` receives as `lock`. A
  store used by one process leaves the default, which returns `None`.
- Entering the lock raises **`SessionBusyError`** when it is not free in time.
  The proxy answers such a request with `503`.
- A save whose token no longer holds the lock must fail: `write` returns
  `False`, and a `Vault` of your own raises `VaultError`.

## Provider

Says where a request goes upstream and how it authenticates; the proxy sends
it and streams the reply. Implement `url`. Every provider takes the options of
`provider`: `base_url`, `api_key`, `headers`, and the connection settings.

```python
# privyx_acme/provider.py
import httpx

from privyx.providers import ApiKey, Provider


class AcmeProvider(Provider):
    name = "acme"

    def url(self, path: str, *, stream: bool) -> str:
        base = self.options.base_url or "https://api.acme.example"
        return f"{base.rstrip('/')}{path}"

    def auth(self) -> httpx.Auth | None:
        return ApiKey(self.options.api_key) if self.options.api_key else None
```

```python
# tests/test_provider.py
from privyx.testing import ProviderSuite
from privyx_acme.provider import AcmeProvider


class TestAcmeProvider(ProviderSuite):
    component = AcmeProvider
    options = {"base_url": "https://api.acme.example", "api_key": "acme-" + "test-key-0123456789"}
```

```yaml
provider:
  type: acme
  base_url: https://api.acme.example
  api_key: ${ACME_API_KEY}
```

- The gateway calls `url` and `headers`. The transparent proxy joins the
  client's path to `base_url` instead, and adds the `headers` option.
- `auth` is used in both modes. In transparent mode, the key of an `ApiKey`
  goes in the header the request's format takes. Any other `httpx.Auth` is
  applied as the request goes out, so one with `requires_request_body = True`
  can sign the masked body.

## Secret

Reads the credential a reference such as `acme://token` names. The component's
`name` is the scheme. Implement `resolve(reference, path)`, where `path` is the
setting, for errors. A failure raises `ConfigError` and never shows the secret.

```python
# privyx_acme/secret.py
import os

from privyx.core import ConfigError
from privyx.secrets import SecretResolver


class AcmeEnvResolver(SecretResolver):
    """Reads `acme://NAME` from the variable `<prefix>NAME`."""

    name = "acme"

    class Options(SecretResolver.Options):
        prefix: str = "ACME_SECRET_"

    options: Options

    def resolve(self, reference: str, path: str) -> str:
        variable = self.options.prefix + reference.removeprefix("acme://")
        value = os.environ.get(variable)
        if value is None:
            raise ConfigError(f"{path}: {variable} is not set")
        return value
```

```python
# tests/test_secret.py
import pytest

from privyx.testing import SecretResolverSuite
from privyx_acme.secret import AcmeEnvResolver


class TestAcmeEnvResolver(SecretResolverSuite):
    component = AcmeEnvResolver
    reference = "acme://token"
    secret = "acme-" + "test-key-0123456789"
    broken = "acme://missing"

    @pytest.fixture(autouse=True)
    def _variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ACME_SECRET_token", self.secret)
```

```yaml
secrets:
  acme:
    prefix: ACME_SECRET_
provider:
  api_key: acme://token
```

A reference resolves only in a credential setting, such as `api_key`, `secret`,
`headers`, or `env`, and is refused in `plugins` and `secrets`, which are read
first. `http`, `https`, `file`, `ftp`, `ws`, `wss`, `data`, and `mailto` cannot
be a resolver's scheme.

## Agent

Knows a tool `privyx run` launches. Every method has a default, so override
what the tool needs: `argv` to point it at the proxy, `env` for variables,
`endpoint` to read where it would send requests on its own, `expand` for a
target syntax of its own, and `check` to fail before launch. Argv is never
passed through a shell.

```python
# privyx_acme/agent.py
from privyx.agents import Agent, Endpoint


class AcmeCliAgent(Agent):
    """A tool that takes its endpoint as `--api-base URL`."""

    name = "acme_cli"

    def argv(self, base_url: str, argv: list[str], endpoint: Endpoint | None) -> list[str]:
        return ["--api-base", base_url, *argv]
```

```python
# tests/test_agent.py
from privyx.testing import AgentSuite
from privyx_acme.agent import AcmeCliAgent


class TestAcmeCliAgent(AgentSuite):
    component = AcmeCliAgent
    environment = {"HOME": "/nonexistent/acme"}
```

```yaml
agents:
  acme-cli:
    command: acme-cli
    type: acme_cli
    env_vars: [ACME_BASE_URL]
```

Keys of the profile that Privyx does not define are the type's options. A tool
whose client describes its machine in top-level request keys or headers names
them in **`metadata_keys`** and **`masked_headers`**: those of every registered
type are masked in every request, since the proxy cannot tell which tool sent
it.

## Dialect

Describes an API format: where a request's conversation text is, and how its
reply is restored. A dialect never masks anything itself. Dialects are
experimental.

```python
# privyx_acme/dialect.py
from privyx.dialects import Dialect


class AcmeChatDialect(Dialect):
    """`POST /acme/v1/chat` with `{"model": ..., "turns": [{"role", "text"}]}`."""

    name = "acme_chat"
    content_keys = frozenset({"turns"})
    opaque_keys = frozenset({"role"})
    default_routes = ("/acme/v1/chat",)
```

```python
# tests/test_dialect.py
from privyx.testing import DialectSuite
from privyx_acme.dialect import AcmeChatDialect


class TestAcmeChatDialect(DialectSuite):
    component = AcmeChatDialect
    requests = [{"model": "m", "turns": [{"role": "user", "text": "hi"}]}]
```

- **`content_keys`** is an allowlist: a top-level key missing from it is
  forwarded as it is. `DialectSuite` plants a fake value in every string of
  `requests`, so give it requests as a real client sends them.
- **`opaque_keys`** names each key inside the content that holds structure
  rather than text. An unknown key is walked and masked.
- **`default_routes`** adds paths to the default route map; one that another
  dialect has stops startup. A `proxy.routes` written in the config replaces
  the whole map, so it must name the plugin's paths itself.
- Override `error_body`, `stream`, `objects`, `add_directive`, and the other
  methods when the format differs from the defaults. Set `stream` on the suite
  to check that a streamed reply is restored at every split.

## Feature

Optional behavior that listens to events, switched on and off by name. Mark its
methods with `listen`: the app adds them when the feature is on and removes
them when it is off.

```python
# privyx_acme/feature.py
import logging
import time
from typing import Any

from privyx.core import listen
from privyx.events import RequestEnded, RequestReceived
from privyx.features import Feature

log = logging.getLogger("privyx_acme")


class SlowRequests(Feature):
    """Logs the requests that took longer than `slow_ms`; never their content."""

    name = "slow_requests"

    class Options(Feature.Options):
        slow_ms: int = 2000

    options: Options

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._started: dict[str, float] = {}

    @listen(RequestReceived)
    def start(self, event: RequestReceived) -> None:
        self._started[event.request_id] = time.monotonic()

    @listen(RequestEnded)
    def end(self, event: RequestEnded) -> None:
        started = self._started.pop(event.request_id, None)
        if started is None:
            return
        elapsed = (time.monotonic() - started) * 1000
        if elapsed > self.options.slow_ms:
            log.warning("request %s took %.0f ms", event.request_id, elapsed)
```

```python
# tests/test_feature.py
from privyx.testing import FeatureSuite
from privyx_acme.feature import SlowRequests


class TestSlowRequests(FeatureSuite):
    component = SlowRequests
```

```yaml
features:
  slow_requests:
    slow_ms: 5000
```

An entry of `features` is `true`, `false`, or a mapping of options, which
switches the feature on unless it says `enabled: false`. A class with
`enabled_by_default = True` is on without an entry. One that keeps state that
must be shared between server processes sets `single_process = True`, and
`server.workers` above 1 then refuses it.

`privyx features list --events` shows each feature and what it listens to.

### Events

The event classes are in `privyx.events`. A listener of a class also hears its
subclasses, so one of `Notification` hears every notification.

**Interceptable** events happen inside a flow. A listener that fails stops it:
the app does not start, or the request is refused.

| Event | Class | When | A listener may |
|---|---|---|---|
| `app.starting` | `AppStarting` | The components are open, before the first request | Stop the start by raising |
| `request.received` | `RequestReceived` | A request on a masking route is parsed, before masking | Change `body` and `headers`, `reject`, `respond` |
| `request.masked` | `RequestMasked` | The request is masked, before the final leak check | Change the masked `body` and `headers`, `reject`, `respond` |

Both request events carry `method`, `path`, `headers`, `body`, `dialect`,
`session_id`, and `request_id`. `event.reject(status, code, message)` answers
with an error in the client's format, and `event.respond(status, body)` answers
in place of the upstream. **`request.received` holds the original values.**
What a listener adds at `request.masked` is checked for leaks too. No listener
can write a credential header (`authorization`, `x-api-key`, `api-key`,
`x-goog-api-key`, `proxy-authorization`).

**Notifications** are facts after they happened, without payload content. A
listener that fails is logged, and the others still run.

| Event | Class | When |
|---|---|---|
| `request.ended` | `RequestEnded` | An exchange is over, however it went: replied, failed, refused, or abandoned. It carries `request_id`; drop what you keep per request here |
| `app.stopping` | `AppStopping` | The app stopped taking requests, before its components close |
| `config.changed` | `ConfigChanged` | `privyx config set` or `unset` wrote a key to a file |
| `audit.write_failed` | `AuditWriteFailed` | An audit record could not be written |
| `session.created`, `.transform`, `.restore`, `.deleted` | `SessionCreated`, `SessionTransform`, `SessionRestore`, `SessionDeleted` | The [audit events](../observability/audit-events.md) of a session |
| `proxy.request`, `.response`, `.error`, `.passthrough`, `.blocked` | `ProxyRequest`, `ProxyResponse`, `ProxyError`, `ProxyPassthrough`, `ProxyBlocked` | The audit events of an exchange |
| `run.unproxied`, `run.budget_exceeded` | `RunUnproxied`, `RunBudgetExceeded` | The audit events of `privyx run` |

A listener runs in place, sync or awaited, so one that does network I/O should
start a task of its own. A feature can emit notifications through
`services.events`; they must never carry a value.

## How loading works

`Privyx` first registers the built-ins of each kind. Then it imports every file
of `plugins.paths` (under a synthetic module name, so plugins never touch
`sys.path`) and every module of `plugins.modules`, and calls each one's
`setup(app)`, once per app. The defaults a plugin added go below the config
files, and secret references are resolved last, so a plugin's resolver can
serve them. A missing path, a module that fails to import, or two components
claiming one name raise `ConfigError` at startup rather than on the first
request.
