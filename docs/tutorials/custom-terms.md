---
description: Teach Privyx your own names, companies, project codenames, and ID formats with word lists and patterns, then add Presidio or an LLM detector for names you cannot list.
---

# Your own names and terms

Out of the box, Privyx finds values that have a recognizable shape: an email
address, a phone number, an API key. A person's name, a customer's company,
or your project's codename looks like any other word, so Privyx masks it only
after you tell it to. This tutorial builds that configuration step by step and
tests it on a sample before any request depends on it.

**You need:** Privyx [installed](../guide/installation.md). **Time:** fifteen
minutes.

## 1. Start with a sample

Collect a few lines of the kind of text you send to a model, and save them as
`sample.txt`. This tutorial uses a support ticket:

```text
Ticket from Bob Marsh (bob.marsh@initech.example), Initech:
"Ann promised the Bluebird beta by Friday. Our order ORD-2026-000123
still shows the old address. Call me on +1 415 555 0142."
Handled by EMP-004217.
```

Without configuration, Privyx finds the email address and the phone number,
and nothing else:

```console
$ privyx detect --transform --stdin < sample.txt
    23:48    EMAIL             'bob.marsh@initech.example'
   169:184   PHONE             '+1 415 555 0142'

Ticket from Bob Marsh (<PRIVYX_EMAIL_1>), Initech:
"Ann promised the Bluebird beta by Friday. Our order ORD-2026-000123
still shows the old address. Call me on <PRIVYX_PHONE_2>."
Handled by EMP-004217.
```

The names, the company, the codename, and the two IDs are still there.

## 2. List the words you know

`terms` maps an entity name to plain words. Privyx escapes them, so no regex
knowledge is needed:

```yaml
# my.yaml
detector:
  type: regex                 # keeps the built-in patterns
  terms:
    PERSON:       [ann, bob]
    ORGANIZATION: [acme, initech]
    PROJECT:      [bluebird]
```

```console
$ privyx detect -c my.yaml --transform "Bob at Initech wants Bluebird specs before Acme does"
     0:3     PERSON            'Bob'
     7:14    ORGANIZATION      'Initech'
    21:29    PROJECT           'Bluebird'
    43:47    ORGANIZATION      'Acme'

<PRIVYX_PERSON_1> at <PRIVYX_ORGANIZATION_2> wants <PRIVYX_PROJECT_3> specs before <PRIVYX_ORGANIZATION_4> does
```

![A terminal: a word list in my.yaml, and privyx detect replacing the listed person, organizations, and project in a sentence with tokens.](../assets/terms.gif)

A few rules:

- The entity name is yours to choose. It becomes part of the token, so the
  model still knows that `<PRIVYX_PROJECT_3>` is a project.
- Matching ignores case and only hits whole words: `ann` does not match
  inside `annual`.
- A term can hold spaces or punctuation: `bob marsh`, `git.internal.example`.
  Longer terms are tried first.

## 3. Add patterns for IDs

Values that follow a format are better described by a regular expression.
`patterns` maps an entity name to one:

```yaml
# my.yaml
detector:
  type: regex
  terms:
    PERSON:       [ann, bob]
    ORGANIZATION: [acme, initech]
    PROJECT:      [bluebird]
  patterns:
    EMPLOYEE_ID: '\bEMP-\d{6}\b'
    ORDER_ID:    '\bORD-\d{4}-\d{6}\b'
    LICENSE_KEY: '(?i)license[ _-]?key\s*[:=]\s*(?P<value>[A-Z0-9-]{10,})'
```

```console
$ privyx detect -c my.yaml --transform "Ann (EMP-004217) refunded ORD-2026-000123. License-Key: QX7T-22LM-90PZ"
     0:3     PERSON            'Ann'
     5:15    EMPLOYEE_ID       'EMP-004217'
    26:41    ORDER_ID          'ORD-2026-000123'
    56:70    LICENSE_KEY       'QX7T-22LM-90PZ'

<PRIVYX_PERSON_1> (<PRIVYX_EMPLOYEE_ID_2>) refunded <PRIVYX_ORDER_ID_3>. License-Key: <PRIVYX_LICENSE_KEY_4>
```

