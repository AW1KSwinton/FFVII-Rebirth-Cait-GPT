from __future__ import annotations

import ctypes
import os
import threading
import time

from mcp.server.fastmcp import FastMCP

from .autonomy import AutonomyError, DEFAULT_CLOSE_TIMEOUT_SECONDS
from .factory import create_lifecycle_controller, create_service, create_startup_coordinator
from .gameplay_narration import GAMEPLAY_NARRATION_INSTRUCTIONS
from .public_stream_events import SpeechTone
from .navigation import NavigationDataError
from .service import SafetyError
from .safety import (
    ArmError, SessionLease, WindowsGameEnvironment, request_session_close,
    request_session_heartbeat, request_session_open, single_gameplay_process,
)

mcp = FastMCP("ff7r-rebirth-agent", instructions=GAMEPLAY_NARRATION_INSTRUCTIONS)
service = create_service()
lifecycle = create_lifecycle_controller()
startup = create_startup_coordinator(lifecycle, service)


def _watch_stdio_disconnect() -> None:
    """Exit an orphaned Windows stdio worker after Codex drops its pipe.

    Codex owns the MCP process lifetime.  On some reconnects the child
    process survived after the transport disappeared, leaving several stale
    workers competing for the same runtime files.  Peeking the pipe does not
    consume MCP input; it only detects the broken client end.  Other hosts
    and regular console/stdin handles are left untouched.
    """
    if os.name != "nt":
        return
    try:
        import msvcrt

        kernel32 = ctypes.windll.kernel32
        kernel32.GetFileType.argtypes = [ctypes.c_void_p]
        kernel32.GetFileType.restype = ctypes.c_ulong
        kernel32.PeekNamedPipe.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.PeekNamedPipe.restype = ctypes.c_bool
        kernel32.GetLastError.restype = ctypes.c_ulong
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(0))
        if kernel32.GetFileType(handle) != 0x0003:  # FILE_TYPE_PIPE
            return
    except (AttributeError, OSError, ValueError):
        return
    while True:
        time.sleep(1.0)
        available = ctypes.c_ulong(0)
        if kernel32.PeekNamedPipe(
            handle,
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        ):
            continue
        if kernel32.GetLastError() == 109:  # ERROR_BROKEN_PIPE
            os._exit(0)


@mcp.tool()
def say_on_stream(text: str, tone: SpeechTone | None = None) -> dict:
    """Queue a short, gameplay-only explanation or reaction through the voice.

    Use before each new gameplay action/decision throughout the session.
    Tone is one of neutral, calm, confident, excited, tense, concerned,
    relieved, surprised, frustrated or amused; omit it for neutral.
    One distinct line per observed action. Do not resend a near-duplicate.
    No game input or chat is sent. Inspect ok and previous_delivery;
    queued does not mean heard. Never read development or backend details.
    """
    return service.say_on_stream(text, tone)


@mcp.tool()
def observe_game() -> dict:
    """Read the latest validated game-state snapshot. This tool is read-only."""
    return service.observe()

@mcp.tool()
def observe_queens_blood(infer_current_hand_is_mulligan: bool = False) -> dict:
    """Read the Queen's Blood sidecar; optionally restore inferred mulligan state without input."""
    return service.observe_queens_blood(
        infer_current_hand_is_mulligan=infer_current_hand_is_mulligan
    )


@mcp.tool()
def mulligan_cards(
    card_indices: list[int] | None = None,
    milliseconds: int = 500,
    timeout_seconds: float = 30.0,
    begin_if_needed: bool = False,
    only_card_name: str | None = None,
    max_selection_retries: int = 5,
    selection_milliseconds: int = 200,
    mark_milliseconds: int = 500,
    infer_current_hand_is_mulligan: bool = False,
    submit_play: bool = True,
) -> dict:
    """Replace opening cards by slots or by an exact resolved card name.

    Each requested slot gets one selection attempt plus up to five bounded
    retries (six attempts total). Every attempt re-reads the hovered slot
    before navigating. Navigation key presses default to 200 ms; marker
    clicks default to 1000 ms; Play and optional Begin Match inputs use
    ``milliseconds``. Set ``submit_play=False`` to leave the verified marker
    selection on the mulligan screen. ``infer_current_hand_is_mulligan``
    resumes an already-started mulligan after a worker restart.
    """
    return service.mulligan_cards(
        card_indices=card_indices,
        milliseconds=milliseconds,
        timeout_seconds=timeout_seconds,
        begin_if_needed=begin_if_needed,
        only_card_name=only_card_name,
        max_selection_retries=max_selection_retries,
        selection_milliseconds=selection_milliseconds,
        mark_milliseconds=mark_milliseconds,
        infer_current_hand_is_mulligan=infer_current_hand_is_mulligan,
        submit_play=submit_play,
    )


