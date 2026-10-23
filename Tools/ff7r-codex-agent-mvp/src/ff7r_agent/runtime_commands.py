from __future__ import annotations

from contextlib import nullcontext

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable

from .autonomy import DEFAULT_CLOSE_TIMEOUT_SECONDS
from .controller_policy import live_controller_policy_evidence, require_proven_live_controller_policy
from .models import Vec3
from .safety import (
    SessionLease,
    request_session_close,
    request_session_heartbeat,
    request_session_open,
    single_gameplay_process,
)


# Generic transport is intentionally backed by an explicit registry rather
# than arbitrary getattr access. New capabilities must be deliberately added.
SERVICE_ALIASES = {
    "observe_game": "observe",
    "observe_combat": "observe_combat",
    "observe_queens_blood": "observe_queens_blood",
    "mulligan_cards": "mulligan_cards",
    "select_queens_blood_cards": "select_queens_blood_cards",
    "play_queens_blood_turn": "play_queens_blood_turn",
    "play_queens_blood": "play_queens_blood_match",
    "play_queens_blood_match": "play_queens_blood_match",
    "tap_queens_blood_button": "tap_queens_blood_button",
    "open_queens_blood_deck_editor": "open_queens_blood_deck_editor",
    "close_queens_blood_deck_editor": "close_queens_blood_deck_editor",
    "probe_queens_blood_card_cells": "probe_queens_blood_card_cells",
    "read_queens_blood_cards": "read_queens_blood_cards",
    "scan_queens_blood_collection": "scan_queens_blood_collection",
    "edit_queens_blood_deck": "edit_queens_blood_deck",
    "travel_to_map_spot": "travel_to_map_spot",
    "load_save_slot": "load_save_slot",
    "save_game_progress": "save_game_progress",
    "arm_working_save_recovery": "arm_working_save_recovery",
    "disarm_working_save_recovery": "disarm_working_save_recovery",
    "read_save_menu": "read_save_menu",
    "get_queens_blood_run_status": "get_queens_blood_run_status",
    "get_queens_blood_preparation_context": "get_queens_blood_preparation_context",
    "prepare_queens_blood_deck": "prepare_queens_blood_deck",
    "revalidate_queens_blood_prepared_deck": "revalidate_queens_blood_prepared_deck",
    "get_campaign_status": "get_campaign_status",
    "get_campaign_next_step": "get_campaign_next_step",
    "assign_queens_blood_run": "assign_queens_blood_run",
    "start_queens_blood_recording": "start_queens_blood_recording",
    "get_queens_blood_recording_status": "get_queens_blood_recording_status",
    "get_production_event_status": "get_production_event_status",
    "register_production_test_context": "register_production_test_context",
    "begin_production_test": "begin_production_test",
    "end_production_test": "end_production_test",
    "stop_queens_blood_recording": "stop_queens_blood_recording",
    "read_field_prompt": "read_field_prompt",
    "get_cutscene_state": "cutscene_state",
    "skip_cutscene": "skip_cutscene",
    "set_cutscene_autoskip": "set_cutscene_autoskip",
    "get_cutscene_autoskip_status": "cutscene_autoskip_status",
    "close_tips": "close_tips",
    "set_tips_autoclose": "set_tips_autoclose",
    "get_tips_autoclose_status": "tips_autoclose_status",
    "open_door": "open_door",
    "use_field_action": "use_field_action",
    "terrain_action": "terrain_action",
    "talk_to_npc": "talk_to_npc",
    "use_guide_moogle": "use_guide_moogle",
    "enter_combat_simulator": "enter_combat_simulator",
    "explore_view": "explore_view",
    "leave_combat_simulator": "leave_combat_simulator",
    "read_screen_texts": "read_screen_texts",
    "read_dialogue_wheel": "read_dialogue_wheel",
    "choose_dialogue_option": "choose_dialogue_option",
    "retry_after_game_over": "retry_after_game_over",
    "battle_menu_tap": "battle_menu_tap",
    "grab_move": "grab_move",
    "climb_move": "climb_move",
    "read_battle_command_menu": "read_battle_command_menu",
    "read_field_menu": "read_field_menu",
    "cast_field_spell": "cast_field_spell",
    "use_field_item": "use_field_item",
    "run_field_menu": "run_field_menu",
    "use_battle_command": "use_battle_command",
    "read_ui_roots": "read_ui_roots",
    "read_npc_dialogue": "read_npc_dialogue",
    "tap_shop_button": "tap_shop_button",
    "read_shop": "read_shop",
    "buy_shop_item": "buy_shop_item",
    "close_shop": "close_shop",
    "prewarm_navigation": "prewarm_navigation",
    "plan_npc_approach": "plan_npc_approach",
    "sweep_route": "sweep_route",
    "pass_queens_blood_turn": "pass_queens_blood_turn",
    "finish_queens_blood_match": "finish_queens_blood_match",
    "queens_blood_card_vendors": "queens_blood_card_vendors",
    "place_queens_blood_card": "place_queens_blood_card",
    "capture_game_window": "capture_game_window",
    "get_navigation_context": "navigation_context",
    "remember_landmark": "remember_landmark",
    "get_agent_status": "status",
    "get_input_status": "input_status",
    "get_session_health": "session_health",
    "wait_for_settled_gameplay": "wait_for_settled_gameplay",
    "tap_button": "tap",
    "observe_map_telemetry": "map_telemetry",
    "observe_map_ui": "map_ui_telemetry",
    "observe_map_poi": "observe_map_poi",
    "release_map_poi": "release_map_poi",
    "observe_map_pins": "observe_map_pins",
    "map_point_to_world": "map_point_to_world",
    "get_map_target": "get_map_target",
    "fast_travel_via_map": "fast_travel_via_map",
    "scan_character_candidates": "scan_character_candidates",
    "read_objective_progress": "read_objective_progress",
    "get_active_objective": "get_active_objective",
    "scan_character_inventory": "scan_character_inventory",
    "scan_actor_inventory": "scan_actor_inventory",
    "scan_nearby_actors": "scan_nearby_actors",
    "scan_traversal_gimmicks": "scan_traversal_gimmicks",
    "scan_character_target": "scan_character_target",
    "resolve_world_target": "resolve_world_target",
    "release_character_scan": "release_character_scan",
    "cycle_map_selection": "map_selection",
    "cycle_map_poi": "map_selection",
    "cycle_map_tab": "map_tab",
    "toggle_map_layer": "toggle_map_layer",
    "toggle_map_era": "toggle_map_era",
    "view_map_corresponding": "view_map_corresponding",
    "drop_map_pin": "drop_map_pin",
    "recover_session_control": "recover_session_control",
    "probe_forward_movement": "probe_forward_movement",
    "tap_vault": "tap_vault",
    "travel_forward_chunk": "travel_forward_chunk",
    "travel_direction_chunk": "travel_direction_chunk",
    "travel_sprint_chunk": "travel_sprint_chunk",
    "travel_vector_chunk": "travel_vector_chunk",
    "plan_predictive_movement": "plan_predictive_movement",
    "travel_planned_chunk": "travel_planned_chunk",
    "navigation_metrics": "navigation_metrics",
    "benchmark_readiness_snapshot": "benchmark_readiness_snapshot",
    "start_chadley_benchmark": "start_chadley_benchmark",
    "get_chadley_benchmark_status": "chadley_benchmark_status",
    "finish_chadley_benchmark": "finish_chadley_benchmark",
    "mark_chadley_benchmark_phase": "mark_chadley_benchmark_phase",
    "discard_chadley_benchmark": "discard_chadley_benchmark",
    "start_human_traversal_recording": "start_human_traversal_recording",
    "sample_human_traversal_recording": "sample_human_traversal_recording",
    "get_human_traversal_recording_status": "human_traversal_recording_status",
    "finish_human_traversal_recording": "finish_human_traversal_recording",
    "run_human_traversal_sampler": "run_human_traversal_sampler",
    "move_stick": "move",
    "move_direction": "move_direction",
    "look_camera": "look_camera",
    "survey_camera": "survey_camera",
    "plan_navigation_route": "plan_navigation_route",
    "navigate_next_segment": "navigate_next_segment",
    "run_local_navigation_episode": "run_local_navigation_episode",
    "diagnose_navigation_stall": "diagnose_navigation_stall",
    "calibrate_camera_horizontal": "calibrate_camera_horizontal",
    "align_camera_to_bearing": "align_camera_to_bearing",
    "align_camera_to_detected_bench": "align_camera_to_detected_bench",
    "stop_all_input": "stop",
    "interact_with_entity": "interact_with_entity",
    "initiate_queens_blood_match": "initiate_queens_blood_match",
    "use_nearby_bench": "use_nearby_bench",
    "say_on_stream": "say_on_stream",
    "interact_with_physical_chadley": "interact_with_physical_chadley",
    "approach_detected_bench": "approach_detected_bench",
    "approach_and_interact": "approach_and_interact",
}

