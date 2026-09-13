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

## Session Lifecycle

1. `get_or_create_session(session_id)` → looks up the vault; creates + persists if missing.
2. `transform(text, session)` → detect → decide → pseudonymize → `vault.save`.
3. `restore(text, session_id)` → load session → deanonymize.

## Providers are Transport

The provider is just a transport. `PrivacyEngine` never knows about HTTP,
SSE, or provider SDKs. This is what allows Privyx to grow from "LLM privacy
proxy" to "AI data privacy gateway" without rearchitecting the core.