@mcp.tool()
def select_queens_blood_cards(
    card_indices: list[int],
    timeout_seconds: float = 30.0,
    max_selection_retries: int = 5,
    selection_milliseconds: int = 200,
) -> dict:
    """Navigate to and verify Queen's Blood hand cards by slot.

    This sends only bounded directional input. Mulligan marking and board
    placement are separate action-specific operations.
    """
    return service.select_queens_blood_cards(
        card_indices=card_indices,
        timeout_seconds=timeout_seconds,
        max_selection_retries=max_selection_retries,
        selection_milliseconds=selection_milliseconds,
    )


@mcp.tool()
def play_queens_blood_turn(opponent_passed: bool = False, allow_automated_decisions: bool = False) -> dict:
    """DEPRECATED - do not use to play. Lets the built-in algorithm choose a turn.

    Play Queen's Blood with your own decisions: read observe_queens_blood,
    choose the move yourself, then call place_queens_blood_card or
    pass_queens_blood_turn. Refused unless the owner explicitly asked for
    automated play (allow_automated_decisions=True).
    """
    return service.play_queens_blood_turn(opponent_passed, allow_automated_decisions)


@mcp.tool()
def play_queens_blood(
    timeout_seconds: float = 1800.0,
    max_actions: int = 60,
    mulligan: bool = True,
    allow_automated_decisions: bool = False,
) -> dict:
    """DEPRECATED - do not use to play. Lets the built-in algorithm play a whole match.

    Play Queen's Blood with your own decisions: choose the deck, mulligan and
    every move yourself (mulligan_cards, place_queens_blood_card,
    pass_queens_blood_turn, finish_queens_blood_match). Refused unless the
    owner explicitly asked for automated play (allow_automated_decisions=True).
    """
    return service.play_queens_blood_match(
        timeout_seconds, max_actions, mulligan, allow_automated_decisions=allow_automated_decisions,
    )


@mcp.tool()
def capture_game_window() -> dict:
    """Capture only the foreground FFVII Rebirth window as visual evidence."""
    try:
        return service.capture_game_window()
    except AutonomyError as exc:
        return {"ok": False, "decision": str(exc)}
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def get_navigation_context(
    target_id: str | None = None,
    target_kind: str | None = None,
) -> dict:
    """Read target-relative surroundings, camera data, and known-world routes."""
    return service.navigation_context(target_id, target_kind)


@mcp.tool()
def remember_landmark(
    landmark_id: str,
    label: str,
    kind: str = "landmark",
    target_id: str | None = None,
    evidence_ref: str | None = None,
) -> dict:
    """Persist the current position or a currently observed target as a landmark."""
    try:
        return service.remember_landmark(
            landmark_id, label, kind, target_id, evidence_ref
        )
    except (NavigationDataError, OSError, SafetyError) as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def get_agent_status() -> dict:
    """Read safety and backend status. This tool is read-only."""
    return service.status()


@mcp.tool()
def get_input_status() -> dict:
    """Read Stage 4A input safety status. This tool cannot arm or unlatch input."""
    return service.input_status()


@mcp.tool()
def get_session_health() -> dict:
    """Readiness diagnosis without sending input or changing game state."""
    return service.session_health()


