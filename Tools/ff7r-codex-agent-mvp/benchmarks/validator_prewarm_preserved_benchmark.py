from __future__ import annotations

import json
from pathlib import Path
import tempfile

from ff7r_agent.live_route_adapter import IndependentJsonValidator
from ff7r_agent.topology_cache import (
    sha256_file,
    static_topology_fingerprint,
    validator_identity,
)
from validator_prewarm_benchmark import run


REAL_SNAPSHOT = (
    Path(__file__).parents[3]
    / "ProjectKnowledge"
    / "Evidence"
    / "2026-08-17-shipping-detour-topology-live"
    / "navigation-native-geometry.json"
)


def main() -> int:
    if not REAL_SNAPSHOT.is_file():
        raise SystemExit(f"preserved shipping topology is unavailable: {REAL_SNAPSHOT}")
    payload = json.loads(REAL_SNAPSHOT.read_text(encoding="utf-8"))
    raw_sha = sha256_file(REAL_SNAPSHOT)
    static_fingerprint = static_topology_fingerprint(payload)
    validation_path = IndependentJsonValidator._validation_source_path().resolve()
    source_sha = validator_identity(validation_path)

    with tempfile.TemporaryDirectory(prefix="ff7r-validator-prewarm-") as directory:
        manifest_path = Path(directory) / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "source_artifact_path": str(REAL_SNAPSHOT.resolve()),
                    "raw_artifact_sha256": raw_sha,
                    "static_topology_fingerprint": static_fingerprint,
                    "validator_source_sha256": source_sha,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        result = run(manifest_path)

    # Timing remains observational.  These are semantic acceptance checks only:
    # the exact current validator must prewarm and the later topology bind/load
    # must reuse that object under the matching identity key.
    if result.get("prewarm_state") != "ready":
        raise AssertionError(f"prewarm did not become ready: {result}")
    if result.get("validator_construction_reused") is not True:
        raise AssertionError(f"matching bind did not reuse validator: {result}")
    result["preserved_shipping_snapshot"] = str(REAL_SNAPSHOT)
    result["timing_assertions"] = False
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
