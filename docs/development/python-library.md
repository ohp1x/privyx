---
description: Use the Privyx engine inside your own Python program. Mask and restore text, restore a stream, or serve the gateway from your code.
---

# Python library

The proxy and the CLI are thin layers over one engine, and that engine is an
ordinary Python object. Use it directly when you want to mask text inside
your own program: before writing to a log, before storing a prompt, in a
batch job, or in a service of your own.

!!! note "Stability"

    The engine's methods shown here are what the proxy itself calls, and the
    scripts on this page are run by the test suite. The Python API is
    experimental and may change in a `0.x` release; pin the version you build
    on.

The engine is asynchronous. Call it from `async` code, or wrap a call in
`asyncio.run`.

## Build the engine from configuration

[`Privyx`](python-api.md) reads the same layers as the CLI: the built-in
defaults, the [config files](../guide/configuration.md#where-settings-come-from)
(or one alone, if you pass its path), the `PRIVYX_*` environment, and overrides
you pass. `app.engine()` assembles the detector, policy, operator, and vault
those settings describe.

```python
--8<-- "examples/from_config.py"
```

`app.engine()` is an async context manager. Contexts open at once, nested or in
parallel tasks, share one engine, and the last to leave closes its components.

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

The configuration is not applied then. Each component takes the defaults of
its own options. The engine has no anchor and no leak check unless you pass
`dlp_check=True`, and that check treats `_` as a word separator only with
`underscore_separates_words=True`. Set them yourself, or start from `Privyx`.

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

Every error Privyx raises derives from `privyx.PrivyxError`. `privyx.core` has
`ConfigError` for a setting that cannot work, `DetectorError` when a scan
fails, `VaultError` when the session store does, and `SessionNotFoundError`
for an unknown session id.

## Mask without holding an engine

The services of [`Privyx`](python-api.md#services) open the engine for each
call, or use the one already open:

```python
import asyncio

from privyx import Privyx

app = Privyx(vault={"type": "sqlite", "dsn": "sessions.db"})


async def main() -> None:
    masked = await app.masking.mask("write to ana@example.com")
    print(masked.text)
    print(await app.masking.unmask(masked.text, session_id=masked.session.session_id))
    print(len(await app.sessions.list()))


asyncio.run(main())
```

## Extending the engine

To add a detector, an operator, a policy, a vault, a provider, or another kind
of component, write a [plugin](plugins.md). The tutorial [A detector plugin](../tutorials/detector-plugin.md)
builds one from start to finish.