LIFECYCLE_COMMANDS = frozenset({
    "get_lifecycle_status",
    "refresh_lifecycle_attachment",
    "launch_game",
    "close_game",
    "recover_stale_lifecycle",
    "wait_for_live_telemetry",
    "wait_for_gameplay_ready",
    "release_game_lifecycle",
    "verify_executable_identity",
})

STARTUP_COMMANDS = frozenset({
    "start_game_to_main_menu",
    "select_game_start",
    "continue_from_last_save",
    "load_last_save",
    "prepare_benchmark_attempt",
    "start_and_continue_from_last_save",
    "restart_and_continue_from_last_save",
    "start_and_load_save_slot",
    "start_new_game",
    # Internal worker operations used by the stable bridge's resumable startup
    # jobs. They are intentionally not second public startup paths.
    "run_start_and_continue_from_last_save",
    "run_start_and_load_save_slot",
    "run_start_new_game",
    "run_recover_working_save",
})

SESSION_COMMANDS = frozenset({
    "open_control_session",
    "heartbeat_control_session",
    "close_control_session",
    "get_control_session_status",
})

SPECIAL_SERVICE_COMMANDS = frozenset({
    "focus_game_window",
    "open_map",
    "tap_combat_attack",
    "run_live_route_adapter",
    "capture_topology_cache",
})


# Only operations that consume card-game UI may wake its expensive probe.
# Run/campaign bookkeeping, recording and saved-inventory deck preparation
# must leave it idle in the field, including while an async travel job runs.
QUEENS_BLOOD_UI_OPERATIONS = frozenset({
    "observe_queens_blood",
    "mulligan_cards",
    "select_queens_blood_cards",
    "play_queens_blood_turn",
    "play_queens_blood",
    "play_queens_blood_match",
    "open_queens_blood_deck_editor",
    "close_queens_blood_deck_editor",
    "probe_queens_blood_card_cells",
    "read_queens_blood_cards",
    "scan_queens_blood_collection",
    "edit_queens_blood_deck",
    "pass_queens_blood_turn",
    "finish_queens_blood_match",
    "place_queens_blood_card",
    "initiate_queens_blood_match",
    "run_initiate_queens_blood_match",
    "run_finish_conversation",
})


def _service_call(service: Any, operation: str, arguments: dict[str, Any]) -> Any:
    method_name = SERVICE_ALIASES[operation]
    callback: Callable[..., Any] | None = getattr(service, method_name, None)
    if not callable(callback):
        raise ValueError(f"runtime operation is unavailable in this worker: {operation}")
    return callback(**arguments)


def _refresh_live_process_identity(service: Any, lifecycle: Any) -> dict[str, Any]:
    """Refresh an attached lifecycle context without ever launching Rebirth.

    Reloadable workers can retain a durable context from an earlier attached
    process.  The live adapter must bind its PID to the process that is
    actually present before render evidence or topology can authorize
    anything.  Restrict this refresh to the real backend and to an observed
    process list; ``allow_launch=False`` makes the operation attach-only.
    """
    if not getattr(getattr(service, "input", None), "is_real", False):
        return {"attempted": False, "reason": "non_real_backend"}
    environment = getattr(lifecycle, "environment", None)
    process_ids = getattr(environment, "game_process_ids", None)
    ensure_started = getattr(lifecycle, "ensure_started", None)
    if not callable(process_ids) or not callable(ensure_started):
        return {"attempted": False, "reason": "lifecycle_attach_refresh_unavailable"}
    processes = process_ids()
    if not processes:
        return {"attempted": False, "reason": "no_game_processes_observed"}
    context = ensure_started(allow_launch=False)
    bound = context.get("context") if isinstance(context, dict) else None
    return {
        "attempted": True,
        "mode": context.get("mode") if isinstance(context, dict) else None,
        "processes": dict(processes),
        "run_id": bound.get("run_id") if isinstance(bound, dict) else None,
        "pid": bound.get("pid") if isinstance(bound, dict) else None,
        "attachment": bound.get("attachment") if isinstance(bound, dict) else None,
    }


def _session_scope(service: Any) -> dict[str, Any]:
    """Use the same lease scope as the configured live service."""
    lease = getattr(service, "_session_lease", None)
    if lease is None:
        return {}
    return {
        "runtime_dir": getattr(lease, "runtime_dir", None),
        "owner_id": getattr(lease, "owner_id", None),
    }


def _running_gameplay_pid(service: Any) -> int | None:
    """The single gameplay process now running, or None (absent, ambiguous, no probe)."""
    probe = getattr(getattr(service, "_environment", None), "game_process_ids", None)
    try:
        return single_gameplay_process(probe()) if callable(probe) else None
    except Exception:
        return None


MAX_NAVIGATION_OBJECTIVE_RECOVERY_ATTEMPTS = 3
MAX_NAVIGATION_OBJECTIVE_ROUTE_ATTEMPTS = 12
_RECOVERABLE_ROUTE_DIVERGENCE_PREFIXES = (
    "post_command_corridor_reacquisition_failed:",
    "multi_polygon_corridor_departure",
    "multi_tile_corridor_departure",
    "multi_polygon_corridor_boundary_ambiguity_unproven",
    "multi_tile_corridor_boundary_ambiguity_unproven",
)


def _classify_navigation_route_result(result: Any) -> str:
    """Classify a completed route attempt without relaxing its safety gate."""

    if not isinstance(result, dict):
        return "invalid_route_result"
    reason = str(result.get("reason") or "")
    if reason.startswith(_RECOVERABLE_ROUTE_DIVERGENCE_PREFIXES):
        return "recoverable_route_divergence"
    if reason.startswith("final_pre_movement_gate_failed:"):
        # A changed physical start requires a new strict preflight. Combined
        # lease, identity or readiness failures remain terminal here.
        failures = set(reason.split(":", 1)[1].split(";"))
        if failures and failures <= {"route_start_horizontal_mismatch", "route_start_vertical_mismatch",
                                     "route_start_polygon_mismatch"}:
            return "recoverable_route_divergence"
    if reason in {"readiness_recovery_timeout", "readiness_recovery_post_settle_observation_unavailable"}:
        return "recoverable_route_readiness_loss"
    if reason == "plan_identity_changed":
        return "recoverable_route_identity_change"
    if reason in {"", "control_cycle_bound_reached"}:
        return "terminal_route_failure"
    return "terminal_route_failure"


def _route_result_has_meaningful_progress(result: Any) -> bool:
    """Return true only when the route trace proves useful progress.

    A command acknowledgement or a non-zero command count is not progress.
    The route trace must show either increasing route progress or a materially
    reduced distance to the target on fresh control-cycle observations.
    """

    if not isinstance(result, dict):
        return False
    progress_values: list[float] = []
    distance_values: list[float] = []
    for event in result.get("timeline") or []:
        if not isinstance(event, dict) or event.get("event") not in {"control_cycle", "pursuit_command"}:
            continue
        # A pursuit_command is recorded only after its hold was sent.
        if event.get("event") == "control_cycle" and event.get("actual_command_sent") is not True:
            continue
        try:
            progress = float(event.get("route_progress"))
            if progress == progress and progress not in {float("inf"), float("-inf")}:
                progress_values.append(progress)
        except (TypeError, ValueError):
            pass
        try:
            distance = float(event.get("distance_to_final_m"))
            if distance == distance and distance not in {float("inf"), float("-inf")}:
                distance_values.append(distance)
        except (TypeError, ValueError):
            pass
    if len(progress_values) >= 2 and progress_values[-1] > progress_values[0] + 1.0e-6:
        return True
    return len(distance_values) >= 2 and distance_values[-1] < distance_values[0] - 0.10


