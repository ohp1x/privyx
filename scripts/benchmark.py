"""Benchmark script for the privacy engine and streaming deanonymizer."""

from __future__ import annotations

import asyncio
import statistics
import time

from privyx.core.engine import PrivacyEngine
from privyx.privacy.detector.builtin import RegexDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
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


if __name__ == "__main__":
    asyncio.run(main())