@mcp.tool()
def open_control_session(seconds: int = 1800, game_pid: int | None = None,
                         model_id: str | None = None, reasoning_effort: str | None = None) -> dict:
    """Open the control session. It lasts for the life of the game (the
    game_pid given, else the one gameplay process running) until the game
    exits or the session is closed or stopped; ``seconds`` is ignored. Declare
    your exact model_id and reasoning_effort for automatic recording filenames
    and YouTube attribution."""
    try:
        if game_pid is None:
            game_pid = single_gameplay_process(WindowsGameEnvironment().game_process_ids())
        return request_session_open(seconds, game_pid=game_pid, model_id=model_id, reasoning_effort=reasoning_effort)
    except ArmError as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def heartbeat_control_session(seconds: int = 1800, model_id: str | None = None,
                              reasoning_effort: str | None = None) -> dict:
    """Re-declare the recording attribution; nothing needs renewing and
    ``seconds`` is ignored. Supply model_id and reasoning_effort together when
    either changes; omitted values retain the existing recording attribution."""
    try:
        return request_session_heartbeat(seconds, model_id=model_id, reasoning_effort=reasoning_effort)
    except ArmError as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def close_control_session(client_id: str | None = None) -> dict:
    """Close the control session and latch all input safety brokers. A task
    that only borrowed the session passes its client_id and closes nothing
    it does not own (and latches nothing)."""
    try:
        return request_session_close(client_id=client_id)
    except ArmError as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def get_control_session_status() -> dict:
    """Read the current control session without changing it."""
    try:
        return SessionLease().status() or {"active": False, "reason": "no_session"}
    except ArmError as exc:
        return {"active": False, "decision": str(exc)}


@mcp.tool()
def get_lifecycle_status() -> dict:
    """Read the controller lease, configured identity, and run context."""
    return lifecycle.status()


@mcp.tool()
def launch_game() -> dict:
    """Launch the configured game executable under the controller lease."""
    try:
        return {"ok": True, "context": lifecycle.start()}
    except AutonomyError as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def close_game(timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS, force: bool = True) -> dict:
    """Close Rebirth (graceful, then force-terminate if ``force``) and release the lease."""
    control_session: dict | None = None
    try:
        try:
            control_session = request_session_close()
        except ArmError as exc:
            # A stale/missing control-session record must not prevent the
            # lifecycle closer from cleaning the exact game run.  Return its
            # diagnostic alongside the close result.
            control_session = {"closed": False, "decision": str(exc)}
        result = lifecycle.close_game(timeout_seconds, force=force)
        result["control_session"] = control_session
        return result
    except AutonomyError as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def recover_stale_lifecycle(expected_control_session: dict | None = None) -> dict:
    """Archive stale lifecycle evidence; optionally recover an unchanged legacy
    control record after independent lifecycle, neutral-input and exit checks."""
    from .control_recovery import recover_stale_lifecycle as recover
    try:
        return recover(service, lifecycle, expected_control_session)
    except (AutonomyError, ArmError) as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def wait_for_live_telemetry(timeout_seconds: float = 30.0) -> dict:
    """Wait for fresh, advancing telemetry from the controller-owned run."""
    try:
        bounded_timeout = max(1.0, min(60.0, float(timeout_seconds)))
        return {"ok": True, "observation": lifecycle.wait_for_fresh_telemetry(bounded_timeout)}
    except AutonomyError as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def wait_for_gameplay_ready(timeout_seconds: float = 60.0) -> dict:
    """Wait until fresh advancing telemetry contains active loaded gameplay."""
    try:
        bounded_timeout = max(1.0, min(120.0, float(timeout_seconds)))
        return {"ok": True, "observation": lifecycle.wait_for_gameplay_ready(bounded_timeout)}
    except AutonomyError as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def release_game_lifecycle() -> dict:
    """Release the controller lease only after the owned game has exited."""
    try:
        lifecycle.release()
        return {"ok": True, "released": True}
    except AutonomyError as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def start_game_to_main_menu(
    session_seconds: int = 1800,
    launch_timeout_seconds: float = 60.0,
    interactive_timeout_seconds: float = 45.0,
    dismiss_attached_title_prompt: bool = True,
) -> dict:
    """Phase 1: launch/attach Rebirth and stop at the title-cleared menu."""
    return startup.start_game_to_main_menu(
        session_seconds=session_seconds,
        launch_timeout_seconds=launch_timeout_seconds,
        interactive_timeout_seconds=interactive_timeout_seconds,
        dismiss_attached_title_prompt=dismiss_attached_title_prompt,
    )


