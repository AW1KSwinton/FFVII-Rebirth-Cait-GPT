#!/usr/bin/env python3
"""Measure terminal async-status serialization with and without route response."""
from __future__ import annotations

import argparse
import json
import statistics
import time

from ff7r_agent.runtime_bridge import ASYNC_ROUTE_WORK_OPERATION, RuntimeSupervisor


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def measure(supervisor: RuntimeSupervisor, job_id: str, include_response: bool, rounds: int):
    elapsed, sizes = [], []
    for _ in range(rounds):
        started = time.perf_counter()
        status = supervisor.call(
            "get_live_route_adapter_status",
            {"job_id": job_id, "include_response": include_response},
        )
        encoded = json.dumps(status, separators=(",", ":")).encode("utf-8")
        elapsed.append((time.perf_counter() - started) * 1000.0)
        sizes.append(len(encoded))
    return elapsed, sizes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload-mb", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=7)
    args = parser.parse_args()
    if args.payload_mb < 1 or args.rounds < 3:
        parser.error("payload must be >=1 MB and rounds >=3")

    payload = "x" * (args.payload_mb * 1024 * 1024)
    supervisor = RuntimeSupervisor()
    job_id = "route-benchmark"
    supervisor._async_request = {
        "job_id": job_id,
        "state": "completed",
        "operation": ASYNC_ROUTE_WORK_OPERATION,
        "started_at": 0.0,
        "worker_generation": 1,
        "worker_pid": 123,
        "response": {"ok": True, "id": 1, "result": {"run": {"state": "ARRIVED", "timeline_blob": payload}}},
    }

    full_ms, full_sizes = measure(supervisor, job_id, True, args.rounds)
    compact_ms, compact_sizes = measure(supervisor, job_id, False, args.rounds)
    result = {
        "payload_mb": args.payload_mb,
        "rounds": args.rounds,
        "full": {
            "median_ms": statistics.median(full_ms),
            "p95_ms": percentile(full_ms, 0.95),
            "bytes": max(full_sizes),
        },
        "compact": {
            "median_ms": statistics.median(compact_ms),
            "p95_ms": percentile(compact_ms, 0.95),
            "bytes": max(compact_sizes),
        },
    }
    result["median_reduction_pct"] = 100.0 * (1.0 - result["compact"]["median_ms"] / result["full"]["median_ms"])
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
