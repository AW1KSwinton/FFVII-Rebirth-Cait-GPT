from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from ff7r_agent.live_route_adapter import (
    DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
    IndependentJsonValidator,
    TopologySession,
    _path_stat_identity_for_validator,
)
from ff7r_agent.topology_cache import resolve_manifest_artifact
from ff7r_agent.validator_prewarm import build_prewarmed_validator_class


def run(manifest_path: Path) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = resolve_manifest_artifact(manifest_path, manifest).resolve()
    raw_sha = str(manifest["raw_artifact_sha256"]).upper()
    static_fingerprint = str(manifest["static_topology_fingerprint"]).upper()
    world_id = str(manifest.get("world_id") or "")
    map_id = str(manifest.get("map_id") or "")
    cache_identity = {
        "artifact_path": _path_stat_identity_for_validator(artifact),
        "raw_artifact_sha256": raw_sha,
        "static_topology_fingerprint": static_fingerprint,
        "world_id": world_id,
        "map_id": map_id,
    }
    session = TopologySession(
        graph=object(),
        artifact_path=str(artifact),
        fingerprint=static_fingerprint,
        static_fingerprint=static_fingerprint,
        artifact_sha256=raw_sha,
        session_id="benchmark-session",
        world_id=world_id,
        map_id=map_id,
        captured_at_utc=None,
        current_session=True,
        movement_sent=False,
        payload_summary={"cache_manifest_identity": cache_identity},
    )

    cold = IndependentJsonValidator(None, None, require_topology_binding=True)
    cold.bind_topology_session(session)
    started = time.perf_counter()
    cold._load()
    cold_ms = (time.perf_counter() - started) * 1000.0

    warm_type = build_prewarmed_validator_class()
    warm = warm_type(None, None, require_topology_binding=True)
    prewarm_started = time.perf_counter()
    prewarm = warm.prewarm_from_manifest(manifest_path)
    prewarm_ms = (time.perf_counter() - prewarm_started) * 1000.0
    bind_started = time.perf_counter()
    binding = warm.bind_topology_session(session)
    warm_bind_ms = (time.perf_counter() - bind_started) * 1000.0
    load_started = time.perf_counter()
    warm._load()
    warm_load_ms = (time.perf_counter() - load_started) * 1000.0

    return {
        "observational_only": True,
        "timing_assertions": False,
        "artifact_path": str(artifact),
        "raw_artifact_sha256": raw_sha,
        "static_topology_fingerprint": static_fingerprint,
        "cold_route_time_construction_ms": cold_ms,
        "background_prewarm_ms": prewarm_ms,
        "warm_route_bind_ms": warm_bind_ms,
        "warm_route_load_ms": warm_load_ms,
        "validator_construction_reused": bool(
            binding.get("validator_construction_reused")
        ),
        "prewarm_state": prewarm.get("state"),
        "interpretation": (
            "Prewarm moves construction off the route-critical path; total CPU construction "
            "work is not claimed reduced by this benchmark."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.manifest.resolve())
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