@mcp.tool()
def select_game_start(
    mode: str,
    menu_actions: list[str] | None = None,
    session_seconds: int = 1800,
    selection_timeout_seconds: float = 60.0,
    interactive_timeout_seconds: float = 45.0,
    expect_gameplay_ready: bool | None = None,
    confirmation_presses: int | None = None,
) -> dict:
    """Phase 2: select Continue, another save, or New Game after phase 1."""
    return startup.select_game_start(
        mode,
        menu_actions=menu_actions,
        session_seconds=session_seconds,
        selection_timeout_seconds=selection_timeout_seconds,
        interactive_timeout_seconds=interactive_timeout_seconds,
        expect_gameplay_ready=expect_gameplay_ready,
        confirmation_presses=confirmation_presses,
    )


@mcp.tool()
def continue_from_last_save(
    session_seconds: int = 1800,
    selection_timeout_seconds: float = 60.0,
    interactive_timeout_seconds: float = 45.0,
) -> dict:
    """Phase 2 default path: Continue, then Resume/Yes, then gameplay."""
    return startup.continue_from_last_save(
        session_seconds=session_seconds,
        selection_timeout_seconds=selection_timeout_seconds,
        interactive_timeout_seconds=interactive_timeout_seconds,
    )


@mcp.tool()
def load_last_save(
    menu_actions: list[str] | None = None,
    session_seconds: int = 1800,
    selection_timeout_seconds: float = 60.0,
    interactive_timeout_seconds: float = 45.0,
    confirmation_presses: int = 1,
) -> dict:
    """Load the latest save from active gameplay through the in-game menu."""
    return startup.load_last_save(
        menu_actions=menu_actions,
        session_seconds=session_seconds,
        selection_timeout_seconds=selection_timeout_seconds,
        interactive_timeout_seconds=interactive_timeout_seconds,
        confirmation_presses=confirmation_presses,
    )


@mcp.tool()
def start_and_continue_from_last_save(
    session_seconds: int = 1800,
    launch_timeout_seconds: float = 60.0,
    selection_timeout_seconds: float = 60.0,
    interactive_timeout_seconds: float = 45.0,
) -> dict:
    """Compose phase 1 startup and the default Continue-from-save path."""
    return startup.start_and_continue_from_last_save(
        session_seconds=session_seconds,
        launch_timeout_seconds=launch_timeout_seconds,
        selection_timeout_seconds=selection_timeout_seconds,
        interactive_timeout_seconds=interactive_timeout_seconds,
    )


@mcp.tool()
def start_and_load_save_slot(
    slot: int | str,
    execute: bool = False,
    skip_if_latest: bool = False,
    session_seconds: int = 1800,
    launch_timeout_seconds: float = 60.0,
    selection_timeout_seconds: float = 60.0,
    interactive_timeout_seconds: float = 45.0,
    readiness_timeout_seconds: float = 120.0,
) -> dict:
    """Start and load a verified save slot from the title, or from an attached field world."""
    return startup.start_and_load_save_slot(
        slot=slot,
        execute=execute,
        skip_if_latest=skip_if_latest,
        session_seconds=session_seconds,
        launch_timeout_seconds=launch_timeout_seconds,
        selection_timeout_seconds=selection_timeout_seconds,
        interactive_timeout_seconds=interactive_timeout_seconds,
        readiness_timeout_seconds=readiness_timeout_seconds,
    )


@mcp.tool()
def start_new_game(
    difficulty: str = "easy",
    combat_style: str | None = None,
    camera: str = "default",
    bonus_check: str = "check",
    skip_cutscenes: bool = True,
    session_seconds: int = 1800,
    launch_timeout_seconds: float = 60.0,
    interactive_timeout_seconds: float = 45.0,
    readiness_timeout_seconds: float = 300.0,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
    client_id: str | None = None,
) -> dict:
    """Start a New Game from the title: difficulty easy/normal/dynamic, or
    with its combat style as in "classic (easy)"; skips the opening cinemas
    and stops when the player has control."""
    return startup.start_new_game(
        model_id=model_id,
        reasoning_effort=reasoning_effort,
        client_id=client_id,
        difficulty=difficulty,
        combat_style=combat_style,
        camera=camera,
        bonus_check=bonus_check,
        skip_cutscenes=skip_cutscenes,
        session_seconds=session_seconds,
        launch_timeout_seconds=launch_timeout_seconds,
        interactive_timeout_seconds=interactive_timeout_seconds,
        readiness_timeout_seconds=readiness_timeout_seconds,
    )


