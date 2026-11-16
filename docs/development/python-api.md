---
description: Assemble Privyx in Python with the experimental Privyx class. Add components, routes, and hooks, then serve the proxy or take the engine.
---

# Python API

`Privyx` puts the proxy together from a Python file of your own: the same
configuration as `privyx proxy`, plus components, routes, and hooks you add in
code. Serve it, or take only the engine for your program.

!!! note "Experimental"

    `Privyx` and the plugin interface may change in a `0.x` release. Pin the
    version you build on.

```python
--8<-- "examples/privyx_app.py"
```

## The class

| Call | Does |
|---|---|
| `Privyx(config=None, **overrides)` | Reads the settings as `privyx proxy` does: `config` is a file read alone, as with `--config`; without it, the usual [config files](../guide/configuration.md#where-settings-come-from). `overrides` win over the files and the environment, as command-line flags do: `Privyx(port=8080, detector={"terms": {...}})`. |
| `px.add(cls)` | Adds a component class, of any [family](plugins.md#what-can-be-plugged-in), selected by its `name`, with its [`Config`](plugins.md#options) checked as for a plugin. Returns `cls`, so it works as a class decorator. |
| `px.router` | A FastAPI `APIRouter` whose routes the proxy serves beside its own. |
| `px.on_startup`, `px.on_shutdown` | Decorators for a function, sync or async, run once the engine is built and when serving stops: by `px.run()`, and for a module's `setup(px)` by `privyx proxy` and `privyx run` too; never by `px.engine()`. |
| `px.run()` | Serves the proxy until stopped, as `privyx proxy` does, with the routes and hooks. |
| `px.engine()` | An async context manager that yields the privacy engine with the components added; the vault is closed on exit. See [Python library](python-library.md) for its calls. |

Components added with `px.add` sit beside those from `plugins.paths` and
`plugins.modules`; two with the same name in one family stop startup. Plugins
are registered for the whole process, so a process holds one `Privyx`.

## Routes

`privyx proxy` and `privyx run` serve the routes of `px.router`, in both
proxy modes, before the proxy's own. A handler finds the engine at
`request.app.state.engine` and the settings at `request.app.state.settings`:

```python
from fastapi import Request


@px.router.get("/acme/sessions/{session_id}/count")
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
- Add routes before `px.run()`; one added later is not served.

## From a package

A package does the same in a `setup(px)` function, and the config names it
under `plugins.modules`. See
[Plugins from a package](plugins.md#plugins-from-a-package).

## Limits

- Code added here, like a plugin, runs in the process that sees unmasked
  text.
- `px.run()` has no `--reload`; restart it after a change.
