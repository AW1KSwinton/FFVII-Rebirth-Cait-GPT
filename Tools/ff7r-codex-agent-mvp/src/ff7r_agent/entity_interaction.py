"""Entity selection, final admission and observed interaction evidence.

No movement implementation lives here. The mission uses LiveRouteAdapter for
all positioning; this module only admits one existing use_interaction pulse.
"""
from __future__ import annotations

import math
from typing import Any

from .navigation_mission import MissionError, finite_position, horizontal_distance

MAX_AGE = 0.5
INTERACTION_RADIUS = 2.5
FINAL_OFFSET = 1.25
STAGING_RADIUS = 4.0
MAX_TARGET_MOVEMENT = 4.0
STABLE_TARGET_MOVEMENT = 0.10
STABLE_PLAYER_MOVEMENT = 0.05


def world_identity(state: dict) -> dict:
    result = {}
    for payload in (state, state.get("navigation_diagnostics"), state.get("navigation"), state.get("map_state")):
        if isinstance(payload, dict):
            for key, alias in (("world_id", "world"), ("session_id", "session")):
                if key not in result and payload.get(key, payload.get(alias)) is not None:
                    result[key] = payload.get(key, payload.get(alias))
    return result


def validate_sample(state: dict, previous: int = -1, *, baseline: dict | None = None,
                    gameplay: bool = True) -> None:
    telemetry = state.get("telemetry") or {}
    age = telemetry.get("age_seconds", state.get("age_seconds"))
    if (telemetry.get("live") is not True or state.get("source") in {None, "simulation", "unknown"}
        or not isinstance(age, (float, int)) or not math.isfinite(age) or not 0 <= age <= MAX_AGE
        or state.get("running") is not True):
        raise MissionError("entity_telemetry_stale_or_unavailable")
    sequence = state.get("sequence")
    if type(sequence) is not int or sequence <= previous:
        raise MissionError("entity_telemetry_not_advancing")
    if not state.get("map_id") or not (state.get("player") or {}).get("actor_id"):
        raise MissionError("entity_player_identity_missing")
    if baseline is not None and (state["map_id"] != baseline["map_id"]
        or state["player"]["actor_id"] != baseline["player"]["actor_id"]
        or world_identity(state) != world_identity(baseline)):
        raise MissionError("entity_world_or_player_changed")
    if gameplay:
        if (any(state.get(k) for k in ("paused", "in_menu", "in_combat", "loading", "target", "nearby_enemies"))
            or (state.get("interaction") or {}).get("state", "idle") != "idle"
            or (state.get("rest") or {}).get("state", "idle") not in {"idle", "bench_detected"}
            or (state["player"].get("locomotion") or {}).get("grounded") is False):
            raise MissionError("entity_not_active_gameplay")
        finite_position(state["player"].get("position"))


def entities(state: dict) -> list[dict]:
    """Deduplicate cross-feed aliases; reject conflicts and within-feed duplicates."""
    found: dict[str, dict] = {}
    feeds = [state.get("nearby_interactables", []), state.get("nearby_actors", [])]
    if state.get("nearby_interactable") is not None:
        feeds.append([state["nearby_interactable"]])
    for feed in feeds:
        if not isinstance(feed, (list, tuple)):
            raise MissionError("entity_feed_invalid")
        seen = set()
        for item in feed:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                raise MissionError("entity_identity_missing")
            key = item["id"]
            if key in seen:
                raise MissionError("entity_ambiguous_duplicate")
            seen.add(key)
            old = found.setdefault(key, {})
            for field in ("id", "kind", "position", "usable", "prompt", "visible", "opened"):
                value = item.get(field)
                if value is None:
                    continue
                if field == "kind":
                    value = str(value).strip().casefold()
                if field in old and old[field] != value:
                    raise MissionError("entity_conflicting_observations")
                old[field] = value
    return list(found.values())