@mcp.tool()
def load_save_slot(slot: int | str, execute: bool = False) -> dict:
    """Load a save slot from the field through Main Menu > Save / Load > Load."""
    return service.load_save_slot(slot, execute=execute)


@mcp.tool()
def restart_and_continue_from_last_save(
    session_seconds: int = 1800,
    close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    launch_timeout_seconds: float = 60.0,
    selection_timeout_seconds: float = 60.0,
    interactive_timeout_seconds: float = 45.0,
) -> dict:
    """Close the owned game, relaunch it, and continue from the latest save."""
    return startup.restart_and_continue_from_last_save(
        session_seconds=session_seconds,
        close_timeout_seconds=close_timeout_seconds,
        launch_timeout_seconds=launch_timeout_seconds,
        selection_timeout_seconds=selection_timeout_seconds,
        interactive_timeout_seconds=interactive_timeout_seconds,
    )


@mcp.tool()
def tap_button(button: str, milliseconds: int = 500) -> dict:
    """Tap one allow-listed abstract game button for a bounded duration."""
    return service.tap(button, milliseconds)


@mcp.tool()
def open_map(milliseconds: int = 500) -> dict:
    """Open the in-game map with one guarded Tab press for route inspection."""
    return service.tap("map", milliseconds)


@mcp.tool()
def observe_map_telemetry() -> dict:
    """Read map identity, world pose, nearby actors, and optional UI map state."""
    return service.map_telemetry()


@mcp.tool()
def observe_map_ui() -> dict:
    """Read the validated, read-only live map overlay telemetry."""
    return service.map_ui_telemetry()


@mcp.tool()
def observe_map_pins(categories: list[str] | None = None, include_hidden: bool = False) -> dict:
    """On demand, read every open-area-map pin (NPCs, quests, fast travel, user pins) with world coordinates."""
    return service.observe_map_pins(categories, include_hidden)


@mcp.tool()
def map_point_to_world(
    x: float | None = None, y: float | None = None, space: str = "map_local",
    z: float | None = None, label: str | None = None,
) -> dict:
    """Convert a map-local point, screen pixel or the map cursor into a world map target."""
    return service.map_point_to_world(x, y, space, z, label)


@mcp.tool()
def get_map_target(target_id: str) -> dict:
    """Return a map target's world coordinates for the route follower."""
    return service.get_map_target(target_id)


@mcp.tool()
def fast_travel_via_map(
    target_id: str, execute: bool = False, max_cursor_steps: int = 12,
    arrival_radius_m: float = 80.0, require_fast_travel_icon: bool = True,
) -> dict:
    """Steer the map cursor onto a fast-travel pin, accept the game's prompt and verify arrival."""
    return service.fast_travel_via_map(
        target_id, execute, max_cursor_steps, arrival_radius_m, require_fast_travel_icon)


@mcp.tool()
def cycle_map_selection(direction: str, milliseconds: int = 500) -> dict:
    """Cycle the current page's selected map item with a guarded Z/X press."""
    return service.map_selection(direction, milliseconds)


@mcp.tool()
def cycle_map_poi(direction: str, milliseconds: int = 500) -> dict:
    """Legacy alias for cycle_map_selection; Z/X meaning depends on the map page."""
    return service.map_selection(direction, milliseconds)


@mcp.tool()
def cycle_map_tab(direction: str, milliseconds: int = 500) -> dict:
    """Cycle the map's top-level tab strip with a guarded Q/E press."""
    return service.map_tab(direction, milliseconds)


@mcp.tool()
def toggle_map_layer(milliseconds: int = 500) -> dict:
    """Toggle the displayed region/world map level with a guarded F3 press."""
    return service.toggle_map_layer(milliseconds)


@mcp.tool()
def toggle_map_era(milliseconds: int = 500) -> dict:
    """Toggle a map's present/past layer where the game exposes it with R."""
    return service.toggle_map_era(milliseconds)


