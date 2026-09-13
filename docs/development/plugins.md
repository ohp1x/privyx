# Plugins

## What Can Be Plugged In

| Extension point | Protocol | Where |
|---|---|---|
| Detector | `Detector.detect()` | `privacy/detector/` |
| Policy | `Policy.decide()` | `privacy/policy/` |
| Operator | `Operator.pseudonymize()/deanonymize()` | `privacy/operator/` |
| Anchor | `Anchor.anchor()/deanchor()` | `privacy/anchor/` |
| Vault | `Vault.get()/put()/delete()` | `vault/` |
| Provider | `Provider.send()/stream()` | `providers/` |
| Stream adapter | `extract_delta()/wrap_delta()` | `streaming/adapters/` |

## Writing a Detector

```python
from privyx.core.context import Context
from privyx.core.result import Detection
from privyx.privacy.detector.base import BaseDetector

class LicensePlateDetector(BaseDetector):
    name = "license_plate"

    def detect_sync(self, text: str, context: Context) -> Detection:
        d = Detection()
        # ... find spans, d.add(start, end, "LICENSE_PLATE", text[start:end])
        return d
```

## Writing an Operator

```python
from privyx.core.session import Session
from privyx.core.result import TransformResult
from privyx.privacy.operator.base import BaseOperator

class MyOperator(BaseOperator):
    name = "myop"
    async def pseudonymize(self, text, detection, session, context):
        ...
    async def deanonymize(self, text, session, context):
        ...
```

## Loading Plugins

`plugins/loader.py` discovers plugins from configured paths and registers
them. Third-party packages can expose entry points under `privyx.plugins`.
