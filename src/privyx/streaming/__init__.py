"""Streaming deanonymization.

The streaming layer guarantees that chunked output, pseudonymized at the
boundary, is deanonymized correctly even when a pseudonym is split across
chunks.  SSE envelopes are separated from text transformation.
"""