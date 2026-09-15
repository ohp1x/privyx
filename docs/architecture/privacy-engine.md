# Privacy Engine Architecture

## Pipeline

The privacy pipeline is the center of Privyx:

```text
                  Privacy Pipeline
                        │
         ┌──────────────┼──────────────┐
         ▼              ▼              ▼
      Request         Stream        Response
         │              │              │
         ▼              ▼              ▼
      Detect        Transform       Restore
         │              │              │
         ▼              ▼              ▼
   Pseudonymize    Deanonymize     Restore
         │              │              │
         └──────────────┼──────────────┘
                        ▼
                      Vault
```

## Protocols

The engine depends only on protocols, never on concrete classes:

```python
class Detector(Protocol):
    async def detect(self, text: str, context: Context) -> Detection: ...

class Policy(Protocol):
    async def decide(self, detection: Detection, context: Context) -> Detection: ...

class Operator(Protocol):
    async def pseudonymize(...): ...
    async def deanonymize(...): ...

class Vault(Protocol):
    async def create(self, session: Session) -> None: ...
    async def get(self, session_id: str) -> Session | None: ...
    async def save(self, session: Session) -> None: ...
    async def delete(self, session_id: str) -> None: ...
```

## Operators

An operator decides *what* a detected span becomes and how (or whether) it is
restored. All operators share the detect → decide → **operate** → vault pipeline;
they differ in the substitution and its reversibility.

| Operator | Substitution | Reversible | Restored in a stream by |
|----------|--------------|------------|-------------------------|
| `pseudonym` | a codec token (`<PRIVYX_EMAIL_1>`), counter- or anchor-derived | yes (vault) | codec syntax |
| `hash` | a codec token whose id is a content hash | yes (vault) | codec syntax |
| `redact` | a fixed string (`[REDACTED]`) | no | — |
| `faker` | a *realistic* fake of the same kind (fake email, name, …) | yes (vault) | exact substituted values |

### Faker

`faker` keeps the *shape* of the data — a model sees a plausible email or name
rather than a placeholder — which can help it reason. Two properties follow from
a fake value being ordinary text with no delimiter:

- **Literal restore.** Reversal matches the exact substituted strings (a trie of
  this session's values), not the token grammar. Batch and streaming share that
  matching, so `stream == batch` still holds. The proxies select the trie
  recognizer for any operator that declares `stream_restore = "literal"` and the
  codec recognizer otherwise, via `StreamRouter`'s injectable processor factory.
- **Possible false positives.** A fake value that also occurs naturally in a
  response can be restored by coincidence — a risk the token operators (whose
  placeholders are syntactically distinctive) do not have. Prefer `pseudonym`
  when exact, collision-free reversal matters more than realism.

`faker` needs the optional extra: `pip install privyx[faker]` (or
`uv sync --all-extras`). Selecting it without the package fails at startup with a
`ConfigError`. Configure the locale and a determinism seed:

```yaml
operator:
  type: faker
  locale: en_US   # optional; Faker's default when omitted
  seed: 1234      # optional; makes the same value fake identically across runs
```

## Session Lifecycle

1. `get_or_create_session(session_id)` → looks up the vault; creates + persists if missing.
2. `transform(text, session)` → detect → decide → pseudonymize → `vault.save`.
3. `restore(text, session_id)` → load session → deanonymize.

## Providers are Transport

The provider is just a transport. `PrivacyEngine` never knows about HTTP,
SSE, or provider SDKs. This is what allows Privyx to grow from "LLM privacy
proxy" to "AI data privacy gateway" without rearchitecting the core.