def _authorization_identity(preflight: Any) -> dict[str, Any]:
    """Extract route identity for objective recovery evidence."""

    if not isinstance(preflight, dict):
        return {}
    proof = preflight.get("authorization_proof")
    authorization = preflight.get("authorization")
    identity: dict[str, Any] = {}
    if isinstance(proof, dict):
        for key in ("route_plan_sha256", "topology_fingerprint", "session_id", "world_id", "map_id"):
            if key in proof:
                identity[key] = proof[key]
    if isinstance(authorization, dict):
        for key in ("route_plan_sha256", "topology_fingerprint", "session_id", "world_id", "map_id"):
            identity.setdefault(key, authorization.get(key))
    static_obstacles = preflight.get("static_obstacles")
    if isinstance(static_obstacles, dict):
        identity["static_obstacle_digest"] = static_obstacles.get("digest")
    return identity


def _run_navigation_objective(
    attempt_factory: Callable[[], dict[str, Any]],
    *,
    dry_run: bool,
    objective_id: str,
    target_position: dict[str, float] | None,
    max_recovery_attempts: int = MAX_NAVIGATION_OBJECTIVE_RECOVERY_ATTEMPTS,
) -> dict[str, Any]:
    """Keep the target objective alive across bounded disposable route attempts.

    Each factory call must construct a new adapter and run its complete
    neutral/preflight/independent-validation sequence.  A route divergence
    therefore cannot reuse its old corridor or authorization.  The wrapper is
    deliberately unaware of topology geometry: localization is accepted only
    when the fresh adapter's existing strict preflight can bind the current
    telemetry position to valid topology.
    """

    if not 1 <= int(max_recovery_attempts) <= MAX_NAVIGATION_OBJECTIVE_RECOVERY_ATTEMPTS:
        raise ValueError(
            f"max_recovery_attempts must be between 1 and {MAX_NAVIGATION_OBJECTIVE_RECOVERY_ATTEMPTS}"
        )

    attempts: list[dict[str, Any]] = []
    objective_timeline: list[dict[str, Any]] = []
    recovery_streak = 0
    total_recoveries = 0
    last_attempt: dict[str, Any] | None = None
    final_run: dict[str, Any] | None = None
    final_preflight: dict[str, Any] | None = None
    final_adapter: Any = None
    objective_state = "NAVIGATION_OBJECTIVE_ACTIVE"

    while len(attempts) < MAX_NAVIGATION_OBJECTIVE_ROUTE_ATTEMPTS:
        attempt = dict(attempt_factory())
        preflight = attempt.get("preflight")
        run_result = attempt.get("run")
        adapter = attempt.get("adapter")
        attempt_index = len(attempts) + 1
        route_identity = _authorization_identity(preflight)
        attempt_record = {
            "attempt_index": attempt_index,
            "recovery_attempt": recovery_streak,
            "settled_readiness": attempt.get("settled_readiness"),
            "settling_error": attempt.get("settling_error"),
            "preflight": preflight,
            "run": run_result,
            "route_identity": route_identity,
            "movement_commands": int((run_result or {}).get("movement_cycles") or 0)
            if isinstance(run_result, dict) else 0,
        }
        attempts.append(attempt_record)
        final_adapter = adapter
        final_preflight = preflight if isinstance(preflight, dict) else None
        final_run = run_result if isinstance(run_result, dict) else None
        last_attempt = attempt_record
        if isinstance(preflight, dict):
            objective_timeline.extend(preflight.get("timeline") or [])
        if isinstance(run_result, dict):
            objective_timeline.extend(run_result.get("timeline") or [])

        if attempt_index > 1:
            previous = attempts[-2]
            objective_timeline.append({
                "event": (
                    "navigation_objective_reauthorization_created"
                    if isinstance(preflight, dict) and preflight.get("movement_authorized") is True
                    else "navigation_objective_relocalization_failed"
                ),
                "objective_id": objective_id,
                "attempt_index": attempt_index,
                "old_route_identity": previous.get("route_identity", {}),
                "new_route_identity": route_identity,
                "preflight_reason": (preflight or {}).get("reason") if isinstance(preflight, dict) else "preflight_result_missing",
                "movement_commands_between": 0,
                "movement_command_sent": False,
            })

        if not isinstance(preflight, dict) or preflight.get("movement_authorized") is not True:
            objective_state = "NAVIGATION_OBJECTIVE_ABORTED"
            objective_timeline.append({
                "event": "navigation_objective_aborted_during_preflight",
                "objective_id": objective_id,
                "attempt_index": attempt_index,
                "reason": (preflight or {}).get("reason") if isinstance(preflight, dict) else "preflight_result_missing",
                "movement_command_sent": False,
            })
            break
        if not isinstance(run_result, dict):
            objective_state = "NAVIGATION_OBJECTIVE_ABORTED"
            objective_timeline.append({
                "event": "navigation_objective_aborted_without_route_result",
                "objective_id": objective_id,
                "attempt_index": attempt_index,
                "movement_command_sent": False,
            })
            break

        classification = _classify_navigation_route_result(run_result)
        attempt_record["classification"] = classification
        progress = _route_result_has_meaningful_progress(run_result)
        attempt_record["meaningful_progress"] = progress
        if progress:
            # A proven progress sample closes the local failure episode. The
            # absolute route-attempt bound below prevents an oscillating
            # objective from becoming unbounded even when terrain repeatedly
            # produces progress followed by divergence.
            recovery_streak = 0
            attempt_record["recovery_budget_reset"] = True
        if dry_run or classification not in {
            "recoverable_route_divergence",
            "recoverable_route_identity_change",
            "recoverable_route_readiness_loss",
        }:
            if run_result.get("state") == "ARRIVED":
                objective_state = "NAVIGATION_OBJECTIVE_COMPLETED"
            elif classification == "terminal_route_failure":
                objective_state = "NAVIGATION_OBJECTIVE_ABORTED"
            objective_timeline.append({
                "event": "navigation_objective_route_terminal",
                "objective_id": objective_id,
                "attempt_index": attempt_index,
                "classification": classification,
                "route_reason": run_result.get("reason"),
                "meaningful_progress": progress,
                "movement_command_sent": False,
            })
            break

        if recovery_streak >= int(max_recovery_attempts):
            objective_state = "NAVIGATION_OBJECTIVE_ABORTED"
            exhausted = dict(run_result)
            exhausted.update({
                "route_terminal_state": exhausted.get("state"),
                "route_terminal_reason": exhausted.get("reason"),
                "state": "STOPPED",
                "reason": "recovery_budget_exhausted",
                "movement_authorized": False,
            })
            final_run = exhausted
            attempt_record["objective_terminal_reason"] = "recovery_budget_exhausted"
            objective_timeline.append({
                "event": "navigation_objective_recovery_budget_exhausted",
                "objective_id": objective_id,
                "attempt_index": attempt_index,
                "recovery_attempts": recovery_streak,
                "max_recovery_attempts": int(max_recovery_attempts),
                "old_route_identity": route_identity,
                "movement_command_sent": False,
            })
            break

        total_recoveries += 1
        recovery_streak += 1
        objective_timeline.append({
            "event": "navigation_objective_route_invalidated",
            "objective_id": objective_id,
            "attempt_index": attempt_index,
            "recovery_attempt": recovery_streak,
            "classification": classification,
            "old_route_identity": route_identity,
            "route_reason": run_result.get("reason"),
            "movement_command_sent": False,
            "authorization_invalidated": True,
        })
        objective_timeline.append({
            "event": "navigation_objective_recovery_started",
            "objective_id": objective_id,
            "recovery_attempt": recovery_streak,
            "target_position": target_position,
            "localization": "fresh_adapter_preflight_from_current_telemetry",
            "movement_command_sent": False,
        })
        # The next loop iteration obtains a fresh settled observation and
        # strict topology binding before it can create a new authorization.

    else:
        objective_state = "NAVIGATION_OBJECTIVE_ABORTED"
        if isinstance(final_run, dict):
            exhausted = dict(final_run)
            exhausted.update({
                "route_terminal_state": exhausted.get("state"),
                "route_terminal_reason": exhausted.get("reason"),
                "state": "STOPPED",
                "reason": "objective_route_attempt_bound_reached",
                "movement_authorized": False,
            })
            final_run = exhausted
        objective_timeline.append({
            "event": "navigation_objective_route_attempt_bound_reached",
            "objective_id": objective_id,
            "max_route_attempts": MAX_NAVIGATION_OBJECTIVE_ROUTE_ATTEMPTS,
            "movement_command_sent": False,
        })

    if final_run is not None:
        final_run = dict(final_run)
        final_run["navigation_objective_id"] = objective_id
        final_run["navigation_objective_state"] = objective_state
        final_run["navigation_objective_target_position"] = target_position
        final_run["navigation_objective_recovery_attempts"] = total_recoveries
        final_run["navigation_objective_timeline"] = list(objective_timeline)
    return {
        "objective_id": objective_id,
        "objective_state": objective_state,
        "objective_target_position": target_position,
        "route_attempts": attempts,
        "recovery_attempts": total_recoveries,
        "timeline": objective_timeline,
        "final_adapter": final_adapter,
        "final_preflight": final_preflight,
        "final_run": final_run,
        "last_attempt": last_attempt,
    }


