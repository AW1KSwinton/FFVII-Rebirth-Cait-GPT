#!/usr/bin/env python3
"""Fresh-process static topology profiling, without admitting a live cache.

Measures the raw CPU/IO stages independently. Manifest/provenance/capture proof
remain the production provider's responsibility and are not simulated here.
OS filesystem caches are not flushed; each round starts a new Python process.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

from ff7r_agent.detour_route import DetourTopologyGraph
from ff7r_agent.topology_cache import static_topology_fingerprint
from navigation_perf import build_fingerprint_payload, summarize_ms


def working_set_bytes():
    if sys.platform != "win32":
        return None
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return counters.WorkingSetSize
    return None


def sample(artifact: Path):
    phases = {}
    memory_before = working_set_bytes()
    mark = time.perf_counter()
    def lap(name):
        nonlocal mark
        current = time.perf_counter()
        phases[name] = (current - mark) * 1000.0
        mark = current
    raw = artifact.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    lap("raw_read_sha256")
    payload = json.loads(raw)
    lap("json_decode")
    fingerprint = static_topology_fingerprint(payload)
    lap("static_semantic_fingerprint")
    graph = DetourTopologyGraph.from_dict(payload)
    lap("graph_and_spatial_index")
    point = graph.polygons[min(graph.polygons)].centroid
    resolution = graph.resolve_point(point, max_snap_distance_m=.75, max_vertical_separation_m=1.0)
    if not resolution.ok:
        raise AssertionError("first polygon centroid did not resolve")
    lap("first_point_resolution")
    memory_after = working_set_bytes()
    return {"phases_ms": phases, "raw_sha256": digest, "fingerprint": fingerprint,
            "polygons": len(graph.polygons), "index_cells": len(graph._point_resolution_index),
            "artifact_bytes": len(raw), "memory_delta_bytes": None if memory_after is None or memory_before is None
            else memory_after - memory_before, "input_sent": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--synthetic-output", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(sample(args.artifact)))
        return
    if args.rounds < 3 or not args.output:
        parser.error("provide --output and at least three rounds")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    artifact = args.artifact
    if args.synthetic_output:
        artifact = args.synthetic_output
        artifact.parent.mkdir(parents=True, exist_ok=True)
        with artifact.open("x", encoding="utf-8") as handle:
            json.dump(build_fingerprint_payload(200, 150, 100.0), handle, separators=(",", ":"))
    if not artifact:
        parser.error("provide --artifact or --synthetic-output")
    samples = [json.loads(subprocess.check_output([sys.executable, str(Path(__file__).resolve()), "--worker",
               "--artifact", str(artifact.resolve())], text=True)) for _ in range(args.rounds)]
    if any((item["raw_sha256"], item["fingerprint"], item["polygons"]) !=
           (samples[0]["raw_sha256"], samples[0]["fingerprint"], samples[0]["polygons"]) for item in samples):
        raise AssertionError("artifact identity changed across workers")
    result = {"schema_version": 1, "python": sys.version, "platform": platform.platform(),
              "artifact": str(artifact.resolve()), "rounds": args.rounds, "fresh_processes": True,
              "os_cache_flushed": False, "live_cache_admitted": False, "timing_assertions": False,
              "summary_ms": {name: summarize_ms([sample["phases_ms"][name] for sample in samples])
                             for name in samples[0]["phases_ms"]}, "samples": samples}
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("rounds", "summary_ms")}, indent=2))


if __name__ == "__main__":
    main()
