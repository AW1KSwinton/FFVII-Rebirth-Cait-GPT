"""Read historical readiness advice without importing it as run progress.

No telemetry, RPC, input, history reconciliation or cache admission takes place
here. The JSON index includes source hashes and detailed evidence for review;
campaign responses expose a compact advisory and a link to that index.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .qb_opponents import DEFAULT_CATALOGUE, load_opponents

DEFAULT_READINESS = DEFAULT_CATALOGUE.with_name("queens-blood-readiness.json")
CLASSIFICATIONS = {"match_recorded", "played_unrecorded", "challenge_locked", "talk_only", "unproven"}


def catalogue_digest(catalogue: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(catalogue, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def load_readiness(path: Path | None = None, *, catalogue: dict[str, Any] | None = None) -> dict[str, Any]:
    """Missing, stale or malformed optional advice never changes campaign state."""
    source = Path(path or DEFAULT_READINESS)
    result: dict[str, Any] = {"available": False, "source": str(source), "campaign_authority": False,
                              "opponents": {}}
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("benchmark") != "regional" or not isinstance(value.get("opponents"), list)):
            raise ValueError("invalid readiness index")
        catalogue = catalogue if catalogue is not None else load_opponents()
        if value.get("catalogue_digest") != catalogue_digest(catalogue):
            return {**result, "reason": "readiness_catalogue_changed"}
        ids = {row["id"] for row in catalogue["opponents"]}
        indexed = {}
        for row in value["opponents"]:
            if (not isinstance(row, dict) or row.get("opponent_id") not in ids
                    or row["opponent_id"] in indexed or row.get("campaign_authority") is not False
                    or row.get("classification") not in CLASSIFICATIONS
                    or not isinstance(row.get("live_reach"), dict)
                    or not isinstance(row.get("availability"), dict)
                    or not isinstance(row.get("offline_approach"), dict)
                    or row["offline_approach"].get("status") not in {"planned", "no_offline_route"}
                    or not isinstance(row["offline_approach"].get("alternative_capture_floor_diagnostics", []), list)
                    or any(not isinstance(item, dict) for item in row["offline_approach"].get(
                        "alternative_capture_floor_diagnostics", []))
                    or not isinstance(row.get("match_events"), list)
                    or any(not isinstance(item, dict) for item in row["match_events"])
                    or not isinstance(row.get("known_quirks"), list)
                    or not isinstance(row.get("unproven_checks"), list)):
                raise ValueError("invalid readiness opponent")
            indexed[row["opponent_id"]] = row
        return {**result, "available": True, "compiled_at_utc": value.get("compiled_at_utc"),
                "opponents": indexed}
    except (OSError, ValueError, TypeError, KeyError):
        return {**result, "reason": "readiness_index_unavailable_or_invalid"}


def readiness_advisory(index: dict[str, Any], opponent_id: str) -> dict[str, Any] | None:
    """Historical reach/result summaries are explicitly separate from run state."""
    identifier = opponent_id.removesuffix("_Younger")
    row = index["opponents"].get(identifier)
    if row is None:
        return None
    approach = row["offline_approach"]
    alternatives = [item for item in approach.get("alternative_capture_floor_diagnostics", []) if item.get("plan")]
    matches = row.get("match_events") or []
    return {"campaign_authority": False, "compiled_at_utc": index["compiled_at_utc"],
            "index_file": index["source"], "classification": row["classification"],
            "live_reach": row["live_reach"], "availability": row["availability"],
            "match_played": row.get("match_played"),
            "historical_results": [{key: event.get(key) for key in (
                "member", "at", "date", "result", "score", "basis", "finish_record_verified", "source")}
                for event in matches],
            "known_quirks": row["known_quirks"], "unproven_checks": row["unproven_checks"],
            "offline_approach": {"status": approach["status"], "reason": approach.get("reason"),
                "plan": approach.get("plan"), "manifest_source": approach.get("manifest_source"),
                "likely_failures": approach.get("likely_failures", []),
                "alternative_travel": approach.get("alternative_travel"),
                "alternative_captures": [{key: item.get(key) for key in (
                    "manifest", "manifest_source", "raw_artifact_sha256", "artifact_hash_verified", "plan")}
                    for item in alternatives], "live_verified": False}}