- Write patterns in single quotes, so YAML leaves the backslashes alone.
- Patterns are case-sensitive; start one with `(?i)` to ignore case.
- A group named `value` masks only that part. The last pattern hides the key
  and leaves `License-Key:` for the model to read.
- A pattern named like a built-in entity, such as `PHONE`, replaces the
  built-in one.

A misspelled key or an invalid regex stops Privyx at startup, with the
setting named in the message, rather than silently not applying.

## 4. Test it on the sample

```console
$ privyx detect -c my.yaml --transform --stdin < sample.txt
    12:15    PERSON            'Bob'
    23:48    EMAIL             'bob.marsh@initech.example'
    51:58    ORGANIZATION      'Initech'
    61:64    PERSON            'Ann'
    78:86    PROJECT           'Bluebird'
   113:128   ORDER_ID          'ORD-2026-000123'
   169:184   PHONE             '+1 415 555 0142'
   198:208   EMPLOYEE_ID       'EMP-004217'

Ticket from <PRIVYX_PERSON_1> Marsh (<PRIVYX_EMAIL_2>), <PRIVYX_ORGANIZATION_3>:
"<PRIVYX_PERSON_4> promised the <PRIVYX_PROJECT_5> beta by Friday. Our order <PRIVYX_ORDER_ID_6>
still shows the old address. Call me on <PRIVYX_PHONE_7>."
Handled by <PRIVYX_EMPLOYEE_ID_8>.
```

Much better, and it shows the limit of a list: `Marsh` is still there, because
only `bob` was listed. You can add `bob marsh`, and you will keep adding names
for as long as new people appear in your text.

## 5. Find names you cannot list

For names nobody listed in advance, add a detector that reads language. A
`detector` list runs several detectors and pools what they find.

