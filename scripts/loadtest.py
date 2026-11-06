#!/usr/bin/env python3
"""Ingestion load test for a running Traceloom server.

Sends synthetic traces (one root span, generations and tool spans per trace) to
POST /api/public/ingestion in batches and reports events/s and request latency.

Target volumes
  lite  (SQLite):          10,000 traces / 50,000 observations, about 2,000 events/s
  full  (ClickHouse etc.): 1,000 events/s sustained on one machine (ARCHITECTURE.md);
                           --traces 200000 --observations-per-trace 50 is the 10M-observation run

Example
  TRACELOOM_PUBLIC_KEY=tl-pk-... TRACELOOM_SECRET_KEY=tl-sk-... \\
  python scripts/loadtest.py --host http://127.0.0.1:8000 --traces 10000
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta


def iso(when: datetime) -> str:
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def trace_events(index: int, observations: int, now: datetime) -> list[dict]:
    """Events for one trace: trace-create, a root span and `observations - 1` children."""
    trace_id = uuid.uuid4().hex
    start = now - timedelta(seconds=index % 3600)
    events = [
        {
            "id": uuid.uuid4().hex,
            "type": "trace-create",
            "timestamp": iso(start),
            "body": {
                "id": trace_id,
                "name": f"loadtest-{index % 20}",
                "userId": f"user-{index % 50}",
                "input": {"q": f"question {index}"},
                "output": f"answer {index}",
                "tags": ["loadtest"],
            },
        }  # fmt: skip
    ]
    root = uuid.uuid4().hex
    for n in range(observations):
        generation = n % 2 == 1
        body = {
            "id": root if n == 0 else uuid.uuid4().hex,
            "traceId": trace_id,
            "name": "root" if n == 0 else ("llm" if generation else "tool"),
            "type": "span" if n == 0 else ("generation" if generation else "tool"),
            "startTime": iso(start + timedelta(milliseconds=10 * n)),
            "endTime": iso(start + timedelta(milliseconds=10 * n + 8)),
            "input": {"n": n},
            "output": {"n": n},
        }
        if n:
            body["parentObservationId"] = root
        if generation:
            body.update(model="claude-sonnet-5-5", usageDetails={"input": 120, "output": 40})
        kind = (
            "span-create"
            if n == 0
            else ("generation-create" if generation else "observation-create")
        )
        events.append({"id": uuid.uuid4().hex, "type": kind, "timestamp": iso(start), "body": body})
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingestion load test for Traceloom.",
        epilog=__doc__.split("Target volumes", 1)[1].split("Example", 1)[0].rstrip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=os.environ.get("TRACELOOM_HOST", "http://127.0.0.1:8000"))
    parser.add_argument("--public-key", default=os.environ.get("TRACELOOM_PUBLIC_KEY"))
    parser.add_argument("--secret-key", default=os.environ.get("TRACELOOM_SECRET_KEY"))
    parser.add_argument("--traces", type=int, default=10_000, help="traces to send (default 10000)")
    parser.add_argument(
        "--observations-per-trace", type=int, default=5, help="default 5 (50k total)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=50, help="events per request (default 50)"
    )
    parser.add_argument("--concurrency", type=int, default=4, help="parallel requests (default 4)")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if not (args.public_key and args.secret_key):
        parser.error("set --public-key/--secret-key or TRACELOOM_PUBLIC_KEY/TRACELOOM_SECRET_KEY")

    import httpx

    now = datetime.now(UTC)
    events: list[dict] = []
    for i in range(args.traces):
        events.extend(trace_events(i, max(1, args.observations_per_trace), now))
    batches = [events[i : i + args.batch_size] for i in range(0, len(events), args.batch_size)]
    latencies: list[float] = []
    failures = 0
    lock = threading.Lock()
    client = httpx.Client(
        base_url=args.host.rstrip("/"),
        auth=(args.public_key, args.secret_key),
        timeout=args.timeout,
    )

    def send(batch: list[dict]) -> None:
        nonlocal failures
        started = time.perf_counter()
        try:
            ok = client.post("/api/public/ingestion", json={"batch": batch}).status_code in (
                200,
                207,
            )
        except httpx.HTTPError:
            ok = False
        with lock:
            latencies.append(time.perf_counter() - started)
            failures += 0 if ok else 1

    print(f"sending {len(events)} events in {len(batches)} requests to {args.host}")
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(send, batches))
    elapsed = time.perf_counter() - started
    ordered = sorted(latencies)
    p95 = ordered[int(len(ordered) * 0.95) - 1] if ordered else 0.0
    print(f"events/s:      {len(events) / elapsed:,.0f}")
    print(f"elapsed:       {elapsed:.1f}s")
    print(
        f"request p50:   {statistics.median(ordered) * 1000:.0f} ms" if ordered else "no requests"
    )
    print(f"request p95:   {p95 * 1000:.0f} ms")
    print(f"failed:        {failures} of {len(batches)} requests")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
