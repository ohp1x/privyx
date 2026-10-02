---
description: Mask logs, JSON documents, and JSONL datasets with privyx mask before they go to a model or a third party, and restore the answer with privyx unmask. No proxy needed.
---

# Files and logs in a pipeline

Not everything reaches a model through an API client you control. You paste a
log into a chat window, send a dataset to a batch job, or hand a file to a
tool that calls a provider on its own. `privyx mask` replaces the sensitive
values in such a file before it leaves, and `privyx unmask` puts them back
into whatever comes back. Both use the same engine as the proxy, without
running one.

**You need:** Privyx [installed](../guide/installation.md). **Time:** ten
minutes.

## 1. Mask a file

Take a log you would like help with:

```text
2026-10-02 09:12:44 ERROR payment failed user=ann@example.com ip=10.0.4.17 card=4111 1111 1111 1111
2026-10-02 09:12:45 WARN  retry with DB_PASSWORD=hunter2 against db-3
2026-10-02 09:12:46 INFO  connecting to postgres://app:s3cr3tpw@db-3:5432/shop
```

```console
$ privyx mask --map map.json -i app.log -o app.masked.log
session: ses_b46149964cd144c7
$ cat app.masked.log
2026-10-02 09:12:44 ERROR payment failed user=<PRIVYX_EMAIL_1> ip=<PRIVYX_IP_ADDRESS_2> card=<PRIVYX_CREDIT_CARD_3>
2026-10-02 09:12:45 WARN  retry with DB_PASSWORD=<PRIVYX_SECRET_4> against db-3
2026-10-02 09:12:46 INFO  connecting to postgres://app:<PRIVYX_URL_CREDENTIAL_5>@db-3:5432/shop
```

`app.masked.log` is safe to paste or send. `map.json` is not: its `mapping`
section holds each token with its original value.

```json
{
  "<PRIVYX_EMAIL_1>": "ann@example.com",
  "<PRIVYX_IP_ADDRESS_2>": "10.0.4.17",
  "<PRIVYX_CREDIT_CARD_3>": "4111 1111 1111 1111",
  "<PRIVYX_SECRET_4>": "hunter2",
  "<PRIVYX_URL_CREDENTIAL_5>": "s3cr3tpw"
}
```

Privyx creates the map readable by its owner only. Keep it out of version
control and delete it when you no longer need the originals back.

## 2. Restore the answer

The model's answer talks about tokens. Save it, and restore it with the same
map:

```text
The payment for <PRIVYX_EMAIL_1> failed because the card <PRIVYX_CREDIT_CARD_3> was declined.
Rotate <PRIVYX_SECRET_4>: it was written to the log in plain text.
```

```console
$ privyx unmask --map map.json -i answer.txt
The payment for ann@example.com failed because the card 4111 1111 1111 1111 was declined.
Rotate hunter2: it was written to the log in plain text.
```

A token the map does not know is left as it is.

## 3. Use it in a pipe

Both commands read standard input and write standard output, so they fit
between other programs:

```bash
kubectl logs deploy/checkout | privyx mask --map map.json --stdin > checkout.masked.log
```

```bash
privyx mask --map map.json -i build.log \
  | your-llm-tool "why did this build fail?" \
  | privyx unmask --map map.json --stdin
```

The `session:` line goes to standard error, so it does not end up in the
pipe.

An existing map is extended, not overwritten. Mask a second file against the
same map and a value seen before keeps its token:

```console
$ privyx mask --map map.json -i second.txt
session: ses_b46149964cd144c7
<PRIVYX_EMAIL_1> retried from <PRIVYX_IP_ADDRESS_6>
```

## 4. JSON and JSONL

For a `.json` file, Privyx masks every string value and leaves keys, numbers,
and structure alone:

```console
$ privyx mask --map request-map.json -i request.json
session: ses_ec806bcac7864c83
{
  "model": "gpt-4o-mini",
  "user": "<PRIVYX_EMAIL_1>",
  "messages": [
    {
      "role": "system",
      "content": "You are a support assistant."
    },
    {
      "role": "user",
      "content": "Refund <PRIVYX_EMAIL_1>, phone <PRIVYX_PHONE_2>"
    }
  ]
}
```

`--path` limits it to one part of the document. It can be given several
times:

```bash
privyx mask --map request-map.json -i request.json --path '$.messages'
```

A `.jsonl` or `.ndjson` file is handled line by line, one JSON document per
line, which suits datasets and event logs:

```console
$ cat tickets.jsonl | privyx mask --map tickets-map.json -f jsonl -i - -o tickets.masked.jsonl
session: ses_128b787e1bb14d3a
$ cat tickets.masked.jsonl
{"id": 1, "from": "<PRIVYX_EMAIL_1>", "text": "Order ORD-2026-000123 still shows the old address."}
{"id": 2, "from": "<PRIVYX_EMAIL_2>", "text": "Please call me on <PRIVYX_PHONE_3>."}
```

The format comes from the file's extension. Reading from a pipe there is no
extension, so name it with `-f json` or `-f jsonl`; without one, input is
treated as plain text.

## 5. The same token in every file

A map file keeps tokens consistent across the files you mask against it. To
get the same token for the same value everywhere, without sharing a map, set
an anchor secret. The token's id is then derived from the value:

```console
$ export PRIVYX_ANCHOR_SECRET=$(openssl rand -hex 32)
$ privyx mask --map a.json "mail ann@example.com"
session: ses_c5bc5e7ff6a94e23
mail <PRIVYX_EMAIL_E55D8A16FDB2F399>
$ privyx mask --map b.json "cc ann@example.com and bob@example.com"
session: ses_8dc07daa04b745ee
cc <PRIVYX_EMAIL_E55D8A16FDB2F399> and <PRIVYX_EMAIL_B4C7EDA9B547BA1C>
```

That makes masked datasets joinable: the same customer has the same token in
every file. Whoever holds the secret can test a guess against a token, so
keep it as private as a key. See [Anchors](../guide/masking.md#anchors).

## 6. Redact for good

When nothing should ever be restored, for a log you attach to a public bug
report for instance, use the `redact` operator. It keeps no mapping:

```yaml
# redact.yaml
operator:
  type: redact
  token: "[REDACTED]"
```

```console
$ privyx mask -c redact.yaml -i app.log 2>/dev/null
2026-10-02 09:12:44 ERROR payment failed user=[REDACTED] ip=[REDACTED] card=[REDACTED]
2026-10-02 09:12:45 WARN  retry with DB_PASSWORD=[REDACTED] against db-3
2026-10-02 09:12:46 INFO  connecting to postgres://app:[REDACTED]@db-3:5432/shop
```

Without `--map`, `privyx mask` warns that the output cannot be unmasked;
`2>/dev/null` hides that warning here, where it is the intent.

## Good to know

- **Your own terms apply here too.** Pass `-c my.yaml` to use the word lists
  and patterns from [Your own names and terms](custom-terms.md). A value
  Privyx does not detect stays in the output, so check a sample first with
  `privyx detect --transform`.
- **A vault instead of a map file.** `--session ID` keeps the mapping in the
  configured vault. It needs a persistent one (`sqlite` or `redis`), since
  the default in-memory vault is gone when the command exits.
- **Exit codes.** Both commands exit with `1` and an `Error:` line on a
  missing file, invalid JSON, or an invalid config, so a pipeline stops
  instead of passing unmasked data on.

## Next steps

- [`privyx mask` reference](../guide/cli.md#privyx-mask): every option.
- [Masking](../guide/masking.md): tokens, hashes, encryption, and realistic
  fakes.
- [Python library](../development/python-library.md): the same engine from
  your own code.
