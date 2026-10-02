# Examples

Scripts that use the Privyx engine from Python, without the proxy. Each one
runs as it is (`python examples/basic.py`), and the test suite runs them all.

| Script | Shows |
|---|---|
| [`from_config.py`](from_config.py) | Building the engine from the same configuration the CLI reads, then masking and restoring a text. Start here. |
| [`basic.py`](basic.py) | Assembling the engine by hand from a detector, a policy, an operator, and a vault. |
| [`stream_restore_openai.py`](stream_restore_openai.py) | Restoring a simulated OpenAI-style stream whose token is split between two chunks. |
| [`stream_restore_anthropic.py`](stream_restore_anthropic.py) | The same for Anthropic-style stream events. |
| [`fastapi_gateway.py`](fastapi_gateway.py) | Serving the gateway from your own program. |

None of them calls a provider. For client code that talks to OpenAI or
Anthropic through a running proxy, see the
[documentation](https://ohp1x.github.io/privyx/tutorials/sdk-app/).

Example configuration files are in [`../configs/`](../configs/), and example
plugins in [`../plugins/`](../plugins/).
