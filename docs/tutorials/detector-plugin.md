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
from privyx.core import Context, Detection
from privyx.detectors import Detector


class MyDetector(Detector):
    name = "my_detector"            # the key under `detectors` that selects it

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

`setup(app)` at the end of the file adds the class to Privyx. A plugin file
must have one.

## 3. Load it

Privyx loads no plugin unless you list its path. Name the folder, and select
the detector by its `name`:

```yaml
# privyx.yaml
plugins:
  paths: [./plugins]

detectors:
  iban: {}                  # the name the plugin registered; runs after regex
```

```console
$ privyx -c privyx.yaml detect --transform "Refund DE89 3704 0044 0532 0130 00 and confirm to ann@example.com"
     7:34    IBAN              'DE89 3704 0044 0532 0130 00'
    50:65    EMAIL             'ann@example.com'

Refund <PRIVYX_IBAN_0D973D52B3AE0D85> and confirm to <PRIVYX_EMAIL_F04C885A31827851>
```

Change the last digit and the checksum no longer holds, so the number is left
alone:

```console
$ privyx -c privyx.yaml detect "DE89370400440532013000"
     0:22    IBAN              'DE89370400440532013000'
$ privyx -c privyx.yaml detect "DE89370400440532013001"
No sensitive entities detected.
```

A plugin never replaces a built-in type: one that takes the name `regex`,
`presidio`, or another built-in stops startup with an error.

## 4. Give it options

A detector declares its options in a nested `Options` class and reads them
from `self.options`. The example declares `countries`:

```yaml
detectors:
  iban:
    countries: [DE, NL]     # an option of the plugin's own
```

```console
$ privyx -c privyx.yaml detect --transform "GB82WEST12345698765432 or NL91ABNA0417164300"
    26:44    IBAN              'NL91ABNA0417164300'

GB82WEST12345698765432 or <PRIVYX_IBAN_5E604519423B0E90>
```

Privyx checks the options against that class, so a misspelled one stops the
proxy instead of being ignored; see
[Options](../development/plugins.md#options).

## 5. Check it with doctor

`privyx doctor` lists the plugins it loaded and runs the whole pipeline once:

```console
$ privyx -c privyx.yaml doctor
Privyx Doctor
=============
  ✓ plugins: 1 plugin(s) — detector: iban
  ✓ anchor key: /home/alice/.privyx/anchor.key is created on first use
  ✓ detector: regex+iban: 1 span(s) ['EMAIL']
  ✓ permissions: sensitive files owner-only (0600)
  ✓ vault: memory: round-trip ok
  ✓ provider: generic → http://localhost:20128
  ✓ proxy: request transformed, 1 mapping(s) vaulted
  ✓ streaming: pseudonym restored across a split chunk
  ✓ routes: transparent: 27 masking route(s): ...
  ✓ configuration: valid: detector=regex+iban policy=default operator=pseudonym vault=memory anchor=hmac
```

A path that does not exist, a file that fails to import, or two plugins with
the same name show up as a failed check there, and stop the proxy at startup
with the reason:

```console
$ privyx -c privyx.yaml proxy
Error: failed to import plugin plugins/broken.py: No module named 'nothing_here'
```

## 6. Test it

Privyx ships a conformance suite for each kind of plugin. Load the folder the
way Privyx does, name the detector, and give it texts with the values it must
find:

```python
from privyx import Privyx
from privyx.core import Context
from privyx.detectors import Detector
from privyx.testing import DetectorSuite

app = Privyx(plugins={"paths": ["plugins"]})
app.settings  # reads the config, which loads the plugins


class TestIbanDetector(DetectorSuite):
    component = app.registry(Detector).get("iban")
    samples = {"DE89 3704 0044 0532 0130 00 EUR": ["DE89 3704 0044 0532 0130 00"]}

    def test_a_wrong_checksum_is_left_alone(self) -> None:
        assert not self.create().detect_sync("DE89370400440532013001", Context()).spans
```

Run it with `pytest`; the suite needs no async plugin.

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
  trust. Privyx loads plugins from the local paths and modules you list and
  from nowhere else.
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

- [Plugins](../development/plugins.md): the contract in full, events, and the
  other things a plugin can be, such as an operator, a policy, a vault, a
  provider, or a feature.
- [Privacy engine](../architecture/privacy-engine.md): how the detector fits
  with the policy, the operator, and the vault.