def resolve_entity(state: dict, target_id: str | None = None, target_kind: str | None = None) -> dict:
    if not target_id and not target_kind:
        raise MissionError("entity_selector_required")
    matches = [e for e in entities(state) if (not target_id or e["id"] == target_id)
               and (not target_kind or e.get("kind") == target_kind.strip().casefold())]
    if not matches:
        raise MissionError("entity_not_observed")
    if len(matches) != 1:
        raise MissionError("entity_selector_ambiguous")
    target = dict(matches[0])
    if not target.get("kind"):
        raise MissionError("entity_kind_missing")
    target["position"] = finite_position(target.get("position"))
    return target


def spatial(state: dict, target: dict) -> dict:
    """Derive bearing/distance from the synchronized live actor pose in cm."""
    player = finite_position(state["player"].get("position"))
    position = finite_position(target.get("position"))
    yaw = (state["player"].get("rotation") or {}).get("yaw")
    if not isinstance(yaw, (int, float)) or not math.isfinite(yaw):
        raise MissionError("entity_player_heading_missing")
    dx, dy = position["x"] - player["x"], position["y"] - player["y"]
    bearing = math.degrees(math.atan2(dy, dx))
    return {"distance_m": math.hypot(dx, dy) / 100,
            "vertical_m": abs(position["z"] - player["z"]) / 100,
            "bearing_degrees": bearing, "heading_error_degrees": (bearing - yaw + 180) % 360 - 180}


def offset_goal(state: dict, target: dict, radius: float) -> dict:
    relative = spatial(state, target)
    bearing = math.radians(relative["bearing_degrees"])
    # Keep actor-root height convention; the route adapter resolves measured feet.
    return {"x": target["position"]["x"] - radius * 100 * math.cos(bearing),
            "y": target["position"]["y"] - radius * 100 * math.sin(bearing),
            "z": target["position"]["z"]}


def target_motion(target: dict, anchor: dict, limit: float) -> None:
    if target["id"] != anchor["id"] or target.get("kind") != anchor.get("kind"):
        raise MissionError("entity_identity_changed")
    if (horizontal_distance(target["position"], anchor["position"]) > limit
        or abs(target["position"]["z"] - anchor["position"]["z"]) / 100 > limit):
        raise MissionError("entity_moved_outside_bound")


def ready(state: dict, target: dict, *, require_facing: bool = True,
          max_distance_m: float = INTERACTION_RADIUS) -> bool:
    if target.get("visible") is False or target.get("opened") is True or target.get("usable") is False:
        return False
    # An exported empty prompt explicitly says no prompt; unknown is not usable.
    prompt = target.get("prompt")
    if prompt is not None and not prompt.strip():
        return False
    if target.get("usable") is not True and not prompt:
        return False
    distance = spatial(state, target)
    return bool(distance["distance_m"] <= max_distance_m and distance["vertical_m"] <= 1.5
                and (not require_facing or abs(distance["heading_error_degrees"]) <= 45))


def require_stable(before: dict, after: dict, target: dict, next_target: dict) -> None:
    target_motion(next_target, target, STABLE_TARGET_MOVEMENT)
    if (horizontal_distance(finite_position(before["player"]["position"]), finite_position(after["player"]["position"])) > STABLE_PLAYER_MOVEMENT
        or abs(before["player"]["position"]["z"] - after["player"]["position"]["z"]) > 5):
        raise MissionError("entity_player_not_settled")
    velocity = (after["player"].get("locomotion") or {}).get("velocity_cm_s")
    if velocity is not None:
        v = finite_position(velocity)
        if math.sqrt(sum(x*x for x in v.values())) > 50:
            raise MissionError("entity_player_not_settled")
    if any(target.get(k) != next_target.get(k) for k in ("usable", "prompt", "visible", "opened")):
        raise MissionError("entity_affordance_unstable")


