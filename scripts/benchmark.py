"""Benchmarks for the request path: detection, masking, restore, and the vault.

    make bench        # or: uv run python scripts/benchmark.py

Absolute numbers depend on the machine; compare runs on the same one.  Timing is
too noisy for CI, so the regression guards live in the tests (adversarial regex
input, many spans, dropped streams) and this script reports the numbers.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import statistics
import tempfile
import time
from typing import Any

import httpx

from privyx.config.loader import load_config
from privyx.core.builder import build_engine
from privyx.core.context import Context
from privyx.core.engine import PrivacyEngine
from privyx.core.session import Session
from privyx.observability.audit import AuditLogger
from privyx.privacy.detector.builtin import DEFAULT_PATTERNS, RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.transparent import TransparentProxy
from privyx.streaming.deanonymizer import StreamingDeanonymizer
from privyx.vault.memory import MemoryVault

SAMPLE = (
    "Contact Alice at alice@example.com or +1 (555) 123-4567. "
    "Her SSN is 123-45-6789 and IP is 192.168.0.1. " * 50
)


async def bench_transform(engine: PrivacyEngine, n: int) -> float:
    session = await engine.get_or_create_session()
    start = time.perf_counter()
    for _ in range(n):
        await engine.transform(SAMPLE, session=session)
    return (time.perf_counter() - start) / n


def bench_stream(n: int) -> float:
    mapping = {"<PRIVYX_EMAIL_1>": "alice@example.com", "<PRIVYX_PHONE_2>": "+1 (555) 123-4567"}
    payload = "Email <PRIVYX_EMAIL_1> phone <PRIVYX_PHONE_2> " * 20
    start = time.perf_counter()
    for _ in range(n):
        d = StreamingDeanonymizer(mapping)
        for i in range(0, len(payload), 7):
            d.feed(payload[i : i + 7])
        d.flush()
    return (time.perf_counter() - start) / n


def bench_patterns(size: int = 80_000) -> tuple[float, str]:
    """The slowest built-in pattern on near-miss input (a quadratic one takes seconds)."""
    inputs = {
        "letters": "a" * size,
        "hex": ("0123456789abcdef" * size)[:size],
        "dotted": ("abc." * size)[:size],
        "a@bbb…": "a@" + "b" * size,
        "bbb…@ccc…": (("b" * 64 + "@" + "c" * 255) * (size // 320 + 1))[:size],
        "-----": "-" * size,
    }
    worst = (0.0, "")
    for entity, pattern in DEFAULT_PATTERNS.items():
        compiled = re.compile(pattern)
        for shape, text in inputs.items():
            start = time.perf_counter()
            for _ in compiled.finditer(text):
                pass
            worst = max(worst, (time.perf_counter() - start, f"{entity} on {shape}"))
    return worst


async def bench_spans(count: int = 16_000) -> tuple[float, float]:
    """pseudonymize and restore one text with ``count`` spans (~55 characters apart)."""
    text = "".join(f"row {i}: user{i}@example.com, note text here padding\n" for i in range(count))
    detection = await RegexDetector().detect(text, Context())
    operator = PseudonymOperator()
    masks, restores = [], []
    for _ in range(5):
        session = Session()
        start = time.perf_counter()
        masked = await operator.pseudonymize(text, detection, session, Context())
        middle = time.perf_counter()
        await operator.deanonymize(masked.text, session, Context())
        masks.append(middle - start)
        restores.append(time.perf_counter() - middle)
    return statistics.median(masks), statistics.median(restores)


def bench_stream_throughput() -> float:
    """Streaming restore in MB/s: 90 KB of prose with a token every ~450 characters."""
    mapping = {"<PRIVYX_EMAIL_1>": "alice@example.com"}
    body = ("The quick brown fox jumps over the lazy dog. " * 10 + "<PRIVYX_EMAIL_1> ") * 200
    runs = []
    for _ in range(3):
        d = StreamingDeanonymizer(mapping)
        start = time.perf_counter()
        for i in range(0, len(body), 64):
            d.feed(body[i : i + 64])
        d.flush()
        runs.append(time.perf_counter() - start)
    return len(body) / statistics.median(runs) / 1e6


def _claude_code_body(kb: int, tag: str) -> bytes:
    """An Anthropic Messages request of about ``kb`` KB: turns of prose, one email each."""
    filler = "lorem ipsum dolor sit amet " * 40 + "\n"
    messages: list[dict[str, Any]] = []
    for turn in range(kb * 1024 // (2 * len(filler)) + 1):
        text = f"{tag}.{turn} {filler}"
        messages.append({"role": "user", "content": f"user{turn}@example.com {text}"})
        messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
    return json.dumps({"model": "m", "max_tokens": 10, "messages": messages}).encode()


async def bench_request(kb: int, new_content: bool, n: int = 10) -> tuple[float, float]:
    """One request through the transparent proxy on the default settings.

    Returns the median time per request and the median ``transform_ms`` the
    audit trail records for it.  The upstream is a mock that answers at once, so
    the time is all Privyx's.  ``new_content`` gives every request new text, as
    the first turn of a conversation does; otherwise the detection cache hits.
    """
    buf = io.StringIO()
    audit = AuditLogger(buf)
    engine, close = await build_engine(load_config(None), audit=audit)
    reply = {"type": "message", "content": [{"type": "text", "text": "ok"}]}
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply))
    )
    proxy = TransparentProxy(engine, origin="http://upstream.test", client=client, audit=audit)
    headers = {"content-type": "application/json", "x-api-key": "k"}

    async def send(tag: str) -> None:
        body = _claude_code_body(kb, tag)
        await proxy.handle(method="POST", path="v1/messages", headers=headers, body=body)

    await send("warmup")
    buf.seek(0)
    buf.truncate()
    times = []
    for i in range(n):
        start = time.perf_counter()
        await send(str(i) if new_content else "same")
        times.append(time.perf_counter() - start)
    records = [json.loads(line) for line in buf.getvalue().splitlines()]
    transform = [r["transform_ms"] for r in records if r["event"] == "proxy.request"]
    await client.aclose()
    await close()
    return statistics.median(times) * 1000, statistics.median(transform)


async def bench_vault(n: int = 100) -> float | None:
    """Vault I/O of one ephemeral request with SQLite on disk, in ms."""
    try:
        from privyx.vault.sqlite import SQLiteVault
    except ImportError:
        return None
    # On disk in the working directory: a tmpfs /tmp would hide the fsync cost.
    with tempfile.TemporaryDirectory(dir=".") as tmp:
        vault = SQLiteVault(f"{tmp}/bench.db")
        await vault.connect()
        start = time.perf_counter()
        for i in range(n):
            # What one ephemeral request does: create, read and save the
            # mapping, read it back to restore, delete.
            session = Session()
            session.put(f"<PRIVYX_EMAIL_{i}>", f"user{i}@example.com")
            await vault.create(session)
            await vault.save(await vault.get(session.session_id) or session)
            await vault.get(session.session_id)
            await vault.delete(session.session_id)
        elapsed = (time.perf_counter() - start) / n
        await vault.close()
    return elapsed * 1000


async def main() -> None:
    engine = PrivacyEngine(
        detector=RegexDetector(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    # Warmup
    await bench_transform(engine, 5)
    times = [await bench_transform(engine, 20) for _ in range(3)]
    print(f"transform:  median {statistics.median(times) * 1000:.2f} ms/op")

    stream_times = [bench_stream(20) for _ in range(3)]
    print(f"stream:     median {statistics.median(stream_times) * 1000:.2f} ms/op")

    worst, where = bench_patterns()
    print(f"patterns:   slowest built-in on 80 KB near-miss input {worst * 1000:.1f} ms ({where})")
    mask, restore = await bench_spans()
    print(
        f"spans:      16,000 in 0.9 MB: pseudonymize {mask * 1000:.0f} ms, "
        f"restore {restore * 1000:.0f} ms"
    )
    print(f"streaming:  restore {bench_stream_throughput():.1f} MB/s")
    for new_content in (True, False):
        total, transform = await bench_request(400, new_content)
        label = "new content" if new_content else "cached     "
        print(f"request:    400 KB, {label} {total:6.1f} ms (transform_ms {transform:.1f})")
    vault_ms = await bench_vault()
    if vault_ms is None:
        print("vault:      skipped (needs the sqlite extra)")
    else:
        print(f"vault:      sqlite on disk, one ephemeral request {vault_ms:.1f} ms of I/O")


if __name__ == "__main__":
    asyncio.run(main())