@mcp.tool()
def view_map_corresponding(milliseconds: int = 500) -> dict:
    """Open the selected map item's corresponding map with a guarded G press."""
    return service.view_map_corresponding(milliseconds)


@mcp.tool()
def drop_map_pin(milliseconds: int = 500) -> dict:
    """Drop a map pin with a guarded F2 press while the map is open."""
    return service.drop_map_pin(milliseconds)


@mcp.tool()
def recover_session_control(
    milliseconds: int = 500,
    timeout_seconds: float = 3.0,
) -> dict:
    """Clear a stuck pause/menu state with at most two bounded menu toggles."""
    return service.recover_session_control(milliseconds, timeout_seconds)


@mcp.tool()
def probe_forward_movement(milliseconds: int = 1000) -> dict:
    """Hold one bounded adaptive forward pulse and classify displacement."""
    return service.probe_forward_movement(milliseconds)


@mcp.tool()
def tap_vault(milliseconds: int = 120) -> dict:
    """Send one guarded space press to attempt a terrain vault when exploration is stalled."""
    return service.tap_vault(milliseconds)


@mcp.tool()
def travel_forward_chunk(
    maximum_ms: int = 3000,
    target_distance_m: float | None = None,
) -> dict:
    """Use a longer adaptive forward hold, stopping at a measured distance or safety change."""
    return service.travel_forward_chunk(maximum_ms, target_distance_m)


@mcp.tool()
def travel_direction_chunk(
    direction: str,
    maximum_ms: int = 3000,
    target_distance_m: float | None = None,
    sprint: bool = False,
) -> dict:
    """Use adaptive W/S/A/D movement for a bounded cardinal travel chunk."""
    return service.travel_direction_chunk(
        direction, maximum_ms, target_distance_m, sprint=sprint
    )


@mcp.tool()
def travel_sprint_chunk(
    direction: str,
    maximum_ms: int = 3000,
    target_distance_m: float | None = None,
) -> dict:
    """Use a bounded cardinal travel chunk with the configured sprint modifier."""
    return service.travel_sprint_chunk(direction, maximum_ms, target_distance_m)


@mcp.tool()
def travel_vector_chunk(
    x: float,
    y: float,
    maximum_ms: int = 3000,
    target_distance_m: float | None = None,
    sprint: bool = False,
) -> dict:
    """Use adaptive normalized W/S/A/D movement, including bounded diagonals."""
    return service.travel_vector_chunk(
        x, y, maximum_ms, target_distance_m, sprint=sprint
    )


@mcp.tool()
def plan_predictive_movement(
    requested_distance_m: float = 20.0,
    target_bearing_degrees: float | None = None,
    maximum_ms: int = 20000,
    target_id: str | None = None,
) -> dict:
    """Plan a clearance-gated hold toward a verified route segment."""
    try:
        return service.plan_predictive_movement(requested_distance_m, target_bearing_degrees, maximum_ms, target_id=target_id)
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def travel_planned_chunk(
    requested_distance_m: float = 20.0,
    target_bearing_degrees: float | None = None,
    maximum_ms: int = 20000,
    target_id: str | None = None,
) -> dict:
    """Execute one clearance-gated predictive movement hold."""
    try:
        return service.travel_planned_chunk(requested_distance_m, target_bearing_degrees, maximum_ms, target_id=target_id)
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def navigation_metrics() -> dict:
    """Summarize movement progress and recovery metrics from action JSONL."""
    try:
        return service.navigation_metrics()
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def start_chadley_benchmark(route_id: str = "nibelheim_upper_square", benchmark_id: str | None = None, goal_id: str | None = None) -> dict:
    """Start a read-only, run-scoped benchmark for the physical Chadley route."""
    try:
        return service.start_chadley_benchmark(route_id, benchmark_id, goal_id)
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def get_chadley_benchmark_status() -> dict:
    """Read live elapsed-time and navigation metrics for the active Chadley run."""
    try:
        return service.chadley_benchmark_status()
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def finish_chadley_benchmark(outcome: str = "completed", note: str | None = None) -> dict:
    """Persist the active Chadley benchmark and final route metrics."""
    try:
        return service.finish_chadley_benchmark(outcome, note)
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def discard_chadley_benchmark() -> dict:
    """Discard an active run without persisting its route metrics or history."""
    try:
        return service.discard_chadley_benchmark()
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def start_human_traversal_recording(route_id: str = "nibelheim_upper_square", recording_id: str | None = None, goal_id: str | None = None) -> dict:
    """Start a pose-based human traversal recording; this tool sends no input."""
    try:
        return service.start_human_traversal_recording(route_id, recording_id, goal_id)
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def sample_human_traversal_recording(include_screenshot: bool = False, note: str | None = None) -> dict:
    """Append one telemetry sample to the active human traversal recording."""
    try:
        return service.sample_human_traversal_recording(include_screenshot, note)
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def get_human_traversal_recording_status() -> dict:
    """Read the active human recording and its accumulated route metrics."""
    try:
        return service.human_traversal_recording_status()
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def finish_human_traversal_recording(outcome: str = "completed", note: str | None = None) -> dict:
    """Finalize the active human traversal recording and append its history."""
    try:
        return service.finish_human_traversal_recording(outcome, note)
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def run_human_traversal_sampler(interval_seconds: float = 2.0) -> dict:
    """Sample an active human traversal until the recording is finalized."""
    try:
        return service.run_human_traversal_sampler(interval_seconds)
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def tap_combat_attack(milliseconds: int = 80) -> dict:
    """Perform one target-gated attack probe; no arbitrary real button is accepted."""
    return service.combat_tap("attack", milliseconds)