=== "Presidio"

    [Presidio](https://microsoft.github.io/presidio/) recognizes names and
    places with a language model that runs locally:

    ```bash
    pip install 'privyx[presidio]'
    python -m spacy download en_core_web_sm
    ```

    ```yaml
    # my.yaml
    detector:
      - type: regex               # built-in patterns, your terms and patterns
        terms:
          ORGANIZATION: [acme, initech]
          PROJECT:      [bluebird]
        patterns:
          EMPLOYEE_ID: '\bEMP-\d{6}\b'
          ORDER_ID:    '\bORD-\d{4}-\d{6}\b'
      - type: presidio            # needs privyx[presidio] and a spaCy model
        language: en
        entities: [PERSON, LOCATION]
    ```

    ```console
    $ privyx detect -c my.yaml --transform --stdin < sample.txt
        12:21    PERSON            'Bob Marsh'
        23:48    EMAIL             'bob.marsh@initech.example'
        51:58    ORGANIZATION      'Initech'
        61:64    PERSON            'Ann'
        78:86    PROJECT           'Bluebird'
       113:128   ORDER_ID          'ORD-2026-000123'
       169:184   PHONE             '+1 415 555 0142'
       198:208   EMPLOYEE_ID       'EMP-004217'

    Ticket from <PRIVYX_PERSON_1> (<PRIVYX_EMAIL_2>), <PRIVYX_ORGANIZATION_3>:
    "<PRIVYX_PERSON_4> promised the <PRIVYX_PROJECT_5> beta by Friday. Our order <PRIVYX_ORDER_ID_6>
    still shows the old address. Call me on <PRIVYX_PHONE_7>."
    Handled by <PRIVYX_EMPLOYEE_ID_8>.
    ```

    `Bob Marsh` is now one `PERSON`, and neither name was listed.

    `entities` limits what Presidio reports. Left empty, it uses every
    recognizer, which here would also mask `Friday` as a `DATE_TIME`. A small
    model also mislabels now and then, a company as a `LOCATION` for
    instance. The value is masked either way; keep your word list for the
    names that must never slip through.

=== "An LLM"

    The `llm` detector asks a language model to list the sensitive values in
    each text. It understands context better than any pattern, and it costs a
    model call per request:

    ```bash
    pip install 'privyx[providers]'
    ```

    ```yaml
    # my.yaml
    detector:
      - type: regex
        terms:
          ORGANIZATION: [acme, initech]
          PROJECT:      [bluebird]
      - type: llm                 # needs privyx[providers] and a provider key
        llm_provider: openai
        llm_model: gpt-4o-mini
    ```

    Three things to know before you choose it:

    - **The detector's model sees the text unmasked.** That is how it finds
      what to mask. Use a provider you already trust with this data, or a
      model you host: the SDK reads `OPENAI_BASE_URL` from the proxy's
      environment, so the detector can call a local, OpenAI-compatible server
      while the chat itself goes to another provider. Do not point it at
      Privyx itself.
    - **It fails closed.** If the scan fails or times out, the request is not
      forwarded, and the client gets a `503`:

        ```json
        {"error": {"type": "privyx_scan_failed", "message": "privyx could not scan this request for sensitive data, so it was not forwarded; see the privyx logs"}}
        ```

        `llm_fallback_on_error: true` scans with the built-in patterns
        instead and lets the request through, without whatever only the
        model would have caught.
    - **It is counted.** The audit trail records the calls and an estimate of
      the tokens they used:
      `detector_counts=llm_calls:1,llm_input_tokens:87,llm_output_tokens:7`.

    The key comes from `llm_api_key` or from the SDK's own variable
    (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). Set `llm_model` to a model your
    account can use.

Whichever you pick, run `privyx detect` on your sample again. It uses the
same detectors as the proxy.

## 6. Put it to work

Give the same file to whatever you run:

```bash
privyx proxy -c my.yaml --upstream https://api.openai.com
privyx run -c my.yaml claude
privyx mask -c my.yaml --map map.json -i ticket.txt
```

or set `PRIVYX_CONFIG=/path/to/my.yaml` once. While you are still tuning the
lists, `--reload` restarts the proxy whenever the file changes:

```bash
privyx proxy -c my.yaml --reload --upstream https://api.openai.com
```

## Mask only some entity types

By default everything a detector finds is masked. The `strict` policy masks
only the types you allow, and leaves the rest in the text:

```yaml
# strict.yaml
detector:
  type: regex
  terms:
    PROJECT: [bluebird]
policy:
  type: strict
  allowed: [EMAIL, PHONE]
```

With `strict`, an entity of your own that is missing from `allowed` is
detected and then left alone, as `PROJECT` is here. `--no-policy` shows what
the policy filtered out:

```console
$ privyx detect -c strict.yaml --transform "bluebird mail a@b.io"
    14:20    EMAIL             'a@b.io'

bluebird mail <PRIVYX_EMAIL_1>
$ privyx detect -c strict.yaml --transform --no-policy "bluebird mail a@b.io"
     0:8     PROJECT           'bluebird'
    14:20    EMAIL             'a@b.io'

<PRIVYX_PROJECT_1> mail <PRIVYX_EMAIL_2>
```

## What to keep in mind

- A word list is exact and fast, and it only knows what you wrote down.
  Review it when people and projects change.
- Common words make poor terms: listing `may` as a name masks every "may".
- Detection results are cached by text, so a long conversation is not
  rescanned on every turn.
- Entity names must start with a letter and hold only letters, digits, and
  underscores.

## Next steps

- [Detection](../guide/detection.md): every detector option, the built-in
  patterns, and the cache.
- [Masking](../guide/masking.md): what a detected value becomes, from tokens
  to realistic fakes.
- [A detector plugin](detector-plugin.md): when a format needs code, such as
  a checksum.