def verification_signals(before: dict, after: dict, target: dict) -> set[str]:
    """Positive deltas only. Missing telemetry and a sent key are never success."""
    signals = set()
    interaction = after.get("interaction") or {}
    if interaction.get("target_id") not in (None, target["id"]):
        raise MissionError("interaction_other_entity")
    if after.get("in_combat") or after.get("target"):
        raise MissionError("interaction_combat_started")
    if (interaction.get("state") in {"interacting", "dialogue", "menu", "transition", "loading", "completed"}
        and interaction != (before.get("interaction") or {})):
        signals.add("interaction_state_changed")
    if after.get("in_menu") is True and before.get("in_menu") is False:
        signals.add("menu_opened")
    if after.get("loading") is True and before.get("loading") is False:
        signals.add("loading_started")
    # A world/player change is evidence only with explicit transition/loading.
    changed_world = (after["map_id"] != before["map_id"] or world_identity(after) != world_identity(before)
                     or after["player"]["actor_id"] != before["player"]["actor_id"])
    if changed_world:
        if "loading_started" in signals or interaction.get("state") in {"transition", "loading"}:
            return signals | {"world_transition"}
        raise MissionError("interaction_unexplained_identity_change")
    rest, old_rest = after.get("rest") or {}, before.get("rest") or {}
    if (target["kind"] == "bench" and rest.get("bench_id") == target["id"]
        and rest.get("state_identity") == before["player"]["actor_id"]
        and rest.get("state") in {"resting", "interaction_completed"}
        and rest != old_rest and rest.get("interaction_id") is not None):
        signals.add("bench_lifecycle_changed")
    # Missing/disabled prompts can also mean the player drifted away. Only use
    # target deltas while both player and same target remain stationary.
    if horizontal_distance(finite_position(before["player"]["position"]), finite_position(after["player"]["position"])) > STABLE_PLAYER_MOVEMENT:
        return signals
    observed = [e for e in entities(after) if e["id"] == target["id"]]
    if not observed:
        if after.get("nearby_entities_complete") is True and before.get("nearby_entities_complete") is True:
            signals.add("target_disappeared_from_complete_feed")
        return signals
    current = resolve_entity(after, target["id"], target["kind"])
    target_motion(current, target, STABLE_TARGET_MOVEMENT)
    if target.get("opened") is False and current.get("opened") is True:
        signals.add("target_opened")
    if target.get("usable") is True and current.get("usable") is False:
        signals.add("target_became_unusable")
    if target.get("prompt") and "prompt" in current and current["prompt"] != target["prompt"]:
        signals.add("target_prompt_changed")
    return signals