@mcp.tool()
def move_stick(x: float, y: float, milliseconds: int = 250) -> dict:
    """Request bounded movement. Axes are in [-1, 1]; duration is capped."""
    return service.move(x, y, milliseconds)


@mcp.tool()
def move_direction(direction: str, milliseconds: int = 1000) -> dict:
    """Use a semantic forward/back/left/right movement pulse."""
    return service.move_direction(direction, milliseconds)


@mcp.tool()
def look_camera(dx: int, dy: int, milliseconds: int = 80) -> dict:
    """Move the game camera by one bounded relative-mouse pulse."""
    return service.look_camera(dx, dy, milliseconds)


@mcp.tool()
def survey_camera(degrees: float = 60.0, milliseconds: int = 80) -> dict:
    """Turn the camera in bounded pulses and report telemetry after each pulse."""
    return service.survey_camera(degrees, milliseconds)


@mcp.tool()
def plan_navigation_route(
    target_id: str | None = None,
    target_kind: str | None = None,
    map_scale_m_per_pixel: float | None = None,
) -> dict:
    """Plan a route envelope from telemetry and recorded landmarks before acting."""
    try:
        return service.plan_navigation_route(target_id, target_kind, map_scale_m_per_pixel)
    except (NavigationDataError, SafetyError) as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def navigate_next_segment(
    target_id: str | None = None,
    target_kind: str | None = None,
    target_position: dict | None = None,
    route_bearing_degrees: float | None = None,
    route_source: str = "unavailable",
    route_segment_start: dict | None = None,
    route_segment_end: dict | None = None,
    maximum_ms: int = 15000,
    maximum_distance_m: float = 40.0,
    survey: bool = True,
    capture_screenshot: bool = False,
    visual_reviewed: bool = False,
    visual_review: dict | None = None,
    movement_vector: dict | None = None,
    sprint: bool = False,
) -> dict:
    """Fuse live route, clearance, camera, map, and movement evidence for one segment."""
    try:
        return service.navigate_next_segment(
            target_id=target_id,
            target_kind=target_kind,
            target_position=target_position,
            route_bearing_degrees=route_bearing_degrees,
            route_source=route_source,
            route_segment_start=route_segment_start,
            route_segment_end=route_segment_end,
            maximum_ms=maximum_ms,
            maximum_distance_m=maximum_distance_m,
            survey=survey,
            capture_screenshot=capture_screenshot,
            visual_reviewed=visual_reviewed,
            visual_review=visual_review,
            movement_vector=movement_vector,
            sprint=sprint,
        )
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def run_local_navigation_episode(
    target_position: dict | None = None,
    target_id: str | None = None,
    route_bearing_degrees: float | None = None,
    route_source: str = "unavailable",
    maximum_seconds: float = 8.0,
    maximum_distance_m: float = 12.0,
    arrival_radius_m: float = 1.5,
    maximum_cycles: int = 8,
    maximum_segment_ms: int = 2000,
    maximum_segment_distance_m: float = 3.0,
    capture_on_terminal_uncertainty: bool = True,
) -> dict:
    """Run several bounded observe/plan/move/validate cycles locally."""
    try:
        return service.run_local_navigation_episode(
            target_position=target_position,
            target_id=target_id,
            route_bearing_degrees=route_bearing_degrees,
            route_source=route_source,
            maximum_seconds=maximum_seconds,
            maximum_distance_m=maximum_distance_m,
            arrival_radius_m=arrival_radius_m,
            maximum_cycles=maximum_cycles,
            maximum_segment_ms=maximum_segment_ms,
            maximum_segment_distance_m=maximum_segment_distance_m,
            capture_on_terminal_uncertainty=capture_on_terminal_uncertainty,
        )
    except Exception as exc:
        return {"ok": False, "terminal_outcome": "backend_error", "terminal_reason": str(exc)}


