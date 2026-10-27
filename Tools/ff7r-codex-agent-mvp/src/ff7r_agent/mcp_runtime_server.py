from __future__ import annotations

import atexit
import ctypes
import os
import threading
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from .gameplay_narration import GAMEPLAY_NARRATION_INSTRUCTIONS
from .public_stream_events import SpeechTone

from .runtime_bridge import (
    RuntimeBridgeError,
    RuntimeInfrastructureError,
    RuntimeSupervisor,
)


mcp = FastMCP("ff7r-rebirth-agent-runtime", instructions=GAMEPLAY_NARRATION_INSTRUCTIONS)
_supervisor: RuntimeSupervisor | None = None
_supervisor_lock = threading.Lock()
_transport_shutting_down = False
_TRANSPORT_SHUTDOWN_ERROR = "runtime transport is shutting down"


def _runtime() -> RuntimeSupervisor:
    global _supervisor
    with _supervisor_lock:
        if _transport_shutting_down:
            raise RuntimeBridgeError(_TRANSPORT_SHUTDOWN_ERROR)
        if _supervisor is None:
            _supervisor = RuntimeSupervisor()
        return _supervisor


def _begin_terminal_shutdown() -> RuntimeSupervisor | None:
    """Atomically close the transport gate and detach the current supervisor.

    The module-global gate is deliberately separate from the supervisor's
    restartable lifecycle state.  Once terminal transport shutdown begins,
    no later MCP request can recreate an actionable supervisor in the small
    interval before the host process exits.
    """

    global _supervisor, _transport_shutting_down
    with _supervisor_lock:
        if _transport_shutting_down:
            return None
        _transport_shutting_down = True
        supervisor = _supervisor
        _supervisor = None
        return supervisor


def _close_runtime() -> None:
    # Do not hold the module lock while RuntimeSupervisor.close() acquires its
    # own request lock and waits for worker/broker cleanup.
    supervisor = _begin_terminal_shutdown()
    if supervisor is not None:
        supervisor.close()


atexit.register(_close_runtime)


def _watch_stdio_disconnect() -> None:
    if os.name != "nt":
        return
    try:
        import msvcrt
        kernel32 = ctypes.windll.kernel32
        kernel32.GetFileType.argtypes = [ctypes.c_void_p]
        kernel32.GetFileType.restype = ctypes.c_ulong
        kernel32.PeekNamedPipe.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.PeekNamedPipe.restype = ctypes.c_bool
        kernel32.GetLastError.restype = ctypes.c_ulong
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(0))
        if kernel32.GetFileType(handle) != 0x0003:
            return
    except (AttributeError, OSError, ValueError):
        return
    while True:
        time.sleep(1.0)
        available = ctypes.c_ulong(0)
        if kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            continue
        if kernel32.GetLastError() == 109:
            # Normal MCP loss owns the stable bridge lifetime. atexit is not
            # guaranteed through os._exit, so explicitly neutralise/close.
            try:
                _close_runtime()
            finally:
                os._exit(0)


