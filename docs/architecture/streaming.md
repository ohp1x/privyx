# Streaming Architecture

## The Problem

A pseudonym like `<PRIVYX_EMAIL_1>` may be split across stream chunks:

```text
chunk 1: "...<PRIVYX_EMAI"
chunk 2: "L_1> is the email..."
```

Naive substring replacement fails on chunk boundaries. Replacing on the
concatenated text is impossible in a live stream (you cannot un-send
previous chunks).

## The Algorithm

A single hold-back scan (`_BufferedStream` in `deanonymizer.py`) drives every
stream. For each position it asks a **recognizer** two questions:

1. **Could a token still start here?** — `longest_prefix_len`. If the viable
   prefix reaches the end of the buffer, that tail is *held back*; it may
   continue in the next chunk.
2. **Is there a complete token here?** — `match_at`. If so, emit its
   replacement and skip past it; otherwise emit one character and advance.

On `flush`, held-back text is resolved: a complete token becomes its
replacement, anything else is emitted verbatim.

Only the "is there a token here?" decision is pluggable
(`streaming/recognizer.py`):

- **`TrieRecognizer`** matches the exact pseudonym strings a session issued
  (a trie of known values) — syntax-agnostic. Backs `StreamingDeanonymizer`.
- **`CodecRecognizer`** matches any text that fits the configured token syntax
  via the token codec, reconstructs the logical
  token, and resolves it against the session mapping. Backs
  `TokenStreamProcessor`, which the proxy uses — so streaming recognizes tokens
  the same way everything else does, and knows no syntax of its own.

## Guarantee

> The concatenation of all output deltas equals the deanonymization of the
> concatenation of all input deltas, for any chunk boundary split.

This is enforced by property-based tests in `tests/property/` (Hypothesis),
including exhaustive single-boundary coverage.

## Components

| File | Role |
|---|---|
| `deanonymizer.py` | The stateful hold-back scan (`StreamingDeanonymizer`, `TokenStreamProcessor`) |
| `recognizer.py` | Trie- vs codec-driven token recognition |
| `trie.py` | Pseudonym → original lookup |
| `buffer.py` | Reusable text accumulation |
| `adapters/` | Provider-specific envelope handling |

## Reasoning Content

Reasoning deltas (Anthropic `thinking_delta`, OpenAI `reasoning_content` /
`reasoning`, Responses `reasoning_summary_text` / `reasoning_text`) are
deanonymized like visible text, each on its own buffer so a held-back fragment
never crosses from one stream into another. Events that are not deltas carry
complete strings and are restored leaf by leaf — see
[proxy.md](proxy.md#what-gets-transformed-and-restored).
