"""Reproducible cache-miss/hit request benchmarks with a local mock upstream.

Run ``uv run python scripts/benchmark_requests.py --help`` for options. Timings
are local proxy costs, without a listening server, provider, or network latency.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import hashlib
import io
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import httpx
from benchmark import _claude_code_body

from privyx.config.defaults import DEFAULTS
from privyx.config.schema import Settings
from privyx.core.builder import build_engine
from privyx.core.context import Context
from privyx.core.engine import PrivacyEngine
from privyx.core.result import Detection
from privyx.core.session import Session
from privyx.observability.audit import AuditLogger
from privyx.privacy.detector.base import BaseDetector
from privyx.privacy.detector.builtin import DEFAULT_PATTERNS, RegexDetector
from privyx.privacy.detector.cache import CachedDetector
from privyx.privacy.operator.pseudonym import PseudonymOperator
from privyx.privacy.policy.default import DefaultPolicy
from privyx.proxy.schemas import transform_request
from privyx.proxy.transparent import TransparentProxy
from privyx.vault.memory import MemoryVault

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/unit/privacy/corpus"
LABEL = re.compile(r"⟦[A-Z_]+:(.*?)⟧", re.DOTALL)


def coding_agent_body(kb: int) -> bytes:
    """Claude-style tool turns from the synthetic detection corpus, about kb KiB."""
    sources = [
        LABEL.sub(lambda m: m[1].replace("·", ""), p.read_text(encoding="utf-8"))
        for p in sorted(CORPUS.glob("*.txt"))
    ]
    if not sources:
        raise RuntimeError("synthetic detection corpus is missing")
    messages: list[dict[str, Any]] = []
    size = turn = 0
    while size < kb * 1024:
        source = f"tool result {turn}\n{sources[turn % len(sources)]}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"call_{turn}",
                            "name": "shell",
                            "input": {"command": f"cat fixture-{turn}.txt"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": f"call_{turn}", "content": source}
                    ],
                },
            ]
        )
        size += len(source.encode())
        turn += 1
    return json.dumps(
        {
            "model": "benchmark",
            "max_tokens": 100,
            "system": [{"type": "text", "text": "Contact benchmark@example.test"}],
            "tools": [
                {
                    "name": "shell",
                    "description": "Read synthetic source files for benchmark@example.test",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string", "description": "Command"}},
                    },
                }
            ],
            "messages": messages,
            "safeguards": {"cwd": "/home/benchmark@example.test/work"},
        }
    ).encode()


def summary(samples: list[float]) -> dict[str, Any]:
    """Milliseconds; p95 uses nearest rank, with raw samples retained."""
    return {
        "p50": statistics.median(samples),
        "p95": sorted(samples)[math.ceil(0.95 * len(samples)) - 1],
        "samples": samples,
    }


async def request_case(
    body: bytes, *, cached: bool, iterations: int, profile: Path | None = None
) -> dict[str, Any]:
    buf = io.StringIO()
    audit = AuditLogger(buf)
    engine, close = await build_engine(Settings.model_validate(DEFAULTS), audit=audit)
    detector = engine.detector
    assert isinstance(detector, CachedDetector)
    original = json.loads(body)
    has_system = "system" in original

    def echo(payload: dict[str, Any]) -> str:
        return payload["system"][0]["text"] if has_system else payload["messages"][0]["content"]

    expected = echo(original)
    email = "benchmark@example.test" if has_system else "user0@example.com"
    forwarded = b""

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal forwarded
        forwarded = request.content
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [{"type": "text", "text": echo(json.loads(forwarded))}],
            },
        )

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            proxy = TransparentProxy(
                engine, origin="http://upstream.test", client=client, audit=audit
            )

            async def send() -> float:
                start = time.perf_counter()
                response = await proxy.handle(
                    method="POST",
                    path="v1/messages",
                    headers={"content-type": "application/json", "x-api-key": "synthetic"},
                    body=body,
                )
                elapsed = (time.perf_counter() - start) * 1000
                # Check outside the timer; proxy batch restore is inside it.
                assert response.status_code == 200
                assert email.encode() not in forwarded, "unmasked email reached the mock upstream"
                assert json.loads(response.body)["content"][0]["text"] == expected
                return elapsed

            # Compile regexes and warm the code path; cache-hit measurements
            # use exactly this body. Cache misses clear the LRU before each send.
            await send()
            times, transforms, hits, misses = [], [], [], []
            for _ in range(iterations):
                if not cached:
                    detector.clear()
                buf.seek(0)
                buf.truncate()
                before_hits, before_misses = detector.hits, detector.misses
                times.append(await send())
                records = [json.loads(line) for line in buf.getvalue().splitlines()]
                requests = [r for r in records if r["event"] == "proxy.request"]
                assert len(requests) == 1
                transforms.append(requests[0]["transform_ms"])
                hits.append(detector.hits - before_hits)
                misses.append(detector.misses - before_misses)
                assert misses[-1] == 0 if cached else misses[-1] > 0
            if profile is not None:
                # An extra cache-miss request: profiler overhead never enters
                # the reported timing samples.
                detector.clear()
                profiler = cProfile.Profile()
                profiler.enable()
                await send()
                profiler.disable()
                profiler.dump_stats(profile)
            return {
                "request_ms": summary(times),
                "transform_ms": summary(transforms),
                "cache_hits": hits,
                "cache_misses": misses,
            }
    finally:
        await close()


async def pattern_costs(body: bytes, iterations: int) -> dict[str, Any]:
    """Per-entity detection on the same unique leaves the request walker scans.

    Includes built-in validation and span creation. These isolated timings are
    diagnostic; their sum is not an end-to-end latency estimate.
    """
    texts: dict[str, None] = {}

    class Recorder(BaseDetector):
        def detect_sync(self, text: str, context: Context) -> Detection:
            texts[text] = None
            return Detection()

    engine = PrivacyEngine(
        detector=Recorder(),
        policy=DefaultPolicy(),
        operator=PseudonymOperator(),
        vault=MemoryVault(),
    )
    session = Session()
    await transform_request(json.loads(body), engine, session.session_id, session=session)
    result = {}
    for entity, pattern in DEFAULT_PATTERNS.items():
        detector = RegexDetector({entity: pattern})
        context = Context()
        samples = []
        count = 0
        for _ in range(iterations):
            start = time.perf_counter()
            count = sum(len(detector.detect_sync(t, context).spans) for t in texts)
            samples.append((time.perf_counter() - start) * 1000)
        result[entity] = {**summary(samples), "spans": count}
    return {"unique_leaves": len(texts), "entities": result}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def metadata(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "git_revision": git("rev-parse", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "tracked_diff_sha256": digest(git("diff", "HEAD").encode()),
        "harness_sha256": {
            name: digest((ROOT / "scripts" / name).read_bytes())
            for name in ("benchmark.py", "benchmark_requests.py")
        },
        "lock_sha256": digest((ROOT / "uv.lock").read_bytes()),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "dependencies": {name: version(name) for name in ("privyx", "httpx", "pydantic")},
        "settings": Settings.model_validate(DEFAULTS).model_dump(mode="json"),
        "parameters": {"iterations": args.iterations, "target_kib": args.size_kb},
        "measurement": {
            "boundary": "TransparentProxy.handle, batch reply, echo restore, mock upstream",
            "cache": "LRU cleared before each cold sample; exact body prewarmed for cached samples",
            "percentile": "nearest rank p95; median p50; times in ms",
            "warmup_requests_per_case": 1,
            "concurrency": 1,
        },
        "workloads": {},
    }


async def main(args: argparse.Namespace) -> None:
    workloads = {
        "conversation": _claude_code_body(args.size_kb, "benchmark"),
        "coding-agent": coding_agent_body(args.size_kb),
    }
    report = metadata(args)
    if args.profile_dir:
        args.profile_dir.mkdir(parents=True, exist_ok=True)
    for name, body in workloads.items():
        profile = args.profile_dir / f"{name}.prof" if args.profile_dir else None
        entry: dict[str, Any] = {"body_bytes": len(body), "body_sha256": digest(body)}
        for cached in (False, True):
            mode = "cached" if cached else "cold"
            entry[mode] = await request_case(
                body,
                cached=cached,
                iterations=args.iterations,
                profile=profile if not cached else None,
            )
            timing = entry[mode]["request_ms"]
            transform = entry[mode]["transform_ms"]["p50"]
            print(
                f"{name:13} {len(body):7} bytes {mode:6} "
                f"p50 {timing['p50']:7.2f} ms p95 {timing['p95']:7.2f} ms "
                f"(transform p50 {transform:.2f} ms)",
                flush=True,
            )
        entry["patterns"] = await pattern_costs(body, args.iterations)
        ranked = sorted(
            entry["patterns"]["entities"].items(), key=lambda p: p[1]["p50"], reverse=True
        )
        print(
            "  patterns: " + ", ".join(f"{k} {v['p50']:.2f} ms" for k, v in ranked[:3]), flush=True
        )
        report["workloads"][name] = entry
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Saved {args.output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--size-kb", type=int, default=400, help="approximate content size in KiB")
    parser.add_argument("--output", type=Path, help="save samples and environment metadata as JSON")
    parser.add_argument(
        "--profile-dir", type=Path, help="save an extra cold-request cProfile per workload"
    )
    args = parser.parse_args()
    if args.iterations < 1 or args.size_kb < 1:
        parser.error("--iterations and --size-kb must be positive")
    asyncio.run(main(args))