@mcp.tool()
def agent_call(operation: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke an allow-listed harness operation with its argument object.

    Narration: prefer the dedicated say_on_stream tool (enumerated tones
    and direct speech ok) before each new gameplay action/decision. The legacy
    agent_call("say_on_stream", ...) remains supported but its outer ok only
    means dispatch succeeded: inspect result.ok and previous_delivery. Speak
    once per observed decision; do not resubmit near-identical placement lines.
    Re-observe and write a distinct reaction after a repetitive rejection.
    Keep fresh speech before every Queen's Blood placement/pass and explain
    deck/mulligan choices. Queued does not mean heard. The voice sends no game
    input and works during an async job; stop/release never waits for speech.
    Never narrate development, services, telemetry, logs or private reasoning.
    Recordings: open_control_session must include your exact model_id and
    reasoning_effort (none/minimal/low/medium/high/xhigh/max/ultra). Supply
    both to heartbeat_control_session before input after either changes;
    ordinary heartbeats, and the harness's own reopens within two minutes,
    keep them. Missing declarations record as unverified.
    Control session: open once (open_control_session; `seconds` is ignored).
    It lasts for the life of the game, with no heartbeat needed, until the
    game exits or someone calls stop_all_input or close_control_session,
    which latch the stop. Missions, routes and approaches use the open
    session and leave it open. Status, health and observation carry
    control {state, session_open, latched, reason, since}: anything other
    than state open means input is refused, and why.
    Preflight: observe_game, get_session_health, open_control_session.
    Load/travel: load_save_slot_async (slot), travel_to_map_spot_async (spot,
    execute); poll get_load_save_slot_status/get_travel_to_map_spot_status.
    New game: start_new_game_async (difficulty easy/normal/dynamic, or with
    its combat style as in "classic (easy)"); poll get_start_new_game_status.
    Navigation: resolve_world_target, plan_npc_approach, start_live_route_adapter,
    get_live_route_adapter_status. `npc_approach --execute --pursuit` is a CLI
    module, not an agent_call operation.
    Explore (no objective, or just looking around; preferred over bounded
    move presses): explore_view (radius_m) reads the navmesh and saves a
    top-down PNG round the player with a labelled metre grid and numbered
    candidate destinations, sending no input; open image.path with your image
    reader. Then explore_goto_async (candidate=ID, or x, y[, z] in cm, or px,
    py of that view; view_id; execute=true) walks there with the pursuit
    planner as one job that leaves the control session open; poll
    get_explore_goto_status. Without execute it only validates. It stops with
    combat_active, cutscene_active, dialogue_active or
    field_telemetry_unavailable; capture_game_window first.
    Far goals: explore_goto_async(toward=true, x, y[, z] or objective=true|"sub",
    execute=true, max_legs) walks in legs, capturing the loaded navmesh,
    taking the reachable point nearest the goal in 3D each time. It stops
    on arrival (arrive_m), on a fight or scene (deal with it, call again)
    or with toward_no_progress.
    Ladders, cracks and crawl spaces: scan_traversal_gimmicks (read-only)
    records them, and routes then cross them. explore_goto_async(gimmick=id,
    execute=true) crosses one once and records where it leads, for a passage
    that ends off the navmesh.
    Objectives: get_active_objective (activity_type main_story, the default)
    reads live progress flags and returns the current supported MSQ step plus
    an exact or declared macro destination. It sends no input. Compare its
    objective_key on later calls; reacquire a macro target after travel.
    Ambiguous or unsupported activities/stages return no destination.
    Interaction: read_field_prompt, talk_to_npc, interact_with_entity,
    open_door, terrain_action, and use_field_action(command) for another
    shown interact prompt such as Check (returns the observed outcome).
    Cutscenes: get_cutscene_state; skip_cutscene skips a playing cinema through
    the game's Paused dialog (not_skippable when it does not pause). A watcher
    skips automatically: set_cutscene_autoskip (mode stream, the default:
    while OBS streams or a rehearsal records; always; off) and
    get_cutscene_autoskip_status.
    Tips: a tutorial tips panel pauses the game (observe_game paused with
    in_menu false; read_screen_texts lists BattleTips Txt_Title
    $tutorial_...). A watcher closes it with Cancel, one press per queued tip:
    set_tips_autoclose (mode stream, the default: while OBS streams or a
    rehearsal records; always; off) and get_tips_autoclose_status (recent
    closes with the tip keys dismissed). close_tips (max_tips) closes the ones
    shown now, on demand.
    Combat simulator: beside Chadley (Talk shown) or with his menu open,
    enter_combat_simulator_async (execute, skip_scenes); poll
    get_enter_combat_simulator_status. leave_combat_simulator (close_menu)
    returns to his menu and closes it.
    Gold Saucer: use_guide_moogle (destination zone, execute) at a Guide
    Moogle rides it; `npc_approach --gold-saucer-zone` walks to one first,
    and NPC approaches in the Gold Saucer ride one when it saves a walk.
    Dialogue choices: read_dialogue_wheel (no input) lists the wheel's
    question, answers and the highlighted one; choose_dialogue_option
    (answer text or index, execute) moves to it, verifies and confirms.
    Game Over: retry_after_game_over (execute) confirms the preselected
    Retry from Last Battle and checks the screen closed.
    Any actor: scan_nearby_actors (radius_m up to 60, contains words) lists
    every actor near Cloud with class, name and position, no input.
    Grab prompts (e.g. the Mt. Nibel air purifier): grab_move holds Grab
    (Ctrl) for one pull, moving x, y (camera-relative) for milliseconds, or
    segments [[x, y, ms], ...]; only while a *_catch prompt shows. It reports
    how far Cloud moved.
    Valves: use_field_action(command="valve_catch") grabs the valve;
    turn_valve (milliseconds, handles, cycles) uses bounded Grab and Sprint
    holds and reports the remaining on-screen Hold markers.
    Field menu (outside fights): read_field_menu (no input). field_menu_async
    with action cast_spell (spell, caster, target, until_hp) heals through Main
    Menu > Spells; action use_item (item, target, until_hp | until_mp | count)
    goes through Inventory > Items; poll get_field_menu_status. The first cast
    or use measures it, the rest follow back to back.
    Loadout and Combat Settings changes (#410): field_menu_async action
    change_loadout (change, member, execute, close_menu) with change
    equipment (category weapon/armor/accessory, item "$Item_..." text key,
    retain_upgrade_setting), materia (row weapon/armor, slot, copy_id from
    read_materia_inventory, take_from when another member holds the copy),
    weapon_skill (slot, skill "$menu_WeaponUpgrade_Core_name_..." key,
    disable_auto_upgrade), shortcut (slot 1-4, category abilities/spells/
    items/limit_breaks, entry text key) or party (member out of the
    frontline, member_in from the backline). Each step is read back on screen
    before the one Confirm; Yes/No prompts are answered only as asked, with
    the highlight read back. verified needs the screen result and a save-data
    diff with only the change's own fields. execute=false returns the
    member's loadout and the party from the save data, no input. The menu is
    left open on the result. Poll get_field_menu_status.
    Equipment/Materia evidence: read_loadout_menu (raw, screen equipment/
    battle_setting/member_change,
    stage member/equipment_category/equipment_inventory/materia_inventory/
    materia_weapon_slot/materia_armor_slot, wait_seconds 0-3).
    inspect_properties/include_choices are explicit single-capture diagnostics,
    never combined with waiting; they do not establish owned-copy identity.
    The reader captures existing equipment-menu evidence without input. Its
    normalized_loadout is unavailable until live bindings are verified.
    field_menu_async action scan_loadout_choices (stage, execute, max_choices,
    timeout_seconds) inspects virtualized choices using bounded Up/Down only;
    it requires that list already open, preserves partial evidence, and leaves
    the menu/control session open. Poll get_field_menu_status.
    preview_loadout_change (snapshot, change, expected_snapshot_sha256) is a
    data-only preview from supplied normalized state, never an execution or
    game outcome. switch_combat_character (member, execute) changes actual
    combat control with typed actor readback, separately from commanding an
    ally. It refuses Tactical Mode, uses at most three Up pulses within eight
    seconds and never replays an uncertain/stalled input. Native combat-switch
    acceptance is pending.
    Climbing walls: climb_move (x, y camera-relative, milliseconds) pushes
    the stick while Cloud hangs on a wall (off the ground and still); walk
    into the yellow holds to start the climb.
    Battle menu: battle_menu_tap (battle_command_menu, up, down, left/right =
    Q/E to switch the commanded member, confirm, cancel) presses the command
    menu only while a fight is in progress; read_battle_command_menu (no
    input) lists it with the highlight, the commanded member and each
    member's HP/MP; use_battle_command (path e.g. ["Items", "Mega-Potion"],
    execute, member="Sephiroth", target) issues an ATB command by name for
    that member, verifying each highlight before Confirm;
    hold_on_enemy_skill=true sends nothing while an enemy attack is telegraphed
    (enemy_attack_incoming), since an interrupted cast still spends the ATB;
    take_highlighted=true takes a further list's highlight (["Limit"]).
    Combat autopilot: combat_autopilot_async (seconds, execute, heal_below)
    commands the whole party until the fight ends (revive, heal, Ether, Limit,
    then the attack that has measurably hurt the enemy most); poll
    get_combat_autopilot_status. It stops on victory, Game Over, refused input
    or its time limit, and retains observed decisions locally.
    Conversation: finish_conversation_async (timeout_seconds); poll
    get_finish_conversation_status with job_id to decline a visible Queen's
    Blood prompt or wait for field telemetry to return. Routes and travel stop
    with dialogue_active or field_telemetry_unavailable: capture_game_window first.
    Queen's Blood campaign: get_campaign_status is the persisted 30-opponent
    ledger for the declared model run; get_campaign_next_step returns the routed
    opponent, target_id, travel spot and reason. Match results come from
    finish_queens_blood_match, not caller claims.
    Queen's Blood: initiate_queens_blood_match_async (target_id,
    stop_at_deck_select, decline; a visit succeeds only with outcome=declined),
    setup_queens_blood_match_async combines pursuit approach, interaction,
    verified deck editing and Begin in one job; poll get_setup_queens_blood_match_status.
    Supply target_id, cards (exactly 15), model_id, reasoning_effort, client_id,
    expected_ref, expected_commit and prepared narration (text/tone objects:
    opponent, approach, interaction, deck, begin; edit plus edit_from when
    changing a known deck; travel/arrival_approach if travel is permitted).
    Set allow_travel=false for a nearby opponent. It stops at the identified
    mulligan hand or a specific failure, never chooses a mulligan or move.
    Use unattended async actions to prepare the next decision in short passes
    between status checks; keep watching the job and act only after a fresh
    observation confirms its outcome. For decks, call
    get_queens_blood_preparation_context (opponent) during a route, travel or
    challenge job for owned cards, tactical details, known deck and opponent
    information; choose cards while that job runs. prepare_queens_blood_deck
    (cards, opponent, rationale, optional fallback, expected_snapshot_sha256)
    validates that choice without input. Use the context snapshot_sha256 to
    reject a plan made from stale data. Loads and other jobs refuse these
    reads: reason from previously fetched context, then refresh after completion.
    Recheck with revalidate_queens_blood_prepared_deck
    (candidate, opponent) after travel; then, only at deck select with no input
    job active, use edit_queens_blood_deck_async
    (cards: exactly 15 owned card IDs), mulligan_cards_async (card_indices);
    poll the corresponding get_*_status with job_id. The model still chooses its
    deck, mulligan and every move with observe_queens_blood,
    place_queens_blood_card (hand_index, row, column), pass_queens_blood_turn,
    and finish_queens_blood_match. Stop: stop_all_input, close_control_session.
    Historical readiness advice never grants runtime campaign wins.
    Campaign: get_campaign_status and get_campaign_next_step resume the
    declared driver's assigned model/slot profile. The director owns results,
    attempts, locked/blocked evidence and regional scheduling; the model chooses
    decks and every move. Call next-step again after finishing a match.
    Use *_async for long operations and inspect the nested final response.
    Diagnostic recording is opt-in per run: start_queens_blood_recording(run_id,
    duration_seconds=1800, max_bytes=67108864, sample_hz=10),
    get_queens_blood_recording_status, stop_queens_blood_recording. It adds no
    poller or input and stores bounded JSONL outside Git for offline replay.
    Recording duration_seconds accepts 1-1800 seconds; stop after the result.
    """
    try:
        return _runtime().call(operation, arguments)
    except RuntimeInfrastructureError as exc:
        return exc.as_response()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


@mcp.tool()
def say_on_stream(text: str, tone: SpeechTone | None = None) -> dict[str, Any]:
    """Queue one fresh gameplay line; return speech acceptance directly.

    Allowed tones: neutral, calm, confident, excited, tense, concerned,
    relieved, surprised, frustrated, amused. Omit tone for neutral.
    Base the line on the latest observed game state. One line per action,
    not multiple near-duplicates for the same pending placement.
    Check ok AND previous_delivery before gameplay input. If acceptance
    is missing or refused, pause input: on repetitive, re-observe and use different
    words; on rate_limited, honour retry_after_seconds and recheck state.
    On delivery failure, pause input. Accepted speech is queued, not heard.
    """
    response = agent_call("say_on_stream", {"text": text, "tone": tone})
    # Unlike the generic dispatcher, expose the actual publisher result at
    # the top level; transport success must not look like speech acceptance.
    if response.get("ok") is not True:
        return response
    speech = response.get("result")
    if not isinstance(speech, dict) or type(speech.get("ok")) is not bool:
        return {"ok": False, "reason": "speech_response_invalid", "input_sent": False}
    return speech


@mcp.tool()
def restart_agent_runtime() -> dict[str, Any]:
    """Reload edited agent/service code without restarting the MCP transport."""
    try:
        return _runtime().restart()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


@mcp.tool()
def get_agent_runtime_status() -> dict[str, Any]:
    """Read worker generation/PID and persistent gamepad-broker state."""
    try:
        return _runtime().status()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


@mcp.tool()
def get_runtime_identity() -> dict[str, Any]:
    """Read the stable bridge and reloadable worker source identity."""
    try:
        return _runtime().identity()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


@mcp.tool()
def get_bootstrap_verification() -> dict[str, Any]:
    """Run identity, lifecycle, session-health, and observation checks read-only."""
    try:
        return _runtime().bootstrap_verification()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


@mcp.tool()
def stop_all_input() -> dict[str, Any]:
    """Run the existing fail-closed stop operation through the worker."""
    try:
        return _runtime().call("stop_all_input", {})
    except RuntimeBridgeError as exc:
        return {"ok": False, "error": str(exc)}


def main() -> None:
    # Capture the stable bridge source binding before the transport loop starts
    # so a later checkout advance is distinguishable from the original process
    # revision and can be recovered once at the facade boundary.
    _runtime()
    threading.Thread(target=_watch_stdio_disconnect, daemon=True).start()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