@mcp.tool()
def diagnose_navigation_stall(
    intended_direction: str = "forward",
    displacement_m: float | None = None,
    vertical_change_m: float | None = None,
) -> dict:
    """Rank reverse, lateral, vault, survey, and map checks without acting."""
    return service.diagnose_navigation_stall(
        intended_direction, displacement_m, vertical_change_m
    )


@mcp.tool()
def calibrate_camera_horizontal(delta: int) -> dict:
    """Send one manually armed, verified calibrated horizontal camera event."""
    return service.calibrate_camera_horizontal(delta)


@mcp.tool()
def align_camera_to_bearing(
    target_bearing_degrees: float,
    tolerance_degrees: float = 2.0,
    maximum_steps: int = 8,
) -> dict:
    """Aim the camera at a measured route-leg bearing without moving Cloud."""
    try:
        return service.align_camera_to_bearing(
            target_bearing_degrees, tolerance_degrees, maximum_steps
        )
    except Exception as exc:
        return {"ok": False, "decision": str(exc)}


@mcp.tool()
def align_camera_to_detected_bench() -> dict:
    """Aim the camera at the currently detected bench using closed-loop telemetry."""
    return service.align_camera_to_detected_bench()


@mcp.tool()
def stop_all_input() -> dict:
    """Release held input and latch the emergency stop."""
    return service.stop()


@mcp.tool()
def use_nearby_bench() -> dict:
    """Use exactly one currently validated nearby bench; no arbitrary input is accepted."""
    return service.use_nearby_bench()


@mcp.tool()
def interact_with_physical_chadley(
    route_id: str = "nibelheim_upper_square",
    visual_confirmation: str = "visual_nibelheim_upper_square_chadley",
) -> dict:
    """Send one bounded interaction press only after the physical Chadley route is visually confirmed."""
    return service.interact_with_physical_chadley(route_id, visual_confirmation)


@mcp.tool()
def initiate_queens_blood_match(
    target_id: str | None = None,
    target_kind: str = "npc",
    dialogue_confirmations: int = 8,
    confirmation_milliseconds: int = 500,
    begin_milliseconds: int = 500,
    timeout_seconds: float = 30.0,
) -> dict:
    """Interact with an NPC and start Queen's Blood through the default Yes choices."""
    return service.initiate_queens_blood_match(
        target_id=target_id,
        target_kind=target_kind,
        dialogue_confirmations=dialogue_confirmations,
        confirmation_milliseconds=confirmation_milliseconds,
        begin_milliseconds=begin_milliseconds,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def approach_detected_bench(maximum_pulses: int = 1, stop_distance: float = 2.5) -> dict:
    """Approach only the currently detected bench using bounded forward pulses; never interact."""
    return service.approach_detected_bench(maximum_pulses, stop_distance)


@mcp.tool()
def approach_and_interact(
    target_kind: str = "bench",
    interaction_distance: float = 1.5,
    max_steps: int = 8,
) -> dict:
    """Run a small bounded state machine to approach one visible target and interact."""
    return service.approach_and_interact(target_kind, interaction_distance, max_steps)


def main() -> None:
    threading.Thread(target=_watch_stdio_disconnect, daemon=True).start()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
