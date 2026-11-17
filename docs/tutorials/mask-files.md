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
session: ses_07b99feb252541f3
$ cat app.masked.log
2026-10-02 09:12:44 ERROR payment failed user=<PRIVYX_EMAIL_F04C885A31827851> ip=<PRIVYX_IP_ADDRESS_C8E127AF2D5584C3> card=<PRIVYX_CREDIT_CARD_9D9EDBCE95014B38>
2026-10-02 09:12:45 WARN  retry with DB_PASSWORD=<PRIVYX_SECRET_A2907293658B4995> against db-3
2026-10-02 09:12:46 INFO  connecting to postgres://app:<PRIVYX_URL_CREDENTIAL_CDCD0EB22012CECC>@db-3:5432/shop
```

`app.masked.log` is safe to paste or send. `map.json` is not: besides the
session id and timestamps, it holds a `mapping` section with each token and
its original value:

```json
{
  "<PRIVYX_EMAIL_F04C885A31827851>": "ann@example.com",
  "<PRIVYX_IP_ADDRESS_C8E127AF2D5584C3>": "10.0.4.17",
  "<PRIVYX_CREDIT_CARD_9D9EDBCE95014B38>": "4111 1111 1111 1111",
  "<PRIVYX_SECRET_A2907293658B4995>": "hunter2",
  "<PRIVYX_URL_CREDENTIAL_CDCD0EB22012CECC>": "s3cr3tpw"
}
```

Privyx creates the map readable by its owner only. Keep it out of version
control and delete it when you no longer need the originals back.

## 2. Restore the answer

The model's answer talks about tokens. Save it, and restore it with the same
map:

```text
The payment for <PRIVYX_EMAIL_F04C885A31827851> failed because the card <PRIVYX_CREDIT_CARD_9D9EDBCE95014B38> was declined.
Rotate <PRIVYX_SECRET_A2907293658B4995>: it was written to the log in plain text.
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

`privyx mask` reads all of its input before it writes anything. To follow a
stream that does not end, such as `tail -f`, use
[`privyx pipe`](../guide/cli.md#privyx-pipe), which writes each line as soon
as it is masked:

```bash
tail -f app.log | privyx pipe --session app-logs
```

`privyx pipe` keeps its mapping in a vault session rather than a map file, so
restoring its output later with `privyx pipe --restore --session app-logs`
needs a `sqlite` or `redis` vault; see [Sessions and vault](../guide/sessions.md).

An existing map is extended, not overwritten. Mask a second file against the
same map and a value seen before keeps its token:

```console
$ privyx mask --map map.json -i second.txt
session: ses_07b99feb252541f3
<PRIVYX_EMAIL_F04C885A31827851> retried from <PRIVYX_IP_ADDRESS_2B178AF7C81A883C>
```

## 4. JSON and JSONL

For a `.json` file, Privyx masks every string value and leaves keys, numbers,
and structure alone:

```console
$ privyx mask --map request-map.json -i request.json
session: ses_c733258f84b142f0
{
  "model": "gpt-4o-mini",
  "user": "<PRIVYX_EMAIL_F04C885A31827851>",
  "messages": [
    {
      "role": "system",
      "content": "You are a support assistant."
    },
    {
      "role": "user",
      "content": "Refund <PRIVYX_EMAIL_F04C885A31827851>, phone <PRIVYX_PHONE_36C1764FD294E3EC>"
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
session: ses_bd76c9559e124589
$ cat tickets.masked.jsonl
{"id": 1, "from": "<PRIVYX_EMAIL_F04C885A31827851>", "text": "Order ORD-2026-000123 still shows the old address."}
{"id": 2, "from": "<PRIVYX_EMAIL_423FB33D92FB967D>", "text": "Please call me on <PRIVYX_PHONE_36C1764FD294E3EC>."}
```

The format comes from the file's extension. Reading from a pipe there is no
extension, so name it with `-f json` or `-f jsonl`; without one, input is
treated as plain text.

## 5. The same token in every file

A token's id is derived from the value with your anchor key, so the same value
gets the same token in every file, with a different map or none:

```console
$ privyx mask --map a.json "mail ann@example.com"
session: ses_de55cc01b7ba4168
mail <PRIVYX_EMAIL_F04C885A31827851>
$ privyx mask --map b.json "cc ann@example.com and bob@example.com"
session: ses_ba9bb3b097aa4623
cc <PRIVYX_EMAIL_F04C885A31827851> and <PRIVYX_EMAIL_423FB33D92FB967D>
```

That makes masked datasets joinable: the same customer has the same token in
every file. The key is created in `~/.privyx/anchor.key` on first use; to get
the same tokens on another machine or for another user, give both the same
secret:

```bash
export PRIVYX_ANCHOR_SECRET=$(openssl rand -hex 32)
```

Whoever holds the key can test a guess against a token, so keep it as private
as an API key. See [Anchors](../guide/masking.md#anchors).

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