def _run_live_route_adapter(service: Any, lifecycle: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Run one explicitly dry-run or live fail-closed adapter attempt.

    This is intentionally a special command: the adapter must be constructed
    with the already-created service input backend and lifecycle identity, and
    ``dry_run`` must be supplied by the caller rather than silently defaulting.
    No lease is opened here; an existing live session remains a prerequisite.
    """
    if "dry_run" not in values or not isinstance(values["dry_run"], bool):
        raise ValueError("run_live_route_adapter requires an explicit boolean dry_run")
    route_test_start = getattr(service, "production_test_route_started", None)
    if callable(route_test_start):
        route_test_start()
    from .live_route_adapter import (
        AgentServiceTelemetrySource,
        AgentServiceRenderEvidenceSource,
        DEFAULT_MAX_CAPTURE_WAIT_SECONDS,
        DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
        DEFAULT_TOPOLOGY_PROBE_PATH,
        DEFAULT_TOPOLOGY_PROBE_DLL_PATH,
        DEFAULT_TOPOLOGY_STATUS_PATH,
        ExistingAnalogueActuator,
        ExistingHorizontalCameraActuator,
        IndependentJsonValidator,
        JsonTopologyProvider,
        LiveAdapterConfig,
        LiveRouteAdapter,
        NavigationSidecarLookahead,
        ValidatedTopologyCacheProvider,
        load_route_follower_factory,
    )
    from .static_obstacles import StaticObstacleOverlay, StaticObstacleStore

    topology_path = values.get("topology_path")
    probe_path = values.get("probe_path")
    validator_snapshot = values.get("validator_snapshot") or topology_path
    if not getattr(service.input, "is_real", False) and not values["dry_run"]:
        raise ValueError("live route adapter requires the real analogue backend")
    live_controller_policy = None
    if not values["dry_run"]:
        # Do this before lifecycle refresh, topology loading, settling or any
        # other route work.  A real route must never inherit the legacy
        # 0.35/100 ms normal envelope by omission.
        live_controller_policy = require_proven_live_controller_policy(values)

    # A direct/reloadable adapter invocation may follow a user-attached game
    # whose previous durable context names an old PID. Refresh that binding
    # before the first telemetry/render gate, without launching the game.
    lifecycle_attachment = _refresh_live_process_identity(service, lifecycle)

    config_values = {
        name: values[name]
        for name in (
            "route_mode",
            "controller_mode",
            "sprint",
            "continuous_fast_follow",
            "max_telemetry_age_seconds",
            "settled_gameplay_timeout_seconds",
            "control_period_seconds",
            "final_arrival_radius_m",
            "waypoint_vertical_tolerance_m",
            "start_max_snap_m",
            "start_max_vertical_m",
            "destination_min_distance_m",
            "destination_max_distance_m",
            "multi_polygon_min_distance_m",
            "multi_polygon_max_distance_m",
            "multi_tile_min_distance_m",
            "multi_tile_max_distance_m",
            "multi_tile_max_crossings",
            "multi_tile_absolute_max_cycles",
            "cached_topology_max_expansions",
            "cached_topology_max_waypoints",
            "cached_topology_absolute_max_cycles",
            "cached_topology_wall_clock_seconds",
            "destination_edge_clearance_m",
            "normal_stick_magnitude",
            "slow_stick_magnitude",
            "minimum_effective_stick_magnitude",
            "approach_stick_magnitude",
            "final_stick_magnitude",
            "final_slow_distance_m",
            "final_control_distance_m",
            "approach_control_period_seconds",
            "final_control_period_seconds",
            "max_cycles",
            "multi_polygon_wall_clock_seconds",
            "post_arrival_samples",
            "require_camera_yaw",
            "camera_follow_enabled",
            "camera_follow_lookahead_m",
            "camera_follow_sprint_lookahead_m",
            "camera_follow_deadband_deg",
            "camera_follow_max_input",
            "camera_follow_gain",
            "camera_follow_settle_hysteresis_deg",
            "camera_follow_smooth",
            "camera_follow_arrival_target",
            "camera_follow_arrival_distance_m",
            "pursuit",
            "routing_objective",
            "portal_corner_inset_m",
            "readiness_recovery_timeout_seconds",
        )
        if name in values
    }
    route_mode = str(values.get("route_mode") or "same_polygon").strip().lower()
    route_target_position = values.get("route_target_position")
    if route_target_position is None:
        route_target_position = values.get("target_position")
    route_target = None
    if route_target_position is not None:
        if not isinstance(route_target_position, dict):
            raise ValueError("route_target_position must contain numeric x, y and z")
        try:
            route_target = Vec3.from_dict(route_target_position)
        except Exception as exc:
            raise ValueError("route_target_position must contain numeric x, y and z") from exc
    if route_mode == "cached_topology" and route_target is None:
        raise ValueError("cached_topology route_mode requires route_target_position")
    config_values["route_mode"] = route_mode
    if live_controller_policy is not None:
        config_values.update(live_controller_policy)
    config_values.setdefault(
        "settled_gameplay_timeout_seconds",
        max(1.0, min(120.0, float(values.get("settle_timeout_seconds", 30.0)))),
    )
    # Scanned ladders, cracks and crawl spaces join islands (gimmick_links.py).
    evidence_path = getattr(service, "traversal_gimmick_evidence_path", None)
    if callable(evidence_path) and config_values.get("traversal_links", True):
        config_values.setdefault("gimmick_evidence_path", evidence_path())
    cache_manifest_path = values.get("topology_cache_manifest_path")
    topology_source = str(values.get("topology_source") or "validated_cache").strip().lower()
    # Real gameplay defaults to the validated static cache.  The explicit
    # native provider remains available for an operator-selected acquisition
    # operation, but a cache miss never falls through to a multi-minute dump.
    use_validated_cache = topology_source != "native_capture" and (
        cache_manifest_path is not None or getattr(service.input, "is_real", False)
    )
    allow_scoped_partial_topology = values.get("allow_scoped_partial_topology", False)
    if type(allow_scoped_partial_topology) is not bool:
        raise ValueError("allow_scoped_partial_topology must be a strict boolean")
    if allow_scoped_partial_topology:
        if os.environ.get("FF7R_RUNTIME_PROFILE") != "FEATURE_VALIDATION":
            raise ValueError("scoped partial topology requires the FEATURE_VALIDATION runtime")
        if route_mode != "cached_topology" or not use_validated_cache:
            raise ValueError("scoped partial topology requires a validated-cache cached_topology route")
    if not use_validated_cache and (not topology_path or not validator_snapshot):
        raise ValueError("topology_path and validator_snapshot are required outside validated-cache routes")
    cached_resources = None
    if use_validated_cache:
        from .validator_prewarm import validated_navigation_resource_key
        cache_resource_key = validated_navigation_resource_key(
            cache_manifest_path or DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
            values.get("native_probe_dll_path") or DEFAULT_TOPOLOGY_PROBE_DLL_PATH,
            values.get("native_probe_sha256"), values.get("validator_source_sha256"),
            values.get("validator_corpus"), allow_scoped_partial_topology,
        )
        cached_resources = getattr(service, "_validated_navigation_resources", None)
        if (cached_resources is not None and cached_resources[0] == cache_resource_key
            and not getattr(cached_resources[1], "_cache_invalidated", False)):
            topology_provider = cached_resources[1]
        else:
            cached_resources = None
            topology_provider = ValidatedTopologyCacheProvider(
                cache_manifest_path or DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
                allow_scoped_partial=allow_scoped_partial_topology,
                native_probe_path=values.get("native_probe_dll_path") or DEFAULT_TOPOLOGY_PROBE_DLL_PATH,
                expected_native_probe_sha256=values.get("native_probe_sha256"),
                expected_validator_source_sha256=values.get("validator_source_sha256"),
            )
    else:
        topology_provider = JsonTopologyProvider(
            topology_path,
            probe_path=probe_path or DEFAULT_TOPOLOGY_PROBE_PATH,
            status_path=values.get("status_path") or DEFAULT_TOPOLOGY_STATUS_PATH,
            max_capture_age_seconds=float(values.get("max_capture_age_seconds", 120.0)),
            max_capture_wait_seconds=float(
                values.get("max_capture_wait_seconds", DEFAULT_MAX_CAPTURE_WAIT_SECONDS)
            ),
            max_pose_delta_m=float(values.get("start_max_snap_m", 0.75)),
            expected_native_probe_sha256=values.get("native_probe_sha256"),
            process_alive=(
                (lambda: bool(getattr(getattr(lifecycle, "environment", None), "game_process_ids")()))
                if callable(getattr(getattr(lifecycle, "environment", None), "game_process_ids", None))
                else None
            ),
        )
    if use_validated_cache:
        independent_validator = cached_resources[2] if cached_resources is not None else IndependentJsonValidator(
            None, values.get("validator_corpus"),
        )
        # Retain parsed immutable topology and the independently built validator
        # across objectives. load_current/bind_topology still verify every file,
        # runtime scope and new route; no movement authorization is retained.
        service._validated_navigation_resources = (cache_resource_key, topology_provider, independent_validator)
        # The topology library prefers a capture covering the destination too.
        try:
            topology_provider.selection_target = route_target
        except AttributeError:
            pass  # immutable test doubles select nothing
        try:
            setattr(independent_validator, "require_topology_binding", True)
        except AttributeError:
            # Test doubles and external validator wrappers may be immutable;
            # the production IndependentJsonValidator carries this attribute.
            pass
    else:
        independent_validator = IndependentJsonValidator(
            validator_snapshot,
            values.get("validator_corpus"),
        )
    artifact_path = values.get("static_obstacle_artifact_path")
    artifact_root = values.get("static_obstacle_artifact_root")
    if artifact_path is None and artifact_root is None:
        artifact_root = Path(
            str(cache_manifest_path or DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH)
        ).resolve().parent / "static-obstacle-companion"
    obstacle_store = getattr(service, "_static_obstacle_store", None)
    if not isinstance(obstacle_store, StaticObstacleStore) or (
        artifact_path is not None
        and Path(str(getattr(obstacle_store, "artifact_path", "") or "")).resolve() != Path(str(artifact_path)).resolve()
    ) or (
        artifact_path is None
        and Path(str(getattr(obstacle_store, "artifact_root", "") or "")).resolve() != Path(str(artifact_root)).resolve()
    ):
        obstacle_store = StaticObstacleStore(artifact_path=artifact_path, artifact_root=artifact_root)
        setattr(service, "_static_obstacle_store", obstacle_store)
    follower_factory = load_route_follower_factory(values.get("follower_module"))
    actuator = ExistingAnalogueActuator(service.input)
    camera_actuator = ExistingHorizontalCameraActuator(service.input)
    render_evidence = AgentServiceRenderEvidenceSource(service)
    sweep_route = getattr(service, "sweep_route", None)
    route_collision_audit = (
        sweep_route
        if route_mode == "cached_topology" and callable(sweep_route) and getattr(service.input, "is_real", False)
        else None
    )
    # With the collision audit, every route re-discovers what is in the way
    # in about a second, so obstacles live only for this route. Persisted
    # ones did more harm: at Kalm 18 records from earlier runs, including
    # party members learned as walls, closed passages to later routes.
    static_obstacles = StaticObstacleOverlay(store=None if route_collision_audit else obstacle_store)
    blocker_memory = None
    gate_registry = None
    if route_collision_audit is not None:
        from .dynamic_gates import GateRegistry
        from .static_obstacles import RecentBlockers
        blocker_memory = getattr(service, "_recent_route_blockers", None)
        if not isinstance(blocker_memory, RecentBlockers):
            blocker_memory = RecentBlockers()
            setattr(service, "_recent_route_blockers", blocker_memory)
        # Automatic doors and gates seen by sweeps, and actors seen to move,
        # outlive one route attempt (and one mission) like the blockers above.
        gate_registry = getattr(service, "_dynamic_gate_registry", None)
        if not isinstance(gate_registry, GateRegistry):
            gate_registry = GateRegistry()
            setattr(service, "_dynamic_gate_registry", gate_registry)

    def build_route_attempt() -> dict[str, Any]:
        """Build and execute one disposable route from current live state."""

        adapter = LiveRouteAdapter(
            AgentServiceTelemetrySource(service, lifecycle),
            topology_provider,
            independent_validator,
            follower_factory,
            actuator,
            camera_actuator=camera_actuator,
            render_evidence=render_evidence,
            config=LiveAdapterConfig(**config_values),
            route_target=route_target,
            # Reuse the service-owned overlay so a confirmed session obstacle
            # survives route replacement; the adapter authorization does not.
            static_obstacles=static_obstacles,
            # Reset the low-rate sidecar query budget for each newly authorized
            # route. It is never polled once per movement cycle.
            lookahead=NavigationSidecarLookahead(maximum_queries_per_plan=2),
            # Check the planned route against the game's collision before
            # moving; the navmesh leaves out individual props.
            route_collision_audit=route_collision_audit,
            blocker_memory=blocker_memory,
            gate_registry=gate_registry,
        )
        settled_readiness = None
        settling_error = None
        if getattr(service.input, "is_real", False):
            try:
                settled_readiness = service.wait_for_settled_gameplay(
                    max(1.0, min(120.0, float(values.get("settle_timeout_seconds", 30.0)))),
                    max_telemetry_age_seconds=adapter.config.max_telemetry_age_seconds,
                )
            except Exception as exc:
                settling_error = {"type": type(exc).__name__, "message": str(exc)}
        preflight = adapter.preflight()
        if settling_error is not None:
            preflight["failures"] = list(preflight.get("failures", [])) + ["gameplay_settling_failed"]
            preflight["reason"] = ";".join(preflight["failures"])
            preflight["movement_authorized"] = False
        run_result = None
        if preflight.get("movement_authorized"):
            run_result = adapter.run(
                dry_run=values["dry_run"],
                max_cycles=int(values["max_cycles"]) if "max_cycles" in values else None,
            )
        return {
            "adapter": adapter,
            "settled_readiness": settled_readiness,
            "settling_error": settling_error,
            "preflight": preflight,
            "run": run_result,
        }

    objective_id = str(
        values.get("navigation_objective_id")
        or values.get("goal_id")
        or values.get("target_id")
        or (f"route-target:{route_target.x:.3f},{route_target.y:.3f},{route_target.z:.3f}" if route_target else "route-objective")
    )
    objective = _run_navigation_objective(
        build_route_attempt,
        dry_run=values["dry_run"],
        objective_id=objective_id,
        target_position=(
            {"x": route_target.x, "y": route_target.y, "z": route_target.z}
            if route_target is not None else None
        ),
        max_recovery_attempts=int(values.get("max_recovery_attempts", MAX_NAVIGATION_OBJECTIVE_RECOVERY_ATTEMPTS)),
    )
    adapter = objective["final_adapter"]
    preflight = objective["final_preflight"]
    run_result = objective["final_run"]
    first_attempt = objective["route_attempts"][0] if objective["route_attempts"] else {}
    settled_readiness = first_attempt.get("settled_readiness")
    settling_error = first_attempt.get("settling_error")
    adapter_config = getattr(adapter, "config", None)
    recorded_config = {}
    effective_controller_policy = None
    if adapter_config is not None:
        recorded_config = {
            "control_period_seconds": adapter_config.control_period_seconds,
            "normal_stick_magnitude": adapter_config.normal_stick_magnitude,
            "controller_mode": adapter_config.controller_mode,
            "sprint": adapter_config.sprint,
            "continuous_fast_follow": adapter_config.continuous_fast_follow,
            "slow_stick_magnitude": adapter_config.slow_stick_magnitude,
            "minimum_effective_stick_magnitude": adapter_config.minimum_effective_stick_magnitude,
            "approach_stick_magnitude": adapter_config.approach_stick_magnitude,
            "final_stick_magnitude": adapter_config.final_stick_magnitude,
            "final_slow_distance_m": adapter_config.final_slow_distance_m,
            "final_control_distance_m": adapter_config.final_control_distance_m,
            "approach_control_period_seconds": adapter_config.approach_control_period_seconds,
            "final_control_period_seconds": adapter_config.final_control_period_seconds,
            "final_arrival_radius_m": adapter_config.final_arrival_radius_m,
            "waypoint_vertical_tolerance_m": adapter_config.waypoint_vertical_tolerance_m,
            "max_telemetry_age_seconds": adapter_config.max_telemetry_age_seconds,
            "preflight_telemetry_wait_seconds": adapter_config.preflight_telemetry_wait_seconds,
            "readiness_recovery_timeout_seconds": adapter_config.readiness_recovery_timeout_seconds,
            "max_cycles": adapter_config.max_cycles,
            "route_mode": adapter_config.route_mode,
            "multi_polygon_min_distance_m": adapter_config.multi_polygon_min_distance_m,
            "multi_polygon_max_distance_m": adapter_config.multi_polygon_max_distance_m,
            "multi_tile_min_distance_m": adapter_config.multi_tile_min_distance_m,
            "multi_tile_max_distance_m": adapter_config.multi_tile_max_distance_m,
            "multi_tile_max_crossings": adapter_config.multi_tile_max_crossings,
            "multi_tile_absolute_max_cycles": adapter_config.multi_tile_absolute_max_cycles,
            "cached_topology_max_expansions": adapter_config.cached_topology_max_expansions,
            "cached_topology_max_waypoints": adapter_config.cached_topology_max_waypoints,
            "cached_topology_absolute_max_cycles": adapter_config.cached_topology_absolute_max_cycles,
            "cached_topology_wall_clock_seconds": adapter_config.cached_topology_wall_clock_seconds,
            "multi_polygon_wall_clock_seconds": adapter_config.multi_polygon_wall_clock_seconds,
            "camera_follow_enabled": adapter_config.camera_follow_enabled,
            "camera_follow_smooth": adapter_config.camera_follow_smooth,
            "pursuit": adapter_config.pursuit,
            "camera_follow_lookahead_m": adapter_config.camera_follow_lookahead_m,
            "camera_follow_sprint_lookahead_m": adapter_config.camera_follow_sprint_lookahead_m,
            "camera_follow_deadband_deg": adapter_config.camera_follow_deadband_deg,
            "camera_follow_max_input": adapter_config.camera_follow_max_input,
            "camera_follow_gain": adapter_config.camera_follow_gain,
            "camera_follow_settle_hysteresis_deg": adapter_config.camera_follow_settle_hysteresis_deg,
        }
        effective_controller_policy = live_controller_policy_evidence(
            adapter_config=adapter_config,
            follower_config=getattr(adapter, "follower", None) and getattr(adapter.follower, "config", None),
        )
    result = {
        "operation": "run_live_route_adapter",
        "dry_run": values["dry_run"],
        "route_target_position": (
            {"x": route_target.x, "y": route_target.y, "z": route_target.z}
            if route_target is not None else None
        ),
        "adapter_config": recorded_config,
        "controller_policy": effective_controller_policy,
        "lifecycle_attachment": lifecycle_attachment,
        "settled_readiness": settled_readiness,
        "settling_error": settling_error,
        "topology_source": getattr(topology_provider, "source_decision", topology_source),
        "topology_cache_manifest_path": str(cache_manifest_path or DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH)
        if use_validated_cache else None,
        "scoped_partial_topology_opt_in": allow_scoped_partial_topology,
        "caller_topology_path": str(topology_path) if topology_path is not None else None,
        "caller_validator_snapshot": str(validator_snapshot) if validator_snapshot is not None else None,
        "validator_topology_binding": (
            dict(getattr(getattr(adapter, "validator", None), "binding_evidence", {}) or {})
            if use_validated_cache else None
        ),
        # Automatic doors and gates the final route goes through (also in
        # the route plan's diagnostics), known before any movement.
        "dynamic_gates": list((preflight or {}).get("dynamic_gates") or []),
        "preflight": preflight,
        "run": run_result,
        "navigation_objective": {
            "id": objective["objective_id"],
            "state": objective["objective_state"],
            "target_position": objective["objective_target_position"],
            "recovery_attempts": objective["recovery_attempts"],
            "route_attempt_count": len(objective["route_attempts"]),
            "timeline": objective["timeline"],
        },
        "route_attempts": objective["route_attempts"],
    }
    route_test_complete = getattr(service, "production_test_route_completed", None)
    if callable(route_test_complete):
        route_test_complete(result)
    return result


def _validate_shipped_topology_capture_paths(
    values: dict[str, Any], expected_paths: dict[str, Path]
) -> None:
    """Reject output overrides the shipped native request cannot honor."""
    for name, expected_path in expected_paths.items():
        configured_path = values.get(name)
        if configured_path is None or configured_path == "":
            continue
        try:
            actual = Path(configured_path).resolve(strict=False)
            expected = Path(expected_path).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"capture_topology_cache cannot validate {name}; shipped native output is "
                f"{expected_path}"
            ) from exc
        if actual != expected:
            raise ValueError(
                f"capture_topology_cache cannot use custom {name}; shipped native output is "
                f"{expected_path} and the native request protocol has no output-path override"
            )


def _capture_topology_cache(service: Any, lifecycle: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Acquire one explicit native capture and atomically admit its cache.

    This operation deliberately stops after topology loading and independent
    route validation. It never calls the follower or sends a non-neutral
    command. The runtime bridge exposes it asynchronously because the native
    BuildReport path can legitimately occupy the worker for several minutes.
    """
    if not getattr(service.input, "is_real", False):
        raise ValueError("capture_topology_cache requires the real analogue backend")
    from .live_route_adapter import (
        AgentServiceRenderEvidenceSource,
        AgentServiceTelemetrySource,
        DEFAULT_MAX_CAPTURE_WAIT_SECONDS,
        DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
        DEFAULT_TOPOLOGY_PATH,
        DEFAULT_TOPOLOGY_PROBE_PATH,
        DEFAULT_TOPOLOGY_PROBE_DLL_PATH,
        DEFAULT_TOPOLOGY_STATUS_PATH,
        DEFAULT_TOPOLOGY_TIMING_PATH,
        ExistingAnalogueActuator,
        IndependentJsonValidator,
        JsonTopologyProvider,
        LiveAdapterConfig,
        LiveRouteAdapter,
        load_route_follower_factory,
    )
    from .topology_cache import (
        admit_topology_cache,
        build_topology_cache_manifest,
        sha256_file,
        TOPOLOGY_PARSER_SCHEMA_IDENTITY,
        validator_identity,
    )

    # The shipped native probe writes these artifacts to fixed D: paths. Its
    # request marker carries no destination fields, so accepting custom paths
    # would wait on files that the native process can never produce. The cache
    # manifest remains a Python-owned output and may use a custom location.
    _validate_shipped_topology_capture_paths(
        values,
        {
            "topology_path": DEFAULT_TOPOLOGY_PATH,
            "probe_path": DEFAULT_TOPOLOGY_PROBE_PATH,
            "timing_path": DEFAULT_TOPOLOGY_TIMING_PATH,
            "status_path": DEFAULT_TOPOLOGY_STATUS_PATH,
        },
    )

    topology_path = Path(values.get("topology_path") or DEFAULT_TOPOLOGY_PATH)
    validator_snapshot = Path(values.get("validator_snapshot") or topology_path)
    probe_path = Path(values.get("probe_path") or DEFAULT_TOPOLOGY_PROBE_PATH)
    timing_path = Path(values.get("timing_path") or DEFAULT_TOPOLOGY_TIMING_PATH)
    status_path = Path(values.get("status_path") or DEFAULT_TOPOLOGY_STATUS_PATH)
    cache_manifest_path = Path(
        values.get("topology_cache_manifest_path") or DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH
    )
    probe_dll_path = Path(values.get("native_probe_dll_path") or DEFAULT_TOPOLOGY_PROBE_DLL_PATH)
    native_probe_sha256 = str(values.get("native_probe_sha256") or "").upper()
    if not native_probe_sha256:
        native_probe_sha256 = sha256_file(probe_dll_path)
    validation_path = Path(__file__).resolve().parents[4] / "Tools" / "ff7r-navigation-validation" / "navigation_validation.py"
    validator_sha256 = str(values.get("validator_source_sha256") or "").upper() or validator_identity(validation_path)
    parser_schema_identity = TOPOLOGY_PARSER_SCHEMA_IDENTITY

    # An optional destination (e.g. the NPC the capture is for) serves as the
    # last-resort evidence route when no bounded test route fits.
    capture_target = None
    if values.get("route_target_position") is not None:
        try:
            capture_target = Vec3.from_dict(values["route_target_position"])
        except Exception as exc:
            raise ValueError("route_target_position must contain numeric x, y and z") from exc

    attachment = _refresh_live_process_identity(service, lifecycle)
    environment = getattr(lifecycle, "environment", None)
    process_alive = getattr(environment, "game_process_ids", None)
    def target_process_alive() -> bool:
        if not callable(process_alive):
            return True
        try:
            processes = process_alive()
            return any("ff7rebirth_" in str(name).lower() for name in processes.values())
        except Exception:
            return True

    provider = JsonTopologyProvider(
        topology_path,
        probe_path=probe_path,
        timing_path=timing_path,
        status_path=status_path,
        max_capture_wait_seconds=float(
            values.get("capture_wait_seconds", max(600.0, DEFAULT_MAX_CAPTURE_WAIT_SECONDS))
        ),
        max_capture_age_seconds=float(values.get("max_capture_age_seconds", 180.0)),
        expected_native_probe_sha256=native_probe_sha256,
        process_alive=target_process_alive if callable(process_alive) else None,
        capture_request_id=values.get("capture_request_id"),
    )
    adapter = LiveRouteAdapter(
        AgentServiceTelemetrySource(service, lifecycle),
        provider,
        IndependentJsonValidator(validator_snapshot, values.get("validator_corpus")),
        load_route_follower_factory(values.get("follower_module")),
        ExistingAnalogueActuator(service.input),
        render_evidence=AgentServiceRenderEvidenceSource(service),
        config=LiveAdapterConfig(
            settled_gameplay_timeout_seconds=float(values.get("settle_timeout_seconds", 30.0)),
            admission_route_fallback=("ordinary_multi_polygon", "ordinary_multi_tile")
            + (("cached_topology",) if capture_target is not None else ()),
            admission_target_edge_projection=True,
        ),
        route_target=capture_target,
    )
    settled = None
    settle_error = None
    try:
        settled = service.wait_for_settled_gameplay(
            max(1.0, min(120.0, float(values.get("settle_timeout_seconds", 30.0))))
        )
    except Exception as exc:
        settle_error = {"type": type(exc).__name__, "message": str(exc)}
    preflight = adapter.preflight(require_lease=False) if settle_error is None else {
        "movement_authorized": False,
        "failures": ["gameplay_settling_failed"],
        "reason": "gameplay_settling_failed",
    }
    if settle_error is not None or not preflight.get("movement_authorized"):
        return {
            "operation": "capture_topology_cache",
            "capture_request_id": provider.request_id,
            "attachment": attachment,
            "settled_readiness": settled,
            "settling_error": settle_error,
            "preflight": preflight,
            "movement_authorized": False,
            "cache_admitted": False,
            "capture_status_timeline": list(provider.capture_status_timeline),
        }
    observation = adapter._last_observation
    topology = adapter.topology_session
    if observation is None or topology is None or provider.request_id is None:
        return {
            "operation": "capture_topology_cache",
            "capture_request_id": provider.request_id,
            "preflight": preflight,
            "movement_authorized": False,
            "cache_admitted": False,
            "failure": "capture_protocol_evidence_missing",
        }
    provenance = {
        "capture_request_id": provider.request_id,
        "independent_route_validation": True,
        "route_plan_sha256": adapter.authorization.route_plan_sha256 if adapter.authorization else None,
        "movement_sent": False,
        "process_id": observation.process_id,
        "capture_session_id": observation.session_id,
        "executable_sha256": observation.executable_sha256 or "",
        "native_probe_sha256": native_probe_sha256,
        "capture_purpose": "static_topology_cache_seed",
        "native_status_timeline": list(provider.capture_status_timeline),
    }
    manifest = build_topology_cache_manifest(
        topology_path,
        executable_sha256=observation.executable_sha256 or "",
        native_probe_sha256=native_probe_sha256,
        parser_schema_identity=parser_schema_identity,
        world_id=observation.world_id,
        map_id=observation.map_id,
        coordinate_space="recast",
        coordinate_transform_identity="x=-recast.x,y=-recast.z,z=recast.y",
        validator_source_sha256=validator_sha256,
        validation_result=preflight.get("validator") or {},
        provenance=provenance,
        created_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    manifest["capture_request_id"] = provider.request_id
    manifest["capture_status_timeline"] = list(provider.capture_status_timeline)
    admitted = admit_topology_cache(topology_path, cache_manifest_path, manifest)
    # A capture sends no movement; release any held input, but never call the
    # stop operation: that latches the brakes and closes the game-lifetime
    # control session (live 2026-10-14, after every capture).
    neutral_result = None
    release = getattr(getattr(service, "input", None), "release_all", None)
    if callable(release):
        release()
        neutral_result = {"released": True, "control_session": "untouched"}
    return {
        "operation": "capture_topology_cache",
        "capture_request_id": provider.request_id,
        "attachment": attachment,
        "settled_readiness": settled,
        "preflight": preflight,
        "movement_authorized": False,
        "cache_admitted": True,
        "cache_manifest_path": str(cache_manifest_path),
        "cache_manifest": admitted,
        "capture_status_timeline": list(provider.capture_status_timeline),
        "neutral_result": neutral_result,
    }


def _invoke_runtime(
    service: Any,
    lifecycle: Any,
    startup: Any,
    operation: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    operation = str(operation or "").strip()
    values = dict(arguments or {})
    if operation in {"start_game_to_main_menu", "start_and_load_save_slot", "run_start_and_load_save_slot",
                     "load_save_slot", "start_new_game", "run_start_new_game"}:
        release = getattr(getattr(service, "telemetry", None), "release_queen_blood_probe", None)
        if callable(release):
            release()

    # Initiation owns the lifecycle: dialogue-only before Yes, full discovery
    # after its verified confirmation. A menu/blank field feed is not consent
    # to scan for absent card widgets during target resolution.
    initiating = operation in {"initiate_queens_blood_match", "run_initiate_queens_blood_match"}
    if (operation in QUEENS_BLOOD_UI_OPERATIONS and not initiating
            and not callable(getattr(getattr(service, "telemetry", None), "queen_blood_reads", None))):
        # The Queen's Blood probe idles (no UI scans) unless requested; wait
        # for it to wake so the operation does not read an idle snapshot.
        telemetry = getattr(service, "telemetry", None)
        wake_probe = getattr(telemetry, "wake_queen_blood_probe", None)
        request_probe = getattr(telemetry, "request_queen_blood_probe", None)
        if callable(wake_probe):
            wake_probe()
        elif callable(request_probe):
            request_probe()

    if operation in SERVICE_ALIASES:
        return _service_call(service, operation, values)

    if operation == "focus_game_window":
        background_input_enabled = getattr(service, "_background_input_enabled", None)
        if callable(background_input_enabled) and background_input_enabled() is True:
            raise ValueError("focus_game_window is unavailable in background-input mode")
        environment = getattr(lifecycle, "environment", None)
        focus = getattr(environment, "focus_game_window", None)
        if not callable(focus):
            raise ValueError("foreground recovery is unavailable in this worker")
        raw_pid = values.get("pid")
        if "wake_display" in values:
            if not isinstance(values["wake_display"], bool):
                raise ValueError("wake_display must be a boolean")
            return focus(int(raw_pid) if raw_pid is not None else None, wake_display=values["wake_display"])
        return focus(int(raw_pid) if raw_pid is not None else None)

    if operation == "open_map":
        return service.tap("map", int(values.get("milliseconds", 500)))
    if operation == "tap_combat_attack":
        return service.combat_tap("attack", int(values.get("milliseconds", 80)))
    if operation == "run_setup_queens_blood_match":
        from .queen_blood_setup import run_runtime_setup
        return run_runtime_setup(service, lifecycle, startup, values)
    if operation == "run_live_route_adapter":
        return _run_live_route_adapter(service, lifecycle, values)
    if operation == "capture_topology_cache":
        return _capture_topology_cache(service, lifecycle, values)
    if operation == "run_explore_goto":
        # Internal worker side of explore_goto_async; started only by the bridge.
        from .explore_mode import run_explore_goto
        return run_explore_goto(service, lifecycle, values)

    if operation == "open_control_session":
        # The session lasts as long as the game: `seconds` is accepted for
        # older clients and ignored. Without an explicit game_pid it binds to
        # the one gameplay process running, so it still ends with that game.
        game_pid = values.get("game_pid")
        if game_pid is None:
            game_pid = _running_gameplay_pid(service)
        return request_session_open(
            values.get("seconds", 1800),
            game_pid=game_pid,
            client_id=values.get("client_id"),
            model_id=values.get("model_id"),
            reasoning_effort=values.get("reasoning_effort"),
            **_session_scope(service),
        )
    if operation == "heartbeat_control_session":
        # Nothing to renew; this re-declares model and effort. `seconds` is ignored.
        return request_session_heartbeat(
            values.get("seconds", 1800),
            model_id=values.get("model_id"),
            reasoning_effort=values.get("reasoning_effort"),
            **_session_scope(service),
        )
    if operation == "close_control_session":
        # An explicit close latches the stop. A task that only borrowed the
        # session passes its client_id, and then closes nothing it does not own.
        return request_session_close(client_id=values.get("client_id"), **_session_scope(service))
    if operation == "get_control_session_status":
        return SessionLease(**_session_scope(service)).status() or {
            "active": False,
            "reason": "no_session",
        }

    if operation == "get_lifecycle_status":
        return lifecycle.status()
    if operation == "refresh_lifecycle_attachment":
        # Attach-only: rebinds a context whose game process has gone (live
        # 2026-10-05: a restarted game kept the previous day's PID until a
        # route compared it with the window capture), never launches.
        attachment = _refresh_live_process_identity(service, lifecycle)
        return {**lifecycle.status(), "attachment": attachment}
    if operation == "verify_executable_identity":
        from .live_route_adapter import LIVE_EXECUTABLE_SHA256

        health = service.session_health()
        if health.get("gameplay_ready") is not True:
            raise ValueError("executable_identity_requires_settled_gameplay")
        if values:
            raise ValueError("executable_identity_does_not_accept_identity_overrides")
        return lifecycle.verify_executable_identity(LIVE_EXECUTABLE_SHA256)
    if operation == "launch_game":
        return {"ok": True, "context": lifecycle.start()}
    if operation == "close_game":
        disarm = getattr(service, "disarm_working_save_recovery", None)
        if callable(disarm) and getattr(service, "_queen_blood_runs", None) is not None:
            disarm()
        control_session = None
        try:
            control_session = request_session_close(**_session_scope(service))
        except Exception as exc:
            control_session = {"closed": False, "decision": str(exc)}
        result = lifecycle.close_game(
            float(values.get("timeout_seconds", DEFAULT_CLOSE_TIMEOUT_SECONDS)),
            force=bool(values.get("force", True)),
        )
        result["control_session"] = control_session
        if result.get("closed") is True or result.get("outcome") == "closed" or result.get("state") == "closed":
            release = getattr(getattr(service, "telemetry", None), "release_queen_blood_probe", None)
            if callable(release):
                result["probe_released"] = release()
        return result
    if operation == "recover_stale_lifecycle":
        from .control_recovery import recover_stale_lifecycle

        if set(values) - {"expected_control_session"}:
            raise ValueError("stale_lifecycle_recovery_arguments_invalid")
        return recover_stale_lifecycle(service, lifecycle, **values)
    if operation == "wait_for_live_telemetry":
        timeout = max(1.0, min(60.0, float(values.get("timeout_seconds", 30.0))))
        return {"ok": True, "observation": lifecycle.wait_for_fresh_telemetry(timeout)}
    if operation == "wait_for_gameplay_ready":
        timeout = max(1.0, min(120.0, float(values.get("timeout_seconds", 60.0))))
        return {"ok": True, "observation": lifecycle.wait_for_gameplay_ready(timeout)}
    if operation == "release_game_lifecycle":
        lifecycle.release()
        return {"ok": True, "released": True}

    if operation == "start_game_to_main_menu":
        return startup.start_game_to_main_menu(**values)
    if operation == "prepare_benchmark_attempt":
        return startup.prepare_benchmark_attempt(**values)
    if operation == "select_game_start":
        mode = values.pop("mode")
        return startup.select_game_start(mode, **values)
    if operation == "continue_from_last_save":
        return startup.continue_from_last_save(**values)
    if operation == "load_last_save":
        return startup.load_last_save(**values)
    if operation == "start_and_continue_from_last_save":
        return startup.start_and_continue_from_last_save(**values)
    if operation == "run_start_and_continue_from_last_save":
        return startup.start_and_continue_from_last_save_resumable(**values)
    if operation in {"start_and_load_save_slot", "run_start_and_load_save_slot"}:
        return startup.start_and_load_save_slot(**values)
    if operation in {"start_new_game", "run_start_new_game"}:
        return startup.start_new_game(**values)
    if operation == "run_recover_working_save":
        from .working_save_recovery import recover
        return recover(startup, **values)
    if operation == "run_initiate_queens_blood_match":
        return service.initiate_queens_blood_match(**values)
    if operation == "run_finish_conversation":
        if values.pop("_start_talk", False) is True:
            return service.talk_to_npc(**values)
        return service.finish_conversation(**values)
    if operation == "restart_and_continue_from_last_save":
        disarm = getattr(service, "disarm_working_save_recovery", None)
        if callable(disarm) and getattr(service, "_queen_blood_runs", None) is not None:
            disarm()
        return startup.restart_and_continue_from_last_save(**values)

    raise ValueError(f"unsupported runtime operation: {operation}")


def invoke_runtime(service: Any, lifecycle: Any, startup: Any, operation: str,
                   arguments: dict[str, Any] | None = None) -> Any:
    """Keep campaign failure evidence beside the normal authoritative responses."""
    recorder = getattr(service, "record_campaign_runtime_outcome", None)
    diagnostics = getattr(service, "_qb_recorder", None)
    if diagnostics is not None:
        diagnostics.capture("operation_start", {"operation": operation, "arguments": arguments or {}})
    try:
        reads = (getattr(getattr(service, "telemetry", None), "queen_blood_reads", None)
                 if operation in QUEENS_BLOOD_UI_OPERATIONS | {"finish_conversation"} else None)
        mode = "cursor" if operation in {"edit_queens_blood_deck", "read_queens_blood_cards"} else "full"
        context = reads(mode) if callable(reads) else nullcontext()
        with context:
            response = _invoke_runtime(service, lifecycle, startup, operation, arguments)
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.capture("operation_error", {"operation": operation, "error": str(exc),
                                                     "error_type": type(exc).__name__})
        if callable(recorder):
            recorder(operation, dict(arguments or {}), {"ok": False, "reason": str(exc),
                                                       "response": getattr(exc, "response", None)})
        raise
    if callable(recorder) and isinstance(response, dict):
        recorder(operation, dict(arguments or {}), response)
    if diagnostics is not None:
        diagnostics.capture("operation_result", {"operation": operation, "response": response})
    return response
