---
description: Use the Privyx engine inside your own Python program. Mask and restore text, restore a stream, or serve the gateway from your code.
---

# Python library

The proxy and the CLI are thin layers over one engine, and that engine is an
ordinary Python object. Use it directly when you want to mask text inside
your own program: before writing to a log, before storing a prompt, in a
batch job, or in a service of your own.

!!! note "Stability"

    Privyx is in its `0.1.x` series. The engine's methods shown here are what
    the proxy itself calls, and the scripts on this page are run by the test
    suite. The import paths of everything else may still move between
    releases; pin the version you build on.

The engine is asynchronous. Call it from `async` code, or wrap a call in
`asyncio.run`.

## Build the engine from configuration

`load_config` reads the same layers as the CLI: the built-in defaults, the
[config files](../guide/configuration.md#where-settings-come-from) (or one
alone, if you pass its path), the `PRIVYX_*` environment, and overrides you
pass. `build_engine`
assembles the detector, policy, operator, and vault those settings describe.

```python
--8<-- "examples/from_config.py"
```

`build_engine` returns the engine and a function that releases the vault.
Call it when you are done.

Everything a config file can do works here: word lists, patterns, Presidio,
the `encrypt` operator, a SQLite or Redis vault, plugins.

## The three calls

| Call | Does |
|---|---|
| `await engine.get_or_create_session(session_id=None)` | Returns the session with that id, or creates one. Without an id, a new random one. |
| `await engine.transform(text, session=session)` | Detects, applies the policy, masks. Returns a result whose `.text` is the masked text and whose `.transformations` list each replacement. |
| `await engine.restore(text, session_id)` | Puts the original values back. Raises `SessionNotFoundError` when the vault has no such session. |

A session is the mapping between tokens and values. Keep its id for as long
as you need to restore text that was masked with it; with a persistent vault,
the id is all you have to keep.

## Assemble it by hand

To skip the configuration layer, pass the four components yourself:

```python
--8<-- "examples/basic.py"
```

## Restore a stream

A model's reply usually arrives in pieces, and a token can be cut in two.
`StreamingDeanonymizer` holds back a fragment that could be the start of a
token until it knows:

```python
--8<-- "examples/stream_restore_openai.py"
```

`feed` returns the text that is safe to show so far; `flush` returns whatever
was still held back when the stream ends.

## Serve the gateway from your program

```python
--8<-- "examples/fastapi_gateway.py"
```

## Errors

Every error Privyx raises derives from `privyx.core.errors.PrivyxError`:
`ConfigError` for a setting that cannot work, `DetectorError` when a scan
fails, `VaultError` when the session store does, and `SessionNotFoundError`
for an unknown session id.

## Extending the engine

To add a detector, an operator, a policy, a vault, or a provider, write a
[plugin](plugins.md). The tutorial [A detector plugin](../tutorials/detector-plugin.md)
builds one from start to finish.
