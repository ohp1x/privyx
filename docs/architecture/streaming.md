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

1. **Trie** — all pseudonyms for the session are stored in a trie.
2. **Frontier** — when input starts matching a trie prefix but the chunk
   ends, the partial prefix is held in the frontier.
3. **Match mode** — subsequent characters are checked against the trie:
   - full match → emit original value
   - prefix continues → extend frontier
   - prefix broken → flush buffered text verbatim, reprocess the failing char

## Guarantee

> The concatenation of all output deltas equals the deanonymization of the
> concatenation of all input deltas, for any chunk boundary split.

This is enforced by property-based tests in `tests/property/` (Hypothesis).

## Components

| File | Role |
|---|---|
| `deanonymizer.py` | The stateful streaming algorithm |
| `trie.py` | Pseudonym → original lookup |
| `frontier.py` | Partial-match state across chunks |
| `buffer.py` | Reusable text accumulation |
| `adapters/` | Provider-specific envelope handling |

## Reasoning Content

Non-text deltas (e.g. Anthropic `thinking_delta`, OpenAI `reasoning_content`)
are handled by adapters: they pass through untouched, never entering the text
transformation layer.
