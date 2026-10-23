#!/usr/bin/env python3
"""Measure legacy two-pass topology artifact IO against one-read hash/decode.

This isolates only raw artifact IO + JSON decode. It does not claim to measure
normalization, graph construction, or spatial-index construction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path


def legacy(path: Path) -> tuple[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return digest.hexdigest().upper(), payload


def single_pass(path: Path) -> tuple[str, object]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest().upper()
    payload = json.loads(raw.decode("utf-8"))
    return digest, payload


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def measure(fn, path: Path, rounds: int) -> list[float]:
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn(path)
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples


def synthetic_artifact(path: Path, target_mb: int) -> None:
    row = {"vertices": [float(i) / 10.0 for i in range(96)], "neighbors": list(range(32))}
    rows = max(1, int(target_mb * 1024 * 1024 / len(json.dumps(row))))
    path.write_text(json.dumps({"tiles": [row] * rows}, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--rounds", type=int, default=15)
    parser.add_argument("--synthetic-mb", type=int, default=18)
    args = parser.parse_args()
    if args.rounds < 3:
        parser.error("--rounds must be >= 3")

    temporary = None
    path = args.artifact
    if path is None:
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "topology.json"
        synthetic_artifact(path, args.synthetic_mb)
    path = path.resolve()

    expected = legacy(path)
    actual = single_pass(path)
    if expected != actual:
        raise SystemExit("equivalence failure: digest or decoded payload changed")

    legacy_ms = measure(legacy, path, args.rounds)
    single_ms = measure(single_pass, path, args.rounds)
    result = {
        "artifact": str(path),
        "artifact_bytes": path.stat().st_size,
        "rounds": args.rounds,
        "equivalent": True,
        "legacy": {"median_ms": statistics.median(legacy_ms), "p95_ms": percentile(legacy_ms, 0.95)},
        "single_pass": {"median_ms": statistics.median(single_ms), "p95_ms": percentile(single_ms, 0.95)},
    }
    result["median_reduction_pct"] = 100.0 * (1.0 - result["single_pass"]["median_ms"] / result["legacy"]["median_ms"])
    print(json.dumps(result, indent=2, sort_keys=True))
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
