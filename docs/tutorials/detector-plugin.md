---
description: Write a Privyx detector plugin in Python. A worked example that finds IBANs and checks their checksum, with options, a doctor check, and a test.
---

# A detector plugin

Word lists and patterns cover most needs. Some values need code: a number
that is only sensitive when its checksum holds, a name you have to look up in
your own customer database, a format too irregular for a regular expression.
For those, Privyx loads detectors you write yourself.

This tutorial builds one that finds IBANs, international bank account
numbers. A pattern can describe their shape. Telling a real IBAN from a
string of the same shape takes a checksum, and that is what the plugin adds.

**You need:** Privyx [installed](../guide/installation.md) and some Python.
**Time:** twenty minutes.

## 1. What a detector is

A detector is a class with one method. It receives a text and returns the
spans it considers sensitive, each with a start, an end, an entity type, and
the matched text:

```python
from privyx.core.context import Context
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector


class MyDetector(BaseDetector):
    name = "my_detector"            # the value `detector.type` selects

    def detect_sync(self, text: str, context: Context) -> Detection:
        detection = Detection()
        # detection.add(start, end, "ENTITY_TYPE", text[start:end])
        return detection
```

Everything after detection, the policy, the tokens, the session, the restore
of the reply, works for your entity as for a built-in one.

## 2. Write the plugin

Create a folder `plugins` next to your config file, and in it `iban.py`:

```python
--8<-- "plugins/detectors/iban.py"
```

Four parts do the work:

- **`_CANDIDATE`** is a deliberately loose pattern: anything shaped like an
  IBAN.
- **`_valid`** runs the standard check: move the first four characters to the
  end, read letters as numbers, and the result modulo 97 must be 1.
- **`_iban_in`** handles what a greedy pattern picks up by accident. In
  `BE68 5390 0754 7034 EUR`, the currency looks like one more group, so the
  candidate fails the check until that group is dropped.
- **`detect_sync`** adds a span for each number that passes, under the entity
  type `IBAN`.

## 3. Load it

Privyx loads no plugin unless you list its path. Name the folder, and select
the detector by its `name`:

```yaml
# privyx.yaml
plugins:
  paths: [./plugins]

detector:
  - type: regex             # the built-in patterns
  - type: iban              # the name the plugin registered
```

```console
$ privyx detect -c privyx.yaml --transform "Refund DE89 3704 0044 0532 0130 00 and confirm to ann@example.com"
     7:34    IBAN              'DE89 3704 0044 0532 0130 00'
    50:65    EMAIL             'ann@example.com'

Refund <PRIVYX_IBAN_1> and confirm to <PRIVYX_EMAIL_2>
```

Change the last digit and the checksum no longer holds, so the number is left
alone:

```console
$ privyx detect -c privyx.yaml "DE89370400440532013000"
     0:22    IBAN              'DE89370400440532013000'
$ privyx detect -c privyx.yaml "DE89370400440532013001"
No sensitive entities detected.
```

A plugin never replaces a built-in type: `regex`, `presidio`, and the other
built-in names always win, and a plugin is used only when `type` is not one
of them.

## 4. Give it options

Keys in a plugin's section that Privyx does not know are passed to the
plugin's `from_config`. The example reads `countries` there:

```yaml
detector:
  - type: regex
  - type: iban
    countries: [DE, NL]     # an option of the plugin's own
```

```console
$ privyx detect -c privyx.yaml --transform "GB82WEST12345698765432 or NL91ABNA0417164300"
    26:44    IBAN              'NL91ABNA0417164300'

GB82WEST12345698765432 or <PRIVYX_IBAN_1>
```

Without a `from_config`, Privyx builds the class with no arguments.

## 5. Check it with doctor

`privyx doctor` lists the plugins it loaded and runs the whole pipeline once:

```console
$ privyx doctor -c privyx.yaml
Privyx Doctor
=============
  ✓ plugins: 1 plugin(s) — detector: iban
  ✓ detector: regex+iban: 1 span(s) ['EMAIL']
  ✓ vault: memory: round-trip ok
  ✓ provider: generic → http://localhost:20128
  ✓ proxy: request transformed, 1 mapping(s) vaulted
  ✓ streaming: pseudonym restored across a split chunk
  ✓ configuration: valid: detector=regex+iban policy=default operator=pseudonym vault=memory anchor=off
```

A path that does not exist, a file that fails to import, or two plugins with
the same name show up as a failed check there, and stop the proxy at startup
with the reason:

```console
$ privyx proxy -c privyx.yaml
Error: failed to import plugin plugins/broken.py: No module named 'nothing_here'
```

## 6. Test it

A detector is plain Python, so a unit test is short. Load the folder the way
Privyx does, build the detector from a config, and check what it finds:

```python
from privyx.config.schema import Settings
from privyx.core.context import Context
from privyx.plugins.loader import load_plugins
from privyx.privacy.detector.yaml import build_detector


async def test_iban_detector_keeps_only_a_valid_checksum() -> None:
    load_plugins(Settings(plugins={"paths": ["plugins"]}))
    detector = build_detector({"type": "iban"})
    text = "DE89 3704 0044 0532 0130 00 EUR, not DE89370400440532013001"
    found = await detector.detect(text, Context())
    assert [span.text for span in found.spans] == ["DE89 3704 0044 0532 0130 00"]
```

This needs `pytest-asyncio` with `asyncio_mode = "auto"`, or a wrapper around
`asyncio.run`.

## 7. Ship it

The plugin is now part of your configuration. Give the same file to
`privyx proxy`, `privyx run`, or `privyx mask` with `-c privyx.yaml`.

In Docker, mount the folder and name it. The image keeps plugins under
`/app/plugins`, and the repository's `docker-compose.yml` already mounts
`./plugins` there:

```bash
docker run -d -p 127.0.0.1:8000:8000 \
  -v "$PWD/plugins:/app/plugins:ro" \
  -v "$PWD/privyx.yaml:/etc/privyx.yaml:ro" \
  -e PRIVYX_CONFIG=/etc/privyx.yaml \
  -e PRIVYX_PLUGIN_PATHS=/app/plugins \
  -e PRIVYX_UPSTREAM_URL=https://api.openai.com \
  ohp1x/privyx
```

`PRIVYX_PLUGIN_PATHS` overrides `plugins.paths` from the file, which is
relative to where Privyx starts.

## What a plugin has to respect

- **It runs inside the proxy and sees unmasked text.** Load only code you
  trust. Privyx loads plugins from the local paths you list and from nowhere
  else.
- **It runs on every piece of text in every request.** Keep it fast. Results
  are cached by text, so a long conversation is not rescanned each turn.
- **A failure fails the request.** If `detect_sync` raises, Privyx answers
  `503` and does not forward the request, rather than forward it unscanned.
- **I/O belongs in `detect`.** For a lookup over the network, override the
  async `detect` method instead of `detect_sync`. Report what the detector
  did with `context.counters["my_lookups"] += 1`: the counts land in the
  audit trail and in `/metrics`, so they must be numbers, never text.
- **Entity names are letters, digits, and underscores**, starting with a
  letter. With the `strict` policy, add yours to `policy.allowed`.

## Next steps

- [Plugins](../development/plugins.md): the contract in full, lifecycle hooks,
  and the other things a plugin can be: an operator, a policy, an anchor, a
  vault, or a provider.
- [Privacy engine](../architecture/privacy-engine.md): how the detector fits
  with the policy, the operator, and the vault.
