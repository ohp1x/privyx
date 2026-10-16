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

## Detection corpus

`tests/unit/privacy/corpus/` holds text an agent typically reads: a `.env`, a
compose file, Python and TSX code, `git log`, server logs, a README, a shell
session, Kubernetes rollout manifests, Terraform plans, and support incidents. Every value that should be masked is labeled in place as
`⟦TYPE:value⟧`, and every value is fake. `test_detection_eval.py` scans the
files with both the installed defaults and the detector from
`configs/default.yaml`, and scores each entity
type: a detection counts only if it masks exactly a labeled value with the
labeled type. The scores are printed at the end of the test run, and the test
fails when a type's precision or recall drops below its floor in `FLOORS`.

When a change improves a type, raise its floor to the new score. Put a new case
in the file where such text really appears, and split a key-shaped fake with
`·` (`ghp_·…`): it is dropped when the corpus is read, so secret scanners leave
the repository alone.

The corpus also covers typed Python and TypeScript assignments, raw and bytes
string literals, multiline triple-quoted secrets, quoted `.env` values, and
CLI credentials with or without `=`. Negative samples keep type declarations,
environment references, timestamps, and a following CLI flag readable.

## Coding-agent payloads in CI

`tests/integration/test_coding_agent_privacy.py` sends synthetic Claude Code,
Codex, and OpenAI Chat payloads through the transparent ASGI app into a local
mock upstream. It needs neither an agent CLI nor a provider account, and runs
with the regular test suite.

Each content surface has a distinct canary: instructions, prompt, tool and
parameter descriptions, echoed tool arguments, source and log text in tool
results, and machine metadata (including Codex's header). The upstream checks
that all ten values were masked, then echoes their tokens as text and tool
arguments. The client must receive every original value, with no tokens left.
Streaming replies split tokens and JSON arguments across SSE events and split
the encoded events across byte chunks.

The matrix covers Messages, Responses, and Chat Completions, batch and streaming
replies, and ephemeral and conversation sessions. Each case sends two requests
to exercise cached detection and session reuse, checks stable instructions and
tool descriptions, and verifies that the audit trail holds no canary values.
These are synthetic protocol checks; the real-agent harness below also tests
the installed CLI's behavior.

## Running

```bash
uv run pytest                     # all
uv run pytest tests/unit          # unit
uv run pytest tests/property -k stream
uv run pytest -n auto             # parallel
```

## End-to-end: a real agent (`make e2e`)

`scripts/test-e2e/run.py` runs the real `claude` CLI through `privyx run` (or, with
`--transparent`, through `privyx proxy` on its defaults: ephemeral sessions, no
anchor) against a local fake Anthropic API. `--agent codex` runs `codex exec` against a
fake Responses API: MCP lookup, a shell read of `notes.txt`, then an answer
echoing the masked values. SSE deltas split tokens and arguments across frames.
Neither scripted run needs a provider key or account. `make e2e` runs both agents
in both modes. HOME,
`CLAUDE_CONFIG_DIR`, and `XDG_CONFIG_HOME` all point into a fresh temp dir, so your
own `~/.claude` and `~/.privyx` are left alone.

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
the provider. Install `claude` and `codex` on PATH before running `make e2e`.
The separate `Agent CLI compatibility` workflow installs the verified baseline
(Claude Code `2.1.301`, Codex `0.114.0`) and runs all four combinations only
on manual dispatch. Dispatch inputs can select newer versions, including
`latest`, before updating the baseline. It reports CLI versions and retains
synthetic request, audit, and CLI logs for seven days,
including failed runs. Codex `0.163.0` was also tried locally but did not
advertise the MCP tool in the expected format; its compatibility remains unverified.
Ordinary CI continues to run the synthetic protocol tests
without installing either CLI. A nonzero CLI exit always fails the harness.

### A real provider (`--upstream`)

A scripted upstream cannot show what a model does with a token. `--upstream ORIGIN`
keeps the recorder in place and relays each recorded request to a real provider:

```bash
ANTHROPIC_API_KEY=... uv run python scripts/test-e2e/run.py --upstream https://api.anthropic.com
CODEX_API_KEY=... uv run python scripts/test-e2e/run.py --agent codex --upstream https://api.openai.com
```

`ORIGIN` is the provider's address without `/v1`. The credential comes from the
environment (`ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`; `CODEX_API_KEY` for
codex) and is left out of the kept files. `--model` picks the model, and
`--transparent` works as above. These runs call the provider and are billed.

The model writes the loop itself, so the prompt asks it to call the MCP tool, read
the file, and answer on one `Echo:` line. The leak, restore, and `system`/`tools`
checks stay; the echoed-turn check gives way to one that the provider accepted
every turn, and `upstream.jsonl` also holds each reply. A token the model wrote
without its `<` `>`, or as its id alone, is a missed restore; a model that skips the MCP tool or
declines to give the `Echo:` line is reported as a note, not a failure. Beyond its read-only
tools, Claude Code may use only the MCP tool and `cat notes.txt`; codex runs in
its read-only sandbox.

`--agent codex` drives `codex exec` in its read-only sandbox with `CODEX_HOME`
in the temp dir. It works with the scripted upstream by default and with a real
provider via `--upstream`. `system`/`tools` stability is not checked for it, since
some models send those definitions as input items.

Each agent is one file in `scripts/test-e2e/` that exports an `Agent`
(`common.py`): how to configure it, launch it, and point it at a proxy, plus,
optionally, a fake upstream. To add an agent, write its file and list it in
`AGENTS` in `run.py`.

## Hypothesis

Hypothesis generates thousands of cases; failures are automatically shrunk
to minimal counterexamples. See `tests/property/` for examples.
