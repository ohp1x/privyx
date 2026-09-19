# Testing

## Layers

| Layer | Location | Purpose |
|---|---|---|
| Unit | `tests/unit/` | Detectors, engine, operators, vaults, SSE, adapters |
| Integration | `tests/integration/` | End-to-end engine + streaming flows |
| Property | `tests/property/` | Hypothesis property tests |

## Property Tests (Principle #13)

All streaming algorithms are property-tested against random chunk boundaries:

- `test_stream_equivalence.py` — streamed output ≡ batch output for any split.
- `test_chunk_boundaries.py` — any boundary split of a pseudonym restores.
- `test_token_stream.py` — the codec's streaming path ≡ batch `restore`, incl.
  exhaustive single-boundary coverage.

This is the guarantee that prevents regressions in the streaming hold-back
scan — the same class of bug we hit with `reasoning_content` in the prototype.

## Running

```bash
uv run pytest                     # all
uv run pytest tests/unit          # unit
uv run pytest tests/property -k stream
uv run pytest -n auto             # parallel
```

## End-to-end: a real agent (`make e2e`)

`scripts/e2e_claude.py` runs the real `claude` CLI through `privyx run` (or, with
`--transparent`, through `privyx proxy` on its defaults: ephemeral sessions, no
anchor) against a local fake Anthropic API, so nothing needs a key or the
network. `make e2e` runs both. HOME,
`CLAUDE_CONFIG_DIR`, and `XDG_CONFIG_HOME` all point into a fresh temp dir, so your
own `~/.claude` and `~/.config/privyx` are left alone.

The script plants canary values everywhere Claude Code gathers context: the
prompt, `CLAUDE.md`, the workspace path, the git branch and log, an MCP server's
instructions, tool description, schema, and output, and a file read through Bash.
The fake upstream plays one agent loop (thinking + MCP call → thinking + Bash call
→ an answer that echoes every token) and records each request Privyx forwards.
The run fails if:

- a canary shows up in a forwarded request (a **leak**; the JSON path is printed);
- the MCP tool's arguments or Claude's final answer still hold a token (a missed
  **restore**);
- an assistant turn Claude Code echoes back differs from what the upstream sent.
  The provider rejects a `thinking` block whose text no longer matches its
  signature, and the fake thinking includes an email the model wrote itself;
- `system` or `tools` changes between turns (a prompt-cache miss every turn).

The temp dir is kept after the run: `upstream.jsonl` shows exactly what reached
the provider. The script needs `claude` on PATH, so it is not part of CI.

## Hypothesis

Hypothesis generates thousands of cases; failures are automatically shrunk
to minimal counterexamples. See `tests/property/` for examples.
