"""Read-only interface between a future objective sampler and target resolution.

This module does not discover active quests or authorize movement. The producer
must supply an evidenced objective snapshot; its runtime source still needs
investigation. Static QuestTask rows alone are not active-objective evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Callable

TARGET_TYPES = {"actor", "object_id", "target_point", "poi", "location"}


def evidenced_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def resolve_active_objective(snapshot: dict[str, Any], *, current_identity: dict[str, str],
                             resolve_target: Callable[[dict[str, Any]], dict[str, Any]],
                             now: datetime | None = None, max_age_seconds: float = 2.5) -> dict[str, Any]:
    """Resolve exactly one current objective/reference; reject unknown identity."""
    refusal = {"ok": False, "position": None, "input_sent": False}

    def reject(reason: str) -> dict[str, Any]:
        return {**refusal, "reason": reason}

    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        return reject("objective_schema_unsupported")
    if (not isinstance(current_identity, dict) or any(not evidenced_text(current_identity.get(key)) for key in ("world_id", "map_id", "process_token"))
            or snapshot.get("identity") != current_identity):
        return reject("objective_identity_mismatch")
    try:
        stamp = datetime.fromisoformat(snapshot["observed_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return reject("objective_timestamp_invalid")
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        if not 0 <= age <= max_age_seconds:
            return reject("objective_snapshot_stale")
    except (ValueError, TypeError, KeyError, AttributeError):
        return reject("objective_timestamp_invalid")
    if (snapshot.get("observed") is not True or type(snapshot.get("sequence")) is not int
            or snapshot["sequence"] < 0 or not evidenced_text(snapshot.get("source")) or not evidenced_text(snapshot.get("authority"))):
        return reject("objective_evidence_missing")
    objectives = snapshot.get("objectives")
    if not isinstance(objectives, list) or any(not isinstance(item, dict) for item in objectives):
        return reject("objective_state_invalid")
    # Unknown states cannot be silently discarded as inactive.
    if any(not isinstance(item.get("state"), str) or item["state"] not in {"active", "completed", "inactive"}
           for item in objectives):
        return reject("objective_state_unknown")
    active = [item for item in objectives if item["state"] == "active"]
    if len(active) != 1:
        return reject("objective_active_ambiguous" if active else "objective_none_active")
    objective = active[0]
    if any(not isinstance(objective.get(key), str) or not objective[key].strip()
           for key in ("quest_id", "objective_id", "step_id")):
        return reject("objective_stable_id_missing")
    references = objective.get("targets")
    if not isinstance(references, list) or len(references) != 1 or not isinstance(references[0], dict):
        return reject("objective_target_ambiguous")
    reference = references[0]
    if (not isinstance(reference.get("target_type"), str) or reference["target_type"] not in TARGET_TYPES
            or not isinstance(reference.get("target_id"), str)
            or not reference["target_id"].strip() or not evidenced_text(reference.get("source"))):
        return reject("objective_target_unsupported")
    resolved = resolve_target(dict(reference))
    if not isinstance(resolved, dict) or not evidenced_text(resolved.get("source")) or not evidenced_text(resolved.get("authority")):
        return reject("objective_target_evidence_missing")
    common = {"objective_id": objective["objective_id"], "quest_id": objective["quest_id"],
              "step_id": objective["step_id"], "target_type": reference["target_type"],
              "target_id": reference["target_id"], "source": resolved["source"], "authority": resolved["authority"],
              "objective_source": snapshot["source"], "reference_source": reference["source"],
              "sample_sequence": snapshot["sequence"], "identity": dict(current_identity), "input_sent": False}
    position = resolved.get("position")
    if resolved.get("status") == "located":
        if (not isinstance(position, dict) or any(type(position.get(axis)) not in {int, float}
                or not math.isfinite(position[axis]) for axis in ("x", "y", "z"))
                or resolved.get("map_id") != current_identity["map_id"]):
            return reject("objective_exact_destination_unproven")
        return {**common, "ok": True, "destination_type": "exact", "position": dict(position)}
    if (resolved.get("status") == "macro" and position is None and isinstance(resolved.get("field"), str)
            and resolved["field"].strip()):
        return {**common, "ok": True, "destination_type": "macro", "position": None,
                "field": resolved["field"], "sub_area": resolved.get("sub_area"), "requires_reacquisition": True}
    return reject("objective_target_unresolved")


def objective_key(result: dict[str, Any]) -> tuple[str, str, str] | None:
    """A new step remains a transition even when the quest ID is unchanged."""
    return tuple(result[key] for key in ("quest_id", "objective_id", "step_id")) if result.get("ok") else None
