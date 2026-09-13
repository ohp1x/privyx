"""Gateway — optional FastAPI server wrapping the privacy engine.

The gateway provides an HTTP interface for clients that do not want to point
their provider base URL at the proxy.  Core has no dependency on this module.
"""