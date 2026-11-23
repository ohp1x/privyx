---
description: Assemble Privyx in Python with the experimental Privyx class. Add components, routes, and listeners, then serve the proxy, take the engine, or call its services.
---

# Python API

`Privyx` puts the proxy together from a Python file of your own: the same
configuration as `privyx proxy`, plus components, routes, and listeners you add
in code. Serve it, or take only the engine or a service for your program.

!!! note "Experimental"

    `Privyx` and the plugin interface may change in a `0.x` release. Pin the
    version you build on.

```python
--8<-- "examples/privyx_app.py"
```

## The class

| Call | Does |
|---|---|
| `Privyx(config=None, **overrides)` | Reads the settings as `privyx proxy` does: `config` is a file read alone, as with `--config`; without it, the usual [config files](../guide/configuration.md#where-settings-come-from). `overrides` win over the files and the environment, as command-line flags do: `Privyx(server={"port": 8080}, detectors={"regex": {"terms": {...}}})`. |
| `app.add(cls)` | Adds a component class, of any [kind](plugins.md#what-can-be-plugged-in), selected by its `name`, with its [`Options`](plugins.md#options) checked as for a plugin. Returns `cls`, so it works as a class decorator. |
| `app.use(module)` | Imports a plugin module and calls its `setup(app)`, once. |
| `app.on(Event, priority=0)` | Decorator for a function, sync or async, called on each [event](plugins.md#events). `AppStarting` and `AppStopping` are emitted as the app is served, never by `app.engine()`. |
| `app.registry(kind)` | The registry of a kind, such as `app.registry(Detector)`. |
| `app.defaults(agents=..., presets=...)` | Adds agent profiles and presets below every config file. |
| `app.router` | A FastAPI `APIRouter` whose routes the proxy serves beside its own. |
| `app.add_middleware(factory, priority=0, **options)` | Serves every request through ASGI middleware, before the routes. |
| `app.settings` | The settings, read once. |
| `app.serve()` | Serves the proxy until stopped, as `privyx proxy` does, with the routes and listeners. |
| `app.asgi()` | The same app for another ASGI server: `uvicorn --factory acme.app:make_app`, where `make_app` returns `Privyx(...).asgi()`. A parent app that mounts it must run its lifespan. |
| `app.engine()` | An async context manager that yields the privacy engine with the components added. Contexts open at once share it, and the last to leave closes it. See [Python library](python-library.md) for its calls. |

Components added with `app.add` sit beside those from `plugins.paths` and
`plugins.modules`; two with the same name in one kind stop startup. Each
`Privyx` has registries of its own, so two in one process do not see each
other's components.

## Services

Each service is an attribute of the app, and experimental like it.

| Service | For |
|---|---|
| `app.masking` | `detect`, `mask`, `unmask`, and `mask_document` |
| `app.sessions` | The vault's sessions: `list`, `get`, `find_token`, `export`, `prune` |
| `app.features` | `list`, `get`, and `enable` or `disable` in this process, unsaved |
| `app.config` | Read, explain, change, validate, and `migrate` the configuration |
| `app.audit` | The audit trail and the tap: `totals`, `records`, `tap_files` |
| `app.plugins` | `list`: what the config loads, and the packages installed |
| `app.agents` | The agents `privyx run` launches |
| `app.diagnostics` | The checks of `privyx doctor` and `privyx config lint` |
| `app.inspection` | Detection span by span, and regex checks, for `privyx detect` |
| `app.workspace` | The working directory's scan, and the checks of its git hooks |

[Python library](python-library.md#mask-without-holding-an-engine) has an
example with `app.masking` and `app.sessions`.

## Routes

`privyx proxy` and `privyx run` serve the routes of `app.router`, in both
proxy modes, before the proxy's own. A handler finds the engine at
`request.app.state.engine` and the settings at `request.app.state.settings`:

```python
from fastapi import Request


@app.router.get("/acme/sessions/{session_id}/count")
async def count(session_id: str, request: Request) -> dict[str, int]:
    session = await request.app.state.engine.vault.get(session_id)
    return {"values": len(session.mapping) if session else 0}
```

- A route that would take a path of `proxy.routes` stops startup: requests
  there must reach masking. A request on a masking path never reaches a
  plugin route either way. The `*` in a Gemini route stands for any segment,
  so `/acme/{item}` is refused for `/acme/cachedContents`; use
  `/acme/items/{item}`.
- `/health` and `/metrics` stay Privyx's.
- Routes are served on the proxy's port without authentication. A route that
  returns a session's mapping returns the original values to anyone who can
  reach the port.
- Add routes before `app.serve()`; one added later is not served.

## From a package

A package does the same in a `setup(app)` function, and the config names it
under `plugins.modules`. See
[Plugins from a package](plugins.md#plugins-from-a-package).

## Limits

- Code added here, like a plugin, runs in the process that sees unmasked
  text.
- `app.serve()` does not reload your code; restart it after a change.