def interact_once(service: Any, *, target_id: str, target_kind: str, expected_position: dict,
                  expected_map_id: str, expected_actor_id: str, expected_world: dict,
                  after_sequence: int, client_id: str, game_pid: int, timeout_seconds: float = 10.0) -> dict:
    """Worker-side admission and verification under the ordinary action lock."""
    from .service import SafetyError

    if not service._action_lock.acquire(blocking=False):
        raise SafetyError("another input action is executing")
    report = {"ok": False, "state": "STOPPED", "interaction_attempted": False,
              "interaction_sent": False, "target_id": target_id, "samples": []}
    try:
        if not math.isfinite(timeout_seconds) or not 0.5 <= timeout_seconds <= 10:
            raise SafetyError("entity_interaction_invalid_time_budget")
        action_deadline = service._monotonic() + timeout_seconds
        if not getattr(service.input, "is_real", False) or service._session_lease is None:
            raise SafetyError("entity_interaction_requires_real_leased_backend")
        lease = service._session_lease.status() or {}
        lease_identity = (lease.get("client_id"), lease.get("game_pid"), lease.get("opened_at"))

        def check_binding():
            current = service._session_lease.status() or {}
            if (not current.get("active") or current.get("client_id") != client_id
                or current.get("game_pid") != game_pid
                or (current.get("client_id"), current.get("game_pid"), current.get("opened_at")) != lease_identity):
                raise SafetyError("entity_interaction_session_changed")
            if game_pid not in service._environment.game_process_ids():
                raise SafetyError("entity_interaction_process_changed")
            if not service._environment.game_is_foreground():
                raise SafetyError("entity_interaction_foreground_lost")

        check_binding()
        service.input.release_all()
        baseline = {"map_id": expected_map_id, "player": {"actor_id": expected_actor_id}, **expected_world}
        anchor = {"id": target_id, "kind": target_kind, "position": finite_position(expected_position)}
        previous = after_sequence
        before = target = None
        # The normal action gate also checks arm, readiness, process and cooldown.
        for _ in range(2):
            check_binding()
            state = service._assert_real_movement_allowed()
            sample = state.to_dict()
            sample["telemetry"] = {"live": state.age_seconds() <= MAX_AGE, "age_seconds": state.age_seconds()}
            validate_sample(sample, previous, baseline=baseline)
            current = resolve_entity(sample, target_id, target_kind)
            target_motion(current, anchor, STABLE_TARGET_MOVEMENT)
            if not ready(sample, current):
                raise SafetyError("entity_interaction_not_usable_or_in_range")
            competitors = [e for e in entities(sample) if e["id"] != target_id and ready(sample, e, require_facing=False)]
            if competitors:
                raise SafetyError("entity_interaction_prompt_ambiguous")
            if before is not None:
                require_stable(before, sample, target, current)
            before, target, previous = sample, current, sample["sequence"]
            report["samples"].append(sample)
            if len(report["samples"]) < 2:
                service._sleeper(0.15)
        check_binding()
        # Durable consume precedes the possible side effect. Never clear on an
        # input error, failed verification, client disconnect or worker reload.
        # The session outlives the mission, so admission is once per request:
        # this target as of the observation the mission acted on.
        service._session_lease.consume_entity_interaction(
            client_id, game_pid, target_id, request_key=f"{target_id}@{after_sequence}")
        check_binding()
        if state.age_seconds() > MAX_AGE or service._monotonic() + 0.25 >= action_deadline:
            raise SafetyError("entity_interaction_admission_expired")
        if service._arm is None or not service._arm.status()[0]:
            raise SafetyError("entity_interaction_arm_lost")
        service._arm.clear()
        if target_kind == "bench" and state.nearby_interactable and state.nearby_interactable.id == target_id:
            begin = getattr(service.telemetry, "begin_bench_interaction", None)
            if begin is not None:
                begin(state)
        report["interaction_attempted"] = True
        try:
            report["input"] = service.input.use_interaction(250)
            report["interaction_sent"] = True
        finally:
            service.input.release_all()
        deadline = min(action_deadline, service._monotonic() + 8.0)
        preceding_signals: set[str] = set()
        for _ in range(40):
            if service._monotonic() >= deadline:
                break
            service._sleeper(min(0.2, max(0, deadline - service._monotonic())))
            if service._monotonic() >= deadline:
                break
            check_binding()
            sample = service.observe()
            # Repeated/stale telemetry after the pulse fails closed immediately.
            validate_sample(sample, previous, gameplay=False)
            previous = sample["sequence"]
            report["samples"].append(sample)
            signals = verification_signals(before, sample, target)
            confirmed = preceding_signals & signals
            if confirmed:
                report.update(ok=True, state="INTERACTION_VERIFIED", verification={
                    "signals": sorted(confirmed), "sequences": [report["samples"][-2]["sequence"], previous]})
                break
            preceding_signals = signals
        if not report["ok"]:
            report["reason"] = "interaction_unverified"
    except Exception as exc:
        report["reason"] = str(exc) or type(exc).__name__
    finally:
        try:
            service.input.release_all()
        except Exception as exc:
            report.update(ok=False, state="CLEANUP_FAILED", cleanup_error=str(exc))
        finally:
            try:
                if service._arm:
                    service._arm.latch_stop()
            except Exception as exc:
                report.update(ok=False, state="CLEANUP_FAILED", arm_cleanup_error=str(exc))
            finally:
                with service._lock:
                    service._stopped = True
                service._last_action_finished = service._monotonic()
                service._action_lock.release()
        service._record_action(action="interact_with_entity", **report)
    return report
