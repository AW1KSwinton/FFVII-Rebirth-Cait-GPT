from __future__ import annotations

import json
import hashlib
from copy import deepcopy
import re
from collections import Counter
import math
import os
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from typing import Any, Callable

from .public_stream_events import (
    public_event_for_action, match_record_target, rehearsal_is_live,
    stream_is_live,
    write_public_event, write_public_match_result, write_public_match_state,
)
from .gameplay_narration import AgentSpeechPublisher
from .production_events import (ProductionEventSink, build_event, emit_development_outcome,
                                feature_test_context, known_live_stream, read_recording_snapshot,
                                state_dir_from_env, feature_validation_runtime, write_json_atomic)
from .live_narration_events import (LiveNarrationEventSink, action_facts,
                                    narrated_rehearsal_lease_present, offline_recording_context,
                                    queens_blood_exchange_facts, requested_session_mode,
                                    worker_policy_enabled)

from .camera_settling import (
    CAMERA_DELTA_PER_DEGREE, CAMERA_MEANINGFUL_CHANGE_DEGREES, CAMERA_SETTLE_ENVELOPE_DEGREES, CAMERA_SETTLE_DURATION_SECONDS, CAMERA_SETTLE_MIN_ADVANCING_SAMPLES, CAMERA_SETTLE_TIMEOUT_SECONDS,
)
from .capture import CaptureError, capture_foreground_game_window, capture_obs_game_source
from .conversation_state import has_field_telemetry
from .benchmark import BenchmarkError, ChadleyBenchmarkStore
from .evidence_fusion import (
    cross_track_distance_m, fresh_capsule_probe, fuse_direction_evidence, horizontal_distance_m,
    normalise_degrees, world_bearing_degrees,
)
from .input import (
    CARD_GAME_GAMEPAD_BUTTONS, InputBackend, InputError, normalise_axes, validate_duration, validate_look_delta,
)
from .local_navigation import LocalNavigationExecutor, LocalNavigationLimits
from .navigation import (
    KnownWorldStore,
    Landmark,
    NavigationTarget,
    decide_navigation_step,
    diagnose_navigation_stall,
    navigation_candidates,
    plan_route,
    select_navigation_target,
)
from .models import Vec3
from .queen_blood_catalogue import catalogue_card
from .queen_blood_deck import MatchDeck
from .queen_blood_commentary import LANE_NAMES, commentary_cues, exchange_baseline, result_reaction
from .queen_blood_geometry import BoardGeometry as QueenBloodBoardGeometry
from .queen_blood_profile import QueenBloodProfile, _write as write_queen_blood_json, _now as queen_blood_now
from .queen_blood_campaign import QueenBloodCampaign, classify_initiation_failure
from .queen_blood_runs import QueenBloodRuns, process_identity as queen_blood_process_identity
from .queen_blood_opponents import find_opponent as find_queen_blood_opponent
from .queen_blood_ai import (
    choose_action as choose_queen_blood_action,
    BoardModel as QueenBloodBoardModel,
    choose_mulligan as choose_queen_blood_mulligan,
)
from .queen_blood import (
    MULLIGAN_HAND_SIZE, QUEEN_BLOOD_PREMATCH_SCREENS,
    QueenBloodError, QueenBloodMove, QueenBloodState,
    plan_queen_blood_input,
    plan_queen_blood_card_navigation,
    plan_queen_blood_mulligan_input,
    queen_blood_summary,
    identified_card_id, match_identity_gaps,
    lanes_disagree_with_cards,
    reconcile_lane_totals,
)
from .predictive import (
    HeadingBlocker, HeadingCandidate, choose_heading, estimate_velocity,
    motion_sample_from_state, plan_motion_hold, replay_navigation_metrics,
    stopping_buffer_m,
)
from .recording import HumanTraversalRecorder, HumanTraversalSampler, RecordingError
from .qb_recording import MAX_SECONDS as QB_RECORDING_MAX_SECONDS, QueenBloodRecorder
from .safety import (
    LIFETIME_GAME, ArmError, ArmState, BenchArmState, CameraArmState, SessionLease,
    WindowsGameEnvironment, describe_control,
)
from .telemetry import SimulationTelemetry, TelemetryError, _queen_blood_screen_shown
from .tips import TIPS_PANEL_PATH, TipsAutoClose


class SafetyError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details


# "Not read yet" for a lease status that may itself be None (no session).
_UNREAD = object()


QUEEN_BLOOD_NPC_INTERACTION_RADIUS_M = 4.0


def _navigation_corridor_blocked(
    navigation_payload: dict[str, Any],
    direction_evidence: dict[str, Any],
) -> bool:
    """Apply same-sample capsule evidence without weakening fail-closed safety.

    The line trace remains the fallback when a capsule sweep is missing, stale,
    unsupported, or sequence-mismatched.  Only a fresh, same-sequence capsule
    result may reinterpret a line hit as non-blocking ground—or detect a block
    that the ray missed.
    """

    capsule = fresh_capsule_probe(navigation_payload, direction_evidence)
    if capsule is not None:
        if bool(capsule.get("blocking_hit", capsule.get("hit", False))):
            return True
        if (
            not bool(direction_evidence.get("hit", False))
            or bool(direction_evidence.get("walkable_surface_hit", False))
            or direction_evidence.get("surface_class") == "walkable_surface_candidate"
        ):
            return False
        try:
            line_clearance = float(direction_evidence.get("clear_distance_m"))
            capsule_clearance = float(capsule.get("clear_distance_m"))
        except (TypeError, ValueError):
            line_clearance = None
            capsule_clearance = None
        if (
            line_clearance is not None
            and capsule_clearance is not None
            and line_clearance >= capsule_clearance
        ):
            # The ray hit is beyond the character-sized sweep. It is useful
            # environment context, but cannot block a bounded hold that stays
            # within the fresh capsule-confirmed distance.
            return False
    return bool(
        direction_evidence.get("hit", False)
        or direction_evidence.get("blocking_hit", False)
    )


def _navigation_obstacle_within_horizon(
    navigation_payload: dict[str, Any],
    direction_evidence: dict[str, Any],
    horizon_m: float,
) -> bool:
    """Only treat a measured hit as an immediate obstacle when it is near."""
    if not _navigation_corridor_blocked(navigation_payload, direction_evidence):
        return False
    try:
        clear_distance_m = float(direction_evidence.get("clear_distance_m"))
    except (TypeError, ValueError):
        return True
    horizon = max(1.0, float(horizon_m))
    if clear_distance_m <= horizon:
        return True
    capsule = fresh_capsule_probe(navigation_payload, direction_evidence)
    if capsule is None:
        # A distant line-trace hit is context for the next segment, not a
        # reason to invalidate the bounded progress just completed.
        return False
    try:
        capsule_distance_m = float(capsule.get("clear_distance_m"))
    except (TypeError, ValueError):
        return True
    return bool(
        bool(capsule.get("blocking_hit", capsule.get("hit", False)))
        and capsule_distance_m <= horizon
    )


def _verified_corridor_clearance_m(
    capsule_probe: dict[str, Any] | None,
    probe_kind: str,
    line_clearance_m: float | None,
) -> float | None:
    """Return the distance actually covered by walkability evidence.

    A short capsule confirmation must not silently authorize the longer
    distance reported by a cheap line trace. Long movement is bounded by the
    strongest evidence that covers the corridor, while navmesh/swept probes
    may provide their own long-range clearance.
    """
    capsule_confirms_corridor = bool(
        capsule_probe is not None
        and (
            capsule_probe.get(
                "corridor_confirmed", not capsule_probe.get("hit", False)
            ) is True
            or (
                capsule_probe.get("walkable_surface_hit") is True
                and capsule_probe.get("blocking_hit", False) is False
            )
        )
    )
    if capsule_confirms_corridor:
        try:
            value = float(capsule_probe.get("clear_distance_m"))
        except (TypeError, ValueError):
            value = None
        if value is not None and math.isfinite(value):
            return max(0.0, value)
    if any(
        marker in str(probe_kind).lower()
        for marker in ("swept", "walkable_surface", "navmesh_corridor")
    ):
        if line_clearance_m is not None and math.isfinite(line_clearance_m):
            return max(0.0, float(line_clearance_m))
    return None


def _bounded_capsule_clearance_m(
    capsule_probe: dict[str, Any] | None,
    movement_horizon_m: float | None,
) -> float | None:
    """Return fresh capsule clearance that covers one short movement segment.

    A character-sized sweep can legitimately hit a wall at the far end of an
    otherwise open corridor.  For a bounded short hold, that endpoint is
    context rather than an immediate collision.  Unknown or near hits remain
    unverified and therefore fail closed.
    """
    if capsule_probe is None or movement_horizon_m is None:
        return None
    try:
        clearance = float(capsule_probe.get("clear_distance_m"))
        horizon = float(movement_horizon_m)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(clearance) or not math.isfinite(horizon):
        return None
    if clearance <= horizon:
        return None
    return clearance


MAX_APPROACH_HEADING_ERROR_DEGREES = 30.0
POST_PULSE_POLL_SECONDS = 0.05
POST_PULSE_SETTLE_TIMEOUT_SECONDS = 5.0
BENCH_SETTLE_ENVELOPE_METRES = 0.03
BENCH_USABILITY_CONFIRM_SAMPLES = 3
BENCH_USABILITY_CONFIRM_TIMEOUT_SECONDS = 1.0
BENCH_LIFECYCLE_POLL_SECONDS = 0.05
BENCH_LIFECYCLE_OBSERVE_TIMEOUT_SECONDS = 25.0
ADAPTIVE_FORWARD_MAXIMUM_MS = 1000
ADAPTIVE_FORWARD_POLL_SECONDS = 0.025
ADAPTIVE_FORWARD_RELEASE_DISPLACEMENT_METRES = 0.05
ADAPTIVE_TRAVEL_DEFAULT_MS = 3000
ADAPTIVE_TRAVEL_MAXIMUM_MS = 20000
ADAPTIVE_TRAVEL_POLL_SECONDS = 0.05
ADAPTIVE_TRAVEL_MAX_DISTANCE_METRES = 40.0
ADAPTIVE_TRAVEL_COORDINATOR_DEFAULT_MS = 15000
ADAPTIVE_TRAVEL_COORDINATOR_DEFAULT_DISTANCE_METRES = 40.0
DEFAULT_PLANNED_DISTANCE_METRES = 20.0
GAMEPLAY_SETTLE_POLL_SECONDS = 0.1
GAMEPLAY_SETTLE_TIMEOUT_SECONDS = 8.0
# Retained as a compatibility constant for older callers and evidence.  A
# settled proof is no longer invalidated merely because this many seconds
# elapsed; it is maintained by fresh, advancing, identity-matched gameplay
# evidence and cleared only by an actual invalidation.
GAMEPLAY_SETTLE_CACHE_SECONDS = 3.0
# The live sampler publishes roughly every 150 ms but, about once per ten
# samples, leaves a 0.5-0.8 s gap (Kalm ned18/ned19). Polling the proof's own
# last sample while it ages through that gap must not discard the proof: the
# sample is still reported as not live (so nothing may act on it), and only
# the next fresh, advancing, identity-matched sample restores readiness. A gap
# longer than the grace, a different sample, or any other invalidating
# condition still clears the proof and forces the multi-sample gate again.
# Opt-in (feature trial): the established contract re-enters the settled
# gate after any strict freshness expiry. Enable per runtime with
# FF7R_SETTLED_PROOF_STALE_GAP_GRACE_SECONDS (0 < value <= maximum).
SETTLED_PROOF_STALE_GAP_GRACE_ENV = "FF7R_SETTLED_PROOF_STALE_GAP_GRACE_SECONDS"
MAX_SETTLED_PROOF_STALE_GAP_GRACE_SECONDS = 1.0


def _settled_proof_stale_gap_grace(value: float | None) -> float:
    if value is None:
        raw = os.environ.get(SETTLED_PROOF_STALE_GAP_GRACE_ENV, "").strip()
        if not raw:
            return 0.0
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{SETTLED_PROOF_STALE_GAP_GRACE_ENV} must be a number") from exc
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= MAX_SETTLED_PROOF_STALE_GAP_GRACE_SECONDS:
        raise ValueError(
            "settled-proof stale-gap grace must be within "
            f"[0, {MAX_SETTLED_PROOF_STALE_GAP_GRACE_SECONDS}] seconds"
        )
    return value
# A ray can pass between fence bars or over/under terrain that blocks Cloud's
# capsule. Until swept walkability evidence is live, line-trace-only evidence
# may authorize only a short reassessment chunk.
LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES = 3.0
ROUTE_CROSS_TRACK_TOLERANCE_METRES = 8.0
MIN_ROUTE_PROGRESS_METRES = 0.05
MIN_GOAL_PROGRESS_METRES = 0.05
# A real corridor can require temporarily increasing straight-line distance
# from the target. This allowance is only used for an explicitly reviewed
# visual detour and is bounded per coordinator segment.
MAX_VISUAL_DETOUR_GOAL_REGRESSION_METRES = 8.0
# Relative-mouse and calibrated UE4SS camera input use different scales, and
# the live game has shown that the response can vary between camera states.
# These constants are only initial guesses; survey_camera closes the loop from
# advancing yaw telemetry instead of trusting a fixed pixels-per-degree value.
CALIBRATED_SURVEY_DELTA_PER_DEGREE = 7.1
# Exploratory look() is bounded by InputBackend.MAX_LOOK_PIXELS=400. The
# calibrated bench path intentionally retains its separate 500-pixel budget.
CAMERA_SURVEY_MAX_ABS_DELTA = 400
CAMERA_MAX_ABS_DELTA = 500
# One-motion camera turns (align_camera_to_bearing smooth=True).
SMOOTH_SWEEP_DEGREES_PER_SECOND = 120.0  # top speed of a steered camera turn
SMOOTH_SWEEP_MIN_DEGREES_PER_SECOND = 20.0
SMOOTH_SWEEP_GAIN = 2.5  # degrees a second per degree still to turn
SMOOTH_SWEEP_SLICE_MS = 100
SMOOTH_SWEEP_MAX_SECONDS = 4.0
SMOOTH_SWEEP_PIXELS_PER_DEGREE = 32.0
CAMERA_ALIGNMENT_DEADBAND_DEGREES = 0.5
CAMERA_SURVEY_MIN_EFFECT_DEGREES = 1.0
CAMERA_SURVEY_MIN_EFFECT_FRACTION = 0.5
CAMERA_SURVEY_MAX_EFFECT_FRACTION = 1.5
# Rebirth ignored a 120-unit calibrated event during live validation; 400 is
# the smallest reliably observed live pulse and is still measured before any
# follow-up pulse is selected.
CAMERA_SURVEY_INITIAL_ABS_DELTA = 400
# Real-game camera telemetry can arrive in batches. Smaller follow-up pulses
# keep one delayed sample from turning a nominal 60-degree survey into a
# materially larger turn.
CAMERA_SURVEY_FOLLOWUP_MAX_ABS_DELTA = 100
CAMERA_SURVEY_MAX_PULSES = 24
# Live calibrated input is quantised and its response varies with camera
# settling; an 8-degree envelope is still a useful 60-degree surroundings
# sweep while avoiding the previous 15+ degree overshoot.
CAMERA_SURVEY_TARGET_TOLERANCE_DEGREES = 8.0
# A fresh telemetry sample can quantise the final yaw by a fraction of a
# degree. Keep the intentional 8-degree survey envelope, but do not reject a
# result that is only a tiny amount beyond its rounded boundary.
CAMERA_SURVEY_RESULT_EPSILON_DEGREES = 0.5
CAMERA_SURVEY_PULSE_SAFETY_FACTOR = 0.8
CAMERA_SURVEY_PULSE_TIMEOUT_SECONDS = 1.5
CAMERA_SURVEY_MAX_RETRIES = 1
SESSION_RECOVERY_MAX_TOGGLES = 2
SESSION_RECOVERY_POLL_SECONDS = 0.10
SESSION_RECOVERY_TIMEOUT_SECONDS = 3.0
MOVEMENT_PROBE_DEFAULT_MS = 1000
MOVEMENT_PROBE_MAX_MS = 1000
PHYSICAL_CHADLEY_ROUTE = "nibelheim_upper_square"
PHYSICAL_CHADLEY_CONFIRMATION = "visual_nibelheim_upper_square_chadley"
PHYSICAL_CHADLEY_LANDMARK_ID = "nibelheim-upper-square-chadley-20260811"
PHYSICAL_CHADLEY_POSITION = Vec3(-33374.386719, -211948.03125, 3996.835205)
# The envelope is deliberately limited to the upper Nibelheim square reached
# and visually checked during the live route. Coordinates are Unreal centimetres.
PHYSICAL_CHADLEY_POSITION_BOUNDS = {
    "x": (-34600.0, -32700.0),
    "y": (-212700.0, -210600.0),
    "z": (3500.0, 4500.0),
}
TARGET_GATED_COMBAT_BUTTONS = frozenset({"attack"})
QUEEN_BLOOD_CURSOR_SETTLE_TIMEOUT_SECONDS = 4.0
# Placement cursor homing: extra presses at the board edge clamp, so one
# more than the board size in each direction reaches (0, 0) from anywhere.
QUEEN_BLOOD_HOME_BUTTONS = ("up",) * 3 + ("queen_blood_left",) * 5
QUEEN_BLOOD_HOME_GAP_SECONDS = 0.4
QUEEN_BLOOD_MAX_PLACEMENT_HOMES = 3
QUEEN_BLOOD_PAD_PLAY_HOLD_MS = 1500
QUEEN_BLOOD_PRESS_GAP_SECONDS = 0.15
QUEEN_BLOOD_BACKGROUND_MAX_AGE_SECONDS = 4.0
QUEEN_BLOOD_DECK_KEY_MS = 150
# The deck editor's details popup is sampled about once a second.
QUEEN_BLOOD_DECK_SETTLE_SECONDS = 1.3
# A game lane total counts score tokens that spawn one at a time after a
# placement or effect; live it reached the cards one to four seconds later
# (#344).
QUEEN_BLOOD_LANE_SETTLE_SECONDS = 6.0
QUEEN_BLOOD_DECK_AREAS = ("Edit", "Strip", "Player")
QUEEN_BLOOD_DECK_LIST_COLUMNS = 5
# A cell's card counts as read once the popup has shown it this long after
# the cursor arrived (the popup is sampled about once a second).
QUEEN_BLOOD_DECK_READ_SECONDS = 1.5
# The fast deck editor (edit_queens_blood_deck). Inputs are not dropped since
# the telemetry cost was cut, so a move is a burst of presses checked once
# against the cursor events; only card reads wait for the details popup,
# which the probe samples once a second.
QUEEN_BLOOD_GRID_COLUMNS = 11
# The deck screens' main button guide (MainMenu_Card_Top.Txt_Guide).
QUEEN_BLOOD_DECK_GUIDE_SUFFIX = ".MainMenu_Card_Top.WidgetTree.Txt_Guide"
QUEEN_BLOOD_DECK_SELECT_GUIDE = "Change Deck"
QUEEN_BLOOD_DECK_EDITOR_GUIDE = "Jump to Card"
# A tips panel over the deck screens ("Enfeebling Abilities" after winning
# such a card, live 2026-10-06) takes their input; its texts are listed
# only while it is shown. Close is Cancel, held: a 150 ms tap was ignored.
# (Tips over the field are closed by the tips auto-close, tips.py.)
QUEEN_BLOOD_TIPS_PATH = TIPS_PANEL_PATH
QUEEN_BLOOD_TIPS_CLOSE_MS = 400
QUEEN_BLOOD_FAST_KEY_MS = 80
QUEEN_BLOOD_FAST_GAP_SECONDS = 0.1
QUEEN_BLOOD_CURSOR_WAIT_SECONDS = 1.5
QUEEN_BLOOD_POPUP_HOLD_SECONDS = 1.15
QUEEN_BLOOD_EDITOR_OPEN_SECONDS = 1.2
QUEEN_BLOOD_EDITOR_CLOSE_SECONDS = 1.2
QUEEN_BLOOD_UNPARSEABLE_LIMIT_SECONDS = 60.0
# Captures are about 5 MB each and were never removed: live 2026-09-30,
# 3,826 of them (18.6 GB) filled the system drive to 0.1 GB free. Only the
# newest are kept.
SCREENSHOT_KEEP = 300
# Matches are played with the agent's own decisions (owner, 2026-09-29): the
# operations that let queen_blood_ai choose moves are kept for reference and
# refuse unless explicitly overridden.
QUEEN_BLOOD_AUTOMATED_PLAY_DEPRECATED = (
    "{operation} is deprecated: play Queen's Blood with your own decisions. Choose the "
    "mulligan and every move yourself from observe_queens_blood, then use mulligan_cards, "
    "place_queens_blood_card and pass_queens_blood_turn, and finish_queens_blood_match on "
    "the result screen. The simulator and queen_blood_ai are kept for reference only; pass "
    "allow_automated_decisions=true only when the owner explicitly asks for automated play."
)
# Match placement (place_queens_blood_card) works the same way: the hand and
# board moves are bursts, each checked once by waiting for the cursor to
# show its target (live 2026-09-29 a one-press-per-settled-read placement
# took 8-21 s, 14 s typical; the settle alone was 1.5 s a press).
# Held long enough to span a probe stall: live 2026-09-30, with the probe
# averaging 136 ms a call (max 919 ms) mid-match, 80 ms taps and a 150 ms
# Confirm were lost repeatedly, while the older 200-400 ms holds rarely were.
QUEEN_BLOOD_MATCH_KEY_MS = 200
# Skipping a cinema (skip_cutscene): Menu pauses a skippable scene within a
# moment; the Paused dialog then takes Down and Confirm.
CUTSCENE_KEY_MS = 100
# The dialog showed within about 0.5 s live, read by the menu texts (0.2-0.4 s).
CUTSCENE_PAUSE_WAIT_SECONDS = 1.5
CUTSCENE_PAUSE_TITLE = "$menu_pause_0000_0000"
CUTSCENE_PAUSE_SKIP = "$menu_pause_0002_0001"
CUTSCENE_MENU_GAP_SECONDS = 0.25
CUTSCENE_POLL_SECONDS = 0.05
# Automatic skipping (#158): "stream" skips while OBS streams or a rehearsal
# records, "always" whenever a cinema plays, "off" never.
CUTSCENE_AUTOSKIP_MODES = ("off", "stream", "always")
CUTSCENE_WATCH_SECONDS = 0.1
# While the watcher cannot send input (no control session, game not in front)
# it only checks whether that has changed, this often, and says so once.
CUTSCENE_BLOCKED_WATCH_SECONDS = 2.0
CUTSCENE_SKIP_LOG = 20
# A scene whose Menu opened no Paused dialog is tried once more this long
# after, in case it offers Skip only after its opening moments, then waited
# out for the rest of the session.
CUTSCENE_RETRY_SECONDS = 2.0
QUEEN_BLOOD_MATCH_CONFIRM_MS = 300
QUEEN_BLOOD_ARRIVAL_SECONDS = 2.0
# Input-free reconciliation after a Confirm whose outcome is still unknown.
# Opponent placement can outlast the ordinary eight-second result wait (#366).
QUEEN_BLOOD_PLACEMENT_RECONCILE_SECONDS = 12.0
# A host actor found by the name-less scan must stand this close (XY) to the
# catalogue's position for its opponent.
QUEEN_BLOOD_HOST_MATCH_CM = 150.0
# Board presses closer than this lose one (live 2026-09-29: right-right and
# down-down each landed one cell short while the hand took 0.1 s gaps).
QUEEN_BLOOD_BOARD_GAP_SECONDS = 0.3
# Hand presses 0.1 s apart were lost too, on the keyboard (live 2026-09-29:
# four Rights to slot 4 stopped on 3, costing a 5 s re-plan).
QUEEN_BLOOD_HAND_GAP_SECONDS = 0.2
# A press in the same direction as the one before is ignored while the cursor
# is still moving: nearly every press lost live (2026-09-29/30, keyboard, with
# 0.2-0.3 s gaps) was the second of Right-Right or Down-Down, on the hand and
# on the board alike. A repeat waits this long after the previous press.
QUEEN_BLOOD_REPEAT_GAP_SECONDS = 0.55
# Board presses and the place Confirm are ignored while a picked-up card is
# still flying to the board: live, presses 0.8 s after the pick-up Confirm
# (2026-09-29) and a Confirm 1.7 s after it (2026-09-30) were lost, with the
# game already previewing the cell. So nothing is pressed until the held
# card's world location has held still this long (or the limit passes), and
# never sooner than the minimum after the pick-up Confirm.
QUEEN_BLOOD_PICKUP_SETTLE_SECONDS = 0.8
QUEEN_BLOOD_HELD_CARD_STILL_SECONDS = 0.4
QUEEN_BLOOD_HELD_CARD_SETTLE_LIMIT_SECONDS = 4.0
QUEEN_BLOOD_MOVE_ATTEMPTS = 3
# The match loop re-sends the mulligan's Play when the mulligan screen is
# still up this long after it was submitted.
QUEEN_BLOOD_MULLIGAN_RESUBMIT_SECONDS = 15.0
QUEEN_BLOOD_MAX_MULLIGAN_RESUBMITS = 3
# Card-game buttons a match operation may send: hand/board arrows, Confirm,
# Cancel, and the Play/Pass key (tap = mulligan Play, hold = pass).
QUEEN_BLOOD_MATCH_BUTTONS = frozenset({
    "queen_blood_left", "queen_blood_right", "up", "down",
    "confirm", "queen_blood_cancel", "queen_blood_play",
})
MENU_INPUT_BUTTONS = frozenset({"confirm", "cancel", "up", "down", "left", "right", "sub_left", "sub_right", "menu", "map", "map_selection_prev", "map_selection_next", "map_drop_pin", "map_tab_prev", "map_tab_next", "map_layer_toggle", "map_era_toggle", "map_view_corresponding", "map_fast_travel", "save_shortcut", "dialogue_left", "dialogue_right", "battle_command_menu"})
# Battle command menu presses, allowed only in a fight (battle_menu_tap).
# A held-Grab pull: this many segments of up to MAX_ACTION_MS each.
GRAB_MAX_SEGMENTS = 8
# Hanging on a climbing wall: off the ground and this still between two
# samples this far apart (a jump or a fall moves far more).
CLIMB_STILL_INTERVAL_SECONDS = 0.15
CLIMB_STILL_MAX_CM = 5.0
CLIMB_SETTLE_SECONDS = 0.4
# The character state while hanging on a climbing wall (live, Mt. Nibel).
CLIMB_CONTROLLER_STATE = 11
BATTLE_MENU_BUTTONS = frozenset({"battle_command_menu", "up", "down", "confirm", "cancel"})
MAP_INPUT_BUTTONS = frozenset({"map_selection_prev", "map_selection_next", "map_drop_pin", "map_tab_prev", "map_tab_next", "map_layer_toggle", "map_era_toggle", "map_view_corresponding", "map_fast_travel"})
MAP_BUTTON_MINIMUM_MS = 500
STARTUP_VISUAL_LABELS = frozenset({"title_prompt", "main_menu"})
STARTUP_VISUAL_BUTTONS = frozenset({"confirm", "cancel"})
QUEEN_BLOOD_DIALOGUE_STATES = frozenset({"interacting", "dialogue", "menu"})
QUEEN_BLOOD_MATCH_PHASES = frozenset({
    "entering", "dealing", "player_turn", "opponent_turn", "resolving", "result",
})
QUEEN_BLOOD_PREMATCH_ACTIVE_PHASES = frozenset({"entering", "dealing"})
QUEEN_BLOOD_INTERACTION_MILLISECONDS = 1000
DOOR_INTERACTION_MILLISECONDS = 300
# A door's scene is over once Cloud has come this far and then stood still
# (within DOOR_STILL_CM) this long; he first stands still while it swings.
DOOR_SAMPLE_SECONDS = 0.2
DOOR_THROUGH_CM = 50.0
DOOR_STILL_CM = 5.0
DOOR_STILL_SECONDS = 0.6
FIELD_ACTION_INTERACTION_MILLISECONDS = 300
# Interact prompts other than Talk and Open, e.g. "$level_action_check".
FIELD_ACTION_KEY_PREFIX = "$level_action_"
# Consecutive blank field reads (map and Cloud gone) taken as a scene.
FIELD_ACTION_BLANK_SAMPLES = 3


def prune_screenshots(directory: Path, keep: int) -> int:
    """Remove all but the newest ``keep`` harness captures (``ff7r-*.bmp`` /
    ``.png``) in ``directory``; returns how many went. Never raises."""
    try:
        captures = sorted(
            (path for pattern in ("ff7r-*.bmp", "ff7r-*.png") for path in Path(directory).glob(pattern)),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return 0
    removed = 0
    for path in captures[:max(0, len(captures) - keep)]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _door_walk_finished(samples: list[dict[str, Any]]) -> bool:
    """Whether Cloud has walked through an opened door and stopped."""
    if len(samples) < 2:
        return False
    first, last = samples[0]["position"], samples[-1]["position"]
    if math.hypot(last["x"] - first["x"], last["y"] - first["y"]) < DOOR_THROUGH_CM:
        return False
    # From the latest sample at least DOOR_STILL_SECONDS back (live, samples
    # came 0.21 s apart, so none fell exactly 0.6 s back and a door ran to the
    # observe limit with Cloud still for 5 s).
    window_starts = [index for index, sample in enumerate(samples)
                     if samples[-1]["t"] - sample["t"] >= DOOR_STILL_SECONDS - 1e-6]
    if not window_starts:
        return False
    still = samples[window_starts[-1]:]
    return all(math.hypot(sample["position"]["x"] - last["x"], sample["position"]["y"] - last["y"]) <= DOOR_STILL_CM
               for sample in still)
QUEEN_BLOOD_DIALOGUE_SETTLE_SECONDS = 1.0
QUEEN_BLOOD_INTERACTION_RESPONSE_SECONDS = 2.0
# The hand can take a full 20 seconds to appear after a Begin Match action.
# Retry that same bounded F3/Confirm pair at most three times, with a complete
# 20 second observation window after each attempt.
QUEEN_BLOOD_MULLIGAN_GRACE_SECONDS = 65.0
QUEEN_BLOOD_BEGIN_RETRY_SECONDS = 20.0
QUEEN_BLOOD_MAX_BEGIN_ATTEMPTS = 3
QUEEN_BLOOD_MARKER_CONFIRMATION_SECONDS = 5.0
# Send the interaction key once. Live 2026-09-25, the rematch dialogue was
# not visible in telemetry and its "Challenge to a match?" prompt appeared
# ~18 s after the key on this machine; the old retries (every ~4 s while no
# dialogue was observed) landed on that prompt and declined it. The
# post-interaction grace below waits without input and the dialogue
# confirmations accept the prompt; a dropped key fails the attempt and the
# caller retries the whole initiation.
QUEEN_BLOOD_MAX_INTERACTION_ATTEMPTS = 1
# UE4SS can take more than one producer scan to construct the deck-selection
# widget after Turner accepts the interaction.  Keep waiting for the explicit
# pre-match signal before sending dialogue confirmations into that transition.
QUEEN_BLOOD_POST_DIALOGUE_GRACE_SECONDS = 30.0
# Wheel-gated dialogue (probe talk_menu): lines advance on their own, so no
# input is sent until the Yes/No wheel is shown. A conversation that ends
# without the wheel (Turner's chatter after a load) is followed by another
# Talk, at most this many times in one initiation.
QUEEN_BLOOD_WHEEL_WAIT_SECONDS = 45.0
QUEEN_BLOOD_WHEEL_CONFIRM_ATTEMPTS = 3
QUEEN_BLOOD_WHEEL_RESPONSE_SECONDS = 8.0
QUEEN_BLOOD_MAX_TALKS = 4
# Yes was confirmed but the deck screen was never observed (#328).
QUEEN_BLOOD_DECK_AFTER_YES_TIMEOUT = "Yes was confirmed but the deck screen did not appear before the timeout"
# Live, a Talk sent as a conversation ended took over 4.6 s to register.
QUEEN_BLOOD_DIALOGUE_ENDED_SECONDS = 8.0
# Declining waits this long after Talk for the conversation to open (live, a
# rematch prompt appeared ~18 s after the key) before treating Talk as lost.
QUEEN_BLOOD_CONVERSATION_OPEN_SECONDS = 25.0
QUEEN_BLOOD_MAX_DIALOGUE_CONFIRMATIONS = 16
QUEEN_BLOOD_MAX_TIMEOUT_SECONDS = 120.0
QUEEN_BLOOD_MULLIGAN_MAX_TIMEOUT_SECONDS = 120.0
# The Queen's Blood sidecar is written by an independent producer.  A single
# generic sample can therefore miss an otherwise current sidecar during its
# bounded timestamp handoff.  Retry only this narrow read before deciding
# whether begin_if_needed may send F3; this is not a discovery scan.
QUEEN_BLOOD_SIDECAR_RETRY_SECONDS = 2.0


class AgentService:
    def __init__(
        self,
        telemetry: Any,
        input_backend: InputBackend,
        max_telemetry_age: float = 2.0,
        arm_state: ArmState | None = None,
        bench_arm_state: BenchArmState | None = None,
        camera_arm_state: CameraArmState | None = None,
        session_lease: SessionLease | None = None,
        environment: Any | None = None,
        action_log: Path | None = None,
        approach_trace_log: Path | None = None,
        extended_settling_diagnostic: bool = False,
        monotonic: Any = time.monotonic,
        minimum_action_delay: float = 0.5,
        sleeper: Any = time.sleep,
        known_world_store: KnownWorldStore | None = None,
        use_recorded_routes: bool = False,
        screenshot_dir: Path | None = None,
        benchmark_root: Path | None = None,
        human_recording_root: Path | None = None,
        settled_gameplay_samples: int = 1,
        queen_blood_cursor_settle_seconds: float = 0.0,
        queen_blood_opponents_file: Path | None = None,
        queen_blood_background_input: bool = False,
        queen_blood_geometry_file: Path | None = None,
        queen_blood_profile_root: Path | None = None,
        settled_proof_stale_gap_grace_seconds: float | None = None,
        public_stream_event_dir: Path | None = None,
        queen_blood_recording_root: Path | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.telemetry = telemetry
        # 0 disables the grace (the established contract); None reads the env.
        self._settled_proof_stale_gap_grace_seconds = _settled_proof_stale_gap_grace(
            settled_proof_stale_gap_grace_seconds
        )
        self._queen_blood_profile_root = queen_blood_profile_root
        self._queen_blood_runs = QueenBloodRuns(queen_blood_profile_root) if queen_blood_profile_root is not None else None
        self._queen_blood_campaign = (
            QueenBloodCampaign(Path(queen_blood_profile_root))
            if queen_blood_profile_root is not None else None
        )
        self._campaign_recording_error: str | None = None
        self._campaign_speech: dict[str, Any] | None = None
        self._public_stream_event_dir = public_stream_event_dir
        production_state_dir = (Path(public_stream_event_dir).parent if public_stream_event_dir is not None
                                else state_dir_from_env())
        self._production_event_sink = (
            ProductionEventSink(Path(production_state_dir))
            if production_state_dir is not None else None
        )
        self._live_narration_event_sink = (
            LiveNarrationEventSink(Path(production_state_dir))
            if production_state_dir is not None else None
        )
        self._production_qb_event_ids: list[str] = []
        self._production_qb_event_counter = 0
        self._public_stream_last_emitted: dict[str, float] = {}
        self._agent_speech = AgentSpeechPublisher(public_stream_event_dir, receipt_context=self._campaign_speech_context)
        self._cutscene_autoskip = "off"
        self._cutscene_watch_stop = Event()
        self._cutscene_watch_thread: Thread | None = None
        # Scenes refused once (when), and scenes refused twice: waited out.
        self._cutscene_refusals: dict[str, float] = {}
        self._cutscene_unskippable: set[str] = set()
        self._cutscene_skips: list[dict[str, Any]] = []
        # Why the watcher cannot skip right now, and since when; None while it can.
        self._cutscene_blocked: dict[str, Any] | None = None
        # The match in progress (set when a match reaches its mulligan) and
        # the last in-turn board a place/pass/observe returned, for
        # finish_queens_blood_match.
        self._queen_blood_match: dict[str, Any] | None = None
        self._queen_blood_last_turn_payload: dict[str, Any] | None = None
        # The board as our last placement or pass left it, for commentary cues.
        self._queen_blood_commentary_baseline: dict[str, Any] | None = None
        self._queen_blood_commentary_after_pass = False
        # The board our latest placement or pass started from (#357).
        self._queen_blood_pre_move_payload: dict[str, Any] | None = None
        # The board whose lane totals stayed apart from its cards for a whole
        # settle wait (#344): not waited on again.
        self._queen_blood_unsettled_lanes: str | None = None
        # Closes tutorial tip panels on demand and from a watcher (tips.py).
        self._tips = TipsAutoClose(self)
        self._queen_blood_geometry = QueenBloodBoardGeometry(queen_blood_geometry_file)
        self._queen_blood_opponents_file = queen_blood_opponents_file
        self._queen_blood_background_input = bool(queen_blood_background_input)
        self.input = input_backend
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._qb_recorder = QueenBloodRecorder(
            queen_blood_recording_root or Path.home() / "AppData" / "Local" / "ff7r-agent" / "qb-recordings",
            monotonic=monotonic,
            wall_clock=self._wall_clock,
        )
        # Dormant hooks: no thread, files, reads or probe requests until start.
        if self.telemetry is not None:
            self.telemetry.qb_recorder = self._qb_recorder
        self.input.qb_recorder = self._qb_recorder
        self.max_telemetry_age = float(max_telemetry_age)
        self._stopped = False
        self._lock = Lock()
        self._action_lock = Lock()
        self._local_episode_owner: int | None = None
        self._arm = arm_state
        self._bench_arm = bench_arm_state
        self._camera_arm = camera_arm_state
        self._session_lease = session_lease
        self._environment = environment or WindowsGameEnvironment()
        self._action_log = action_log
        self._approach_trace_log = approach_trace_log
        self._extended_settling_diagnostic = extended_settling_diagnostic
        self._monotonic = monotonic
        self._minimum_action_delay = float(minimum_action_delay)
        self._sleeper = sleeper
        try:
            self._settled_gameplay_samples = max(1, min(8, int(settled_gameplay_samples)))
        except (TypeError, ValueError):
            raise ValueError("settled_gameplay_samples must be an integer")
        # Real input can land late on a slow machine; the Queen's Blood hand
        # cursor must hold one value this long before a card is toggled.
        self._queen_blood_cursor_settle_seconds = max(0.0, float(queen_blood_cursor_settle_seconds))
        self._settled_gameplay_identity: tuple[Any, ...] | None = None
        # The identity a settled proof had when only ground contact was lost
        # (a step or bump while running); see _session_health_from_state.
        self._ground_loss_identity: tuple[Any, ...] | None = None
        self._settled_gameplay_sequence: int | None = None
        self._settled_gameplay_at = float("-inf")
        # The proof carries the freshness contract under which it was
        # established.  Generic service readiness normally uses
        # ``max_telemetry_age``; a movement caller may require a stricter
        # bound without changing that global startup policy.
        self._settled_gameplay_freshness_limit: float | None = None
        self._settled_gameplay_source = "reacquisition_required"
        self._settled_gameplay_invalidation_reason: str | None = (
            "initial_multi_sample_proof_required"
        )
        self._warm_session_identity: tuple[Any, ...] | None = None
        self._known_world_store = known_world_store
        # Durable landmarks/routes are design-time evidence by default.  A
        # caller must explicitly opt in before historical waypoints can steer
        # runtime navigation.
        self._use_recorded_routes = bool(use_recorded_routes)
        self._screenshot_dir = screenshot_dir
        self._chadley_benchmark = ChadleyBenchmarkStore(
            benchmark_root or Path.home() / "AppData" / "Local" / "ff7r-agent" / "benchmarks",
            action_log=action_log,
            screenshot_dir=screenshot_dir,
        )
        self._human_recording = HumanTraversalRecorder(
            human_recording_root or Path.home() / "AppData" / "Local" / "ff7r-agent" / "human-recordings",
            action_log=action_log,
        )
        self._last_action_finished = float("-inf")
        self._last_action: dict[str, Any] | None = None
        self._queen_blood_mulligan_inferred_sequence: int | None = None
        self._queen_blood_mulligan_inferred_hand: list[dict[str, Any]] | None = None
        self._queen_blood_mulligan_inferred_basis: str | None = None
        self._queen_blood_turn_one_inferred_sequence: int | None = None
        self._queen_blood_turn_one_inferred_payload: dict[str, Any] | None = None
        self._motion_samples: list[Any] = []
        self._heading_blockers: list[HeadingBlocker] = []
        self._navigation_trace: list[dict[str, Any]] = []
        self._load_navigation_history()

    @staticmethod
    def _normalise_degrees(value: float) -> float:
        return (value + 180.0) % 360.0 - 180.0

    @staticmethod
    def _validate_visual_review(
        visual_review: dict[str, Any] | None,
        selected_bearing_degrees: float | None,
    ) -> tuple[bool, str]:
        """Validate an autonomous review of the aligned navigation frame.

        A boolean flag is not evidence: the review must identify the frame,
        confirm that the corridor is clear and continuous, and explicitly
        rule out a dead end.  The bearing check prevents reusing a review of
        a different camera direction.
        """
        if not isinstance(visual_review, dict):
            return False, "structured visual review is required"
        evidence_path = visual_review.get("evidence_path")
        if not evidence_path or not Path(str(evidence_path)).is_file():
            return False, "visual review evidence_path is missing or not a file"
        if visual_review.get("corridor_clear", visual_review.get("clear")) is not True:
            return False, "visual review did not confirm a clear corridor"
        if visual_review.get("route_continuity") is not True:
            return False, "visual review did not confirm route continuity"
        if visual_review.get("dead_end") is not False:
            return False, "visual review did not explicitly rule out a dead end"
        try:
            reviewed_bearing = float(visual_review["reviewed_bearing_degrees"])
        except (KeyError, TypeError, ValueError):
            return False, "visual review must include reviewed_bearing_degrees"
        try:
            captured_bearing = float(visual_review["camera_bearing_degrees"])
        except (KeyError, TypeError, ValueError):
            return False, "visual review must include camera_bearing_degrees"
        if abs(normalise_degrees(captured_bearing - reviewed_bearing)) > MAX_APPROACH_HEADING_ERROR_DEGREES:
            return False, "visual review frame bearing does not match the reviewed corridor"
        if selected_bearing_degrees is not None:
            error = abs(normalise_degrees(reviewed_bearing - selected_bearing_degrees))
            if error > MAX_APPROACH_HEADING_ERROR_DEGREES:
                return False, "visual review bearing does not match the selected corridor"
        return True, "visual corridor review accepted"

    def _write_approach_trace(self, records: list[dict[str, Any]]) -> None:
        if not self._approach_trace_log or not records:
            return
        self._approach_trace_log.parent.mkdir(parents=True, exist_ok=True)
        with self._approach_trace_log.open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, sort_keys=True) + "\n")

    def _remember_motion_state(self, state: Any) -> None:
        sample = motion_sample_from_state(state)
        if sample is None or (self._motion_samples and sample.sequence is not None and self._motion_samples[-1].sequence == sample.sequence):
            return
        self._motion_samples.append(sample)
        if len(self._motion_samples) > 32:
            del self._motion_samples[:-32]

    def _map_heading_blockers(self, map_id: str | None) -> list[HeadingBlocker]:
        return [item for item in self._heading_blockers if item.map_id is None or item.map_id == map_id]

    def _load_navigation_history(self) -> None:
        """Rehydrate recent no-progress movement evidence from the action log."""
        if self._action_log is None or not self._action_log.is_file():
            return
        try:
            lines = self._action_log.read_text(encoding="utf-8").splitlines()[-128:]
        except OSError:
            return
        for line in lines:
            try:
                record = json.loads(line)
                pre = record.get("pre_position")
                if not isinstance(pre, dict) or float(record.get("horizontal_displacement") or 0.0) >= 0.10:
                    continue
                self._heading_blockers.append(HeadingBlocker(
                    position=Vec3.from_dict(pre),
                    bearing_degrees=float(record["bearing_degrees"]) if record.get("bearing_degrees") is not None else None,
                    direction=str(record.get("direction") or "forward"),
                    map_id=str(record["map_id"]) if record.get("map_id") is not None else None,
                    source="action_log",
                    reason=str(record.get("release_reason") or record.get("outcome") or "no_progress"),
                ))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        self._heading_blockers = self._heading_blockers[-16:]

    def _record_navigation_trace(self, result: dict[str, Any], predictive_plan: dict[str, Any] | None = None) -> None:
        pre, post = result.get("pre_position"), result.get("post_position")
        if not isinstance(pre, dict) or not isinstance(post, dict):
            return
        direction = str(result.get("direction") or "forward")
        bearing = None
        choice = predictive_plan.get("heading_choice") if isinstance(predictive_plan, dict) else None
        if isinstance(choice, dict):
            for item in choice.get("ranked", []):
                if isinstance(item, dict) and item.get("direction") == direction:
                    try:
                        bearing = float(item["bearing_degrees"])
                    except (KeyError, TypeError, ValueError):
                        pass
                    break
        trace = {"pre_position": dict(pre), "post_position": dict(post), "direction": direction, "bearing_degrees": bearing, "horizontal_displacement": float(result.get("horizontal_displacement") or 0.0), "release_reason": result.get("release_reason", result.get("outcome")), "sequence": result.get("post_sequence"), "map_id": result.get("map_id")}
        self._navigation_trace.append(trace)
        self._navigation_trace = self._navigation_trace[-64:]
        if trace["horizontal_displacement"] < 0.10:
            try:
                blocker = HeadingBlocker(position=Vec3.from_dict(pre), bearing_degrees=bearing, direction=direction, map_id=str(result["map_id"]) if result.get("map_id") is not None else None, source="movement_no_progress", reason=str(trace["release_reason"] or "no_progress"))
            except (KeyError, TypeError, ValueError):
                blocker = None
            if blocker is not None:
                self._heading_blockers = (self._heading_blockers + [blocker])[-16:]
                result["heading_blocked_for_retry"] = True
                result["heading_blocker"] = {"position": asdict(blocker.position), "bearing_degrees": blocker.bearing_degrees, "direction": blocker.direction, "map_id": blocker.map_id, "radius_m": blocker.radius_m, "reason": blocker.reason}
        result["navigation_trace_tail"] = list(self._navigation_trace[-8:])

    def _planning_target(self, state: Any, target_id: str | None) -> NavigationTarget | None:
        selected = select_navigation_target(state, target_id=target_id)
        if selected is not None or target_id is None or self._known_world_store is None or not state.map_id:
            return selected
        stored = next(
            (
                item for item in self._runtime_landmarks(state.map_id)
                if item.landmark_id == target_id
            ),
            None,
        )
        return NavigationTarget.from_landmark(stored, state) if stored is not None else None

    def _runtime_landmarks(self, map_id: str | None) -> list[Landmark]:
        """Return named targets, excluding historical positional waypoints."""
        if self._known_world_store is None or not map_id:
            return []
        return [
            item for item in self._known_world_store.list_landmarks(map_id)
            if item.kind.strip().lower() not in {"waypoint", "route_waypoint"}
        ]

    def _route_leg_for_target(
        self,
        state: Any,
        target_id: str | None,
        explicit_bearing: float | None,
    ) -> dict[str, Any]:
        """Resolve the currently applicable route leg from live player state.

        ``plan_route`` projects the current player position onto the known or
        runtime route, so calling this for each local episode cycle advances
        from a completed leg instead of reusing the initial bearing.
        """
        target = self._planning_target(state, target_id)
        if target is None:
            payload = state.navigation if isinstance(state.navigation, dict) else {}
            if explicit_bearing is not None and any(payload.get(key) for key in ("navmesh_path", "walkable_path", "path_points", "occupancy_grid")):
                return {
                    "bearing": float(explicit_bearing),
                    "route_plan": None,
                    "route_source": str(payload.get("path_source") or payload.get("source") or "runtime_path"),
                    "route_segment_start": None,
                    "route_segment_end": None,
                }
            return {
                "bearing": None,
                "route_plan": None,
                "route_source": None,
                "route_segment_start": None,
                "route_segment_end": None,
            }
        routes = (
            self._known_world_store.list_routes(state.map_id)
            if self._use_recorded_routes and self._known_world_store is not None and state.map_id
            else []
        )
        route_plan = plan_route(state, target, routes)
        if route_plan.path_preference == "route_required" or not route_plan.legs:
            return {
                "bearing": None,
                "route_plan": asdict(route_plan),
                "route_source": None,
                "route_segment_start": None,
                "route_segment_end": None,
            }
        leg = route_plan.legs[0]
        try:
            bearing = float(leg["bearing_degrees"])
        except (KeyError, TypeError, ValueError):
            bearing = None
        return {
            "bearing": bearing,
            "route_plan": asdict(route_plan),
            "route_source": route_plan.route_source,
            "route_segment_start": leg.get("from_position"),
            "route_segment_end": leg.get("to_position"),
        }

    def _route_segment_for_target(self, state: Any, target_id: str | None, explicit_bearing: float | None) -> tuple[float | None, dict[str, Any] | None, str | None]:
        resolved = self._route_leg_for_target(state, target_id, explicit_bearing)
        return (
            resolved.get("bearing"),
            resolved.get("route_plan"),
            resolved.get("route_source"),
        )

    @staticmethod
    def _is_permission_read_error(exc: Exception) -> bool:
        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, PermissionError):
                return True
            current = current.__cause__ or current.__context__
        return False

    def status(self) -> dict[str, Any]:
        if getattr(self.input, "is_real", False) and self._arm:
            _, _, stopped = self._arm.snapshot()
            with self._lock:
                self._stopped = stopped
        return {
            "emergency_stop": self._stopped,
            "telemetry_backend": type(self.telemetry).__name__,
            "input_backend": type(self.input).__name__,
            "max_telemetry_age_seconds": self.max_telemetry_age,
            "camera_calibration_available": bool(self._camera_arm),
            "control_session": (
                self._session_lease.status() if self._session_lease is not None else None
            ),
            "control": self.control_state(),
        }

    def control_state(self, session: Any = _UNREAD) -> dict[str, Any] | None:
        """Whether a control session authorises input, and if not, why not.

        ``{state, session_open, latched, reason, since}`` (see
        safety.describe_control).  Reported with the status, health and
        observation so a closed session or a latched stop is seen where the
        agent looks, instead of surfacing as errors from whatever acts next.
        None for a backend that takes no real input.  ``session`` is a lease
        status the caller has just read.
        """
        lease = self._session_lease
        if lease is None or not getattr(self.input, "is_real", False):
            return None
        error: str | None = None
        if session is _UNREAD:
            try:
                session = lease.status()
            except ArmError as exc:
                session, error = None, str(exc)
        if session and session.get("active"):
            return describe_control(session, None, stopped=False)
        stopped, stopped_at = bool(self._stopped), None
        if self._arm is not None:
            try:
                info = getattr(self._arm, "stop_info", None)
                stopped, stopped_at = info() if callable(info) else (self._arm.snapshot()[2], None)
            except Exception:
                pass
        return describe_control(session, lease.last_close(), stopped=stopped,
                                stopped_at=stopped_at, error=error)

    def reconnect_gamepad(self) -> dict[str, Any]:
        """Refresh an optional virtual controller after gameplay is settled."""
        reconnect = getattr(self.input, "reconnect_gamepad", None)
        if not callable(reconnect):
            return {"ok": True, "event": "gamepad_reconnect_not_required"}
        return dict(reconnect())

    def _background_input_enabled(self) -> bool:
        return getattr(self.input, "supports_background_input", False) is True

    def _input_reachable(self) -> bool:
        return self._background_input_enabled() or self._environment.game_is_foreground()

    def input_status(self, state: Any | None = None) -> dict[str, Any]:
        real = bool(getattr(self.input, "is_real", False))
        backend_status = None
        backend_status_fn = getattr(self.input, "backend_status", None)
        if callable(backend_status_fn):
            try:
                backend_status = backend_status_fn()
            except Exception as exc:
                backend_status = {"error": str(exc)}
        if real and self._arm:
            armed, remaining, stopped = self._arm.snapshot()
            with self._lock:
                self._stopped = stopped
        else:
            armed, remaining = False, 0.0
        if real and self._bench_arm:
            bench_armed, bench_remaining, bench_stopped = self._bench_arm.snapshot()
        else:
            bench_armed, bench_remaining, bench_stopped = False, 0.0, True
        if real and self._camera_arm:
            camera_armed, camera_remaining, camera_stopped = self._camera_arm.snapshot()
        else:
            camera_armed, camera_remaining, camera_stopped = False, 0.0, True
        process_exists = self._environment.game_process_exists() if real else None
        foreground = self._environment.game_is_foreground() if real else None
        session_read = self._session_lease is not None
        session = self._session_lease.status() if session_read else None
        telemetry_live = False
        telemetry_age: float | None = None
        running = paused = in_menu = False
        target_clear = False
        try:
            state = state if state is not None else self.telemetry.observe()
            telemetry_age = state.age_seconds()
            telemetry_live = state.source == "simulation" or telemetry_age <= self.max_telemetry_age
            running, paused, in_menu = state.running, state.paused, state.in_menu
            target_clear = state.target is None
        except Exception:
            pass
        return {
            "backend": type(self.input).__name__,
            "backend_status": backend_status,
            "real_input": real,
            "armed": armed,
            "seconds_remaining": remaining,
            "bench_armed": bench_armed,
            "bench_seconds_remaining": bench_remaining,
            "bench_emergency_stop": bench_stopped,
            "camera_armed": camera_armed,
            "camera_seconds_remaining": camera_remaining,
            "camera_emergency_stop": camera_stopped,
            "emergency_stop": self._stopped,
            "game_process_exists": process_exists,
            "foreground_window": foreground,
            "input_reachable": foreground is True or self._background_input_enabled(),
            "telemetry_live": telemetry_live,
            "telemetry_age_seconds": telemetry_age,
            "game_running": bool(running and telemetry_live),
            "loaded_gameplay": bool(
                running and telemetry_live and not paused and not in_menu
            ),
            "gameplay_settled": bool(
                self._settled_gameplay_samples <= 1
                or self._settled_gameplay_proof_present()
            ),
            "paused": paused,
            "in_menu": in_menu,
            "target_clear": target_clear,
            "last_action": self._last_action,
            "control_session": session,
            "control": self.control_state(session) if session_read else None,
        }

    def _settled_gameplay_proof_present(self) -> bool:
        return (
            self._settled_gameplay_samples <= 1
            or (
                self._settled_gameplay_identity is not None
                and self._settled_gameplay_sequence is not None
            )
        )

    def _invalidate_settled_gameplay(self, reason: str) -> None:
        """Clear the settled proof without weakening the next reacquisition."""
        if str(reason) == "physical_ground_not_ready" and self._settled_gameplay_identity is not None:
            self._ground_loss_identity = self._settled_gameplay_identity
        elif str(reason) != "physical_ground_not_ready":
            self._ground_loss_identity = None
        self._settled_gameplay_identity = None
        self._settled_gameplay_sequence = None
        self._settled_gameplay_at = float("-inf")
        self._settled_gameplay_freshness_limit = None
        self._settled_gameplay_source = "invalidated"
        self._settled_gameplay_invalidation_reason = str(reason)

    def _record_settled_gameplay_proof(
        self,
        state: Any,
        source: str,
        freshness_max_age_seconds: float | None = None,
    ) -> None:
        self._settled_gameplay_identity = self._gameplay_identity(state)
        self._settled_gameplay_sequence = int(state.sequence)
        self._settled_gameplay_at = self._monotonic()
        self._settled_gameplay_freshness_limit = float(
            self.max_telemetry_age
            if freshness_max_age_seconds is None
            else freshness_max_age_seconds
        )
        self._settled_gameplay_source = str(source)
        self._settled_gameplay_invalidation_reason = None

    def _session_health_from_state(self, state: Any) -> dict[str, Any]:
        """Build readiness from one exact telemetry sample.

        Settled gameplay is a proof, not a short-lived cache.  Once the
        configured multi-sample gate has established it, each fresh advancing
        sample with the same gameplay identity extends the proof.  A repeated
        sequence is observable but never extends it; an actual invalidating
        condition clears it and forces the normal multi-sample gate again.
        """
        age = state.age_seconds()
        proof_freshness_limit = (
            self._settled_gameplay_freshness_limit
            if self._settled_gameplay_proof_present()
            and self._settled_gameplay_freshness_limit is not None
            else self.max_telemetry_age
        )
        live = state.source == "simulation" or age <= proof_freshness_limit
        player_spatial = bool(
            state.player is not None and state.player.position is not None
        )
        process_exists = (
            self._environment.game_process_exists()
            if getattr(self.input, "is_real", False) else None
        )
        foreground = (
            self._environment.game_is_foreground()
            if getattr(self.input, "is_real", False) else None
        )
        locomotion = getattr(state.player, "locomotion", None)
        physical_ground_ready = locomotion is None or locomotion.grounded is not False
        loaded_gameplay = bool(
            state.running
            and live
            and not state.paused
            and not state.in_menu
            and state.map_id
            and player_spatial
            and physical_ground_ready
            and (process_exists is not False)
            and (foreground is not False or self._background_input_enabled())
        )
        real_settling = (
            bool(getattr(self.input, "is_real", False))
            and self._settled_gameplay_samples > 1
        )
        identity = self._gameplay_identity(state) if real_settling else None
        invalidation_reason: str | None = None
        # The proof's own last sample, aging through one bounded sampler gap.
        stale_gap_hold = bool(
            real_settling
            and self._settled_proof_stale_gap_grace_seconds > 0.0
            and not live
            and state.source != "simulation"
            and self._settled_gameplay_identity is not None
            and self._settled_gameplay_sequence is not None
            and int(state.sequence) == self._settled_gameplay_sequence
            and identity == self._settled_gameplay_identity
            and age <= self._settled_proof_stale_gap_grace_seconds
        )
        if real_settling:
            if not state.running:
                invalidation_reason = "game_not_running"
            elif not live and not stale_gap_hold:
                invalidation_reason = "telemetry_stale"
            elif process_exists is False:
                invalidation_reason = "process_missing"
            elif foreground is False and not self._background_input_enabled():
                invalidation_reason = "foreground_missing"
            elif state.paused:
                invalidation_reason = "paused"
            elif state.in_menu:
                invalidation_reason = "menu"
            elif not state.map_id:
                invalidation_reason = "map_missing"
            elif not player_spatial:
                invalidation_reason = "player_not_loaded"
            elif not physical_ground_ready:
                invalidation_reason = "physical_ground_not_ready"

            if invalidation_reason is not None:
                self._invalidate_settled_gameplay(invalidation_reason)
            elif (
                self._settled_gameplay_identity is None
                and self._ground_loss_identity is not None
                and identity == self._ground_loss_identity
            ):
                # Only ground contact was lost (live: bumpy terrain at sprint
                # speed invalidated the proof, which otherwise needs Cloud to
                # stand still to settle again). Grounded again, live and in
                # the same world as the proof: restore it.
                self._ground_loss_identity = None
                self._record_settled_gameplay_proof(
                    state,
                    "ground_contact_reacquired",
                    freshness_max_age_seconds=proof_freshness_limit,
                )
            elif self._settled_gameplay_identity is not None:
                if identity != self._settled_gameplay_identity:
                    self._invalidate_settled_gameplay("gameplay_identity_changed")
                elif (
                    self._settled_gameplay_sequence is not None
                    and int(state.sequence) < self._settled_gameplay_sequence
                ):
                    self._invalidate_settled_gameplay("telemetry_sequence_regressed")
                elif (
                    self._settled_gameplay_sequence is not None
                    and int(state.sequence) > self._settled_gameplay_sequence
                ):
                    self._record_settled_gameplay_proof(
                        state,
                        "continuity_refreshed",
                        freshness_max_age_seconds=proof_freshness_limit,
                    )
        gameplay_settled = bool(
            loaded_gameplay
            and (
                not real_settling
                or (
                    self._settled_gameplay_proof_present()
                    and identity == self._settled_gameplay_identity
                )
            )
        )
        proof_present = bool(
            real_settling and self._settled_gameplay_proof_present()
        )
        proof_age = (
            max(0.0, self._monotonic() - self._settled_gameplay_at)
            if proof_present else None
        )
        proof_identity_matches = bool(
            proof_present
            and identity is not None
            and identity == self._settled_gameplay_identity
        )
        proof_state = (
            "multi_sample_established"
            if proof_present and self._settled_gameplay_source == "multi_sample_established"
            else self._settled_gameplay_source
        )
        if not proof_present and invalidation_reason is None and real_settling:
            proof_state = "reacquisition_required"
        diagnosis = (
            "ready"
            if gameplay_settled else
            "gameplay_settling" if loaded_gameplay else
            "telemetry_stale" if not live else
            "game_not_running" if not state.running else
            "paused_or_menu" if state.paused or state.in_menu else
            "player_not_loaded" if not player_spatial or not state.map_id else
            "foreground_missing" if foreground is False and not self._background_input_enabled() else
            "process_missing" if process_exists is False else
            "target_or_combat_active" if state.target is not None or state.in_combat else
            "not_ready"
        )
        gameplay_ready = gameplay_settled
        return {
            "ok": True,
            "sequence": state.sequence,
            "telemetry_live": live,
            "telemetry_age_seconds": age,
            "game_running": state.running,
            "paused": state.paused,
            "in_menu": state.in_menu,
            "map_id": state.map_id,
            "player_spatial": player_spatial,
            "loaded_gameplay": loaded_gameplay,
            "gameplay_settled": gameplay_settled,
            "target_clear": state.target is None,
            "in_combat": state.in_combat,
            "process_exists": process_exists,
            "foreground_window": foreground,
            "gameplay_ready": gameplay_ready,
            "input_reachable": foreground is not False or self._background_input_enabled(),
            "physical_ground_ready": physical_ground_ready,
            "diagnosis": diagnosis,
            "loading_evidence": False,
            "loading_evidence_source": None,
            "settled_proof_present": proof_present,
            "settled_proof_sequence": self._settled_gameplay_sequence if proof_present else None,
            "settled_proof_age_seconds": proof_age,
            "settled_proof_max_telemetry_age_seconds": (
                self._settled_gameplay_freshness_limit
                if proof_present
                else None
            ),
            "settled_proof_identity_matches": proof_identity_matches,
            "settled_proof_state": proof_state,
            "settled_proof_source": self._settled_gameplay_source,
            "settled_proof_invalidation_reason": self._settled_gameplay_invalidation_reason,
            "settled_proof_stale_gap_hold": stale_gap_hold,
            "health_sample_sequence": int(state.sequence),
        }

    def session_health(self, state: Any | None = None) -> dict[str, Any]:
        """Return readiness computed from the supplied or one fresh sample."""
        state = state if state is not None else self.telemetry.observe()
        health = self._session_health_from_state(state)
        control = self.control_state()
        if control is not None:
            health["control"] = control
        return health

    @staticmethod
    def _warm_state_blocker(state: Any) -> str | None:
        """Return a blocker that makes a settled proof unsafe to reuse."""
        if bool(getattr(state, "in_combat", False)) or getattr(state, "target", None) is not None:
            return "combat_active"
        for name in ("loading", "loading_or_streaming", "in_loading", "in_cutscene", "in_interaction"):
            if bool(getattr(state, name, False)):
                return "loading_or_transitioning"
        for attribute in ("navigation_diagnostics", "navigation", "map_state"):
            payload = getattr(state, attribute, None)
            if not isinstance(payload, dict):
                continue
            if bool(payload.get("in_combat", payload.get("combat_active", False))):
                return "combat_active"
            if bool(payload.get("loading", payload.get("loading_or_streaming", False))):
                return "loading_or_transitioning"
        return None

    @staticmethod
    def _warm_input_is_neutral(input_status: dict[str, Any]) -> bool:
        """Require explicit neutral broker evidence for a warm proof."""
        if input_status.get("real_input") is not True:
            return True
        backend_status = input_status.get("backend_status")
        if not isinstance(backend_status, dict):
            return False
        if backend_status.get("stick_state") != "neutral":
            return False
        if backend_status.get("neutralisation_error") not in (None, ""):
            return False
        last_stick = backend_status.get("last_stick")
        if not isinstance(last_stick, dict):
            return False
        try:
            x = float(last_stick["x"])
            y = float(last_stick["y"])
        except (KeyError, TypeError, ValueError):
            return False
        return math.isfinite(x) and math.isfinite(y) and x == 0.0 and y == 0.0

    @staticmethod
    def _warm_control_session_identity(control_session: Any) -> tuple[Any, ...] | None:
        if not isinstance(control_session, dict):
            return None
        return (
            control_session.get("session_marker"),
            control_session.get("owner_id"),
            control_session.get("controller_pid"),
            control_session.get("game_pid"),
            control_session.get("opened_at"),
        )

    def benchmark_readiness_snapshot(self) -> dict[str, Any]:
        """Inspect warm benchmark readiness without settling or authorizing input.

        This is intentionally a fast proof check for a later coordinator.  It
        consumes one telemetry sample and reuses the service health/input
        checks, but it never sends input, waits for settling, or grants route
        or movement admission.
        """
        state = self.telemetry.observe()
        health = self._session_health_from_state(state)
        input_status = self.input_status(state)
        invalidation_reason = self._warm_state_blocker(state)
        real_input = input_status.get("real_input") is True
        control_session = input_status.get("control_session")
        session_identity = self._warm_control_session_identity(control_session)
        if real_input and control_session is not None:
            if not isinstance(control_session, dict) or control_session.get("active") is not True:
                invalidation_reason = invalidation_reason or "control_session_inactive"
            elif control_session.get("owner_matches") is not True:
                invalidation_reason = invalidation_reason or "control_session_owner_mismatch"
            elif (
                self._warm_session_identity is not None
                and session_identity != self._warm_session_identity
            ):
                invalidation_reason = invalidation_reason or "control_session_changed"
        if not self._warm_input_is_neutral(input_status):
            invalidation_reason = invalidation_reason or "input_not_neutral"
        if input_status.get("emergency_stop") is True:
            invalidation_reason = invalidation_reason or "emergency_stop_latched"
        if invalidation_reason is not None:
            self._invalidate_settled_gameplay(invalidation_reason)
            health = self._session_health_from_state(state)
            input_status = self.input_status(state)
            self._warm_session_identity = None
        elif real_input and control_session is not None:
            self._warm_session_identity = session_identity

        proof_valid = bool(
            health.get("gameplay_ready") is True
            and health.get("settled_proof_present") is True
            and health.get("settled_proof_identity_matches") is True
            and self._warm_input_is_neutral(input_status)
            and input_status.get("emergency_stop") is False
            and (
                not real_input
                or (
                    input_status.get("game_process_exists") is True
                    and (input_status.get("input_reachable", input_status.get("foreground_window")) is True)
                )
            )
            and (
                control_session is None
                or (
                    isinstance(control_session, dict)
                    and control_session.get("active") is True
                    and control_session.get("owner_matches") is True
                )
            )
        )
        return {
            "ok": True,
            "readiness": "warm_session" if proof_valid else "settling_required",
            "sequence": int(state.sequence),
            "session_health": health,
            "input_status": input_status,
            "settled_proof_valid": proof_valid,
            "settled_proof_invalidation_reason": health.get("settled_proof_invalidation_reason"),
            "settling_required": not proof_valid,
            "movement_authorized": False,
            "navigation_admitted": False,
            "navigation_admission_required": True,
            "fresh_advancing_state_required": True,
        }

    def wait_for_settled_gameplay(
        self,
        timeout_seconds: float = GAMEPLAY_SETTLE_TIMEOUT_SECONDS,
        max_telemetry_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Establish the post-save settled-world gate without sending input."""
        freshness_limit = self._settling_freshness_limit(max_telemetry_age_seconds)
        state = self.telemetry.observe()
        if not getattr(self.input, "is_real", False) or self._settled_gameplay_samples <= 1:
            return {
                "ok": True,
                "readiness": "settled_loaded_active_gameplay",
                "sequence": state.sequence,
                "stable_samples": 1,
                "max_telemetry_age_seconds": freshness_limit,
            }
        settled = self._ensure_settled_gameplay(
            state,
            timeout_seconds,
            max_telemetry_age_seconds=freshness_limit,
        )
        return {
            "ok": True,
            "readiness": "settled_loaded_active_gameplay",
            "sequence": settled.sequence,
            "stable_samples": self._settled_gameplay_samples,
            "map_id": settled.map_id,
            "max_telemetry_age_seconds": freshness_limit,
        }

    def _settling_freshness_limit(self, requested: float | None) -> float:
        """Return a validated settling age without changing global readiness."""
        if requested is None:
            return float(self.max_telemetry_age)
        value = float(requested)
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("max_telemetry_age_seconds must be finite and positive")
        return value

    def mark_gameplay_settled(self, stable_samples: int | None = None) -> dict[str, Any]:
        """Cache readiness already proven by the lifecycle startup wrapper.

        The lifecycle controller performs the authoritative post-save sample
        window. This handoff avoids making the first real action repeat that
        same wait while still re-reading and validating the current live
        state before caching it.
        """
        state = self.telemetry.observe()
        if not state.running or state.paused or state.in_menu:
            raise SafetyError("cannot cache settled gameplay while paused or in a menu")
        if not state.map_id or state.player is None or state.player.position is None:
            raise SafetyError("cannot cache settled gameplay without player spatial telemetry")
        if state.source != "simulation" and state.age_seconds() >= self.max_telemetry_age:
            raise SafetyError("cannot cache settled gameplay from stale telemetry")
        if getattr(self.input, "is_real", False):
            if not self._environment.game_process_exists():
                raise SafetyError("cannot cache settled gameplay without the game process")
            if not self._input_reachable():
                raise SafetyError("cannot cache settled gameplay while the game is not foreground")
        self._settled_gameplay_identity = self._gameplay_identity(state)
        self._settled_gameplay_sequence = state.sequence
        self._settled_gameplay_at = self._monotonic()
        self._settled_gameplay_freshness_limit = float(self.max_telemetry_age)
        self._settled_gameplay_source = "multi_sample_established"
        self._settled_gameplay_invalidation_reason = None
        return {
            "ok": True,
            "readiness": "settled_loaded_active_gameplay",
            "sequence": state.sequence,
            "stable_samples": int(stable_samples or self._settled_gameplay_samples),
            "map_id": state.map_id,
        }

    @staticmethod
    def _queen_blood_move_record(move: Any) -> dict[str, Any]:
        return {
            "hand_index": int(move.hand_index),
            "row": int(move.row),
            "column": int(move.column),
            "card_id": move.card_id,
            "score": move.score,
        }

    def _assert_queens_blood_action_allowed(
        self, state: Any, queen_blood: QueenBloodState
    ) -> None:
        if getattr(self.input, "is_real", False) and self._session_lease is not None:
            self._rearm_active_session()
        if getattr(self.input, "is_real", False) and self._arm:
            _, _, stopped = self._arm.snapshot()
            with self._lock:
                self._stopped = stopped
        if self._stopped:
            raise SafetyError("agent emergency stop is latched")
        if not state.running:
            raise SafetyError("game telemetry reports that the game is stopped")
        if state.source != "simulation" and state.age_seconds() > self._queen_blood_freshness_limit():
            raise SafetyError("Queen's Blood telemetry is stale")
        if not queen_blood.active:
            raise SafetyError("Queen's Blood is not active")
        if queen_blood.phase != "player_turn":
            raise SafetyError(
                f"Queen's Blood is not awaiting the player: {queen_blood.phase}"
            )
        if getattr(self.input, "is_real", False):
            if not self._environment.game_process_exists():
                raise SafetyError("FFVII Rebirth process was not found")
            if not self._queen_blood_input_reachable():
                raise SafetyError("FFVII Rebirth is not the foreground window")
            armed, _ = self._arm.status() if self._arm else (False, 0.0)
            if not armed:
                raise SafetyError("Queen's Blood input is not manually armed")
        if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
            raise SafetyError("minimum delay between card-game actions has not elapsed")

        self._observe_match_deck(self._queen_blood_payload(state))

    def _assert_queens_blood_mulligan_action_allowed(
        self,
        observed_state: Any | None = None,
    ) -> tuple[Any, QueenBloodState]:
        state = self._assert_action_allowed(
            allow_paused=True,
            allow_queen_blood_mulligan=True,
            observed_state=observed_state,
        )
        if not getattr(self.input, "is_real", False):
            raise SafetyError("Queen's Blood mulligan requires the Windows backend")
        payload = self._queen_blood_payload(state)
        if not isinstance(payload, dict):
            raise SafetyError("Queen's Blood telemetry is unavailable")
        inferred_payload = self._queen_blood_inferred_mulligan_payload(state)
        if inferred_payload is not None:
            payload = inferred_payload
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError as exc:
            raise SafetyError(str(exc)) from exc
        if not (
            queen_blood.active
            and queen_blood.phase == "dealing"
            and (
                queen_blood.screen == "mulligan"
                or (queen_blood.match_screen and bool(queen_blood.hand))
            )
            and len(queen_blood.hand) == MULLIGAN_HAND_SIZE
        ):
            raise SafetyError("Queen's Blood mulligan hand is not ready")
        if not self._environment.game_process_exists():
            raise SafetyError("FFVII Rebirth process was not found")
        if not self._queen_blood_input_reachable():
            raise SafetyError("FFVII Rebirth is not the foreground window")
        armed, _ = self._arm.status() if self._arm else (False, 0.0)
        if not armed:
            raise SafetyError("Queen's Blood input is not manually armed")
        if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
            raise SafetyError("minimum delay between card-game actions has not elapsed")
        return state, queen_blood

    @classmethod
    def _queen_blood_has_identified_cards(cls, state: Any) -> bool:
        """Whether the current sidecar exposes at least one identified hand card."""
        payload = cls._queen_blood_payload(state)
        if not isinstance(payload, dict):
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        return any(bool(card.card_id.strip()) for card in queen_blood.hand)

    @classmethod
    def _queen_blood_hand_named(cls, state: Any) -> bool:
        """Whether every card in the hand (at least five) has a resolved name."""
        current = cls._queen_blood_state_or_none(state)
        return current is not None and len(current.hand) >= MULLIGAN_HAND_SIZE and all(
            bool(card.name and card.name.strip()) for card in current.hand
        )

    @classmethod
    def _queen_blood_complete_named_hand(cls, state: Any) -> bool:
        """Whether all five hand slots have stable IDs and resolved names."""
        payload = cls._queen_blood_payload(state)
        if not isinstance(payload, dict):
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        return len(queen_blood.hand) == MULLIGAN_HAND_SIZE and all(
            bool(card.card_id.strip()) and bool(card.name and card.name.strip())
            for card in queen_blood.hand
        )

    def _queen_blood_inferred_mulligan_payload(self, state: Any) -> dict[str, Any] | None:
        """Apply an authorized begin-match or restart-recovery inference."""
        sequence = getattr(state, "sequence", None)
        if (
            self._queen_blood_mulligan_inferred_sequence is None
            or not isinstance(sequence, int)
            or sequence < self._queen_blood_mulligan_inferred_sequence
        ):
            return None
        payload = self._queen_blood_payload(state)
        if not isinstance(payload, dict):
            return None
        try:
            raw_state = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return None
        if raw_state.turn > 0 or raw_state.phase in {
            "player_turn", "opponent_turn", "resolving", "result",
        }:
            return None
        inferred = dict(payload)
        if not self._queen_blood_has_identified_cards(state):
            if not self._queen_blood_mulligan_inferred_hand:
                return None
            inferred["hand"] = self._queen_blood_mulligan_inferred_hand
        raw_screen = inferred.get("screen")
        inferred.update({
            "active": True,
            "phase": "dealing",
            "screen": "mulligan",
            "begin_match_available": False,
            "match_screen": True,
        })
        inferred["state_inference"] = {
            "state": "mulligan",
            "basis": self._queen_blood_mulligan_inferred_basis
            or "identified_hand_after_begin_match",
            "raw_screen": raw_screen,
            "sequence": sequence,
        }
        return inferred

    def _queen_blood_inferred_turn_one_payload(self, state: Any) -> dict[str, Any] | None:
        sequence = getattr(state, "sequence", None)
        if (
            self._queen_blood_turn_one_inferred_sequence is None
            or self._queen_blood_turn_one_inferred_payload is None
            or not isinstance(sequence, int)
            or sequence < self._queen_blood_turn_one_inferred_sequence
        ):
            return None
        payload = self._queen_blood_payload(state)
        if isinstance(payload, dict):
            try:
                current = QueenBloodState.from_dict(payload)
            except QueenBloodError:
                current = None
            if current is not None and current.phase in {
                "player_turn", "opponent_turn", "resolving", "result",
            }:
                self._queen_blood_turn_one_inferred_sequence = None
                self._queen_blood_turn_one_inferred_payload = None
                return payload
        inferred = dict(self._queen_blood_turn_one_inferred_payload)
        inferred["state_inference"] = {
            "state": "player_turn",
            "basis": "play_key_sent_after_mulligan_marker_verification",
            "sequence": sequence,
        }
        return inferred

    def _queen_blood_observe(self, mode: str = "full", *, stable: bool = False):
        reads = getattr(self.telemetry, "queen_blood_reads", None)
        if not callable(reads):
            return self.telemetry.observe()
        # Require two independent equal, complete reads. Missing identities
        # cannot verify a board or carry older card facts into a new sample.
        # Keep the four-complete-sample change budget; allow up to four extra
        # reads for incomplete exports, without sending input (#366).
        with reads(mode):
            previous = None
            previous_sample = None
            complete_samples = 0
            incomplete_samples = []
            for _ in range(8 if stable else 1):
                state = self.telemetry.observe()
                if not stable:
                    return state
                payload = state.queen_blood or {}
                gaps = match_identity_gaps(payload) if mode == "full" else []
                if gaps:
                    incomplete_samples.append({"sequence": state.sequence, "missing": gaps})
                    previous = previous_sample = None
                    self._sleeper(0.1)
                    continue
                complete_samples += 1
                signature = {key: payload.get(key) for key in (
                    "active", "phase", "turn", "match_id", "screen", "begin_match_available",
                    "match_screen", "cursor", "hand", "board", "player_score",
                    "opponent_score", "winner", "result", "legal_moves",
                    "mulligan_selection", "popups", "deck_screen",
                )}
                # A matching acknowledgement is an independent native read,
                # even when the base sequence has not ticked. Without tickets,
                # require an advancing sequence instead of rereading one frame.
                sample = payload.get("probe_request") or state.sequence
                if signature == previous and sample != previous_sample:
                    return state
                previous, previous_sample = deepcopy(signature), sample
                if complete_samples == 4:
                    break
        details = {"complete_samples": complete_samples, "incomplete_samples": incomplete_samples,
                   "requires_fresh_observation": True}
        if incomplete_samples:
            raise SafetyError("Queen's Blood card identities incomplete across verification samples", details=details)
        raise SafetyError("Queen's Blood state changed across four verification samples", details=details)

    @staticmethod
    def _queen_blood_lane_signature(payload: dict[str, Any]) -> str:
        board = payload.get("board") if isinstance(payload.get("board"), dict) else {}
        cells = [[cell.get(key) for key in ("row", "column", "owner", "card_id", "power")]
                 for cell in board.get("cells") or [] if isinstance(cell, dict)]
        return json.dumps([board.get("lanes"), cells], sort_keys=True, default=str)

    def _await_settled_queen_blood_lanes(self, state: Any) -> tuple[Any, dict[str, Any] | None]:
        """On our turn, wait (bounded) while a game lane total differs from
        its card sum: the totals lag the cards while score tokens spawn, and
        a card power read mid-effect can be off for one read (#344). Returns
        the state to use and, when it waited, how that went."""
        payload = self._queen_blood_payload(state)
        if (payload is None or payload.get("phase") != "player_turn"
                or not lanes_disagree_with_cards(payload)):
            return state, None
        if self._queen_blood_lane_signature(payload) == self._queen_blood_unsettled_lanes:
            return state, {"settled": False, "waited_seconds": 0.0, "already_waited": True}
        started = self._monotonic()
        while self._monotonic() - started < QUEEN_BLOOD_LANE_SETTLE_SECONDS:
            self._sleeper(0.1)
            state = self._queen_blood_observe()
            payload = self._queen_blood_payload(state)
            if payload is None or payload.get("phase") != "player_turn" or not lanes_disagree_with_cards(payload):
                state = self._queen_blood_observe(stable=True)
                return state, {"settled": True, "waited_seconds": round(self._monotonic() - started, 2)}
        self._queen_blood_unsettled_lanes = self._queen_blood_lane_signature(payload)
        return state, {"settled": False, "waited_seconds": round(self._monotonic() - started, 2)}

    def _deck_observe(self, *, full: bool = False, stable: bool = False):
        return self._queen_blood_observe("full" if full else "cursor", stable=stable)

    def observe_queens_blood(
        self,
        infer_current_hand_is_mulligan: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(infer_current_hand_is_mulligan, bool):
            raise SafetyError("infer_current_hand_is_mulligan must be Boolean")
        state = self._queen_blood_observe(stable=True)
        state, lane_settle = self._await_settled_queen_blood_lanes(state)
        if infer_current_hand_is_mulligan:
            if not self._queen_blood_has_identified_cards(state):
                return {
                    "ok": False,
                    "available": False,
                    "sequence": state.sequence,
                    "reason": "cannot_infer_mulligan_without_identified_hand",
                }
            raw_payload = self._queen_blood_payload(state)
            raw_hand = raw_payload.get("hand") if isinstance(raw_payload, dict) else None
            self._queen_blood_mulligan_inferred_sequence = state.sequence
            self._queen_blood_mulligan_inferred_hand = (
                [dict(card) for card in raw_hand if isinstance(card, dict)]
                if isinstance(raw_hand, list) else None
            )
            self._queen_blood_mulligan_inferred_basis = "identified_hand_recovery"
        payload = self._queen_blood_inferred_turn_one_payload(state)
        if payload is None:
            payload = self._queen_blood_inferred_mulligan_payload(state)
        if payload is None:
            payload = state.queen_blood
        if not isinstance(payload, dict) or payload.get("probe_idle") is True:
            # A visible deck's native sample can be missing (or its cursor
            # request can acknowledge an idle snapshot). Do not report a
            # visible menu as absent, or invent card/deck data from UI text.
            mode = self._deck_screen_mode()
            deck_visible = mode in {"select", "editor"}
            return {
                "ok": False,
                "available": False,
                "sequence": state.sequence,
                "reason": ("queen_blood_deck_screen_visible_probe_unavailable"
                           if deck_visible else "queen_blood_telemetry_unavailable"),
                **({"deck_screen_mode": mode} if deck_visible else {}),
            }
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError as exc:
            return {
                "ok": False,
                "available": False,
                "sequence": state.sequence,
                "reason": "queen_blood_telemetry_invalid",
                "detail": str(exc),
            }
        # The game's lane totals can lag behind the cards (#329): report the
        # card sums and flag any lane where the game's total differs. Only our
        # turn waits for them to settle (#344); elsewhere a difference is
        # still settling.
        payload = reconcile_lane_totals(payload, settled=queen_blood.phase == "player_turn")
        self._observe_match_deck(payload)
        result = {
            "ok": True,
            "available": True,
            "sequence": state.sequence,
            "age_seconds": state.age_seconds(),
            "queen_blood": payload,
            "summary": queen_blood_summary(queen_blood),
        }
        mismatched = [LANE_NAMES[lane["row"]] for lane in payload.get("board", {}).get("lanes", [])
                      if isinstance(lane, dict) and lane.get("mismatch")]
        if mismatched:
            result["lane_totals_mismatch"] = mismatched
        if lane_settle is not None:
            result["lane_settle"] = lane_settle
        if queen_blood.phase == "player_turn":
            # What changed since our last move, for narration that reacts
            # (#182). An effect of our own card that resolved after its
            # placement was read counts as part of the exchange.
            commentary = commentary_cues(self._queen_blood_commentary_baseline, payload,
                                         player_passed=self._queen_blood_commentary_after_pass)
            if commentary is not None:
                result["commentary"] = commentary
                if commentary.get("notable") is True:
                    self._emit_production_qb_event(
                        "queens_blood_notable_exchange", queen_blood,
                        cues=commentary.get("cues") or [],
                        projection=commentary.get("projection"),
                        projection_change=commentary.get("projection_change"),
                    )
        return result

    def _wait_for_queen_blood_transition(
        self, previous_sequence: int, timeout_seconds: float
    ) -> Any:
        deadline = self._monotonic() + timeout_seconds
        latest = self.telemetry.observe()
        while latest.sequence <= previous_sequence and self._monotonic() < deadline:
            self._sleeper(0.05)
            latest = self.telemetry.observe()
        return latest

    @staticmethod
    def _queen_blood_hand_identity(hand: Any) -> tuple[tuple[int, str, str | None], ...]:
        return tuple(
            sorted(
                (
                    int(card.hand_index),
                    str(card.card_id),
                    card.name,
                )
                for card in hand
            )
        )

    @classmethod
    def _queen_blood_post_mulligan_ready(
        cls,
        state: Any,
        previous_hand: tuple[tuple[int, str, str | None], ...],
    ) -> bool:
        payload = cls._queen_blood_payload(state)
        if payload is None:
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        if not (
            queen_blood.active
            and queen_blood.phase == "player_turn"
            and len(queen_blood.hand) == MULLIGAN_HAND_SIZE
        ):
            return False
        return cls._queen_blood_hand_identity(queen_blood.hand) != previous_hand

    @classmethod
    def _queen_blood_turn_one_ready(cls, state: Any) -> bool:
        payload = cls._queen_blood_payload(state)
        if payload is None:
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        return (
            queen_blood.active
            and queen_blood.phase == "player_turn"
            and len(queen_blood.hand) == MULLIGAN_HAND_SIZE
        )

    @staticmethod
    def _queen_blood_mulligan_selection(queen_blood: QueenBloodState) -> tuple[int, ...] | None:
        """Return marked slots only when the producer exposes every marker.

        A missing marker is different from an empty selection.  The harness
        must not press Play based only on the sequence of inputs it attempted
        to send, because a missed navigation event could mark a different
        card while the hand identities themselves remain unchanged.
        """
        if len(queen_blood.hand) != MULLIGAN_HAND_SIZE:
            return None
        markers = [card.mulligan_selected for card in queen_blood.hand]
        if any(marker is None for marker in markers):
            return None
        return tuple(sorted(
            card.hand_index for card in queen_blood.hand
            if card.mulligan_selected is True
        ))

    @staticmethod
    def _queen_blood_positive_mulligan_indices(
        queen_blood: QueenBloodState,
    ) -> tuple[int, ...]:
        """Return only affirmative marker observations, even if others are unknown."""
        return tuple(sorted(
            card.hand_index for card in queen_blood.hand
            if card.mulligan_selected is True
        ))

    @classmethod
    def _queen_blood_mulligan_marker_available_for(
        cls,
        state: Any,
        target_index: int,
    ) -> bool:
        payload = cls._queen_blood_payload(state)
        if not isinstance(payload, dict):
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        if cls._queen_blood_mulligan_selection(queen_blood) is not None:
            return True
        return (
            0 <= target_index < len(queen_blood.hand)
            and queen_blood.hand[target_index].mulligan_selected is not None
        )

    @classmethod
    def _queen_blood_mulligan_marker_for(
        cls,
        state: Any,
        target_index: int,
    ) -> bool | None:
        payload = cls._queen_blood_payload(state)
        if not isinstance(payload, dict):
            return None
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return None
        for card in queen_blood.hand:
            if card.hand_index == target_index:
                return card.mulligan_selected
        return None

    def _wait_for_queen_blood_mulligan_marker(
        self,
        previous_sequence: int,
        action_deadline: float,
        target_index: int,
        trace: list[dict[str, Any]],
    ) -> tuple[Any, bool]:
        """Wait for a fresh card marker, allowing lag before another toggle."""
        confirmation_deadline = min(
            action_deadline,
            self._monotonic() + QUEEN_BLOOD_MARKER_CONFIRMATION_SECONDS,
        )
        latest = self._wait_for_queen_blood_sample(
            previous_sequence,
            confirmation_deadline,
            lambda state: self._queen_blood_mulligan_marker_for(
                state, target_index
            ) is True,
            trace,
            return_latest_on_timeout=True,
        )
        if latest is None or latest.sequence <= previous_sequence:
            raise SafetyError(
                "Queen's Blood marker telemetry did not advance after the input; "
                "stopping without another toggle"
            )
        if (
            latest.source != "simulation"
            and latest.age_seconds() > self._queen_blood_freshness_limit()
        ):
            raise SafetyError(
                "Queen's Blood marker telemetry became stale after the input; "
                "stopping without another toggle"
            )
        marker = self._queen_blood_mulligan_marker_for(latest, target_index)
        if marker is None:
            raise SafetyError(
                "Queen's Blood marker stayed unknown after the input; "
                "stopping without another toggle"
            )
        return latest, marker

    @staticmethod
    def _queen_blood_mulligan_cursor(queen_blood: QueenBloodState) -> int:
        """Return the currently hovered hand slot, failing closed if absent."""
        cursor = queen_blood.cursor
        if cursor is None or cursor.area != "hand" or cursor.hand_index is None:
            raise SafetyError(
                "Queen's Blood mulligan cursor telemetry did not identify the hovered hand card"
            )
        hand_size = max(MULLIGAN_HAND_SIZE, len(queen_blood.hand))
        if not 0 <= cursor.hand_index < hand_size:
            raise SafetyError(f"Queen's Blood cursor slot is outside the {hand_size}-card hand")
        return cursor.hand_index

    @classmethod
    def _queen_blood_mulligan_cursor_is(
        cls,
        state: Any,
        target_index: int,
    ) -> tuple[QueenBloodState, int] | None:
        payload = cls._queen_blood_payload(state)
        if payload is None:
            return None
        try:
            queen_blood = QueenBloodState.from_dict(payload)
            cursor_index = cls._queen_blood_mulligan_cursor(queen_blood)
        except (QueenBloodError, SafetyError):
            return None
        return (queen_blood, cursor_index) if cursor_index == target_index else None

    @classmethod
    def _queen_blood_mulligan_markers_observed(cls, state: Any) -> bool:
        payload = cls._queen_blood_payload(state)
        if payload is None:
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        return cls._queen_blood_mulligan_selection(queen_blood) is not None

    @staticmethod
    def _queen_blood_named_mulligan_indices(
        queen_blood: QueenBloodState,
        only_card_name: str,
    ) -> tuple[int, ...]:
        """Resolve an exact localized card name to opening-hand slots.

        A named selector must not treat an unresolved name as a non-match:
        doing so could leave the requested card in the hand while still
        claiming that only matching cards were considered.
        """
        target_name = only_card_name.strip().casefold()
        if not target_name:
            raise SafetyError("only_card_name must not be empty")
        if any(card.name is None or not str(card.name).strip() for card in queen_blood.hand):
            raise SafetyError(
                "Queen's Blood named mulligan selection requires resolved names for every opening card"
            )
        return tuple(sorted(
            card.hand_index
            for card in queen_blood.hand
            if str(card.name).strip().casefold() == target_name
        ))

    def _assert_queens_blood_card_selection_allowed(self) -> tuple[Any, QueenBloodState]:
        state = self._assert_action_allowed(
            allow_paused=True,
            allow_queen_blood_mulligan=True,
        )
        if not isinstance(state.queen_blood, dict):
            raise SafetyError("Queen's Blood telemetry is unavailable")
        try:
            queen_blood = QueenBloodState.from_dict(state.queen_blood)
        except QueenBloodError as exc:
            raise SafetyError(str(exc)) from exc
        if not queen_blood.active or queen_blood.phase not in {"dealing", "player_turn"}:
            raise SafetyError(
                "Queen's Blood card selection requires an active dealing or player-turn hand"
            )
        if not queen_blood.hand:
            raise SafetyError("Queen's Blood card selection hand is not ready")
        if getattr(self.input, "is_real", False):
            if not self._environment.game_process_exists():
                raise SafetyError("FFVII Rebirth process was not found")
            if not self._queen_blood_input_reachable():
                raise SafetyError("FFVII Rebirth is not the foreground window")
            armed, _ = self._arm.status() if self._arm else (False, 0.0)
            if not armed:
                raise SafetyError("Queen's Blood input is not manually armed")
        if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
            raise SafetyError("minimum delay between card-game actions has not elapsed")
        return state, queen_blood

    @classmethod
    def _queen_blood_cursor_or_none(cls, state: Any) -> int | None:
        payload = cls._queen_blood_payload(state)
        if payload is None:
            return None
        try:
            return cls._queen_blood_mulligan_cursor(QueenBloodState.from_dict(payload))
        except (QueenBloodError, SafetyError):
            return None

    def _wait_for_settled_queen_blood_cursor(
        self,
        previous_sequence: int,
        action_deadline: float,
        trace: list[dict[str, Any]],
    ) -> Any:
        """Return a fresh sample once the hand cursor has stopped moving.

        Queued navigation input can land after a sample already showed the
        cursor on the target, so toggling on the first matching sample can
        mark the neighbouring card (live 2026-09-24: slot 3 was marked while
        targeting slot 2). The cursor must hold one value across fresh samples
        for ``queen_blood_cursor_settle_seconds``; on timeout the latest sample
        is returned and the caller's hover check decides.
        """
        return self._wait_for_settled_queen_blood_value(
            previous_sequence,
            action_deadline,
            trace,
            self._queen_blood_cursor_or_none,
        )

    def _wait_for_settled_queen_blood_value(
        self,
        previous_sequence: int,
        action_deadline: float,
        trace: list[dict[str, Any]],
        key: Any,
    ) -> Any:
        """Return a fresh sample once ``key(sample)`` has held for the settle window."""
        state = self._wait_for_queen_blood_sample(
            previous_sequence,
            action_deadline,
            lambda _state: True,
            trace,
        )
        settle = self._queen_blood_cursor_settle_seconds
        if settle <= 0:
            return state
        deadline = min(
            action_deadline,
            self._monotonic() + settle + QUEEN_BLOOD_CURSOR_SETTLE_TIMEOUT_SECONDS,
        )
        value = key(state)
        stable_since = self._monotonic()
        while self._monotonic() - stable_since < settle and self._monotonic() < deadline:
            state = self._wait_for_queen_blood_sample(
                state.sequence,
                deadline,
                lambda _state: True,
                trace,
                return_latest_on_timeout=True,
            )
            current = key(state)
            if current != value:
                value = current
                stable_since = self._monotonic()
        return state

    @classmethod
    def _queen_blood_state_or_none(cls, state: Any) -> QueenBloodState | None:
        payload = cls._queen_blood_payload(state)
        if payload is None:
            return None
        try:
            return QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return None

    def _queen_blood_placement_key(self, state: Any) -> tuple[bool, tuple[int, int] | None] | None:
        queen_blood = self._queen_blood_state_or_none(state)
        if queen_blood is None:
            return None
        return queen_blood.placement.active, self._queen_blood_cursor_cell(queen_blood)

    def _queen_blood_cursor_cell(self, queen_blood: QueenBloodState) -> tuple[int, int] | None:
        """The cell under the held card: the preview target on a lit cell,
        else the learned board geometry applied to the held card's location.

        Every read also teaches the geometry: placed cards' locations, and
        the held card's location over a preview-confirmed cell."""
        geometry = self._queen_blood_geometry
        geometry.observe_many("board", (
            ((cell.row, cell.column), (cell.location[0], cell.location[1]))
            for cell in queen_blood.board
            if cell.card_present and cell.location is not None
        ))
        placement = queen_blood.placement
        location = placement.actor_location
        if placement.active and placement.target is not None and location is not None:
            geometry.observe("hover", placement.target, (location[0], location[1]))
        if placement.position is not None:
            return placement.position
        if not placement.active or location is None:
            return None
        return geometry.hovered_cell((location[0], location[1]))

    def _select_queens_blood_card_slot(
        self,
        target_index: int,
        *,
        action_deadline: float,
        max_selection_attempts: int,
        selection_milliseconds: int,
        trace: list[dict[str, Any]],
        selection_trace: list[dict[str, Any]],
        inputs: list[dict[str, Any]],
        after_hover: Any = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Navigate to one hand slot and verify it from fresh telemetry.

        ``after_hover`` is an optional action-specific verifier. Mulligan uses
        it to toggle and verify the marker; ordinary card selection only
        verifies the hovered slot and leaves the card-game action to its
        caller (for example, board placement during a player turn).
        """
        for attempt in range(1, max_selection_attempts + 1):
            cursor_state = self.telemetry.observe()
            if not cursor_state.running:
                raise SafetyError("game telemetry reports that the game is stopped")
            payload = self._queen_blood_payload(cursor_state)
            if payload is None:
                raise SafetyError("Queen's Blood cursor telemetry is unavailable")
            try:
                cursor_queen_blood = QueenBloodState.from_dict(payload)
                cursor_index = self._queen_blood_mulligan_cursor(cursor_queen_blood)
            except (QueenBloodError, SafetyError) as exc:
                raise SafetyError(str(exc)) from exc
            attempt_record: dict[str, Any] = {
                "target_index": target_index,
                "attempt": attempt,
                "max_attempts": max_selection_attempts,
                "selection_milliseconds": selection_milliseconds,
                "sequence_before": cursor_state.sequence,
                "cursor_before": cursor_index,
                "navigation": [],
            }
            try:
                navigation = plan_queen_blood_card_navigation(
                    cursor_index,
                    target_index,
                    max(MULLIGAN_HAND_SIZE, len(cursor_queen_blood.hand)),
                )
            except QueenBloodError as exc:
                raise SafetyError(str(exc)) from exc
            for step_index, step in enumerate(navigation):
                if step_index:
                    # A release and the next press within one game frame
                    # merge into one press (the game runs slower unfocused),
                    # and a repeat of the same direction is ignored while the
                    # cursor still moves (QUEEN_BLOOD_REPEAT_GAP_SECONDS).
                    same = navigation[step_index - 1].button == step.button
                    self._sleeper(QUEEN_BLOOD_REPEAT_GAP_SECONDS if same else QUEEN_BLOOD_PRESS_GAP_SECONDS)
                self._queen_blood_press(step.button, selection_milliseconds)
                attempt_record["navigation"].append(step.button)
                inputs.append({
                    "button": step.button,
                    "milliseconds": selection_milliseconds,
                    "reason": step.reason,
                    "target_index": target_index,
                    "attempt": attempt,
                    "index": len(inputs) + 1,
                })

            if navigation:
                # The burst is planned from a fresh cursor sample, so the
                # cursor only moves toward the target: its first report there
                # is its arrival (no settle window; that cost 1.5 s a card).
                navigation_state = self._wait_for_queen_blood_sample(
                    cursor_state.sequence,
                    min(action_deadline, self._monotonic() + QUEEN_BLOOD_ARRIVAL_SECONDS),
                    lambda sample: self._queen_blood_cursor_or_none(sample) == target_index,
                    trace,
                    return_latest_on_timeout=True,
                ) or cursor_state
                if self._queen_blood_cursor_or_none(navigation_state) != target_index:
                    # Not there in time: let queued presses land before the
                    # next attempt re-plans from where the cursor stops.
                    navigation_state = self._wait_for_settled_queen_blood_cursor(
                        navigation_state.sequence, action_deadline, trace,
                    )
                try:
                    navigation_queen_blood = QueenBloodState.from_dict(
                        navigation_state.queen_blood
                    )
                    cursor_after = self._queen_blood_mulligan_cursor(
                        navigation_queen_blood
                    )
                except (QueenBloodError, SafetyError):
                    cursor_after = None
                attempt_record["sequence_after_navigation"] = navigation_state.sequence
                attempt_record["cursor_after_navigation"] = cursor_after
                hovered_state = navigation_state
            else:
                cursor_after = cursor_index
                attempt_record["sequence_after_navigation"] = cursor_state.sequence
                attempt_record["cursor_after_navigation"] = cursor_after
                hovered_state = cursor_state

            if cursor_after != target_index:
                attempt_record["outcome"] = "hover_not_verified"
                selection_trace.append(attempt_record)
                continue

            accepted_state = hovered_state
            accepted = True
            if after_hover is not None:
                accepted, accepted_state = after_hover(hovered_state, attempt_record)
            if accepted:
                attempt_record.setdefault("outcome", "hover_verified")
                selection_trace.append(attempt_record)
                return accepted_state, attempt_record
            selection_trace.append(attempt_record)

        raise SafetyError(
            "Queen's Blood card selection could not verify hover "
            f"for slot {target_index} after {max_selection_attempts} attempts"
        )

    def select_queens_blood_cards(
        self,
        card_indices: list[int] | tuple[int, ...],
        timeout_seconds: float = 30.0,
        max_selection_retries: int = 5,
        selection_milliseconds: int = 200,
    ) -> dict[str, Any]:
        """Navigate to and verify one or more hand cards by slot.

        This is the reusable hand-selection primitive for Queen's Blood. It
        sends only bounded directional input and verifies the hovered slot
        after every attempt; mulligan marking and board placement remain
        action-specific operations layered on top.
        """
        try:
            timeout_seconds = float(timeout_seconds)
            selection_milliseconds = validate_duration(selection_milliseconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid Queen's Blood card-selection parameters") from exc
        if not 1.0 <= timeout_seconds <= QUEEN_BLOOD_MULLIGAN_MAX_TIMEOUT_SECONDS:
            raise SafetyError(
                "timeout_seconds must be between 1 and "
                f"{QUEEN_BLOOD_MULLIGAN_MAX_TIMEOUT_SECONDS:g}"
            )
        try:
            max_selection_retries = int(max_selection_retries)
        except (TypeError, ValueError) as exc:
            raise SafetyError("max_selection_retries must be an integer between 0 and 5") from exc
        if not 0 <= max_selection_retries <= 5:
            raise SafetyError("max_selection_retries must be between 0 and 5")
        if not isinstance(card_indices, (list, tuple)):
            raise SafetyError("card_indices must be an array of one to five slots")
        try:
            plan_queen_blood_mulligan_input(card_indices)
        except QueenBloodError as exc:
            raise SafetyError(str(exc)) from exc

        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        pre_state = None
        trace: list[dict[str, Any]] = []
        inputs: list[dict[str, Any]] = []
        try:
            pre_state, _ = self._assert_queens_blood_card_selection_allowed()
            action_deadline = self._monotonic() + timeout_seconds
            last_state = pre_state
            selection_trace: list[dict[str, Any]] = []
            max_selection_attempts = max_selection_retries + 1
            for target_index in sorted(int(index) for index in card_indices):
                last_state, _ = self._select_queens_blood_card_slot(
                    target_index,
                    action_deadline=action_deadline,
                    max_selection_attempts=max_selection_attempts,
                    selection_milliseconds=selection_milliseconds,
                    trace=trace,
                    selection_trace=selection_trace,
                    inputs=inputs,
                )
            result = {
                "ok": True,
                "action": "select_queens_blood_cards",
                "outcome": "cards_hover_verified",
                "selected_indices": sorted(int(index) for index in card_indices),
                "max_selection_retries": max_selection_retries,
                "max_selection_attempts": max_selection_attempts,
                "pre_sequence": pre_state.sequence,
                "post_sequence": last_state.sequence,
                "inputs": inputs,
                "selection_trace": selection_trace,
                "queen_blood": last_state.queen_blood,
                "trace": trace,
            }
            self._record_action(**result)
            return result
        except (QueenBloodError, SafetyError) as exc:
            self._record_action(
                action="select_queens_blood_cards",
                ok=False,
                pre_sequence=pre_state.sequence if pre_state is not None else None,
                inputs=inputs,
                trace=trace,
                rejection_reason=str(exc),
            )
            raise SafetyError(str(exc)) from exc
        finally:
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def mulligan_cards(
        self,
        card_indices: list[int] | tuple[int, ...] | None = None,
        milliseconds: int = 500,
        timeout_seconds: float = 90.0,
        begin_if_needed: bool = False,
        only_card_name: str | None = None,
        max_selection_retries: int = 5,
        selection_milliseconds: int = 200,
        mark_milliseconds: int = 500,
        infer_current_hand_is_mulligan: bool = False,
        submit_play: bool = True,
    ) -> dict[str, Any]:
        """Mark opening-hand cards and, by default, submit Play.

        The mulligan state is inferred from identified hand cards after Begin
        Match, without reading the prompt banner. Each requested card is
        hovered and toggled with Confirm held for ``mark_milliseconds``; its
        marker (the probe's ``Pnl_Mulligan.Visibility``) is confirmed from
        fresh telemetry before Play is sent. Set
        ``submit_play=False`` to stop after marker verification and leave the
        mulligan screen open. After all
        bounded Confirm attempts, virtual X360 A/confirm is a fallback only when
        fresh telemetry still marks the focused card unselected. Marker retries
        reuse the same hovered slot and do not alter card-navigation timing.
        ``infer_current_hand_is_mulligan`` restores this inference after a
        worker restart without sending Begin Match again. An empty selection
        confirms that no card is
        marked and submits the opening hand unchanged. If the game has not
        published turn-one telemetry after Play, the returned state records
        that turn one was inferred from the verified submission.
        """
        try:
            milliseconds = validate_duration(milliseconds)
            selection_milliseconds = validate_duration(selection_milliseconds)
            mark_milliseconds = validate_duration(mark_milliseconds)
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid Queen's Blood mulligan parameters") from exc
        if not 1.0 <= timeout_seconds <= QUEEN_BLOOD_MULLIGAN_MAX_TIMEOUT_SECONDS:
            raise SafetyError(
                "timeout_seconds must be between 1 and "
                f"{QUEEN_BLOOD_MULLIGAN_MAX_TIMEOUT_SECONDS:g}"
            )
        try:
            max_selection_retries = int(max_selection_retries)
        except (TypeError, ValueError) as exc:
            raise SafetyError("max_selection_retries must be an integer between 0 and 5") from exc
        if not 0 <= max_selection_retries <= 5:
            raise SafetyError("max_selection_retries must be between 0 and 5")
        max_selection_attempts = max_selection_retries + 1
        if not isinstance(infer_current_hand_is_mulligan, bool):
            raise SafetyError("infer_current_hand_is_mulligan must be Boolean")
        if not isinstance(submit_play, bool):
            raise SafetyError("submit_play must be Boolean")
        if only_card_name is not None:
            if not isinstance(only_card_name, str) or not only_card_name.strip():
                raise SafetyError("only_card_name must be a non-empty string")
            if card_indices is not None:
                raise SafetyError("only_card_name cannot be combined with card_indices")
        elif card_indices is None:
            card_indices = (0, 1, 2)
        if card_indices is not None and not isinstance(card_indices, (list, tuple)):
            raise SafetyError("card_indices must be an array of one to five slots")

        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        pre_state = None
        pre_queen_blood = None
        trace: list[dict[str, Any]] = []
        inputs: list[dict[str, Any]] = []
        mulligan_started = self._monotonic()
        timings: dict[str, float] = {"clock_start": round(self._wall_clock().timestamp(), 3)}
        try:
            initial_state = self.telemetry.observe()
            action_deadline = self._monotonic() + timeout_seconds
            if infer_current_hand_is_mulligan:
                if not self._queen_blood_has_identified_cards(initial_state):
                    raise SafetyError(
                        "cannot infer Queen's Blood mulligan without an identified hand"
                    )
                inferred_payload = self._queen_blood_payload(initial_state)
                raw_hand = inferred_payload.get("hand") if isinstance(inferred_payload, dict) else None
                self._queen_blood_mulligan_inferred_sequence = initial_state.sequence
                self._queen_blood_mulligan_inferred_hand = (
                    [dict(card) for card in raw_hand if isinstance(card, dict)]
                    if isinstance(raw_hand, list) else None
                )
                self._queen_blood_mulligan_inferred_basis = "identified_hand_recovery"
            if begin_if_needed and not isinstance(initial_state.queen_blood, dict):
                sidecar_deadline = min(
                    self._monotonic() + QUEEN_BLOOD_SIDECAR_RETRY_SECONDS,
                    self._monotonic() + timeout_seconds,
                )
                while (
                    not isinstance(initial_state.queen_blood, dict)
                    and self._monotonic() < sidecar_deadline
                ):
                    self._sleeper(
                        min(
                            0.1,
                            max(0.01, sidecar_deadline - self._monotonic()),
                        )
                    )
                    initial_state = self.telemetry.observe()
            if (
                begin_if_needed
                and not self._queen_blood_mulligan_ready(initial_state)
                and self._queen_blood_inferred_mulligan_payload(initial_state) is None
                and self._queen_blood_prematch_visible(initial_state)
            ):
                pre_state = self._assert_action_allowed(
                    allow_paused=True,
                    allow_queen_blood_dialogue_transition=True,
                    observed_state=initial_state,
                )
                if not getattr(self.input, "is_real", False):
                    raise SafetyError("Queen's Blood mulligan requires the Windows backend")
                if not self._environment.game_process_exists():
                    raise SafetyError("FFVII Rebirth process was not found")
                if not self._queen_blood_input_reachable():
                    raise SafetyError("FFVII Rebirth is not the foreground window")
                armed, _ = self._arm.status() if self._arm else (False, 0.0)
                if not armed:
                    raise SafetyError("Queen's Blood input is not manually armed")
                self._queen_blood_mulligan_inferred_sequence = None
                self._queen_blood_mulligan_inferred_hand = None
                self._queen_blood_mulligan_inferred_basis = None
                self._queen_blood_turn_one_inferred_sequence = None
                self._queen_blood_turn_one_inferred_payload = None
                begin_deadline = min(
                    action_deadline,
                    self._monotonic() + QUEEN_BLOOD_MULLIGAN_GRACE_SECONDS,
                )
                mulligan_ready = False
                for begin_attempt in range(QUEEN_BLOOD_MAX_BEGIN_ATTEMPTS):
                    previous_sequence = pre_state.sequence
                    self._assert_deck_select_for_begin()
                    self._ensure_game_foreground()  # F3 is keyboard-only
                    self.input.tap("queen_blood_begin_match", milliseconds)
                    inputs.append({
                        "button": "queen_blood_begin_match",
                        "key": "F3",
                        "milliseconds": milliseconds,
                        "reason": "begin_match_before_mulligan",
                        "attempt": begin_attempt + 1,
                    })
                    # Begin Match is a two-action operation: F3 opens the
                    # start popup, then Confirm/Yes accepts it. Retry the
                    # complete pair only while the bounded QB producer keeps
                    # reporting the same deck-select screen.
                    self._sleeper(
                        min(
                            QUEEN_BLOOD_DIALOGUE_SETTLE_SECONDS,
                            max(0.0, begin_deadline - self._monotonic()),
                        )
                    )
                    self._tap_queen_blood_confirm(
                        milliseconds,
                        allow_stale_queen_blood_transition=True,
                        keyboard=True,
                    )
                    inputs.append({
                        "button": "confirm",
                        "milliseconds": milliseconds,
                        "reason": "start_match_default_yes_before_mulligan",
                        "attempt": begin_attempt + 1,
                    })
                    pre_state = self._wait_for_queen_blood_sample(
                        pre_state.sequence,
                        min(
                            begin_deadline,
                            self._monotonic() + QUEEN_BLOOD_BEGIN_RETRY_SECONDS,
                        ),
                        self._queen_blood_has_identified_cards,
                        trace,
                        return_latest_on_timeout=True,
                        allow_stale_queen_blood_transition=True,
                    )
                    if pre_state is not None and self._queen_blood_has_identified_cards(pre_state):
                        self._queen_blood_mulligan_inferred_sequence = pre_state.sequence
                        payload = self._queen_blood_payload(pre_state)
                        raw_hand = payload.get("hand") if isinstance(payload, dict) else None
                        self._queen_blood_mulligan_inferred_hand = (
                            [dict(card) for card in raw_hand if isinstance(card, dict)]
                            if isinstance(raw_hand, list) else None
                        )
                        self._queen_blood_mulligan_inferred_basis = "identified_hand_after_begin_match"
                        mulligan_ready = True
                        break
                    if pre_state is None or pre_state.sequence <= previous_sequence:
                        raise SafetyError(
                            "Queen's Blood telemetry did not advance after Begin Match; "
                            "refusing another input"
                        )
                    payload = self._queen_blood_payload(pre_state)
                    if payload is None:
                        raise SafetyError(
                            "Queen's Blood sidecar is no longer fresh after Begin Match; "
                            "refusing another input"
                        )
                    if not self._queen_blood_prematch_visible(pre_state):
                        break
                if not mulligan_ready:
                    raise SafetyError(
                        "Queen's Blood hand identities did not appear after "
                        f"{QUEEN_BLOOD_MAX_BEGIN_ATTEMPTS} Begin Match attempts"
                    )
            candidate_state = pre_state or initial_state
            if (
                self._queen_blood_mulligan_inferred_sequence is not None
                and not self._queen_blood_complete_named_hand(candidate_state)
            ):
                candidate_state = self._wait_for_queen_blood_sample(
                    candidate_state.sequence,
                    action_deadline,
                    self._queen_blood_complete_named_hand,
                    trace,
                )
            pre_state, pre_queen_blood = self._assert_queens_blood_mulligan_action_allowed(
                observed_state=candidate_state
            )
            timings["hand_ready_seconds"] = round(self._monotonic() - mulligan_started, 2)
            if only_card_name is not None:
                card_indices = self._queen_blood_named_mulligan_indices(
                    pre_queen_blood,
                    only_card_name,
                )
                if not card_indices:
                    result = {
                        "ok": True,
                        "action": "mulligan_cards",
                        "outcome": "no_matching_cards",
                        "performed": False,
                        "verified": False,
                        "only_card_name": only_card_name.strip(),
                        "selected_indices": [],
                        "max_selection_retries": max_selection_retries,
                        "max_selection_attempts": max_selection_attempts,
                        "pre_sequence": pre_state.sequence,
                        "post_sequence": pre_state.sequence,
                        "pre_hand": pre_state.queen_blood.get("hand", []),
                        "queen_blood": pre_state.queen_blood,
                        "inputs": [],
                        "selection_trace": [],
                        "trace": trace,
                    }
                    self._record_action(**result)
                    return result
            try:
                # Validate the requested slots, but do not use this static
                # plan for live input: every target is navigated from a fresh
                # cursor sample below.
                plan_queen_blood_mulligan_input(card_indices)
            except QueenBloodError as exc:
                raise SafetyError(str(exc)) from exc
            # Resume an interrupted selection: requested cards that fresh
            # telemetry already reports marked need no further toggle. A
            # marked card outside the request would be discarded against
            # the caller's intent, so stop before sending any input.
            requested = {int(index) for index in card_indices}
            pre_marked = self._queen_blood_mulligan_selection(pre_queen_blood)
            if pre_marked is not None and set(pre_marked) - requested:
                raise SafetyError(
                    "Queen's Blood mulligan already has unrequested cards marked: "
                    f"{sorted(set(pre_marked) - requested)}"
                )
            expected_selected: set[int] = set(pre_marked or ()) & requested
            marked_state = pre_state
            last_selection_sequence = pre_state.sequence
            selection_trace: list[dict[str, Any]] = [
                {
                    "target_index": index,
                    "attempt": 0,
                    "outcome": "already_marked_verified",
                    "observed_indices": list(pre_marked or ()),
                    "expected_indices": sorted(expected_selected),
                }
                for index in sorted(expected_selected)
            ]
            for target_index in sorted(requested - expected_selected):
                def mark_after_hover(marker_state: Any, attempt_record: dict[str, Any]) -> tuple[bool, Any]:
                    marker_queen_blood = QueenBloodState.from_dict(marker_state.queen_blood)
                    observed_selected = self._queen_blood_mulligan_selection(marker_queen_blood)
                    observed_positive = self._queen_blood_positive_mulligan_indices(
                        marker_queen_blood
                    )
                    previously_selected = set(expected_selected)
                    expected_after_mark = previously_selected | {target_index}
                    if observed_selected is not None:
                        observed_set = set(observed_selected)
                        if observed_set == expected_after_mark:
                            expected_selected.add(target_index)
                            attempt_record["observed_indices"] = list(observed_selected)
                            attempt_record["expected_indices"] = sorted(expected_selected)
                            attempt_record["outcome"] = "already_marked_verified"
                            return True, marker_state
                        if observed_set != previously_selected:
                            attempt_record["observed_indices"] = list(observed_selected)
                            attempt_record["expected_indices"] = sorted(previously_selected)
                            attempt_record["outcome"] = "unexpected_existing_marker"
                            return False, marker_state
                    elif target_index in observed_positive:
                        if set(observed_positive) != expected_after_mark:
                            attempt_record["observed_indices"] = list(observed_positive)
                            attempt_record["expected_indices"] = sorted(expected_after_mark)
                            attempt_record["outcome"] = "unexpected_existing_marker"
                            return False, marker_state
                        expected_selected.add(target_index)
                        attempt_record["observed_indices"] = list(observed_positive)
                        attempt_record["expected_indices"] = sorted(expected_selected)
                        attempt_record["outcome"] = "already_marked_verified"
                        return True, marker_state

                    if set(observed_positive) != previously_selected:
                        attempt_record["observed_indices"] = list(observed_positive)
                        attempt_record["expected_indices"] = sorted(previously_selected)
                        attempt_record["outcome"] = "unexpected_existing_marker"
                        return False, marker_state

                    expected_selected_tuple = tuple(sorted(expected_after_mark))
                    # Keyboard navigation already verified the in-game card
                    # focus; Confirm toggles the mulligan mark on that card.
                    # Live validation (2026-09-24) showed a left-click in the
                    # client margin marked only slot 0, while Enter marked
                    # the focused slot 2 on the first press.
                    self._tap_queen_blood_confirm(mark_milliseconds)
                    inputs.append({
                        "button": "confirm",
                        "milliseconds": mark_milliseconds,
                        "reason": "mark_mulligan_card",
                        "target_index": target_index,
                        "attempt": attempt_record["attempt"],
                        "index": len(inputs) + 1,
                    })
                    marker_input_time = self._monotonic()
                    marked_state, target_marked = self._wait_for_queen_blood_mulligan_marker(
                        marker_state.sequence,
                        action_deadline,
                        target_index,
                        trace,
                    )
                    marked_queen_blood = QueenBloodState.from_dict(marked_state.queen_blood)
                    observed_selected = self._queen_blood_mulligan_selection(marked_queen_blood)
                    observed_positive = self._queen_blood_positive_mulligan_indices(
                        marked_queen_blood
                    )
                    attempt_record["sequence_marked"] = marked_state.sequence
                    attempt_record["target_marker_after_click"] = target_marked
                    attempt_record["marker_wait_seconds"] = round(
                        self._monotonic() - marker_input_time, 3
                    )
                    attempt_record["expected_indices"] = list(expected_selected_tuple)
                    attempt_record["observed_indices"] = list(
                        observed_selected if observed_selected is not None else observed_positive
                    )
                    if target_marked and (
                        observed_selected == expected_selected_tuple
                        or (
                            observed_selected is None
                            and target_index in observed_positive
                            and set(observed_positive) == expected_after_mark
                        )
                    ):
                        expected_selected.add(target_index)
                        attempt_record["outcome"] = "mark_verified"
                        return True, marked_state
                    if target_marked:
                        attempt_record["outcome"] = "unexpected_marker_change"
                        raise SafetyError(
                            "Queen's Blood marker telemetry changed an unexpected card; "
                            "stopping before another toggle"
                        )
                    marker_by_index = {
                        card.hand_index: card for card in marked_queen_blood.hand
                    }
                    target_card = marker_by_index.get(target_index)
                    if (
                        (observed_selected is not None
                         and set(observed_selected) != previously_selected)
                        or set(observed_positive) != previously_selected
                    ):
                        attempt_record["outcome"] = "unexpected_marker_change"
                        raise SafetyError(
                            "Queen's Blood marker telemetry changed an unexpected card; "
                            "stopping before another toggle"
                        )
                    attempt_record["outcome"] = "marker_still_false_after_wait"
                    can_try_gamepad = (
                        attempt_record["attempt"] == max_selection_attempts
                        and target_card is not None
                        and target_card.mulligan_selected is False
                        and set(observed_positive) == previously_selected
                    )
                    press_gamepad_a = getattr(self.input, "press_gamepad_a", None)
                    if can_try_gamepad:
                        if not callable(press_gamepad_a):
                            attempt_record["gamepad_fallback"] = {
                                "outcome": "unavailable",
                            }
                            return False, marked_state
                        gamepad_result = press_gamepad_a(mark_milliseconds)
                        inputs.append({
                            "button": "gamepad_a",
                            "device": "virtual_x360",
                            "milliseconds": mark_milliseconds,
                            "reason": "mulligan_click_fallback",
                            "target_index": target_index,
                            "attempt": attempt_record["attempt"],
                            "result": gamepad_result,
                            "index": len(inputs) + 1,
                        })
                        gamepad_state, gamepad_marked = self._wait_for_queen_blood_mulligan_marker(
                            marked_state.sequence,
                            action_deadline,
                            target_index,
                            trace,
                        )
                        gamepad_queen_blood = QueenBloodState.from_dict(
                            gamepad_state.queen_blood
                        )
                        gamepad_selected = self._queen_blood_mulligan_selection(
                            gamepad_queen_blood
                        )
                        gamepad_positive = self._queen_blood_positive_mulligan_indices(
                            gamepad_queen_blood
                        )
                        attempt_record["sequence_gamepad_a"] = gamepad_state.sequence
                        attempt_record["gamepad_a_result"] = gamepad_result
                        attempt_record["target_marker_after_gamepad_a"] = gamepad_marked
                        attempt_record["observed_indices_gamepad_a"] = list(
                            gamepad_selected
                            if gamepad_selected is not None else gamepad_positive
                        )
                        if gamepad_marked and (
                            gamepad_selected == expected_selected_tuple
                            or (
                                gamepad_selected is None
                                and target_index in gamepad_positive
                                and set(gamepad_positive) == expected_after_mark
                            )
                        ):
                            expected_selected.add(target_index)
                            attempt_record["outcome"] = "mark_verified_gamepad_fallback"
                            return True, gamepad_state
                        if gamepad_marked:
                            attempt_record["outcome"] = "unexpected_gamepad_marker_change"
                            raise SafetyError(
                                "Queen's Blood gamepad input changed an unexpected card; "
                                "stopping before another input"
                            )
                        attempt_record["outcome"] = "gamepad_a_marker_mismatch"
                        return False, gamepad_state
                    return False, marked_state

                try:
                    marked_state, _ = self._select_queens_blood_card_slot(
                        target_index,
                        action_deadline=action_deadline,
                        max_selection_attempts=max_selection_attempts,
                        selection_milliseconds=selection_milliseconds,
                        trace=trace,
                        selection_trace=selection_trace,
                        inputs=inputs,
                        after_hover=mark_after_hover,
                    )
                except SafetyError as exc:
                    if str(exc).startswith("Queen's Blood card selection could not verify hover"):
                        raise SafetyError(
                            "Queen's Blood mulligan could not verify hover and marker selection "
                            f"for slot {target_index} after {max_selection_attempts} attempts"
                        ) from exc
                    raise
                last_selection_sequence = marked_state.sequence
                timings[f"marked_{target_index}_seconds"] = round(self._monotonic() - mulligan_started, 2)

            if not card_indices:
                observed_selected = self._queen_blood_mulligan_selection(pre_queen_blood)
                if observed_selected is None:
                    marker_state = self._wait_for_queen_blood_sample(
                        pre_state.sequence,
                        action_deadline,
                        self._queen_blood_mulligan_markers_observed,
                        trace,
                    )
                    marker_payload = self._queen_blood_payload(marker_state)
                    if not isinstance(marker_payload, dict):
                        raise SafetyError("Queen's Blood mulligan marker telemetry was not available")
                    pre_queen_blood = QueenBloodState.from_dict(marker_payload)
                    pre_state = marker_state
                    last_selection_sequence = marker_state.sequence
                    observed_selected = self._queen_blood_mulligan_selection(pre_queen_blood)
                if observed_selected is None:
                    raise SafetyError("Queen's Blood mulligan marker telemetry was not available")
                if observed_selected:
                    raise SafetyError(
                        "Queen's Blood has marked cards, but the requested mulligan selects none"
                    )
                selection_trace.append({
                    "expected_indices": [],
                    "observed_indices": [],
                    "outcome": "no_mulligan_markers_verified",
                })

            if not submit_play:
                marker_final_state = marked_state if card_indices else pre_state
                marker_payload = self._queen_blood_payload(marker_final_state)
                if not isinstance(marker_payload, dict):
                    raise SafetyError("Queen's Blood marker telemetry was unavailable after selection")
                result = {
                    "ok": True,
                    "action": "mulligan_cards",
                    "outcome": "cards_marked_verified" if expected_selected else "opening_hand_unchanged_verified",
                    "performed": True,
                    "verified": True,
                    "selection_verified": True,
                    "play_sent": False,
                    "only_card_name": only_card_name.strip() if only_card_name is not None else None,
                    "selected_indices": sorted(expected_selected),
                    "max_selection_retries": max_selection_retries,
                    "max_selection_attempts": max_selection_attempts,
                    "mark_milliseconds": mark_milliseconds,
                    "state_inference": (
                        "identified_hand_recovery"
                        if infer_current_hand_is_mulligan else None
                    ),
                    "pre_sequence": pre_state.sequence,
                    "post_sequence": marker_final_state.sequence,
                    "pre_hand": pre_state.queen_blood.get("hand", []),
                    "queen_blood": marker_payload,
                    "inputs": inputs,
                    "selection_trace": selection_trace,
                    "trace": trace,
                }
                self._record_action(**result)
                return result

            self._queen_blood_press("queen_blood_play", milliseconds)
            inputs.append({
                "button": "queen_blood_play",
                "milliseconds": milliseconds,
                "reason": "submit_mulligan",
                "index": len(inputs) + 1,
            })
            self.input.release_all()
            timings["play_sent_seconds"] = round(self._monotonic() - mulligan_started, 2)
            try:
                final_state = self._wait_for_queen_blood_sample(
                    last_selection_sequence,
                    min(action_deadline, self._monotonic() + 3.0),
                    self._queen_blood_turn_one_ready,
                    trace,
                    return_latest_on_timeout=True,
                    allow_stale_queen_blood_transition=True,
                )
            except SafetyError as exc:
                if str(exc) not in {
                    "Queen's Blood telemetry is stale",
                    "Queen's Blood state did not advance before the action deadline",
                }:
                    raise
                final_state = self.telemetry.observe()
            turn_one_observed = self._queen_blood_turn_one_ready(final_state)
            if turn_one_observed and not self._queen_blood_hand_named(final_state):
                # The probe names a swapped-in card from its next identity read
                # (it withholds a reused widget's old identity), so the first
                # turn-one sample can hold an unnamed card; allow a moment.
                named = self._wait_for_queen_blood_sample(
                    final_state.sequence,
                    min(action_deadline, self._monotonic() + 3.0),
                    self._queen_blood_hand_named,
                    trace,
                    return_latest_on_timeout=True,
                    allow_stale_queen_blood_transition=True,
                )
                if named is not None and self._queen_blood_turn_one_ready(named):
                    final_state = named
            final_payload = self._queen_blood_payload(final_state)
            if not turn_one_observed:
                base_payload = self._queen_blood_payload(pre_state)
                if isinstance(final_payload, dict):
                    try:
                        final_hand_count = len(QueenBloodState.from_dict(final_payload).hand)
                    except QueenBloodError:
                        final_hand_count = 0
                    if final_hand_count == MULLIGAN_HAND_SIZE:
                        base_payload = final_payload
                if not isinstance(base_payload, dict):
                    raise SafetyError("Queen's Blood hand telemetry was lost after Play was sent")
                final_payload = dict(base_payload)
                final_payload.update({
                    "active": True,
                    "phase": "player_turn",
                    "turn": 1,
                    "screen": "match",
                    "begin_match_available": False,
                    "match_screen": True,
                    "state_inference": {
                        "state": "player_turn",
                        "basis": "play_key_sent_after_mulligan_marker_verification",
                        "sequence": final_state.sequence,
                    },
                })
            if not isinstance(final_payload, dict):
                raise SafetyError("Queen's Blood telemetry was unavailable after Play was sent")
            final_queen_blood = QueenBloodState.from_dict(final_payload)
            before_by_index = {card.hand_index: card for card in pre_queen_blood.hand}
            after_by_index = {card.hand_index: card for card in final_queen_blood.hand}
            selected_indices = tuple(sorted(int(index) for index in card_indices))
            changed_indices = tuple(
                index for index in selected_indices
                if before_by_index[index].card_id != after_by_index[index].card_id
            )
            unchanged_indices = tuple(
                index for index in range(MULLIGAN_HAND_SIZE)
                if index not in selected_indices
                and before_by_index[index].card_id == after_by_index[index].card_id
            )
            missing_names = tuple(
                card.hand_index for card in final_queen_blood.hand if not card.name
            )
            # The game re-sorts the hand after the swap (live 2026-09-29 the
            # replacement came in at the end), so the kept cards are checked
            # as a multiset, not slot by slot; a replacement may repeat a kind.
            kept = Counter(
                before_by_index[index].card_id for index in range(MULLIGAN_HAND_SIZE)
                if index not in selected_indices
            )
            after_ids = Counter(card.card_id for card in final_queen_blood.hand)
            kept_cards_present = not (kept - after_ids)
            verified = (
                turn_one_observed
                and len(final_queen_blood.hand) >= MULLIGAN_HAND_SIZE
                and kept_cards_present
                and not missing_names
            )
            self._queen_blood_mulligan_inferred_sequence = None
            self._queen_blood_mulligan_inferred_hand = None
            self._queen_blood_mulligan_inferred_basis = None
            if not turn_one_observed:
                self._queen_blood_turn_one_inferred_payload = dict(final_payload)
            else:
                self._queen_blood_turn_one_inferred_payload = None
            self._queen_blood_turn_one_inferred_sequence = (
                None if turn_one_observed else final_state.sequence
            )
            result = {
                "ok": verified or not turn_one_observed,
                "action": "mulligan_cards",
                "outcome": (
                    "turn_one_hand_verified" if verified
                    else "turn_one_inferred_after_play" if not turn_one_observed
                    else "turn_one_hand_unverified"
                ),
                "performed": True,
                "verified": verified,
                "selection_verified": True,
                "turn_one_verified": turn_one_observed,
                "turn_one_inferred": not turn_one_observed,
                "only_card_name": only_card_name.strip() if only_card_name is not None else None,
                "selected_indices": list(selected_indices),
                "max_selection_retries": max_selection_retries,
                "max_selection_attempts": max_selection_attempts,
                "mark_milliseconds": mark_milliseconds,
                "state_inference": (
                    "identified_hand_recovery"
                    if infer_current_hand_is_mulligan else None
                ),
                "changed_indices": list(changed_indices),
                "kept_cards_present": kept_cards_present,
                "timings": dict(timings, done_seconds=round(self._monotonic() - mulligan_started, 2),
                                clock_end=round(self._wall_clock().timestamp(), 3)),
                "unchanged_indices": list(unchanged_indices),
                "missing_name_indices": list(missing_names),
                "pre_sequence": pre_state.sequence,
                "post_sequence": final_state.sequence,
                "pre_hand": pre_state.queen_blood.get("hand", []),
                "queen_blood": final_payload,
                "inputs": inputs,
                "selection_trace": selection_trace,
                "trace": trace,
            }
            self._record_action(**result)
            return result
        except (QueenBloodError, SafetyError) as exc:
            self._record_action(
                action="mulligan_cards",
                ok=False,
                pre_sequence=pre_state.sequence if pre_state is not None else None,
                inputs=inputs,
                trace=trace,
                rejection_reason=str(exc),
            )
            raise SafetyError(str(exc)) from exc
        finally:
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def tap_queens_blood_button(self, button: str, milliseconds: int = 400) -> dict[str, Any]:
        """Send one card-game button during an active match (bounded, guarded).

        Only the Queen's Blood hand/board navigation, Confirm, Cancel and the
        Play/Pass key are accepted, and only while fresh telemetry reports an
        active match. The response carries the state sampled afterwards.
        This raw button operation never wakes an idle probe: it requires an
        already observed match. Reading its active telemetry renews the normal
        request; a rejected field/title call leaves the scanner idle.
        """
        if button not in QUEEN_BLOOD_MATCH_BUTTONS:
            raise SafetyError(
                f"button {button!r} is not a Queen's Blood match button; "
                f"allowed: {sorted(QUEEN_BLOOD_MATCH_BUTTONS)}"
            )
        try:
            milliseconds = validate_duration(milliseconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid Queen's Blood button duration") from exc
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            try:
                state, queen_blood = self._assert_queens_blood_match_input_allowed()
            except SafetyError as exc:
                # Live 2026-10-12 (#343): Confirm on a result screen read as
                # "telemetry is unavailable", since this call never wakes the
                # probe and it idles once the match ends. Say what to use.
                raise SafetyError(
                    f"{exc}; tap_queens_blood_button only sends input during a match turn"
                    " and never wakes the Queen's Blood probe: close a result screen"
                    " with tap_button(button=\"confirm\")"
                ) from exc
            self._queen_blood_press(button, milliseconds)
            self.input.release_all()
            post = self._wait_for_queen_blood_sample(
                state.sequence,
                self._monotonic() + 5.0,
                lambda _state: True,
                [],
                return_latest_on_timeout=True,
            )
            result = {
                "ok": True,
                "action": "tap_queens_blood_button",
                "button": button,
                "milliseconds": milliseconds,
                "pre_sequence": state.sequence,
                "post_sequence": post.sequence if post is not None else None,
                "queen_blood": post.queen_blood if post is not None else None,
            }
            self._record_action(**result)
            return result
        finally:
            try:
                self.input.release_all()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def _assert_queens_blood_match_input_allowed(self) -> tuple[Any, QueenBloodState]:
        """Guard for match input that may run outside the player's hand turn.

        Like the turn guard, but accepts a placement in progress and the
        opening deal so recovery input (Cancel) can be sent.
        """
        state = self.telemetry.observe()
        payload = self._queen_blood_payload(state)
        if payload is None:
            raise SafetyError("Queen's Blood telemetry is unavailable")
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError as exc:
            raise SafetyError(str(exc)) from exc
        if not queen_blood.active:
            raise SafetyError("Queen's Blood is not active")
        if queen_blood.phase not in {"player_turn", "dealing"}:
            raise SafetyError(f"Queen's Blood is not awaiting the player: {queen_blood.phase}")
        self._assert_queens_blood_action_allowed(
            state,
            queen_blood if queen_blood.phase == "player_turn"
            else replace(queen_blood, phase="player_turn"),
        )
        return state, queen_blood

    def pass_queens_blood_turn(
        self,
        hold_milliseconds: int = 2200,
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        """Pass the turn by holding the Play/Pass key, then verify it ended.

        Passing is a hold: the HUD's PassGauge fills while the key is down.
        Success requires fresh telemetry that the turn left the player (the
        opponent's turn or the end of the match) with no new card placed.
        An unobserved pass returns ok=False; the caller may narrate and retry.
        """
        try:
            hold_milliseconds = validate_duration(hold_milliseconds, "queen_blood_play")
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid Queen's Blood pass parameters") from exc
        if not 1.0 <= timeout_seconds <= 60.0:
            raise SafetyError("timeout_seconds must be between 1 and 60")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        trace: list[dict[str, Any]] = []
        try:
            self._claim_campaign_speech_before_input()
            state = self.telemetry.observe()
            payload = self._queen_blood_payload(state)
            if payload is None:
                raise SafetyError("Queen's Blood telemetry is unavailable")
            queen_blood = QueenBloodState.from_dict(payload)
            self._assert_queens_blood_action_allowed(state, queen_blood)
            if queen_blood.placement.active:
                raise SafetyError("cannot pass while a card is being placed")
            self._queen_blood_pre_move_payload = payload
            # Guarded like the board presses (re-arms the open control
            # session and refocuses the game). Live, 1.5 s holds left the
            # first pass of three matches unregistered; 2.2 s is sent.
            self._tap_queen_blood_confirm_or_button("queen_blood_play", hold_milliseconds)
            self.input.release_all()
            post = self._wait_for_queen_blood_sample(
                state.sequence,
                self._monotonic() + timeout_seconds,
                lambda sample: self._queen_blood_turn_left_player(sample),
                trace,
                return_latest_on_timeout=True,
            )
            verified = (
                post is not None
                and post.sequence > state.sequence
                and self._queen_blood_turn_left_player(post)
            )
            result = {
                "ok": verified,
                "action": "pass_queens_blood_turn",
                "verified": verified,
                "outcome": "turn_passed" if verified else "pass_not_observed",
                "hold_milliseconds": hold_milliseconds,
                "pre_sequence": state.sequence,
                "post_sequence": post.sequence if post is not None else None,
                "queen_blood": post.queen_blood if post is not None else None,
            }
            if not verified:
                result["reason"] = "pass_not_observed"
            self._record_action(**result)
            return result
        except QueenBloodError as exc:
            raise SafetyError(str(exc)) from exc
        finally:
            try:
                self.input.release_all()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def place_queens_blood_card(
        self,
        hand_index: int,
        row: int,
        column: int,
        selection_milliseconds: int = QUEEN_BLOOD_MATCH_KEY_MS,
        board_milliseconds: int = QUEEN_BLOOD_MATCH_KEY_MS,
        confirm_milliseconds: int = QUEEN_BLOOD_MATCH_CONFIRM_MS,
        timeout_seconds: float = 60.0,
        max_board_steps: int = 30,
    ) -> dict[str, Any]:
        """Play one hand card onto one board cell.

        The hand and board moves are each sent as one burst from the observed
        cursor and checked once, by waiting for telemetry to show the cursor
        on its target (re-planned from where it is seen if it is not). Pick
        up waits for the placement to start; Confirm is sent only once the
        game previews the target cell; the result must show a card on that
        cell, and reports whether the hand holds one copy fewer of the card.
        Before placement Confirm, a failed pick-up may be cancelled. After
        Confirm, ambiguous telemetry leaves input untouched for inspection.
        """
        try:
            hand_index = int(hand_index)
            row = int(row)
            column = int(column)
            selection_milliseconds = validate_duration(selection_milliseconds)
            board_milliseconds = validate_duration(board_milliseconds)
            confirm_milliseconds = validate_duration(confirm_milliseconds)
            timeout_seconds = float(timeout_seconds)
            max_board_steps = int(max_board_steps)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid Queen's Blood placement parameters") from exc
        if not (0 <= row < 3 and 0 <= column < 5):
            raise SafetyError("Queen's Blood placement cell is outside the 3x5 board")
        if not 5.0 <= timeout_seconds <= 180.0:
            raise SafetyError("timeout_seconds must be between 5 and 180")
        if not 1 <= max_board_steps <= 60:
            raise SafetyError("max_board_steps must be between 1 and 60")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        trace: list[dict[str, Any]] = []
        inputs: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        hand_trace: list[dict[str, Any]] = []
        state = sample = card = None
        stage = "admission"
        # Wall-clock ends too, to compare with the caller's round trip.
        timings: dict[str, float] = {"clock_start": round(self._wall_clock().timestamp(), 3)}
        picked_up = False
        confirm_attempted = False
        started = self._monotonic()
        deadline = started + timeout_seconds
        target = (row, column)

        def failure_details() -> dict[str, Any]:
            # Reuse the samples already read by the action. A diagnostic must
            # neither poll the game nor retain the probe's large reflection tree.
            retained = trace if len(trace) <= 72 else trace[:8] + trace[-64:]
            observations = []
            for item in retained:
                payload = item.get("queen_blood")
                try:
                    current = QueenBloodState.from_dict(payload) if isinstance(payload, dict) else None
                except QueenBloodError:
                    current = None
                position = self._queen_blood_cursor_cell(current) if current else None
                observations.append({
                    "sequence": item["sequence"],
                    "stage": item.get("stage"),
                    "phase": current.phase if current else None,
                    "turn": current.turn if current else None,
                    "manager_found": payload.get("manager_found") if isinstance(payload, dict) else None,
                    "placement_active": current.placement.active if current else None,
                    "cursor_cell": list(position) if position is not None else None,
                    "preview_target": list(current.placement.target) if current and current.placement.target else None,
                })
            timings["place_seconds"] = round(self._monotonic() - started, 2)
            return {
                "action": "place_queens_blood_card", "stage": stage,
                "hand_index": hand_index, "row": row, "column": column,
                "card_id": card.card_id if card else None,
                "pre_sequence": state.sequence if state else None,
                "last_sequence": trace[-1]["sequence"] if trace else (sample.sequence if sample else None),
                "confirm_attempted": confirm_attempted,
                "requires_fresh_observation": True,
                "inputs": inputs, "inputs_are_attempts": True,
                "placement_steps": steps, "hand_trace": hand_trace,
                "timings": timings, "observations": observations,
                "observation_count": len(trace), "observations_truncated": len(trace) > len(retained),
            }

        try:
            self._claim_campaign_speech_before_input()
            state = self._queen_blood_observe(stable=True)
            sample = state
            trace.append({"sequence": state.sequence, "queen_blood": state.queen_blood, "stage": stage})
            payload = self._queen_blood_payload(state)
            if payload is None:
                raise SafetyError("Queen's Blood telemetry is unavailable")
            queen_blood = QueenBloodState.from_dict(payload)
            self._assert_queens_blood_action_allowed(state, queen_blood)
            if queen_blood.placement.active:
                raise SafetyError("a card is already being placed; cancel it first")
            self._queen_blood_pre_move_payload = payload
            card = next((item for item in queen_blood.hand if item.hand_index == hand_index), None)
            if card is None:
                raise SafetyError(f"no hand card at position {hand_index}")
            if not identified_card_id(card.card_id) or any(not identified_card_id(item.card_id) for item in queen_blood.hand):
                raise SafetyError("Queen's Blood hand identities are incomplete; observe again before selecting a card")
            if card.playable is False:
                raise SafetyError(f"hand card {hand_index} has no legal placement")
            cell = queen_blood.cell(row, column)
            if cell is None:
                raise SafetyError("board telemetry does not include the target cell")
            if cell.card_present or cell.owner != "player":
                raise SafetyError(
                    f"cell ({row}, {column}) is not an empty player cell: owner={cell.owner}"
                )
            if card.cost is not None and (cell.rank or 0) < card.cost:
                raise SafetyError(
                    f"cell ({row}, {column}) rank {cell.rank} is below card cost {card.cost}"
                )
            copies_before = sum(1 for item in queen_blood.hand if item.card_id == card.card_id)
            hand_size = max(MULLIGAN_HAND_SIZE, len(queen_blood.hand))

            def burst(buttons: list[str], milliseconds: int, reason: str, gap: float) -> None:
                for position, button in enumerate(buttons):
                    inputs.append({"button": button, "reason": reason})
                    self._tap_queen_blood_confirm_or_button(button, milliseconds)
                    repeat = position + 1 < len(buttons) and buttons[position + 1] == button
                    self._sleeper(QUEEN_BLOOD_REPEAT_GAP_SECONDS if repeat else gap)

            def wait(sample: Any, seconds: float, predicate: Any) -> Any:
                trace_start = len(trace)
                try:
                    return self._wait_for_queen_blood_sample(
                        sample.sequence,
                        min(deadline, self._monotonic() + seconds),
                        predicate,
                        trace,
                        return_latest_on_timeout=True,
                    ) or sample
                finally:
                    for item in trace[trace_start:]:
                        item["stage"] = stage

            # Hand: one burst from the observed slot, re-planned from where the
            # cursor is seen if it does not arrive.
            sample = state
            # Every hand-cursor value seen, with its time since the call began
            # (live 2026-09-29 some hand moves needed extra presses).
            stage = "hand_navigation"

            def seen_cursor(item: Any) -> int | None:
                value = self._queen_blood_cursor_or_none(item)
                hand_trace[-1]["seen"].append([round(self._monotonic() - started, 2), value])
                return value

            for attempt in range(QUEEN_BLOOD_MOVE_ATTEMPTS + 1):
                cursor = self._queen_blood_cursor_or_none(sample)
                if cursor == hand_index:
                    break
                if cursor is None:
                    raise SafetyError("Queen's Blood telemetry does not show the hand cursor")
                if attempt == QUEEN_BLOOD_MOVE_ATTEMPTS:
                    raise SafetyError(f"the hand cursor did not reach slot {hand_index}")
                try:
                    navigation = plan_queen_blood_card_navigation(cursor, hand_index, hand_size)
                except QueenBloodError as exc:
                    raise SafetyError(str(exc)) from exc
                hand_trace.append({"from": cursor, "presses": len(navigation),
                                   "at": round(self._monotonic() - started, 2), "seen": []})
                burst([step.button for step in navigation], selection_milliseconds, "hover_card",
                      QUEEN_BLOOD_HAND_GAP_SECONDS)
                sample = wait(sample, QUEEN_BLOOD_ARRIVAL_SECONDS, lambda item: seen_cursor(item) == hand_index)
                if self._queen_blood_cursor_or_none(sample) != hand_index:
                    # Let queued presses land before re-planning.
                    sample = self._wait_for_settled_queen_blood_cursor(sample.sequence, deadline, trace)
                    hand_trace[-1]["settled"] = self._queen_blood_cursor_or_none(sample)
            timings["hand_seconds"] = round(self._monotonic() - started, 2)

            def placement_active(item: Any) -> bool:
                key = self._queen_blood_placement_key(item)
                return key is not None and key[0]

            stage = "pick_up"
            for attempt in range(1, 3):
                inputs.append({"button": "confirm", "reason": "pick_up_card", "attempt": attempt})
                self._tap_queen_blood_confirm(confirm_milliseconds)
                picked_at = self._monotonic()
                sample = wait(sample, 6.0, placement_active)
                if placement_active(sample):
                    picked_up = True
                    break
                current = self._queen_blood_state_or_none(sample)
                if current is None or current.phase != "player_turn":
                    raise SafetyError("the turn changed before the card was picked up")
            if not picked_up:
                raise SafetyError("the card was not picked up after two Confirm presses")
            timings["pick_up_seconds"] = round(self._monotonic() - started, 2)
            stage = "held_card_settle"
            self._sleeper(max(0.0, picked_at + QUEEN_BLOOD_PICKUP_SETTLE_SECONDS - self._monotonic()))
            still: dict[str, Any] = {"location": None, "since": None}

            def held_card_still(item: Any) -> bool:
                current = self._queen_blood_state_or_none(item)
                location = current.placement.actor_location if current is not None else None
                now = self._monotonic()
                if location is None:
                    return True  # not reported: the minimum pause is all there is
                if location != still["location"]:
                    still.update(location=location, since=now)
                    return False
                return now - still["since"] >= QUEEN_BLOOD_HELD_CARD_STILL_SECONDS

            sample = wait(sample, QUEEN_BLOOD_HELD_CARD_SETTLE_LIMIT_SECONDS, held_card_still)
            timings["card_landed_seconds"] = round(self._monotonic() - started, 2)

            # Board: the cursor moves over all 15 cells (edges clamp) and is
            # located from the preview, the cursor widget or the held card's
            # learned geometry; unlocated, the burst starts from the corner.
            def at_target(item: Any) -> bool:
                current = self._queen_blood_state_or_none(item)
                return (current is not None and current.placement.active
                        and current.placement.target == target
                        and self._queen_blood_cursor_cell(current) == target)

            moves = 0
            stage = "board_navigation"
            for attempt in range(QUEEN_BLOOD_MOVE_ATTEMPTS + 1):
                current = self._queen_blood_state_or_none(sample)
                if current is None or not current.placement.active:
                    # One sample can miss the held card (live 2026-09-30 a
                    # placement was abandoned and the card returned to the
                    # hand); only a placement gone for a second has ended.
                    sample = wait(sample, 1.0, placement_active)
                    current = self._queen_blood_state_or_none(sample)
                    if current is None or not current.placement.active:
                        raise SafetyError("the placement ended before the card reached its cell")
                position = self._queen_blood_cursor_cell(current)
                steps.append({
                    "step": len(steps),
                    "observed": list(position) if position else None,
                    "preview_target": current.placement.target is not None,
                })
                if at_target(sample):
                    break
                if attempt == QUEEN_BLOOD_MOVE_ATTEMPTS:
                    if position == target:
                        raise SafetyError(f"the game does not accept ({row}, {column}) for the held card")
                    raise SafetyError("the placement cursor did not reach the target cell")
                buttons: list[str] = []
                start_cell = position
                if start_cell is None:
                    buttons.extend(QUEEN_BLOOD_HOME_BUTTONS)
                    start_cell = (0, 0)
                buttons.extend(["down" if row > start_cell[0] else "up"] * abs(row - start_cell[0]))
                buttons.extend(["queen_blood_right" if column > start_cell[1] else "queen_blood_left"]
                               * abs(column - start_cell[1]))
                moves += len(buttons)
                if moves > max_board_steps + len(QUEEN_BLOOD_HOME_BUTTONS):
                    raise SafetyError("the placement cursor did not reach the target cell")
                burst(buttons, board_milliseconds, "move_placement", QUEEN_BLOOD_BOARD_GAP_SECONDS)
                sample = wait(sample, QUEEN_BLOOD_ARRIVAL_SECONDS, at_target)
                if not at_target(sample):
                    sample = self._wait_for_settled_queen_blood_value(
                        sample.sequence, deadline, trace, self._queen_blood_placement_key
                    )
            timings["board_seconds"] = round(self._monotonic() - started, 2)
            if moves:
                # The card slides to its new cell after the preview already
                # names it; a Confirm sent then was ignored (live 2026-09-30).
                still.update(location=None, since=None)
                sample = wait(sample, QUEEN_BLOOD_HELD_CARD_SETTLE_LIMIT_SECONDS, held_card_still)
                timings["card_still_seconds"] = round(self._monotonic() - started, 2)

            def committed(item: Any) -> bool:
                # The cursor can disappear while the opponent responds. Only
                # the intended card on the requested cell proves a placement;
                # neither a vanished preview nor an occupied *other* cell does.
                current = self._queen_blood_state_or_none(item)
                target_cell = current.cell(row, column) if current else None
                return (
                    current is not None
                    and item.sequence > state.sequence
                    and item.running
                    and item.source == state.source
                    and (item.source == "simulation" or item.age_seconds() <= self._queen_blood_freshness_limit())
                    and not current.placement.active
                    and target_cell is not None
                    and target_cell.card_present
                    and target_cell.owner == "player"
                    and target_cell.card_id == card.card_id
                )

            def placement_response(item: Any, verified: bool, reason: str | None = None) -> dict[str, Any]:
                timings["place_seconds"] = round(self._monotonic() - started, 2)
                timings["clock_end"] = round(self._wall_clock().timestamp(), 3)
                after = self._queen_blood_state_or_none(item)
                target_after = after.cell(row, column) if after else None
                copies_after = (
                    sum(hand_card.card_id == card.card_id for hand_card in after.hand)
                    if after and all(identified_card_id(hand_card.card_id) for hand_card in after.hand) else None
                )
                result = {
                    "ok": verified,
                    "action": "place_queens_blood_card",
                    "verified": verified,
                    "outcome": "card_placed" if verified else "placement_unverified",
                    "card_left_hand": copies_after == copies_before - 1 if copies_after is not None else None,
                    "pre_hand_card_count": copies_before,
                    "post_hand_card_count": copies_after,
                    "retry_safe": False if not verified else None,
                    "post_state": {
                        "phase": after.phase if after else None,
                        "turn": after.turn if after else None,
                        "placement_active": after.placement.active if after else None,
                        "cursor_cell": list(self._queen_blood_cursor_cell(after)) if after and after.placement.active
                                       and self._queen_blood_cursor_cell(after) is not None else None,
                        "target_cell": {
                            "card_present": target_after.card_present,
                            "card_id": target_after.card_id,
                            "owner": target_after.owner,
                        } if target_after else None,
                        "hand_card_copies": copies_after,
                    },
                    "hand_index": hand_index,
                    "card_id": card.card_id,
                    "name": card.name,
                    "row": row,
                    "column": column,
                    "pre_sequence": state.sequence,
                    "post_sequence": item.sequence,
                    "placement_steps": steps,
                    "hand_trace": hand_trace,
                    "inputs": inputs,
                    "timings": timings,
                    "queen_blood": item.queen_blood,
                }
                if reason is not None:
                    result["input_error"] = reason
                if not verified:
                    result.update(reason=reason or "board_hand_or_placement_state_ambiguous",
                                  requires_fresh_observation=True)
                self._record_action(**result)
                return result

            verified = False
            stage = "placement_confirm"
            for attempt in range(1, 3):
                # A backend error can arrive after the game accepted input.
                confirm_attempted = True
                inputs.append({"button": "confirm", "reason": "place_card", "attempt": attempt})
                self._tap_queen_blood_confirm(confirm_milliseconds)
                sample = wait(sample, 8.0, committed)
                if committed(sample):
                    verified = True
                    picked_up = False
                    break
                # Do not infer failure from the preview vanishing. Reconcile
                # a *fresh* observation before deciding whether to send any
                # more input: the card may already be on the board.
                previous_sequence = sample.sequence
                sample = wait(sample, 1.0, committed)
                if committed(sample):
                    verified = True
                    picked_up = False
                    break
                current = self._queen_blood_state_or_none(sample)
                target_cell = current.cell(row, column) if current else None
                if not (
                    attempt == 1
                    and sample.sequence > previous_sequence
                    and current is not None
                    and current.active
                    and current.phase == "player_turn"
                    and current.turn == queen_blood.turn
                    and current.placement.active
                    and current.placement.target == target
                    and self._queen_blood_cursor_cell(current) == target
                    and target_cell is not None
                    and not target_cell.card_present
                    and sum(item.card_id == card.card_id for item in current.hand) == copies_before
                    and any(item.hand_index == hand_index and item.card_id == card.card_id
                            for item in current.hand)
                ):
                    # Unknown/changed state is not permission to retry or
                    # cancel. Give the exporter/opponent a bounded input-free
                    # recovery window; only the exact landed card can resolve it.
                    stage = "placement_reconciliation"
                    sample = wait(sample, QUEEN_BLOOD_PLACEMENT_RECONCILE_SECONDS, committed)
                    if committed(sample):
                        verified = True
                        picked_up = False
                    break
            return placement_response(sample, verified)
        except (SafetyError, QueenBloodError) as exc:
            if confirm_attempted:
                # Reconcile a fresh board after a reported input error; never
                # replay Confirm or Cancel against an uncertain next screen.
                try:
                    latest = self.telemetry.observe()
                except Exception as observe_exc:
                    raise SafetyError("placement outcome unknown; stop input and observe the board and hand",
                                      details=failure_details()) from observe_exc
                verified = latest.sequence > state.sequence and committed(latest)
                if not verified:
                    try:
                        stage = "placement_reconciliation"
                        latest = wait(latest, QUEEN_BLOOD_PLACEMENT_RECONCILE_SECONDS, committed)
                        verified = latest.sequence > state.sequence and committed(latest)
                    except (SafetyError, QueenBloodError):
                        pass  # Still unknown: never retry Confirm or Cancel.
                if verified:
                    picked_up = False
                return placement_response(latest, verified, str(exc))
            if picked_up:
                try:
                    latest = self.telemetry.observe()
                    trace.append({"sequence": latest.sequence, "queen_blood": latest.queen_blood,
                                  "stage": "recovery_observation"})
                    current = self._queen_blood_state_or_none(latest)
                    if current is not None and current.placement.active:
                        inputs.append({"button": "queen_blood_cancel", "reason": "cancel_observed_held_card"})
                        self._tap_queen_blood_confirm_or_button("queen_blood_cancel", confirm_milliseconds)
                except Exception:
                    pass
            details = failure_details()
            if isinstance(exc, SafetyError) and exc.details:
                details["verification"] = deepcopy(exc.details)
            self._record_action(ok=False, verified=False, error=str(exc), **details)
            if isinstance(exc, SafetyError):
                exc.details = details
                raise
            raise SafetyError(str(exc), details=details) from exc
        finally:
            self._queen_blood_geometry.save()
            try:
                self.input.release_all()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def queens_blood_card_vendors(self, area: str | None = None) -> dict[str, Any]:
        """Where Queen's Blood cards can be bought (read-only, no game input).

        Vendors from the offline catalogue, optionally only those in ``area``
        (a field id such as ``1110-KALMT`` or its name, e.g. "Kalm"), each
        stock row marked with which of its cards the loaded save already owns.
        A row appears in the shop once its ``view_flag`` story flag is set.
        """
        from .qb_card_vendors import vendors_for
        from .navigation_values import MissionError

        profile = self.queen_blood_profile()
        owned = None
        if profile is not None:
            owned = {card: int(entry.get("owned", 0)) for card, entry in profile.inventory()["cards"].items()}
        try:
            vendors = vendors_for(area, owned=owned)
        except MissionError as exc:
            raise SafetyError(str(exc)) from exc
        return {"ok": True, "action": "queens_blood_card_vendors", "area": area,
                "save_profile": profile.profile_id if profile is not None else None,
                "owned_known": owned is not None, "vendors": vendors}

    def queen_blood_profile(self) -> QueenBloodProfile | None:
        """Records for the verified loaded slot and declared model only."""
        status = self.get_queens_blood_run_status()
        if status["profile_id"] is None:
            return None
        return QueenBloodProfile(Path(self._queen_blood_profile_root), status["profile_id"])

    def get_queens_blood_run_status(self) -> dict[str, Any]:
        state_dir = self._public_stream_event_dir.parent if self._public_stream_event_dir is not None else None
        driver = match_record_target(state_dir)[0] if state_dir is not None else None
        status = (self._queen_blood_runs.status(driver, queen_blood_process_identity(self._environment))
                  if self._queen_blood_runs is not None else
                  {"ok": True, "driver": driver, "assignment": None, "loaded_save": None,
                   "loaded_save_verified": False, "profile_id": None, "run_loaded": False,
                   "efforts_share_run": True})
        status["driver_state"] = {
            "directory": str(state_dir) if state_dir is not None else None,
            "stream_live": stream_is_live(state_dir) if state_dir is not None else False,
            "rehearsal_live": rehearsal_is_live(state_dir) if state_dir is not None else False,
        }
        if state_dir is not None:
            declaration_path = state_dir / ("public-driver.json" if status["driver_state"]["stream_live"]
                                             else "public-driver-rehearsal.json")
            try:
                declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
                status["driver_state"]["declaration"] = {
                    key: declaration.get(key) for key in
                    ("version", "driver", "modelId", "effort", "runId", "streamStartedAt", "declaredAt")
                }
            except (OSError, ValueError, AttributeError) as exc:
                status["driver_state"]["declaration_error"] = type(exc).__name__
        if self._public_stream_event_dir is not None:
            loaded, assigned = status["loaded_save"], status["assignment"]
            write_queen_blood_json(self._public_stream_event_dir.parent / "public-model-run.json", {
                "version": 1, "at": queen_blood_now(), "driver": driver["driver"] if driver else None,
                "modelId": driver["model_id"] if driver else None,
                "declarationRunId": driver["run_id"] if driver else None,
                "slot": loaded["slot"] if loaded else None,
                "assignedSlot": assigned["slot"] if assigned else None,
            })
        return status

    def get_queens_blood_preparation_context(self, opponent: str) -> dict[str, Any]:
        """Read planning inputs without a UI read, publication or input lock."""
        from .queen_blood_preparation import prepare_runtime_snapshot
        if self._queen_blood_profile_root is None or self._public_stream_event_dir is None:
            raise SafetyError("a verified loaded-save profile is required for deck planning")
        try:
            return prepare_runtime_snapshot(root=Path(self._queen_blood_profile_root),
                state_dir=self._public_stream_event_dir.parent, environment=self._environment,
                arguments={"opponent": opponent}, context_only=True)
        except (ValueError, OSError, TypeError) as exc:
            raise SafetyError(str(exc)) from exc

    def prepare_queens_blood_deck(self, cards: list[str], opponent: str, rationale: str,
                                fallback: list[str] | None = None,
                                expected_snapshot_sha256: str | None = None) -> dict[str, Any]:
        """Plan from a saved ownership snapshot, without UI reads or the input lock."""
        from .queen_blood_preparation import prepare_deck
        run = self.get_queens_blood_run_status()
        if not run.get("profile_id") or self._queen_blood_profile_root is None:
            raise SafetyError("a verified loaded-save profile is required for deck preparation")
        inventory = QueenBloodProfile(Path(self._queen_blood_profile_root), run["profile_id"]).inventory()
        try:
            return prepare_deck(run, inventory, cards=cards, opponent=opponent, rationale=rationale,
                                fallback=fallback, expected_snapshot_sha256=expected_snapshot_sha256)
        except ValueError as exc:
            raise SafetyError(str(exc)) from exc

    def revalidate_queens_blood_prepared_deck(self, candidate: dict[str, Any], opponent: str) -> dict[str, Any]:
        """Recheck the snapshot only; the normal editor still verifies deck select."""
        from .queen_blood_preparation import revalidate_deck
        run = self.get_queens_blood_run_status()
        if not run.get("profile_id") or self._queen_blood_profile_root is None:
            raise SafetyError("the prepared deck's loaded-save profile is no longer verified")
        inventory = QueenBloodProfile(Path(self._queen_blood_profile_root), run["profile_id"]).inventory()
        try:
            return revalidate_deck(candidate, run, inventory, opponent=opponent)
        except ValueError as exc:
            raise SafetyError(str(exc)) from exc

    def _queen_blood_campaign_assignment(self) -> dict[str, Any]:
        status = self.get_queens_blood_run_status()
        assignment = status.get("assignment")
        if self._queen_blood_campaign is None or not isinstance(assignment, dict):
            raise SafetyError("declare and assign the model run before using its Queen's Blood campaign")
        return assignment

    def get_campaign_status(self) -> dict[str, Any]:
        """Persistent regional campaign state for the declared model run."""
        assignment = self._queen_blood_campaign_assignment()
        campaign = self._queen_blood_campaign
        if campaign is None:
            raise SafetyError("Queen's Blood campaign persistence is unavailable")
        try:
            if self._campaign_recording_error is not None:
                raise SafetyError(self._campaign_recording_error)
            return campaign.status(assignment)
        except (OSError, ValueError) as exc:
            raise SafetyError(str(exc)) from exc

    def get_campaign_next_step(self) -> dict[str, Any]:
        """The next routed regional opponent; never chooses a deck or move."""
        assignment = self._queen_blood_campaign_assignment()
        campaign = self._queen_blood_campaign
        if campaign is None:
            raise SafetyError("Queen's Blood campaign persistence is unavailable")
        try:
            return campaign.next_step(assignment)
        except (OSError, ValueError) as exc:
            raise SafetyError(str(exc)) from exc

    def assign_queens_blood_run(self, slot: Any) -> dict[str, Any]:
        """Assign an existing working save to the declared model; no game input."""
        from .save_slot_load import resolve_save_slot
        status = self.get_queens_blood_run_status()
        if self._queen_blood_runs is None or not status["driver"]:
            raise SafetyError("declare the driver before assigning its model run")
        target = resolve_save_slot(slot)
        assigned = self._queen_blood_runs.assign(status["driver"], target["file"])
        return {**self.get_queens_blood_run_status(), "assignment": assigned, "input_sent": False}

    def _campaign_speech_context(self) -> dict[str, Any] | None:
        from .queen_blood_speech_receipt import binding
        if self._public_stream_event_dir is None:
            return None
        mode = "rehearsal" if match_record_target(self._public_stream_event_dir.parent)[2] == "qb-match-history-rehearsal" else "live"
        return binding(self.get_queens_blood_run_status(), mode)

    def _claim_campaign_speech_before_input(self) -> None:
        """Require a receipt only for actual live or explicitly narrated rehearsal."""
        self._campaign_speech = None
        campaign = self._queen_blood_campaign
        if campaign is None or not campaign.path.exists() or self._public_stream_event_dir is None:
            return
        state_dir = Path(self._public_stream_event_dir).parent
        actual_live = stream_is_live(state_dir)
        worker_mode = requested_session_mode(state_dir)
        if actual_live and worker_mode == "live" and worker_policy_enabled(state_dir):
            # Worker mode never falls back to model-authored synchronous speech,
            # even while its independent narrator is unavailable.
            return
        if not actual_live:
            if offline_recording_context(state_dir):
                return
            if worker_mode == "rehearsal" and rehearsal_is_live(state_dir):
                # Explicit narrated-test mode is worker-driven. The player does
                # not become a synchronous fallback if the independent worker
                # or its voice lease is temporarily unavailable.
                return
            if not rehearsal_is_live(state_dir) or not narrated_rehearsal_lease_present(state_dir):
                return
        from .queen_blood_speech_receipt import binding, consume_locked
        status = self.get_queens_blood_run_status()
        mode = "live" if actual_live else "rehearsal"
        with self._agent_speech._shared_gate(self._public_stream_event_dir) as acquired:
            if acquired:
                self._campaign_speech = consume_locked(self._public_stream_event_dir, binding(status, mode))
        if self._campaign_speech is None:
            raise SafetyError("a fresh successful run-bound say_on_stream line is required before this campaign move")

    def _emit_live_narration_action_event(self, values: dict[str, Any], kind: str, cooldown: float) -> None:
        sink = getattr(self, "_live_narration_event_sink", None)
        state_dir = Path(self._public_stream_event_dir).parent if self._public_stream_event_dir is not None else None
        if (sink is None or state_dir is None or kind == "queens_blood_turn"
                or not getattr(self.input, "is_real", False)):
            return
        facts = action_facts(kind, values)
        action = str(values.get("action") or "")
        if cooldown > 0:
            time_key = str(int(time.time() // cooldown))
        else:
            time_key = str(values.get("timestamp_utc") or "")
        source_key = f"{kind}:{action}:{time_key}:{json.dumps(facts, separators=(',', ':'))}"
        try:
            sink.emit(kind, facts, source_key=source_key)
        except Exception:
            pass  # Advisory narrator events can never interrupt game actions.

    def _emit_live_narration_qb_exchange(self, story_id: str, state: Any, cues: list[dict[str, Any]],
                                         projection: dict[str, Any] | None,
                                         projection_change: Any) -> None:
        sink = getattr(self, "_live_narration_event_sink", None)
        if sink is None or not story_id or state is None:
            return
        facts = queens_blood_exchange_facts(cues, projection, projection_change)
        if not facts:
            return
        turn = getattr(state, "turn", None)
        source_key = f"{story_id}:{turn if isinstance(turn, int) else 'unknown'}:{json.dumps(facts, separators=(',', ':'))}"
        try:
            sink.emit("queens_blood_exchange", facts, source_key=source_key)
        except Exception:
            pass  # Advisory narrator events can never interrupt a match.

    def _record_campaign_action(self, record: dict[str, Any]) -> None:
        """Record evidence only against the verified authoritative model run."""
        if self._queen_blood_campaign is None:
            return
        try:
            status = self.get_queens_blood_run_status()
            assignment = status.get("assignment")
            if not status.get("run_loaded") or not isinstance(assignment, dict):
                return
            action = dict(record)
            # Only actual match actions may inherit the active match opponent.
            # Route/capture hooks must never reuse an old completed match.
            if (action.get("action") in {"place_queens_blood_card", "pass_queens_blood_turn", "finish_queens_blood_match"}
                    and not action.get("opponent") and self._queen_blood_match):
                action["opponent"] = self._queen_blood_match.get("opponent")
            if self._action_log is not None:
                action["action_log_path"] = str(self._action_log)
            mode = "rehearsal" if match_record_target(self._public_stream_event_dir.parent)[2] == "qb-match-history-rehearsal" else "live"
            speech = None
            if action.get("action") in {"place_queens_blood_card", "pass_queens_blood_turn"}:
                from .queen_blood_speech_receipt import binding
                claimed = self._campaign_speech
                if isinstance(claimed, dict) and claimed.get("context") == binding(status, mode):
                    speech = claimed
                action["speech_receipt_verified"] = speech is not None
                record["speech_receipt_verified"] = speech is not None
                record["speech"] = speech
                if speech is not None:
                    self._qb_recorder.capture("speech_receipt", {"action": action.get("action"), "at": record.get("timestamp_utc"), "speech": speech})
            self._queen_blood_campaign.record_action(assignment, action, speech, mode=mode)
        except (ValueError, OSError, SafetyError) as exc:
            self._campaign_recording_error = f"campaign evidence could not be persisted: {exc}"
        finally:
            if record.get("action") in {"place_queens_blood_card", "pass_queens_blood_turn"}:
                self._campaign_speech = None

    def record_campaign_runtime_outcome(self, operation: str, arguments: dict[str, Any],
                                        response: dict[str, Any]) -> None:
        """Internal dispatcher hook for route/lookup failures and screenshot evidence."""
        if operation == "run_finish_conversation":
            operation = "finish_conversation"
        if operation not in {"resolve_world_target", "travel_to_map_spot", "run_live_route_adapter",
                             "capture_game_window", "plan_npc_approach", "finish_conversation"}:
            return
        navigation = response.get("navigation_objective") or {}
        run = response.get("run") or {}
        preflight = response.get("preflight") or {}
        failed = response.get("ok") is False or response.get("outcome") in {
            "blocked", "failed", "stuck", "dialogue_active", "field_telemetry_unavailable"}
        failed = failed or navigation.get("state") == "NAVIGATION_OBJECTIVE_ABORTED"
        if operation == "run_live_route_adapter" and arguments.get("dry_run") is True:
            return
        succeeded = (response.get("ok") is True or navigation.get("state") == "NAVIGATION_OBJECTIVE_COMPLETED"
                     or operation == "travel_to_map_spot" and response.get("state") == "arrived"
                     or operation == "capture_game_window")
        if not failed and not succeeded:
            return
        self._record_campaign_action({"timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                      "action": operation,
                                      **response, "ok": not failed,
                                      "reason": response.get("reason") or run.get("reason") or preflight.get("reason") or response.get("status"),
                                      "opponent": arguments.get("name") or arguments.get("target_id"),
                                      "arguments": arguments})

    def save_game_progress(self, slot: Any = None, execute: bool = False, calibrate_prompt: dict | None = None,
                           resolve_calibration: dict | None = None, test_save: bool = False,
                           probe_prompt: bool = False, story_save: bool = False) -> dict[str, Any]:
        """Save to the run's slot (live streams only) or, for development, to
        the operator's FF7R_TEST_SAVE_SLOT (``test_save``). ``story_save``
        writes the operator's FF7R_STORY_SAVE_SLOT, a story run outside the
        campaign, in any stream mode. ``probe_prompt`` opens the Save prompt,
        selects Yes for a screenshot and cancels."""
        from .save_slot_save import SaveSlotWriter, SaveCalibrationResolver
        if resolve_calibration is not None:
            if slot is not None or calibrate_prompt is not None or test_save or probe_prompt or story_save:
                raise SafetyError("save resolution cannot be combined with saving")
            return SaveCalibrationResolver(self).run(execute=execute, **resolve_calibration)
        mode = self._queen_blood_record_mode()
        refusal = None
        if test_save is True and mode == "live":
            refusal = "save_test_slot_not_on_live_stream"
        elif (execute is True and calibrate_prompt is None and test_save is not True
                and story_save is not True and probe_prompt is not True and mode != "live"):
            # Owner decision 2026-10-08: the run's slot is written only during
            # a live stream; rehearsals and development never overwrite it.
            refusal = "save_requires_live_stream"
        if refusal:
            return {"ok": False, "execute": execute, "saved": False, "state": "save_refused",
                    "decision": refusal, "input_sent": False, "presses": []}
        result = SaveSlotWriter(self).run(slot=slot, execute=execute, calibrate_prompt=calibrate_prompt,
                                          test_save=test_save, probe_prompt=probe_prompt, story_save=story_save)
        if (result.get("saved") and calibrate_prompt is None and not result.get("test_save")
                and not result.get("story_save")):
            from .working_save_recovery import arm, receipt_path
            from .queen_blood_profile import _now, _read
            profile = self.queen_blood_profile()
            if profile is not None:
                receipt = (result.get("receipt") or {}).get("loaded_save") or {}
                result["campaign_wins_saved"] = profile.mark_live_wins_saved(
                    saved_at=_now(), receipt=receipt.get("load_id"))
            prior = _read(receipt_path(self))
            if prior.get("phase") == "armed":
                try:
                    result["recovery_target"] = arm(self)
                except Exception as exc:
                    result["recovery_target"] = {"ok": False, "decision": str(exc)}
        return result

    def arm_working_save_recovery(self) -> dict[str, Any]:
        from .working_save_recovery import arm
        return arm(self)

    def disarm_working_save_recovery(self) -> dict[str, Any]:
        from .working_save_recovery import disarm
        return disarm(self)

    def invalidate_loaded_save(self, reason: str) -> None:
        if self._queen_blood_runs is not None:
            self._queen_blood_runs.invalidate(reason)
            self.get_queens_blood_run_status()

    def begin_save_load(self, file: dict[str, Any]) -> dict[str, Any] | None:
        if self._queen_blood_runs is None:
            return None
        candidate = self._queen_blood_runs.begin_load(file, queen_blood_process_identity(self._environment))
        self.get_queens_blood_run_status()
        return candidate

    def complete_save_load(self, candidate: dict[str, Any] | None) -> dict[str, Any]:
        if self._queen_blood_runs is None or candidate is None:
            return {"verified": False, "reason": "loaded_save_tracking_unavailable"}
        receipt = self._queen_blood_runs.complete_load(candidate, queen_blood_process_identity(self._environment))
        self.get_queens_blood_run_status()
        return receipt

    def _load_queen_blood_opponent_cards(self, opponent_key: str | None) -> list[str]:
        """Card ids this opponent has been seen playing in earlier matches."""
        profile = self.queen_blood_profile()
        if opponent_key is not None and profile is not None:
            known = profile.opponent(opponent_key).get("card_ids")
            if known:
                return [str(item) for item in known]
            return []  # per-machine observations never seed a model's run
        if self._queen_blood_profile_root is not None:
            return []
        return self._load_legacy_queen_blood_opponent_cards(opponent_key)

    def _queen_blood_opponent_record(self, opponent_key: str | None) -> Any:
        """The game-data record (deck, AI) of a known NPC opponent, or None.

        Matched by the requested name or id, then by the CardGameOpponent
        name key the deck screen reported for it (recorded in the profile)."""
        if opponent_key is None:
            return None
        name_id = None
        profile = self.queen_blood_profile()
        if profile is not None:
            try:
                name_id = profile.opponent(opponent_key).get("name_id")
            except (OSError, ValueError, AttributeError):
                name_id = None
        return find_queen_blood_opponent(opponent_key, name_id if isinstance(name_id, str) else None)

    def _load_legacy_queen_blood_opponent_cards(self, opponent_key: str | None) -> list[str]:
        """The pre-profile, per-machine opponent file (migrated on first use)."""
        path = self._queen_blood_opponents_file
        if opponent_key is None or path is None:
            return []
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            cards = data.get("opponents", {}).get(opponent_key, {}).get("card_ids", [])
            return [str(item) for item in cards if isinstance(item, str)]
        except (OSError, ValueError, AttributeError):
            return []

    def _save_queen_blood_opponent_cards(self, opponent_key: str | None, card_ids: list[str]) -> None:
        profile = self.queen_blood_profile()
        if opponent_key is not None and profile is not None:
            try:
                profile.update_opponent(opponent_key, card_ids=card_ids)
            except OSError:
                pass
            return
        if self._queen_blood_profile_root is not None:
            return
        path = self._queen_blood_opponents_file
        if opponent_key is None or path is None:
            return
        path = Path(path)
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            opponents = data.setdefault("opponents", {})
            opponents[opponent_key] = {"card_ids": list(card_ids)}
            data["schema_version"] = 1
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            pass

    # -- deck editor -----------------------------------------------------

    @classmethod
    def _deck_screen(cls, state: Any) -> dict[str, Any]:
        payload = cls._queen_blood_payload(state)
        screen = payload.get("deck_screen") if isinstance(payload, dict) else None
        return screen if isinstance(screen, dict) else {}

    @classmethod
    def _deck_active_area(cls, screen: dict[str, Any]) -> str | None:
        """The deck-screen area whose cursor moved last: Edit (owned grid),
        Strip (deck strip above it) or Player (deck-select deck list)."""
        cursors = (screen.get("editor") or {}).get("cursors") or {}
        best, best_sequence = None, None
        for area in QUEEN_BLOOD_DECK_AREAS:
            if area in {"Edit", "Strip"} and (screen.get("windows") or {}).get("CardDeckList") is False:
                continue  # Hook cursors survive closing the editor.
            cursor = cursors.get(area)
            if not isinstance(cursor, dict) or cursor.get("sequence") is None:
                continue
            if best_sequence is None or float(cursor["sequence"]) > best_sequence:
                best, best_sequence = area, float(cursor["sequence"])
        return best

    @classmethod
    def _deck_area_hovered(cls, screen: dict[str, Any], area: str) -> str | None:
        """The card in the visible details popup belonging to ``area``.

        Live 2026-09-26 the owned grid and the deck strip each have a
        MainMenu_Card_CardInformation_C popup, and the deck-select list has
        its own ``Information`` popup, which stays marked visible under the
        editor. Both editor popups can read as visible at once, one with a
        stale card (live 2026-10-06: the strip's card check read the grid's
        Fleetwing while the strip cursor was on a Grasslands Wolf), so each
        area reads only its own. The two are top-level widgets told apart
        by their object number: the strip's is the lower (live 2026-10-06,
        two editor sessions: _2147476171/_2147476173 and
        _2147474947/_2147474949, strip first).
        """
        detail = screen.get("detail") or {}
        resolved = detail.get("resolved")
        if not isinstance(resolved, list):
            hovered = detail.get("hovered")
            return hovered.get("card_id") if isinstance(hovered, dict) else None
        entries = [entry for entry in resolved if isinstance(entry, dict)]
        if area == "Player":
            for entry in entries:
                if entry.get("popup_shown") is True and "CardList_Player" in str(entry.get("path") or ""):
                    return entry.get("card_id")
            return None
        if area not in {"Edit", "Strip"}:
            return None
        editor_popups = sorted(
            (entry for entry in entries
             if str(entry.get("popup") or "").startswith("MainMenu_Card_CardInformation_C")),
            key=lambda entry: cls._popup_number(str(entry.get("popup") or "")))
        if len(editor_popups) >= 2:
            entry = editor_popups[0] if area == "Strip" else editor_popups[1]
            return entry.get("card_id") if entry.get("popup_shown") is True else None
        for entry in editor_popups:
            if entry.get("popup_shown") is True:
                return entry.get("card_id")
        return None

    @staticmethod
    def _popup_number(name: str) -> int:
        """The object number ending a widget name (MainMenu_..._C_2147474947)."""
        match = re.search(r"_(\d+)$", name)
        return int(match.group(1)) if match else 0

    @classmethod
    def _deck_editor_view(cls, state: Any, area: str = "Edit") -> tuple[tuple[int, int] | None, str | None]:
        """(cursor, hovered card id) for one deck-screen area.

        The strip's cursor is reported as (0, index). The hovered card is only
        given while ``area`` is the area that moved last (a stale popup
        otherwise names a card elsewhere).
        """
        screen = cls._deck_screen(state)
        if area in {"Edit", "Strip"} and (screen.get("windows") or {}).get("CardDeckList") is False:
            return None, None
        active = cls._deck_active_area(screen)
        if area in {"Edit", "Strip"} and active == "Player":
            return None, None  # Visible archetype widgets can outlive the real editor.
        cursor = ((screen.get("editor") or {}).get("cursors") or {}).get(area)
        position = None
        if isinstance(cursor, dict) and cursor.get("index") is not None:
            row = 0 if area == "Strip" else cursor.get("row")
            if row is not None:
                position = (int(row), int(cursor["index"]))
        card_id = cls._deck_area_hovered(screen, area) if active in {None, area} else None
        return position, card_id

    @classmethod
    def _deck_total(cls, state: Any) -> int | None:
        """Cards in the deck. Live 2026-09-26 the deck-select list's total
        (CardList_Player) does not change while the editor is open; the
        editor header's own (CardDeckList) does, so it is read while the
        grid or strip is active."""
        screen = cls._deck_screen(state)
        total = (screen.get("deck") or {}).get("total")
        if cls._deck_active_area(screen) in {"Edit", "Strip"}:
            for entry in (screen.get("detail") or {}).get("totals") or []:
                if isinstance(entry, dict) and ".CardDeckList." in str(entry.get("path") or ""):
                    total = entry.get("text")
                    break
        # A short deck is drawn red: "<color=red>14</color>/15" (live).
        plain = re.sub(r"<[^>]*>", "", str(total))
        try:
            return int(plain.split("/")[0])
        except (TypeError, ValueError):
            return None

    def _deck_screen_texts(self) -> list[tuple[str, str]]:
        """The visible menu texts as (widget path, text)."""
        from .map_query import request_snapshot
        response, _ = request_snapshot(self, heights=False, flags=("menutexts",), require_map=False)
        if response.get("texts_scope") != "menu":
            response, _ = request_snapshot(self, heights=False, flags=("alltexts",), require_map=False)
        return [(str(item.get("path", "")), str(item.get("text", "")))
                for item in response.get("texts") or [] if isinstance(item, dict)]

    def _deck_screen_mode(self) -> str | None:
        """Which deck screen holds the input, from the main button guide
        (MainMenu_Card_Top.Txt_Guide): "select" ("L1/R1 Change Deck ...") on
        deck select with the editor closed, "editor" ("Jump to Card ...")
        with the strip focused, or "View Deck ... Craft Prebuilt Deck" with
        the collection grid focused, "tips" while a tips panel covers them, "other" for
        anything else or an unreadable guide, None without a live text
        reader (a simulated or test telemetry source). The game's window
        flags cannot tell deck select from the editor (live 2026-10-06)."""
        if getattr(self.telemetry, "path", None) is None:
            return None
        try:
            texts = self._deck_screen_texts()
        except Exception:
            return "other"
        if any(QUEEN_BLOOD_TIPS_PATH in path for path, _ in texts):
            return "tips"
        guide = next((text.replace("\xa0", " ") for path, text in texts
                      if path.endswith(QUEEN_BLOOD_DECK_GUIDE_SUFFIX)), None)
        if guide is None:
            return "other"
        if (QUEEN_BLOOD_DECK_EDITOR_GUIDE in guide
                or ("View Deck" in guide and "Craft Prebuilt Deck" in guide)):
            return "editor"
        return "select" if QUEEN_BLOOD_DECK_SELECT_GUIDE in guide else "other"

    def _deck_screen_mode_without_tips(self) -> str | None:
        """The deck screen mode once any tips panel over it is closed."""
        mode = self._deck_screen_mode()
        for _attempt in range(3):
            if mode != "tips":
                return mode
            self._ensure_game_foreground()
            self.input.tap("cancel", QUEEN_BLOOD_TIPS_CLOSE_MS)
            self.input.release_all()
            self._sleeper(1.0)
            mode = self._deck_screen_mode()
        return mode

    def _assert_deck_select_for_begin(self) -> None:
        """Begin Match is F3, then Confirm. On deck select that starts the
        match; in the open editor F3 is Craft Prebuilt Deck, and its
        "Overwrite Deck #1 with Starter Deck?" opens on Yes, so the Confirm
        overwrote the deck (live 2026-10-06, after a failed edit left the
        editor open). Refuse unless deck select is shown."""
        mode = self._deck_screen_mode_without_tips()
        if mode not in {None, "select"}:
            raise SafetyError(f"Begin Match refused: deck select is not shown ({mode})")

    def _deck_editor_current(self, timeout: float = 3.0, area: str = "Edit") -> tuple[Any, Any]:
        """The area's view, re-reading briefly past samples without the deck
        screen (the probe's pre-match branch can momentarily omit it)."""
        deadline = self._monotonic() + timeout
        view = self._deck_editor_view(self._deck_observe(), area)
        while view[0] is None and self._monotonic() < deadline:
            self._sleeper(0.3)
            view = self._deck_editor_view(self._deck_observe(), area)
        return view

    def _deck_key(self, button: str) -> None:
        """One keyboard press on the deck screens, which only take input focused."""
        if getattr(self.input, "is_real", False) and self._session_lease is not None:
            self._rearm_active_session()
        self._ensure_game_foreground()
        self.input.tap(button, QUEEN_BLOOD_DECK_KEY_MS)
        self.input.release_all()

    def _deck_editor_settle(
        self, previous: tuple[Any, Any] | None, timeout: float, area: str = "Edit",
    ) -> tuple[tuple[int, int] | None, str | None]:
        """Wait for the editor view to change from ``previous`` and hold still.

        The details popup is sampled about once a second, so a view counts as
        settled once it has held for QUEEN_BLOOD_DECK_SETTLE_SECONDS.
        """
        deadline = self._monotonic() + timeout
        view = self._deck_editor_view(self._deck_observe(), area)
        since = self._monotonic()
        while self._monotonic() < deadline:
            self._sleeper(0.3)
            current = self._deck_editor_view(self._deck_observe(), area)
            if current != view:
                view, since = current, self._monotonic()
                continue
            if view != previous and self._monotonic() - since >= QUEEN_BLOOD_DECK_SETTLE_SECONDS:
                return view
        return view

    def _deck_editor_goto(
        self, row: int, index: int, max_presses: int = 40, area: str = "Edit",
    ) -> tuple[Any, Any]:
        view = self._deck_editor_current(area=area)
        stalls = 0
        for _press in range(max_presses):
            if view[0] is None:
                view = self._deck_editor_current(area=area)
            position = view[0]
            if position == (row, index):
                return view
            if position is None:
                raise SafetyError("the deck editor cursor is not observable")
            if position[0] < row and position[1] != index:
                # Change columns in the full row before descending into a
                # short final row; ascend before changing columns there.
                button = "queen_blood_right" if index > position[1] else "queen_blood_left"
            elif position[0] != row:
                button = "down" if row > position[0] else "up"
            else:
                button = "queen_blood_right" if index > position[1] else "queen_blood_left"
            self._deck_key(button)
            moved = self._deck_editor_settle(view, 5.0, area)
            if moved[0] is not None and button == "down" and moved[0][0] < position[0]:
                raise SafetyError(f"the deck {area} has no row {row} (Down wrapped to the top)")
            # Two presses that do not move the cursor: the cell is not there.
            stalls = stalls + 1 if moved[0] == position else 0
            if stalls >= 2:
                raise SafetyError(f"the deck {area} cursor cannot reach ({row}, {index})")
            view = moved
        raise SafetyError(f"the deck {area} cursor did not reach ({row}, {index})")

    def _ensure_deck_details_live(self) -> tuple[Any, Any]:
        """Make sure the details popup follows the grid cursor.

        With details off the popup's text blocks keep their last card and
        still read as shown (live 2026-09-26), so a hovered card is only
        trusted once a cursor step changes it: the owned grid lists each card
        once. Otherwise Toggle Details (F4) is pressed and the check repeated.
        """
        for _attempt in range(3):
            before = self._deck_editor_current()
            if before[0] is None:
                raise SafetyError("the deck editor cursor is not observable")
            forward, back = "queen_blood_right", "queen_blood_left"
            self._deck_key(forward)
            after = self._deck_editor_settle(before, 4.0)
            if after[0] == before[0]:
                forward, back = back, forward
                self._deck_key(forward)
                after = self._deck_editor_settle(before, 4.0)
            live = after[0] != before[0] and after[1] is not None and after[1] != before[1]
            if after[0] != before[0]:
                self._deck_key(back)
                self._deck_editor_settle(after, 4.0)
            if live:
                return self._deck_editor_view(self._deck_observe())
            self._deck_key("queen_blood_details")
            self._sleeper(1.5)
            self._deck_observe(full=True)
        raise SafetyError("the deck editor's card details do not follow the cursor")

    def _deck_cell_card(self, area: str, target: tuple[int, int], timeout: float = 8.0) -> str | None:
        """The card at ``target`` once the cursor is there and the popup has
        had a full refresh (neighbouring cells can hold the same card)."""
        deadline = self._monotonic() + timeout
        arrived: float | None = None
        card: str | None = None
        while self._monotonic() < deadline:
            view = self._deck_editor_view(self._deck_observe(), area)
            if view[0] != target:
                arrived = None
            else:
                if arrived is None or view[1] != card:
                    arrived, card = self._monotonic(), view[1]
                elif card is not None and self._monotonic() - arrived >= QUEEN_BLOOD_DECK_READ_SECONDS:
                    return card
            self._sleeper(0.3)
        return None

    def _read_queens_blood_deck_list(self) -> list[str]:
        """Read the deck on the deck-select screen, one list cell at a time.

        The list is 5 cards per row; details (F4) must follow the cursor,
        shown by the list's own popup being visible.
        """
        total = self._deck_total(self._deck_observe())
        if total is None or not 1 <= total <= 15:
            raise SafetyError("the deck total is not observable on the deck screen")
        active = self._deck_active_area(self._deck_screen(self._deck_observe()))
        if self._deck_editor_current(area="Player")[0] is None or active not in {None, "Player"}:
            # No list cursor event yet, or the editor's areas moved last
            # (closing the editor reports nothing): nudge the list cursor.
            self._deck_key("queen_blood_right")
            self._sleeper(1.0)
            self._deck_key("queen_blood_left")
            self._sleeper(1.0)
        for _attempt in range(3):
            view = self._deck_editor_current(area="Player")
            if view[0] is not None and self._deck_cell_card("Player", view[0], 4.0) is not None:
                break
            self._deck_key("queen_blood_details")
            self._sleeper(1.5)
        else:
            raise SafetyError("the deck list's card details did not appear")
        cards = []
        for position in range(total):
            target = divmod(position, QUEEN_BLOOD_DECK_LIST_COLUMNS)
            self._deck_editor_goto(*target, area="Player")
            card = self._deck_cell_card("Player", target)
            if card is None:
                raise SafetyError(f"the deck list card at {target} was not read")
            cards.append(card)
        return cards

    def _wait_deck_total(self, expected: int, timeout: float = 6.0) -> bool:
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            if self._deck_total(self._deck_observe()) == expected:
                return True
            self._sleeper(0.3)
        return False

    def _deck_find(self, area: str, card_id: str, max_cells: int = 160) -> tuple[int, int]:
        """Walk ``area`` from its first cell until the hovered card is ``card_id``.

        Movement follows the collection scan: Right along a row, the next
        row's first cell at a row end, and the walk ends on any wrap. Cells a
        lag-doubled press skipped are then visited directly.
        """
        view = self._deck_editor_goto(0, 0, area=area)
        seen: dict[tuple[int, int], str | None] = {}
        row_ends: dict[int, int] = {}
        for _step in range(max_cells):
            position = view[0]
            if position is None:
                break
            card = self._deck_cell_card(area, position)
            seen[position] = card
            if card == card_id:
                return position
            self._deck_key("queen_blood_right")
            moved = self._deck_editor_settle(view, 4.0, area)
            if moved[0] is not None and moved[0][0] == position[0] and moved[0][1] > position[1]:
                view = moved
                continue
            row_ends[position[0]] = position[1]
            if area == "Strip":
                break
            if moved[0] is not None and moved[0][0] == position[0] + 1:
                view = self._deck_editor_goto(moved[0][0], 0, area=area)
                continue
            if moved[0] != position:
                self._deck_editor_goto(*position, area=area)
            self._deck_key("down")
            below = self._deck_editor_settle(view, 4.0, area)
            if below[0] is None or below[0][0] != position[0] + 1:
                break
            view = self._deck_editor_goto(below[0][0], 0, area=area)
        for row, last in sorted(row_ends.items()):
            for index in range(last + 1):
                if (row, index) not in seen:
                    self._deck_editor_goto(row, index, area=area)
                    if self._deck_cell_card(area, (row, index)) == card_id:
                        return row, index
        raise SafetyError(f"{card_id} was not found in the deck {area}")

    @staticmethod
    def _deck_order_key(card_id: str) -> tuple[int, int, str]:
        """The deck screens list cards by cost, then card number (live).

        The card number is the game's UniqueId, not the card_id suffix:
        live 2026-10-12 the strip listed Riot Trooper (card_id_001, number
        2), Grenadier (card_id_016, number 3), Elphadunk (card_id_010,
        number 11). By card_id it removed from the wrong cell, and grid
        additions fell back to walking the grid (#341).

        A cost-0 card (one played over an allied card, e.g. Insectoid
        Chimera) comes after every costed card: live 2026-10-06 the grid
        ended "... Magic Pot (cost 3), Insectoid Chimera". Sorted first, it
        put every expected grid cell one off.
        """
        from .queen_blood_catalogue import catalogue_card, catalogue_unique_id
        card = catalogue_card(card_id)
        if card is None or card.cost is None:
            return (99, 0, str(card_id))
        cost = int(card.cost)
        number = catalogue_unique_id(card_id)
        return (90 if cost == 0 else cost, 10**6 if number is None else number, str(card_id))

    def _deck_cursor(self, area: str) -> tuple[int, int] | None:
        return self._deck_editor_view(self._deck_observe(), area)[0]

    def _deck_fast_key(self, button: str, milliseconds: int = QUEEN_BLOOD_FAST_KEY_MS) -> None:
        if getattr(self.input, "is_real", False) and self._session_lease is not None:
            self._rearm_active_session()
        self._ensure_game_foreground()
        self.input.tap(button, milliseconds)
        self._sleeper(QUEEN_BLOOD_FAST_GAP_SECONDS)

    def _deck_fresh_cursor(self, area: str) -> None:
        """Make ``area`` report where its cursor really is.

        Entering an area (the strip from the grid, the grid from the strip,
        the list after the editor closes) sends no cursor event, so the last
        one can be stale (live: two removals landed one strip entry right of
        their target). A step there and back sends fresh ones.
        """
        screen = self._deck_screen(self._deck_observe())
        before = (((screen.get("editor") or {}).get("cursors") or {}).get(area) or {}).get("sequence")
        for _attempt in range(3):
            milliseconds = QUEEN_BLOOD_FAST_KEY_MS if _attempt == 0 else QUEEN_BLOOD_DECK_KEY_MS * _attempt
            self._deck_fast_key("queen_blood_right", milliseconds)
            # Observe the first key before sending the return key. Otherwise
            # a delayed Right event can satisfy the freshness check while
            # Left is still pending, leaving callers with a transient cursor.
            deadline = self._monotonic() + QUEEN_BLOOD_CURSOR_WAIT_SECONDS
            middle = before
            while self._monotonic() < deadline:
                screen = self._deck_screen(self._deck_observe())
                middle = (((screen.get("editor") or {}).get("cursors") or {}).get(area) or {}).get("sequence")
                if middle is not None and middle != before and self._deck_active_area(screen) == area:
                    break
                self._sleeper(0.05)
            self._deck_fast_key("queen_blood_left", milliseconds)
            deadline = self._monotonic() + QUEEN_BLOOD_CURSOR_WAIT_SECONDS
            while self._monotonic() < deadline:
                screen = self._deck_screen(self._deck_observe())
                now = (((screen.get("editor") or {}).get("cursors") or {}).get(area) or {}).get("sequence")
                if now is not None and now != middle and self._deck_active_area(screen) == area:
                    return
                self._sleeper(0.05)
            before = now
        raise SafetyError(f"the deck {area} cursor did not report a fresh event")

    def _deck_grid_cursor_ready(self) -> None:
        """Make the owned grid report its cursor once the editor opens.

        Opening the editor sends no cursor event (live 2026-10-06) and the
        deck list's events from reading the deck are newer, so the grid
        reads as inactive until its cursor steps there and back.
        """
        deadline = self._monotonic() + QUEEN_BLOOD_EDITOR_OPEN_SECONDS * 4
        while True:
            try:
                self._deck_fresh_cursor("Edit")
            except SafetyError:
                if self._monotonic() >= deadline:
                    raise
                self._sleeper(0.3)
                continue
            if self._deck_active_area(self._deck_screen(self._deck_observe())) == "Edit":
                return
            if self._monotonic() >= deadline:
                raise SafetyError("the deck editor's grid reports no cursor")
            self._sleeper(0.3)

    def _deck_enter(self, button: str, area: str) -> None:
        """Press ``button`` to move the cursor into ``area`` and make sure it
        got there. Live 2026-10-06 the Down after the last strip removal was
        lost; the grid moves that followed were pressed in the strip and the
        edit stopped with the editor open on a short deck."""
        for _attempt in range(3):
            self._deck_fast_key(button)
            try:
                self._deck_fresh_cursor(area)
            except SafetyError:
                self._sleeper(0.3)
                continue
            if self._deck_active_area(self._deck_screen(self._deck_observe())) == area:
                # The game may construct this pane's details popup lazily.
                # Discover it at the verified pane transition, then use known
                # paths throughout the burst; never rescan on each cursor check.
                self._deck_observe(full=True, stable=True)
                return
            self._sleeper(0.3)
        raise SafetyError(f"the deck cursor did not enter the {area}")

    def _deck_step(self, area: str, position: tuple[int, int], target: tuple[int, int]) -> None:
        """Move horizontally in a full row, avoiding missing final-row cells."""
        rows, columns = target[0] - position[0], target[1] - position[1]
        horizontal = ["queen_blood_right" if columns > 0 else "queen_blood_left"] * abs(columns)
        vertical = ["down" if rows > 0 else "up"] * abs(rows)
        for button in (horizontal + vertical if rows > 0 else vertical + horizontal):
            self._deck_fast_key(button)

    def _deck_move(self, area: str, target: tuple[int, int]) -> None:
        """Press straight from the cursor to ``target``, then check it arrived.

        Descend after changing columns; ascend before changing columns.
        Read the cursor event once the burst is sent, correcting if needed.
        Refused unless ``area`` holds the cursor: each area keeps its last
        cursor event, so another area's presses would look like a stall.
        """
        for _attempt in range(3):
            state = self._deck_observe()
            active = self._deck_active_area(self._deck_screen(state))
            if active not in {None, area}:
                raise SafetyError(f"the deck cursor is in the {active}, not the {area}")
            position = self._deck_editor_view(state, area)[0]
            deadline = self._monotonic() + 1.0
            while position is None and self._monotonic() < deadline:
                self._sleeper(0.05)
                position = self._deck_cursor(area)
            if position is None:
                raise SafetyError(f"the deck {area} cursor is not observable")
            if position == target:
                return
            self._deck_step(area, position, target)
            deadline = self._monotonic() + QUEEN_BLOOD_CURSOR_WAIT_SECONDS
            while self._monotonic() < deadline:
                if self._deck_cursor(area) == target:
                    return
                self._sleeper(0.05)
        raise SafetyError(f"the deck {area} cursor did not reach {target}")

    def _assert_deck_cursor_card(self, area: str, target: tuple[int, int], card_id: str,
                                 *, expected_total: int | None = None) -> None:
        """Refuse a Confirm unless the card under the cursor is ``card_id``:
        a Confirm on the wrong cell adds or removes the wrong card."""
        before_total = self._deck_total(self._deck_observe())
        if expected_total is not None and before_total != expected_total:
            raise SafetyError("the deck count changed before Confirm; not confirming")
        hovered = self._deck_popup_card(area, target, expected_total=expected_total)
        if hovered is None and area == "Strip":
            state = self._deck_observe()
            screen = self._deck_screen(state)
            position, _ = self._deck_editor_view(state, area)
            # Recorded Pietro failure: a burst briefly reached9/117, then
            # a later cursor event wrapped to0 before popup settling ended.
            # Reapproach once only if the same strip is still active and the
            # count is unchanged; never substitute another popup or Confirm.
            if (self._deck_active_area(screen) == area and position is not None
                    and position != target and before_total is not None
                    and 0 <= before_total <= 15 and self._deck_total(state) == before_total):
                self._deck_move(area, target)
                hovered = self._deck_popup_card(area, target, expected_total=expected_total)
        if hovered != card_id:
            raise SafetyError(f"the deck {area} cursor is on {hovered} at {target}, not {card_id}; not confirming")

    def _discard_incomplete_deck(self) -> None:
        """Leave a short deck through Quit Without Saving, restoring the saved deck."""
        mode = self._deck_screen_mode()
        if mode == "select":
            return  # An earlier recovery already closed the editor.
        if mode not in {None, "editor"}:
            raise SafetyError(f"cannot discard a deck without a verified editor ({mode})")
        self._deck_key("queen_blood_cancel")
        self._sleeper(0.8)
        self._deck_fast_key("up")
        self._deck_fast_key("confirm")
        self._sleeper(QUEEN_BLOOD_EDITOR_CLOSE_SECONDS)
        deadline = self._monotonic() + 3.0
        while self._monotonic() < deadline:
            mode = self._deck_screen_mode()
            if mode == "select":
                return
            if mode is None:  # Simulated readers have no menu-text producer.
                screen = self._deck_screen(self._deck_observe())
                if ((screen.get("windows") or {}).get("CardDeckList") is False
                        or self._deck_active_area(screen) == "Player"):
                    return
            self._sleeper(0.1)
        raise SafetyError("the editor did not close after quitting without saving")

    def _deck_popup_card(self, area: str, target: tuple[int, int], previous: str | None = None,
                         *, expected_total: int | None = None) -> str | None:
        """The card named by the details popup once it has refreshed on ``target``.

        After a single-cell snake-read step, a different card proves the
        popup refreshed. Initial positioning can cross several cells and
        duplicate cards do not prove a refresh, so those reads settle first.
        """
        arrived = self._monotonic()
        deadline = arrived + 4.0
        card = None
        while self._monotonic() < deadline:
            state = self._deck_observe()
            if expected_total is not None and self._deck_total(state) != expected_total:
                raise SafetyError("the deck count changed while checking the card; not confirming")
            view = self._deck_editor_view(state, area)
            if view[0] == target:
                card = view[1]
                if card is not None and ((previous is not None and card != previous) or
                        self._monotonic() - arrived >= QUEEN_BLOOD_POPUP_HOLD_SECONDS):
                    return card
            self._sleeper(0.05)
        # A popup read at an earlier position is not proof of the current
        # card, even if it briefly matched while crossing several cells.
        return None

    def _read_deck_list_stable(self) -> list[str]:
        # Native composition reads happen at the batch boundaries. Cursor
        # acknowledgements during the batch never perform global discovery.
        previous = self._read_deck_list_fast()
        for _ in range(3):
            current = self._read_deck_list_fast()
            if Counter(current) == Counter(previous):
                return current
            previous = current
        raise SafetyError("deck composition changed across four verification reads")

    def _match_deck_before_begin(self, state: Any) -> MatchDeck:
        """Verify this attempt's deck without cursor input or inventory fallback.

        Missing bulk evidence permits the existing begin flow with a null deck.
        UI/save safety refusals propagate, just as they do for explicit reads.
        Two equal, independently guarded snapshots retain every duplicate copy.
        """
        run = self.get_queens_blood_run_status()
        if not run.get("loaded_save_verified") or self.queen_blood_profile() is None:
            return MatchDeck()
        screen = self._deck_screen(state)
        if not (screen.get("observed") is True and screen.get("shown") is True):
            return MatchDeck()  # A menu guide alone does not verify composition.
        loaded = run.get("loaded_save")
        first = self._read_queen_blood_arrays("Player")
        if first is None:
            return MatchDeck()
        second = self._read_queen_blood_arrays("Player")
        if second is None:
            return MatchDeck()
        fields = ("source", "controller", "profile_id", "slot", "guard")
        cards = second["arrays"]["Player"]["card_ids"]
        if (Counter(cards) != Counter(first["arrays"]["Player"]["card_ids"])
                or any(first.get(field) != second.get(field) for field in fields)
                or self.get_queens_blood_run_status().get("loaded_save") != loaded):
            raise SafetyError("the deck or loaded save changed across match deck verification (no Begin sent)")
        return MatchDeck(cards, verification={
            "source": "deck_select_native_card_arrays",
            "verified_at": self._wall_clock().isoformat(),
            **{field: second.get(field) for field in fields if field != "source"},
        })

    def _observe_match_deck(self, payload: Any) -> None:
        if not self._queen_blood_match or not isinstance(payload, dict):
            return
        evidence = self._queen_blood_match.setdefault("deck_evidence", MatchDeck())
        try:
            evidence.observe(QueenBloodState.from_dict(payload))
        except QueenBloodError:
            pass  # Invalid/absent observations cannot supply deck facts.

    def _read_deck_list_fast(self) -> list[str]:
        """Read the complete deck in bulk; serial reads are for simulations."""
        bulk = self._read_queen_blood_arrays("Player")
        if bulk is not None:
            self._last_deck_read_timings = [bulk["seconds"]]
            return bulk["arrays"]["Player"]["card_ids"]
        if getattr(self.input, "is_real", False) and not isinstance(self.telemetry, SimulationTelemetry):
            reason = getattr(self, "_bulk_card_fallback_reason", None) or "bulk_reader_unavailable"
            raise SafetyError(f"the complete deck could not be read ({reason}); cursor fallback is disabled")
        total = self._deck_total(self._deck_observe())
        deadline = self._monotonic() + 6.0
        while (total is None or not 0 <= total <= 15) and self._monotonic() < deadline:
            self._sleeper(0.2)
            total = self._deck_total(self._deck_observe())
        if total is None or not 0 <= total <= 15:
            raise SafetyError("the deck total is not observable on the deck screen")
        if total == 0:
            # An emptied deck (live 2026-09-29, emptied by hand): no cells.
            self._last_deck_read_timings = []
            return []
        # Closing the editor reports nothing: make the list cursor current.
        self._deck_fresh_cursor("Player")
        diagnostics = []
        for attempt in range(3):
            start = self._deck_cursor("Player")
            if start is not None and self._deck_popup_card("Player", start) is not None:
                break
            screen = self._deck_screen(self._deck_observe())
            popups = [entry for entry in (screen.get("detail") or {}).get("resolved", [])
                      if isinstance(entry, dict) and "CardList_Player" in str(entry.get("path") or "")]
            shown = any(entry.get("popup_shown") is True for entry in popups)
            diagnostics.append({"attempt": attempt + 1, "cursor": start,
                                "active_area": self._deck_active_area(screen), "popup_shown": shown,
                                "popup_cards": [entry.get("card_id") for entry in popups]})
            if attempt == 2:
                error = SafetyError("the deck list's card details did not appear")
                error.add_note(f"Deck list popup diagnostics: {json.dumps(diagnostics)}")
                raise error
            # A shown popup with unreadable details needs a cursor refresh,
            # never another F4 that would hide it. A hidden popup can safely
            # retry a lost F4. Do not blindly undo a delayed successful toggle.
            if not shown:
                self._deck_key("queen_blood_details")
                self._sleeper(1.5)
            self._deck_fresh_cursor("Player")
        # Snake order (a row forwards, the next backwards): one press a card.
        order = []
        for row in range((total + QUEEN_BLOOD_DECK_LIST_COLUMNS - 1) // QUEEN_BLOOD_DECK_LIST_COLUMNS):
            cells = [index for index in range(row * QUEEN_BLOOD_DECK_LIST_COLUMNS,
                                              min(total, (row + 1) * QUEEN_BLOOD_DECK_LIST_COLUMNS))]
            order += cells if row % 2 == 0 else cells[::-1]
        cards: dict[int, str] = {}
        timings = []
        previous_card = None
        position = self._deck_cursor("Player")
        for index in order:
            target = divmod(index, QUEEN_BLOOD_DECK_LIST_COLUMNS)
            started = self._monotonic()
            if position is None or previous_card is None:
                self._deck_move("Player", target)
            else:
                self._deck_step("Player", position, target)
            card = self._deck_popup_card("Player", target, previous_card)
            if card is None:
                # The burst missed: go there checked, and read it in full.
                self._deck_move("Player", target)
                card = self._deck_popup_card("Player", target)
            timings.append(round(self._monotonic() - started, 2))
            if card is None:
                raise SafetyError(f"the deck list card at {target} was not read")
            cards[index] = card
            previous_card, position = card, target
        self._last_deck_read_timings = timings  # seconds per card, in reading order
        return [cards[index] for index in range(total)]

    def _confirm_deck_card(self, area: str, target: tuple[int, int], card_id: str,
                           before_total: int, delta: int) -> int:
        """Confirm one checked card and observe exactly one count change.

        Every copy is checked separately: duplicate additions/removals can
        otherwise reuse a stale popup or repeat an input that was dropped.
        The list count stays stale in the editor; _deck_total reads its
        live strip counter instead.
        """
        self._assert_deck_cursor_card(area, target, card_id, expected_total=before_total)
        self._deck_fast_key("confirm")
        after_total = before_total + delta
        if not self._wait_deck_total(after_total, 3.0):
            raise SafetyError(f"Confirm on {card_id} did not change the deck count to {after_total}")
        return after_total

    def _deck_native_grid_cell(self, card_id: str) -> tuple[int, int] | None:
        """Where the owned grid shows ``card_id`` now, read without input.

        The native Edit array is the grid in its displayed order. Live
        2026-10-12 (#341) the profile's cost/number layout put Grenadier and
        Hell Rider II in the wrong cells, and walking the grid from its first
        cell to find each took about a minute. None without a native reader,
        on a refused read or when the grid does not list the card.
        """
        try:
            bulk = self._read_queen_blood_arrays("Edit")
        except SafetyError as exc:
            self._bulk_card_fallback_reason = str(exc)
            return None
        ordered = (((bulk or {}).get("arrays") or {}).get("Edit") or {}).get("card_ids") or []
        if card_id not in ordered:
            return None
        return divmod(ordered.index(card_id), QUEEN_BLOOD_GRID_COLUMNS)

    def _deck_locate_addition(self, card_id: str, predicted: tuple[int, int]) -> tuple[tuple[int, int], str]:
        """Put the grid cursor on ``card_id`` and say how its cell was found.

        The native grid's cell first, then the profile's predicted one; the
        details popup must name the card either way (a wrong cell would add
        the wrong card). Walking the grid is the last resort: it costs about
        three seconds a cell.
        """
        candidates = []
        native = self._deck_native_grid_cell(card_id)
        if native is not None:
            candidates.append((native, "native_grid"))
        if predicted != native:
            candidates.append((predicted, "profile_order"))
        for cell, located in candidates:
            self._deck_move("Edit", cell)
            if self._deck_popup_card("Edit", cell) == card_id:
                return cell, located
        target = self._deck_find("Edit", card_id)
        self._assert_deck_cursor_card("Edit", target, card_id)
        return target, "grid_search"

    def _apply_deck_edits(self, current: list[str], target: list[str], owned: list[str]) -> list[dict[str, Any]]:
        """Open the editor, make only the needed edits, close it.

        Removals are made in the deck strip from the right (indices to the
        left do not move); additions in the owned grid in grid order. Both
        layouts are computed: the strip lists each card in the deck once,
        by cost then card number, and Confirm there removes one copy; the
        grid lists every owned card in that order, 11 to a row (live).
        """
        steps: list[dict[str, Any]] = []
        timings: dict[str, float] = {}
        self._last_deck_edit_phase_timings = timings
        step_timings: list[dict[str, Any]] = []
        self._last_deck_edit_step_timings = step_timings
        mark = self._monotonic()
        def lap(name):
            nonlocal mark
            now = self._monotonic()
            timings[name] = round(now - mark, 4)
            mark = now
        total = len(current)
        remove = Counter(current) - Counter(target)
        add = Counter(target) - Counter(current)
        if not remove and not add:
            return steps
        strip = sorted(set(current), key=self._deck_order_key)
        removals = [(strip.index(card_id), card_id, count) for card_id, count in remove.items()]
        grid = sorted(set(owned), key=self._deck_order_key)
        additions = sorted((grid.index(card_id) for card_id in add.elements()))
        self._deck_key("queen_blood_edit_deck")
        self._sleeper(QUEEN_BLOOD_EDITOR_OPEN_SECONDS)
        self._deck_observe(full=True, stable=True)
        self._deck_grid_cursor_ready()
        # Each Confirm checks the card under the cursor, which needs the
        # details popup following it (off, it keeps its last card).
        self._ensure_deck_details_live()
        lap("open_and_details")
        if removals:
            self._deck_move("Edit", (0, self._deck_cursor("Edit")[1] if self._deck_cursor("Edit") else 0))
            self._deck_enter("up", "Strip")  # Up from the grid's first row enters the strip
            for index, card_id, count in sorted(removals, reverse=True):
                started = self._monotonic()
                self._deck_move("Strip", (0, index))
                # A wrong cell removes the wrong card. The strip lists each
                # card once, so further copies stay under the cursor.
                for _ in range(count):
                    total = self._confirm_deck_card("Strip", (0, index), card_id, total, -1)
                    steps.append({"removed": card_id, "strip_index": index,
                                  "verified": True, "total_after": total})
                    step_timings.append({"removed": card_id, "strip_index": index,
                                         "seconds": round(self._monotonic() - started, 4)})
                    started = self._monotonic()
            self._deck_enter("down", "Edit")
        lap("removals")
        previous = None
        target = (0, 0)
        located = "profile_order"
        for index in additions:
            card_id = grid[index]
            started = self._monotonic()
            if index != previous:
                target, located = self._deck_locate_addition(card_id, divmod(index, QUEEN_BLOOD_GRID_COLUMNS))
            total = self._confirm_deck_card("Edit", target, card_id, total, 1)
            steps.append({"added": card_id, "grid": list(target),
                          "verified": True, "total_after": total})
            step_timings.append({"added": card_id, "grid": list(target), "located": located,
                                 "seconds": round(self._monotonic() - started, 4)})
            previous = index
        lap("additions")
        if not self._wait_deck_total(15, 2.0):
            # Not fifteen cards: the deck assumed was not the one there, and
            # the game will not close the editor on it ("This deck cannot be
            # used ... Finish building anyway?", live). Quit Without Saving,
            # one above the highlighted Continue Deckbuilding, restores it.
            self._discard_incomplete_deck()
            steps.append({"abandoned": "deck total not 15; quit without saving"})
            return steps
        self._deck_key("queen_blood_cancel")  # Esc: back to the deck screen
        self._sleeper(QUEEN_BLOOD_EDITOR_CLOSE_SECONDS)
        # Final native Player-array verification needs no cursor input. The
        # fallback list reader refreshes its own cursor before reading cells.
        self._deck_observe()  # Fresh post-Cancel sample, without cursor input.
        mode = self._deck_screen_mode()
        if mode not in {None, "select"}:
            raise SafetyError(f"the deck editor did not return to deck select ({mode})")
        lap("close_and_select")
        return steps

    def edit_queens_blood_deck(self, cards: list[str]) -> dict[str, Any]:
        """Make the deck on the deck-select screen exactly ``cards``, quickly.

        Starts on the pre-match deck screen with the editor closed. The deck
        on screen is read first (a remembered one can be stale). Only the
        difference is edited. Small cursor/card/count acknowledgements guard
        each Confirm. Two independent composition reads verify the deck at
        each batch boundary; a mismatch replans the remaining edits once.
        A no-op reuses the verified starting composition.
        The cards must be owned (save profile inventory) in at least the
        requested counts.
        """
        target = [str(card) for card in cards]
        if len(target) != 15:
            raise SafetyError("a Queen's Blood deck has exactly 15 cards")
        profile = self.queen_blood_profile()
        owned = {card: entry.get("owned", 0) for card, entry in (profile.inventory()["cards"].items() if profile else [])}
        wanted = Counter(target)
        short = {card: count for card, count in wanted.items() if owned.get(card, 0) < count}
        if short:
            raise SafetyError(f"not enough owned copies for {sorted(short)}; scan the collection first")
        mode = self._deck_screen_mode()
        if mode not in {None, "select", "tips"}:
            # The deck read and the plan assume deck select: with the editor
            # open they would read and press the wrong areas. Nothing is
            # pressed: closing an open editor would keep its unsaved edits.
            raise SafetyError(f"a deck edit starts on deck select with the editor closed ({mode})")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        started = self._monotonic()
        steps: list[dict[str, Any]] = []
        try:
            if mode == "tips":
                mode = self._deck_screen_mode_without_tips()
                if mode not in {None, "select"}:
                    raise SafetyError(f"a deck edit starts on deck select with the editor closed ({mode})")
            screen = self._deck_screen(self._deck_observe(full=True, stable=True))
            if not (screen.get("observed") and screen.get("shown")):
                raise SafetyError("the Queen's Blood deck screen is not shown")
            # The deck on screen, never one remembered: a remembered deck
            # from another save or an earlier overwrite plans the wrong edits.
            before = self._read_deck_list_stable()
            owned_ids = [card for card, count in owned.items() if count]
            attempts = []
            after: list[str] = []
            verification = "full_read"
            for attempt in range(2):
                edit_started = self._monotonic()
                steps = self._apply_deck_edits(before, target, owned_ids)
                edit_seconds = round(self._monotonic() - edit_started, 4)
                verification_started = self._monotonic()
                if not steps:
                    after = list(before)
                    verification = "unchanged_two_reads"
                else:
                    after = self._read_deck_list_stable()
                    verification = "two_reads_after_edit_batch"
                attempts.append({"assumed_current": before, "steps": steps, "read_after": after,
                                 "edit_seconds": edit_seconds,
                                 "verification_seconds": round(self._monotonic() - verification_started, 4),
                                 "edit_phases_seconds": dict(getattr(self, "_last_deck_edit_phase_timings", {})),
                                 "step_timings": list(getattr(self, "_last_deck_edit_step_timings", []))})
                if Counter(after) == wanted:
                    break
                # Not the deck asked for: start again from what is there.
                before = after
            else:
                raise SafetyError(f"the deck reads {sorted(after)} after editing and one retry")
            if profile:
                profile.record_deck(after, source="edit_queens_blood_deck")
            result = {"ok": True, "action": "edit_queens_blood_deck", "changed": bool(steps),
                      "before": attempts[0]["assumed_current"], "after": after, "steps": steps,
                      "attempts": len(attempts), "current_from": "read",
                      "attempt_timings": [{k: attempt[k] for k in ("edit_seconds", "verification_seconds", "edit_phases_seconds", "step_timings")} for attempt in attempts],
                      "verification": verification,
                      "read_timings": getattr(self, "_last_deck_read_timings", None),
                      "read_source": "native_card_arrays" if getattr(self, "_last_bulk_card_read", None) else "cursor",
                      "bulk_fallback_reason": getattr(self, "_bulk_card_fallback_reason", None),
                      "seconds": round(self._monotonic() - started, 2)}
            self._record_action(**result)
            return result
        except Exception as exc:
            # Never leave the editor open: Begin Match there overwrote a deck
            # (live 2026-10-06). A short deck is quit without saving; a full
            # one is closed as it stands and read back for the caller.
            try:
                state = self._deck_observe()
                total = self._deck_total(state)
                mode = self._deck_screen_mode()
                editor_open = mode == "editor" if mode is not None else self._deck_editor_view(state)[0] is not None
                if editor_open and total is not None and total != 15:
                    self._discard_incomplete_deck()
                elif editor_open:
                    self._deck_key("queen_blood_cancel")
                    self._sleeper(QUEEN_BLOOD_EDITOR_CLOSE_SECONDS)
                mode = self._deck_screen_mode()
                if mode not in {None, "select"}:
                    exc.add_note(f"Deck select is not shown ({mode}); Begin Match is refused until it is.")
                try:
                    exc.add_note(f"Deck after the failed edit: {self._read_deck_list_fast()}")
                except Exception as read_error:
                    exc.add_note(f"The deck could not be read after the failed edit: {read_error}")
            except Exception as recovery_error:
                exc.add_note(f"Unable to leave the deck editor: {recovery_error}")
            raise
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def _queen_blood_array_source(self, area: str) -> dict[str, Any]:
        """A fresh game-thread receipt, without invoking a population function."""
        path = Path(self.telemetry.queen_blood_path)
        request_path = path.with_name("queen_blood_cells.request")
        result_path = path.with_name("queen_blood_cells.json")
        ticket = uuid.uuid4().hex
        content = "\t".join(["v2", ticket, str(int(time.time()) + 8), area, "read",
                              "-1", "-1", "-", "-", "0"]) + "\n"
        try:
            temporary = request_path.with_suffix(".tmp")
            temporary.write_text(content, encoding="ascii", newline="\n")
            temporary.replace(request_path)
            started = self._monotonic()
            while self._monotonic() - started < 2:
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if isinstance(result, dict) and result.get("ticket") == ticket:
                        return result
                except (OSError, ValueError):
                    pass
                self._sleeper(0.02)
            raise ValueError("bulk_card_source_timeout: no matching published reply within 2 seconds")
        finally:
            try:
                if ticket in request_path.read_text(encoding="ascii"):
                    request_path.unlink()
            except (OSError, ValueError):
                pass

    def _read_queen_blood_arrays(self, area: str) -> dict[str, Any] | None:
        """Try a fresh read-only snapshot and expose any source failure.

        Callers hold the action lock. A changed UI or loaded-save receipt is
        a safety refusal, never a reason to start navigating a different UI.
        """
        self._last_bulk_card_read = None
        self._bulk_card_fallback_reason = None
        if not getattr(self.input, "is_real", False) or not getattr(self.telemetry, "queen_blood_path", None):
            return None
        started = self._monotonic()
        loaded = self.get_queens_blood_run_status()
        if not loaded.get("loaded_save_verified") or self.queen_blood_profile() is None:
            raise SafetyError("bulk card reads require the verified loaded save and model")
        mode = self._deck_screen_mode()
        expected = "select" if area == "Player" else "editor"
        if mode != expected:
            raise SafetyError(f"bulk {area} card reads require {expected}, observed {mode}")
        screen = self._deck_screen(self._deck_observe())
        if not screen.get("shown") or not screen.get("observed"):
            raise SafetyError(
                f"the Queen's Blood {mode} screen is visible in the menu guide, "
                "but the native deck probe did not observe it (no input sent)"
            )
        try:
            from .queen_blood_memory import read_card_arrays
            source = self._queen_blood_array_source(area)
            result = read_card_arrays(source, loaded["loaded_save"]["process"])
            after = self._queen_blood_array_source(area)
            for key in ("controller", "controller_address", "top_path", "top_address", "guard"):
                if source.get(key) != after.get(key) or after.get("ok") is not True:
                    raise SafetyError("the deck UI changed during the bulk read")
            current = self.get_queens_blood_run_status()
            if (not current.get("loaded_save_verified") or current.get("loaded_save") != loaded["loaded_save"]
                    or self._deck_screen_mode() != mode):
                raise SafetyError("the loaded save or deck screen changed during the bulk read")
            if area == "Player" and len(result["arrays"]["Player"]["card_ids"]) != self._deck_total(self._deck_observe()):
                raise ValueError("the native deck length differs from the screen counter")
            result.update(area=area, profile_id=loaded["profile_id"],
                          slot=loaded["loaded_save"]["slot"], guard=source["guard"],
                          input_sent=False, population_calls=0,
                          seconds=round(self._monotonic() - started, 4))
            self._last_bulk_card_read = result
            return result
        except (OSError, ValueError, KeyError) as exc:
            self._bulk_card_fallback_reason = str(exc)
            return None

    def read_queens_blood_cards(self, area: str = "Player") -> dict[str, Any]:
        """Enumerate the current deck or owned editor grid without any input."""
        if area not in {"Player", "Edit"}:
            raise SafetyError("bulk card reads require Player or Edit")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            self._deck_observe(full=True, stable=True)
            result = self._read_queen_blood_arrays(area)
            if result is None:
                return {"ok": False, "input_sent": False,
                        "reason": getattr(self, "_bulk_card_fallback_reason", None) or "bulk_reader_unavailable"}
            width = 5 if area == "Player" else 11
            result["cards"] = [{"position": i, "row": i // width, "index": i % width,
                                "card_id": card, "name": catalogue_card(card).name}
                               for i, card in enumerate(result["arrays"][area]["card_ids"])]
            self._record_action(action="read_queens_blood_cards", **result)
            return result
        finally:
            self._action_lock.release()

    def probe_queens_blood_card_cells(self, area: str = "Player", positions: list[list[int]] | None = None,
                                     execute: bool = False, memory: bool = False) -> dict[str, Any]:
        """Opt-in, one-shot widget experiment; never used by the deck reader.

        Inspect is the default. Execution needs a verified profile, an active
        cursor and identified original card so the game-thread probe can restore
        the pane. A refusal is returned with evidence, never silently retried.
        """
        if area not in {"Player", "Edit"} or type(execute) is not bool or type(memory) is not bool or memory and execute:
            raise SafetyError("cell probes require Player/Edit and a Boolean execute flag")
        positions = [] if positions is None else positions
        width = 5 if area == "Player" else 11
        if (not isinstance(positions, list) or len(positions) > 64
                or any(not isinstance(p, list) or len(p) != 2
                       or any(type(n) is not int for n in p)
                       or not 0 <= p[0] < 20 or not 0 <= p[1] < width for p in positions)
                or execute and not positions):
            raise SafetyError("cell probe positions must be bounded row/index pairs")
        path = getattr(self.telemetry, "queen_blood_path", None)
        if path is None:
            raise SafetyError("the Queen's Blood sidecar path is unavailable")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        request_path = Path(path).with_name("queen_blood_cells.request")
        ticket = uuid.uuid4().hex
        try:
            state = self.telemetry.observe()
            screen = self._deck_screen(state)
            if not screen.get("shown") or not screen.get("observed"):
                raise SafetyError("the Queen's Blood deck screen is not shown")
            profile = self.queen_blood_profile()
            if profile is None:
                raise SafetyError("the loaded model/save profile is unverified")
            cursor = self._deck_cursor(area)
            original = None
            owned_count = 0
            if execute:
                if self._session_lease is None:
                    raise SafetyError("open a control session before a widget experiment")
                if self._deck_active_area(screen) != area or cursor is None:
                    raise SafetyError("the original card position is unverified")
                original = self._deck_popup_card(area, cursor)
                if original is None:
                    raise SafetyError("the original card identity is unverified")
                if area == "Edit":
                    inventory = profile.inventory()
                    loaded = self.get_queens_blood_run_status().get("loaded_save") or {}
                    if str(inventory.get("scanned_at") or "") < str(loaded.get("at") or ""):
                        raise SafetyError("scan this loaded collection before probing grid positions")
                    owned_count = len(profile.owned_counts())
                total = self._deck_total(state) if area == "Player" else owned_count
                if not total or any(r * width + i >= total for r, i in positions):
                    raise SafetyError("a requested cell is outside the observed list")
            row, index = cursor or (-1, -1)
            content = "\t".join(["v1", ticket, str(int(time.time()) + 8), area,
                                  "1" if execute else "0", str(row), str(index), original or "-",
                                  "|".join(f"{r},{i}" for r, i in positions) or "-", str(owned_count)]) + "\n"
            temporary = request_path.with_suffix(".tmp")
            temporary.write_text(content, encoding="ascii")
            temporary.replace(request_path)
            result_path = Path(path).with_name("queen_blood_cells.json")
            started = self._monotonic()
            while self._monotonic() - started < 10:
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if isinstance(result, dict) and result.get("ticket") == ticket:
                        result["wall_seconds"] = round(self._monotonic() - started, 3)
                        if memory:
                            from .queen_blood_memory import inspect_controller
                            loaded = self.get_queens_blood_run_status()
                            if not loaded.get("loaded_save_verified"):
                                raise SafetyError("memory inspection requires the verified loaded game process")
                            try:
                                result["memory"] = inspect_controller(result, loaded["loaded_save"]["process"])
                            except (OSError, ValueError) as exc:
                                result["memory_error"] = str(exc)
                        self._record_action(action="probe_queens_blood_card_cells", **result)
                        return result
                except (OSError, ValueError):
                    pass
                self._sleeper(0.05)
            return {"ok": False, "experimental": True, "ticket": ticket,
                    "reason": "cell_probe_timeout", "input_sent": None, "requires_review": True}
        finally:
            # Remove only our own ticket; do not consume another caller's request.
            try:
                if ticket in request_path.read_text(encoding="ascii"):
                    request_path.unlink()
            except (OSError, ValueError):
                pass
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def open_queens_blood_deck_editor(self, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """From the pre-match deck screen, open the editor with details shown."""
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            state = self._deck_observe(full=True, stable=True)
            screen = self._deck_screen(state)
            if not (screen.get("observed") and screen.get("shown")):
                mode = self._deck_screen_mode()
                if mode in {"select", "editor"}:
                    raise SafetyError(
                        f"the Queen's Blood {mode} screen is visible in the menu guide, "
                        "but the native deck probe did not observe it (no input sent)"
                    )
                raise SafetyError("the Queen's Blood deck screen is not shown")
            mode = self._deck_screen_mode_without_tips()
            if mode is None and self._deck_editor_view(state)[0] is not None:
                try:
                    self._deck_fresh_cursor("Edit")
                except SafetyError:
                    pass  # retained editor cursors do not prove it is open
                state = self.telemetry.observe()
            already_open = mode == "editor" if mode is not None else self._deck_editor_view(state)[0] is not None
            if not already_open:
                self._deck_key("queen_blood_edit_deck")
                self._sleeper(QUEEN_BLOOD_EDITOR_OPEN_SECONDS)
            self._deck_grid_cursor_ready()
            view = self._ensure_deck_details_live()
            result = {"ok": True, "action": "open_queens_blood_deck_editor",
                      "cursor": list(view[0]) if view[0] else None, "hovered": view[1]}
            self._record_action(**result)
            return result
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def close_queens_blood_deck_editor(self, expected_cards: list[str]) -> dict[str, Any]:
        """Close an unchanged editor, verified against its original 15-card deck.

        Read expected_cards on deck select before opening the editor. Refuse
        incomplete/changed drafts without input; Escape would keep those edits.
        An already closed editor is an input-free, composition-verified no-op.
        """
        if (not isinstance(expected_cards, list) or len(expected_cards) != 15
                or any(not isinstance(card, str) for card in expected_cards)):
            raise SafetyError("expected_cards must contain the original 15 card IDs, including copies")
        expected = Counter(expected_cards)
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            state = self._deck_observe(full=True, stable=True)
            screen = self._deck_screen(state)
            mode = self._deck_screen_mode()
            if not (screen.get("observed") and screen.get("shown") and mode in {"select", "editor"}):
                raise SafetyError("closing the editor requires a verified deck screen (no input sent)")
            if mode == "select":
                actual = self._read_deck_list_stable()
                if Counter(actual) != expected:
                    raise SafetyError("the selected deck differs from expected_cards (no input sent)")
                return {"ok": True, "action": "close_queens_blood_deck_editor",
                        "outcome": "already_closed", "input_sent": False, "cards": actual}
            # Both arrays are read under the editor's live root/save guards.
            # Player contains the current draft, with every duplicate copy.
            binding = None
            for _ in range(2):
                bulk = self._read_queen_blood_arrays("Edit")
                if bulk is None:
                    raise SafetyError("the editor draft cannot be verified (no input sent)")
                current_binding = {key: bulk.get(key) for key in ("controller", "profile_id", "slot", "guard")}
                if binding is not None and current_binding != binding:
                    raise SafetyError("the editor or loaded save changed before closing (no input sent)")
                binding = current_binding
                actual = bulk["arrays"]["Player"]["card_ids"]
                if Counter(actual) != expected:
                    raise SafetyError("the editor has unsaved deck changes; refusing to keep them (no input sent)")
            # Recheck the guide after the reads; never send Escape to a new UI.
            if self._deck_screen_mode() != "editor":
                raise SafetyError("the editor changed before closing (no input sent)")
            self._deck_key("queen_blood_cancel")
            deadline = self._monotonic() + 5.0
            while self._deck_screen_mode() == "editor" and self._monotonic() < deadline:
                self._sleeper(0.1)
            if self._deck_screen_mode() != "select":
                raise SafetyError("the editor did not return to deck select; stop input and inspect")
            actual = self._read_deck_list_stable()
            if Counter(actual) != expected:
                raise SafetyError("the deck changed after closing; stop input and inspect")
            result = {"ok": True, "action": "close_queens_blood_deck_editor",
                      "outcome": "editor_closed", "input_sent": True,
                      "deck_unchanged": True, "cards": actual}
            self._record_action(**result)
            return result
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def scan_queens_blood_collection(self, max_cells: int = 160) -> dict[str, Any]:
        """Walk the deck editor's owned-card grid and record the inventory.

        Needs the editor open with details on (open_queens_blood_deck_editor).
        The grid lists each owned card once; every cell is read from the
        details popup, so a lag-doubled press only skips a cell, which is
        then revisited. The result replaces the save profile's inventory.
        """
        max_cells = max(1, min(int(max_cells), 400))
        profile = self.queen_blood_profile()
        if profile is None:
            raise SafetyError("the loaded save and model profile are unverified; load a slot and declare the driver before scanning")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            bulk = self._read_queen_blood_arrays("Edit")
            if bulk is not None:
                if bulk["guard"].get("filters"):
                    raise SafetyError("reopen an unfiltered deck screen before recording ownership")
                ordered = bulk["arrays"]["Edit"]["card_ids"]
                if not ordered or len(ordered) > max_cells:
                    raise SafetyError("the full owned grid exceeds the collection scan bounds")
                inventory = profile.record_collection(ordered, source="native_card_arrays")
                result = {"ok": True, "action": "scan_queens_blood_collection", "profile_id": profile.profile_id,
                          "cells": [{"row": i // QUEEN_BLOOD_GRID_COLUMNS, "index": i % QUEEN_BLOOD_GRID_COLUMNS,
                                     "card_id": card} for i, card in enumerate(ordered)],
                          "distinct_cards": len(ordered), "read_source": "native_card_arrays",
                          "inventory": inventory["cards"],
                          "input_sent": False, "seconds": bulk["seconds"]}
                self._record_action(**result)
                return result
            view = self._deck_editor_current()
            if view[0] is None:
                raise SafetyError("open the deck editor first")
            self._ensure_deck_details_live()
            view = self._deck_editor_goto(0, 0)
            cells: dict[tuple[int, int], str] = {}
            row_ends: dict[int, int] = {}
            # Live, Down on the last row wraps to the top: only a move to the
            # next row continues the walk; anything else ends it. The press
            # budget is a backstop against any other cycle.
            presses = 0
            while len(cells) < max_cells and presses < max_cells * 4:
                position, card_id = view
                if position is None:
                    break
                if card_id is not None:
                    cells[position] = card_id
                self._deck_key("queen_blood_right")
                presses += 1
                moved = self._deck_editor_settle(view, 4.0)
                if moved[0] is not None and moved[0][0] == position[0] and moved[0][1] > position[1]:
                    view = moved
                    continue
                row_ends[position[0]] = position[1]
                if moved[0] is not None and moved[0][0] == position[0] + 1:
                    view = self._deck_editor_goto(moved[0][0], 0)
                    continue
                if moved[0] != position:
                    # Wrapped (or jumped) elsewhere: step back before trying Down.
                    self._deck_editor_goto(*position)
                self._deck_key("down")
                presses += 1
                below = self._deck_editor_settle(view, 4.0)
                if below[0] is None or below[0][0] != position[0] + 1:
                    break
                view = self._deck_editor_goto(below[0][0], 0)
            # Revisit cells skipped by doubled presses.
            for row, last in sorted(row_ends.items()):
                for index in range(last + 1):
                    if (row, index) not in cells:
                        position, card_id = self._deck_editor_goto(row, index)
                        if card_id is not None:
                            cells[(row, index)] = card_id
            ordered = [cells[key] for key in sorted(cells)]
            inventory_cards = {}
            if profile is not None and ordered:
                inventory_cards = profile.record_collection(ordered, source="deck_editor_scan")["cards"]
            result = {
                "ok": True,
                "action": "scan_queens_blood_collection",
                "profile_id": profile.profile_id if profile else None,
                "cells": [{"row": r, "index": i, "card_id": c} for (r, i), c in sorted(cells.items())],
                "distinct_cards": len(set(ordered)),
                "inventory": inventory_cards,
            }
            self._record_action(**result)
            return result
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def _tap_queen_blood_confirm_or_button(self, button: str, milliseconds: int) -> None:
        """Send a board-navigation button through the same guarded path as Confirm."""
        if getattr(self.input, "is_real", False) and self._session_lease is not None:
            self._rearm_active_session()
        self._queen_blood_press(button, milliseconds)

    def _foreground_to_restore(self) -> int | None:
        """The non-game window in front now, to hand focus back to later."""
        current = getattr(self._environment, "foreground_window_handle", None)
        if not callable(current) or self._environment.game_is_foreground():
            return None
        try:
            return current()
        except Exception:
            return None

    def _queen_blood_pad_route(self, button: str) -> bool:
        """Whether a card-game button goes to the virtual pad (no focus needed).

        Only while the game is in the background: the pad is read as a
        polled state, and live 2026-09-29 80 ms pad taps were lost (three
        placements landed a cell or card short in one match) where keyboard
        presses, which are queued, never were.
        """
        return (
            self._queen_blood_background_input
            and getattr(self.input, "is_real", False)
            and callable(getattr(self.input, "tap_gamepad", None))
            and button in CARD_GAME_GAMEPAD_BUTTONS
            and not self._environment.game_is_foreground()
        )

    def _queen_blood_freshness_limit(self) -> float:
        """Card-game telemetry age limit. Unfocused, the game runs slower and
        the probe publishes about once a second with gaps up to 1.7 s (live
        2026-09-25), so background play allows older samples."""
        if self._queen_blood_pad_route("confirm"):
            return max(float(self.max_telemetry_age), QUEEN_BLOOD_BACKGROUND_MAX_AGE_SECONDS)
        return float(self.max_telemetry_age)

    def _queen_blood_input_reachable(self) -> bool:
        """The game can receive card-game input: it has focus, or background
        pad input is enabled (the pad reaches it without focus)."""
        return self._environment.game_is_foreground() or self._queen_blood_pad_route("confirm")

    def _queen_blood_press(self, button: str, milliseconds: int, *, keyboard: bool = False) -> None:
        """Send one card-game button: on the virtual pad in background mode,
        otherwise (or when ``keyboard``) by keyboard after bringing the game
        to the foreground."""
        self._assert_one_off_visit_unchanged()
        if keyboard and self._background_input_enabled():
            raise SafetyError(
                "this Queen's Blood action requires keyboard input and is unavailable in background mode"
            )
        if not keyboard and self._queen_blood_pad_route(button):
            if button == "queen_blood_play":
                # On the pad, Play is a hold: live, a 500 ms Y tap left the
                # mulligan open and a 1.5 s hold submitted it.
                milliseconds = max(int(milliseconds), QUEEN_BLOOD_PAD_PLAY_HOLD_MS)
            self.input.tap_gamepad(button, milliseconds)
            return
        self._ensure_game_foreground()
        self.input.tap(button, milliseconds)

    def _ensure_game_foreground(self, force: bool = False) -> None:
        """Refocus the game before a card-game press if another window took focus.

        Key presses go to the foreground window; live, the Claude app took
        focus mid-match and several presses were silently lost. ``force``
        refocuses even with background input on, for keyboard-only screens.
        """
        if not getattr(self.input, "is_real", False):
            return
        if self._background_input_enabled() and not force:
            return
        if self._environment.game_is_foreground():
            return
        focus = getattr(self._environment, "focus_game_window", None)
        if not callable(focus):
            raise SafetyError("FFVII Rebirth is not the foreground window")
        result = focus()
        if not (isinstance(result, dict) and result.get("foreground") is True):
            raise SafetyError("could not bring FFVII Rebirth back to the foreground")
        # The first key press right after a focus switch was observed to be
        # lost; give the window a moment to take keyboard input.
        self._sleeper(0.3)

    @classmethod
    def _queen_blood_turn_left_player(cls, state: Any) -> bool:
        payload = cls._queen_blood_payload(state)
        if payload is None:
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        return queen_blood.phase in {"opponent_turn", "result", "inactive"}

    def play_queens_blood_turn(
        self, opponent_passed: bool = False, allow_automated_decisions: bool = False,
    ) -> dict[str, Any]:
        """DEPRECATED: let queen_blood_ai choose and play one turn.

        Refused unless ``allow_automated_decisions``; agents play with their
        own decisions through place_queens_blood_card / pass_queens_blood_turn.
        """
        if allow_automated_decisions is not True:
            raise SafetyError(QUEEN_BLOOD_AUTOMATED_PLAY_DEPRECATED.format(operation="play_queens_blood_turn"))
        state = self.telemetry.observe()
        queen_blood = self._queen_blood_state_or_none(state)
        if queen_blood is None:
            raise SafetyError("Queen's Blood telemetry is unavailable")
        if queen_blood.phase != "player_turn" or queen_blood.placement.active:
            raise SafetyError(f"Queen's Blood is not awaiting the player: {queen_blood.phase}")
        plan = choose_queen_blood_action(queen_blood, opponent_passed=bool(opponent_passed))
        if plan.kind == "place":
            result = self.place_queens_blood_card(plan.hand_index, plan.row, plan.column)
        else:
            result = self.pass_queens_blood_turn()
        return {"ok": True, "action": "play_queens_blood_turn", "plan": plan.to_dict(), "result": result}

    @staticmethod
    def _queen_blood_opponent_card_count(queen_blood: QueenBloodState | None) -> int | None:
        if queen_blood is None or not queen_blood.board:
            return None
        return sum(1 for cell in queen_blood.board if cell.card_present and cell.owner == "opponent")

    def _queen_blood_stop_latched(self) -> bool:
        """True only for an operator stop, not the routine post-action latch.

        Every real action latches the stop broker when it finishes and the
        next action re-arms from the live control session. stop_all_input
        closes that session (and the bridge terminates an async worker), so
        a latched stop only ends the match loop when no session is active.
        """
        if getattr(self.input, "is_real", False) and self._session_lease is not None:
            status = self._session_lease.status()
            if status and status.get("active"):
                return False
        if getattr(self.input, "is_real", False) and self._arm:
            _, _, stopped = self._arm.snapshot()
            with self._lock:
                self._stopped = stopped
        return bool(self._stopped)

    def _queen_blood_record_mode(self) -> str:
        """``live`` only while OBS streams; rehearsals and dev runs never count."""
        directory = self._public_stream_event_dir
        if directory is None:
            return "test"
        if stream_is_live(directory.parent):
            return "live"
        return "rehearsal" if rehearsal_is_live(directory.parent) else "test"

    def _release_queen_blood_probe_after_result(self, state: Any) -> bool | None:
        if not self._queen_blood_result_exit_observed(state):
            return None
        release = getattr(self.telemetry, "release_queen_blood_probe", None)
        return bool(release()) if callable(release) else None

    def _queen_blood_result_exit_observed(self, state: Any) -> bool:
        """A missing result sample is unknown; fresh loaded field proves exit."""
        if not self._field_loaded(state):
            return False
        payload = self._queen_blood_payload(state)
        return payload is None or (
            self._queen_blood_state_or_none(state) is not None
            and not _queen_blood_screen_shown(payload)
        )

    def _close_queen_blood_result(self, deadline: float) -> tuple[bool, Any, list[dict[str, Any]]]:
        """Confirm only an observed result, then require two advancing field samples."""
        inputs: list[dict[str, Any]] = []
        field_samples = 0
        previous_sequence = None
        next_confirm_at = self._monotonic()
        state = None
        for _ in range(600):
            now = self._monotonic()
            if now >= deadline:
                break
            state = self.telemetry.observe()
            advancing = previous_sequence is None or state.sequence > previous_sequence
            if advancing:
                field_samples = field_samples + 1 if self._queen_blood_result_exit_observed(state) else 0
                previous_sequence = state.sequence
                if field_samples >= 2:
                    return True, state, inputs
            if (advancing and len(inputs) < 3 and now >= next_confirm_at
                    and self._queen_blood_result_shown(state)
                    and (state.source == "simulation" or state.age_seconds() <= self._queen_blood_freshness_limit())):
                inputs.append({"button": "confirm", "reason": "close_result",
                               "attempt": len(inputs) + 1, "sequence": state.sequence})
                self._tap_queen_blood_confirm(300, allow_stale_queen_blood_transition=True)
                self.input.release_all()
                next_confirm_at = self._monotonic() + 6.0
            self._sleeper(0.25)
        return False, state, inputs

    def _leave_queen_blood_result(self, deadline: float) -> tuple[bool, bool, bool | None]:
        """Close the result screen, then wait for settled field gameplay."""
        left, state, _inputs = self._close_queen_blood_result(min(deadline, self._monotonic() + 24.0))
        settled = False
        probe_released = self._release_queen_blood_probe_after_result(state) if left else None
        if left:
            end = min(deadline, self._monotonic() + 120.0)
            loaded_samples = 0
            while self._monotonic() < end:
                if self._queen_blood_pad_route("confirm"):
                    # Background play: settled gameplay also demands focus,
                    # so require loaded field telemetry on consecutive samples.
                    loaded_samples = loaded_samples + 1 if self._field_loaded(self.telemetry.observe()) else 0
                    settled = loaded_samples >= 3
                    if settled:
                        break
                    self._sleeper(2.0)
                    continue
                try:
                    settled = bool(self.wait_for_settled_gameplay().get("ok"))
                except SafetyError:
                    settled = False
                if settled:
                    break
                self._sleeper(5.0)
        if (settled and probe_released is None
                and callable(getattr(self.telemetry, "release_queen_blood_probe", None))):
            probe_released = self._release_queen_blood_probe_after_result(self._deck_observe())
        return left, settled, probe_released

    def _field_loaded(self, state: Any) -> bool:
        """Field gameplay is loaded: player position on a map, no menu or
        pause, and no card game running."""
        queen_blood = self._queen_blood_state_or_none(state)
        return bool(
            state.running
            and not state.paused
            and not state.in_menu
            and state.map_id
            and state.player is not None
            and state.player.position is not None
            and (queen_blood is None or not queen_blood.active)
            and (state.source == "simulation" or state.age_seconds() <= self.max_telemetry_age)
        )

    def play_queens_blood_match(
        self,
        timeout_seconds: float = 1800.0,
        max_actions: int = 60,
        mulligan: bool = True,
        poll_seconds: float = 0.5,
        leave_result: bool = True,
        opponent: str | None = None,
        allow_automated_decisions: bool = False,
    ) -> dict[str, Any]:
        """DEPRECATED: let queen_blood_ai play a whole match.

        Refused unless ``allow_automated_decisions``: matches are played with
        the agent's own decisions (see QUEEN_BLOOD_AUTOMATED_PLAY_DEPRECATED).
        Kept, with the simulator, for reference.

        Plays a Queen's Blood match from the mulligan to the result screen.

        With ``opponent`` (an NPC name or id) and no match in progress, the
        match is first initiated with that NPC from the field.

        With ``leave_result`` the result screen is closed with Confirm and the
        call returns once field gameplay has settled again (the post-match
        dialogue plays without input).

        Each step re-reads telemetry: the opening hand is resolved through
        mulligan_cards, every player turn is decided by the rules model in
        queen_blood_ai and executed with the verified place/pass operations,
        and the opponent's turns are waited out. The loop stops on the result
        screen, an emergency stop, the timeout, the action limit, or three
        consecutive failed actions (each failure is retried from fresh state).
        """
        if allow_automated_decisions is not True:
            raise SafetyError(QUEEN_BLOOD_AUTOMATED_PLAY_DEPRECATED.format(operation="play_queens_blood_match"))
        try:
            timeout_seconds = float(timeout_seconds)
            max_actions = int(max_actions)
            poll_seconds = float(poll_seconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid Queen's Blood match parameters") from exc
        if not 10.0 <= timeout_seconds <= 3600.0:
            raise SafetyError("timeout_seconds must be between 10 and 3600")
        if not 1 <= max_actions <= 120:
            raise SafetyError("max_actions must be between 1 and 120")
        poll_seconds = min(2.0, max(0.1, poll_seconds))
        started = self._monotonic()
        deadline = started + timeout_seconds
        actions: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        if opponent:
            current = self._queen_blood_state_or_none(self.telemetry.observe())
            if current is None or not current.active:
                for attempt in range(1, 4):
                    try:
                        initiated = self.initiate_queens_blood_match(target_id=str(opponent))
                    except SafetyError as exc:
                        failures.append({"error": str(exc), "phase": "initiate", "attempt": attempt})
                        if attempt == 3 or "emergency stop" in str(exc):
                            raise
                        # Let the declined or dropped dialogue finish before
                        # talking to the opponent again.
                        self._sleeper(20.0)
                        try:
                            self.wait_for_settled_gameplay()
                        except SafetyError:
                            pass
                        continue
                    actions.append({
                        "kind": "initiate",
                        "opponent": str(opponent),
                        "outcome": initiated.get("outcome"),
                        "attempt": attempt,
                        "at_seconds": round(self._monotonic() - started, 1),
                    })
                    break
        consecutive_failures = 0
        mulligan_done = not mulligan
        mulligan_submitted_at: float | None = None
        mulligan_resubmits = 0
        seen_active = False
        opponent_cards_after_our_turn: int | None = None
        held_since: float | None = None
        outcome = "timeout"
        final_state: Any = None
        last_turn_lanes: list[list[int]] | None = None
        last_turn_state: QueenBloodState | None = None
        unparseable_since: float | None = None
        opponent_key = str(opponent).strip().lower() if opponent else None
        opponent_seen = self._load_queen_blood_opponent_cards(opponent_key)
        opponent_known_at_start = len(opponent_seen)
        # A known NPC's DataTable deck models its replies from the first
        # turn of a first match (live, Turner played exactly his DeckID deck);
        # cards seen on the board are still added in case the deck differs.
        opponent_record = self._queen_blood_opponent_record(opponent_key)
        opponent_deck_ids = list(opponent_record.deck) if opponent_record is not None else []
        while self._monotonic() < deadline:
            if self._queen_blood_stop_latched():
                outcome = "emergency_stop"
                break
            if len(actions) >= max_actions:
                outcome = "action_limit"
                break
            final_state = self.telemetry.observe()
            queen_blood = self._queen_blood_state_or_none(final_state)
            if queen_blood is None:
                # Live, one unreadable hand card made every sample
                # unparseable and the loop waited silently; give up with the
                # parse error instead.
                payload = self._queen_blood_payload(final_state)
                if seen_active and isinstance(payload, dict):
                    unparseable_since = (
                        unparseable_since if unparseable_since is not None else self._monotonic()
                    )
                    if self._monotonic() - unparseable_since >= QUEEN_BLOOD_UNPARSEABLE_LIMIT_SECONDS:
                        try:
                            QueenBloodState.from_dict(payload)
                            error = "Queen's Blood telemetry was unavailable"
                        except QueenBloodError as exc:
                            error = f"Queen's Blood telemetry could not be parsed: {exc}"
                        failures.append({"error": error,
                                         "at_seconds": round(self._monotonic() - started, 1)})
                        outcome = "telemetry_unparseable"
                        break
                self._sleeper(poll_seconds)
                continue
            unparseable_since = None
            if queen_blood.phase == "result":
                outcome = "match_finished"
                break
            if not queen_blood.active:
                if seen_active:
                    outcome = "match_ended"
                    break
                self._sleeper(poll_seconds)
                continue
            seen_active = True
            new_opponent_cards = {
                cell.card_id for cell in queen_blood.board
                if cell.card_present and cell.owner == "opponent" and cell.card_id
            } - set(opponent_seen)
            if new_opponent_cards:
                opponent_seen.extend(sorted(new_opponent_cards))
                self._save_queen_blood_opponent_cards(opponent_key, opponent_seen)
            if queen_blood.phase in {"player_turn", "opponent_turn"}:
                # Once play has started the mulligan is over; the result
                # transition later reads "dealing" again (live 2026-09-25 a
                # Play hold was re-sent there, and on the result screen Play
                # is Rematch).
                mulligan_submitted_at = None
                lanes_now = [[lane.player_power, lane.opponent_power] for lane in queen_blood.lanes]
                if len(lanes_now) == 3 and all(
                    isinstance(value, int) for lane in lanes_now for value in lane
                ):
                    last_turn_lanes = lanes_now
                    last_turn_state = queen_blood
            action: dict[str, Any] | None = None
            try:
                if (
                    not mulligan_done
                    and queen_blood.phase == "dealing"
                    and self._queen_blood_mulligan_selection(queen_blood) is not None
                ):
                    indices = choose_queen_blood_mulligan(queen_blood)
                    result = self.mulligan_cards(
                        indices, submit_play=True, infer_current_hand_is_mulligan=True
                    )
                    mulligan_done = True
                    mulligan_submitted_at = self._monotonic()
                    action = {"kind": "mulligan", "card_indices": indices,
                              "outcome": result.get("outcome")}
                elif (
                    mulligan_submitted_at is not None
                    and queen_blood.phase == "dealing"
                    and self._queen_blood_mulligan_selection(queen_blood) is not None
                    and self._monotonic() - mulligan_submitted_at >= QUEEN_BLOOD_MULLIGAN_RESUBMIT_SECONDS
                    and mulligan_resubmits < QUEEN_BLOOD_MAX_MULLIGAN_RESUBMITS
                ):
                    # The mulligan screen is still up: Play did not land.
                    self._assert_queens_blood_mulligan_action_allowed(final_state)
                    self._tap_queen_blood_confirm_or_button("queen_blood_play", 1500)
                    self.input.release_all()
                    mulligan_resubmits += 1
                    mulligan_submitted_at = self._monotonic()
                    action = {"kind": "resubmit_mulligan", "attempt": mulligan_resubmits}
                elif queen_blood.phase == "player_turn" and queen_blood.placement.active:
                    # A card left held on our turn (an interrupted action):
                    # return it to the hand once it has stayed held a while.
                    held_since = held_since if held_since is not None else self._monotonic()
                    if self._monotonic() - held_since >= 6.0:
                        self._tap_queen_blood_confirm_or_button("queen_blood_cancel", 250)
                        self.input.release_all()
                        held_since = None
                        action = {"kind": "cancel_held_card"}
                elif queen_blood.phase == "player_turn" and not queen_blood.placement.active:
                    held_since = None
                    mulligan_done = True
                    settled = self._wait_for_settled_queen_blood_value(
                        final_state.sequence,
                        deadline,
                        [],
                        lambda sample: (
                            lambda item: None if item is None else (
                                item.phase,
                                item.placement.active,
                                tuple((cell.owner, cell.rank, cell.card_present) for cell in item.board),
                            )
                        )(self._queen_blood_state_or_none(sample)),
                    )
                    queen_blood = self._queen_blood_state_or_none(settled)
                    if queen_blood is None or queen_blood.phase != "player_turn" or queen_blood.placement.active:
                        continue
                    opponent_cards = self._queen_blood_opponent_card_count(queen_blood)
                    opponent_passed = (
                        opponent_cards_after_our_turn is not None
                        and opponent_cards == opponent_cards_after_our_turn
                    )
                    plan = choose_queen_blood_action(
                        queen_blood,
                        opponent_passed=opponent_passed,
                        opponent_cards=[
                            catalogue_card(card_id)
                            for card_id in dict.fromkeys(opponent_deck_ids + opponent_seen)
                        ],
                    )
                    if plan.kind == "place":
                        result = self.place_queens_blood_card(plan.hand_index, plan.row, plan.column)
                    else:
                        result = self.pass_queens_blood_turn()
                    # The opponent-pass baseline only holds once our turn
                    # verifiably ended. Live 2026-09-25 an unobserved pass
                    # left the turn ours, the unchanged opponent card count
                    # read as an opponent pass, and a closing pass was chosen
                    # that let the opponent take a lane. After our own pass
                    # the match continuing means the opponent played (two
                    # passes end it), so no baseline is kept (#345).
                    if result.get("outcome") == "card_placed":
                        after = self._queen_blood_state_or_none(self.telemetry.observe())
                        opponent_cards_after_our_turn = self._queen_blood_opponent_card_count(after)
                    else:
                        opponent_cards_after_our_turn = None
                    action = {
                        "kind": plan.kind,
                        "plan": plan.to_dict(),
                        "opponent_passed": opponent_passed,
                        "outcome": result.get("outcome"),
                        "verified": result.get("verified"),
                        "lanes_before": [
                            [lane.player_power, lane.opponent_power] for lane in queen_blood.lanes
                        ],
                    }
            except SafetyError as exc:
                consecutive_failures += 1
                failures.append({"error": str(exc), "phase": queen_blood.phase,
                                 "at_seconds": round(self._monotonic() - started, 1)})
                if "emergency stop" in str(exc) or consecutive_failures >= 3:
                    outcome = "action_failed"
                    break
                if queen_blood.phase == "player_turn" and self._queen_blood_pad_route("queen_blood_cancel"):
                    # A virtual pad connecting or disconnecting opens the
                    # pause menu, which telemetry does not report; B closes
                    # it (A does not), and on our turn B only cancels.
                    try:
                        self._tap_queen_blood_confirm_or_button("queen_blood_cancel", 200)
                        self.input.release_all()
                        failures[-1]["recovery"] = "pad_b"
                    except (SafetyError, InputError) as recovery_exc:
                        failures[-1]["recovery_error"] = str(recovery_exc)
                self._sleeper(1.0)
                continue
            if action is not None:
                consecutive_failures = 0
                action["at_seconds"] = round(self._monotonic() - started, 1)
                actions.append(action)
                continue
            self._sleeper(poll_seconds)
        final_payload = self._queen_blood_payload(final_state) if final_state is not None else None
        final = self._queen_blood_state_or_none(final_state) if final_state is not None else None
        result_info = final_payload.get("result") if isinstance(final_payload, dict) else None
        # The result is scored from the last board seen during play with the
        # rules model: a lane scores its power for the side that leads it,
        # plus that side's lane-win score bonuses in the lane (Posh Chocobo:
        # live 21-12 on screen where bare lane totals gave 18-12) and the
        # round-end score rule. The result widget cannot decide it: live
        # 2026-09-25, CardGame_Top_C.Win read shown and both score labels
        # read 99 after matches the lanes show were lost.
        score = None
        won = None
        if outcome == "match_finished" and last_turn_state is not None:
            player_score, opponent_score = QueenBloodBoardModel.from_state(last_turn_state).lane_points()
            score = {"player": player_score, "opponent": opponent_score,
                     "source": "last_turn_board_rules_model"}
            won = player_score > opponent_score
        left_result_screen = None
        field_settled = None
        probe_released = None
        if outcome == "match_finished" and leave_result:
            left_result_screen, field_settled, probe_released = self._leave_queen_blood_result(deadline)
        summary = {
            "ok": outcome == "match_finished",
            "action": "play_queens_blood_match",
            "outcome": outcome,
            "won": won,
            "score": score,
            "final_lanes": last_turn_lanes,
            "opponent_cards_known": {
                "before_match": opponent_known_at_start,
                "card_ids": list(opponent_seen),
                "names": [
                    (catalogue_card(card_id).name if catalogue_card(card_id) else None)
                    for card_id in opponent_seen
                ],
            },
            "opponent_deck": {
                "deck_id": opponent_record.deck_id,
                "ai_id": opponent_record.ai_id,
                "opponent_row": opponent_record.opponent_row,
                "seen_outside_deck": sorted(set(opponent_seen) - set(opponent_deck_ids)),
            } if opponent_record is not None else None,
            "left_result_screen": left_result_screen,
            "field_settled": field_settled,
            "probe_released": probe_released,
            "elapsed_seconds": round(self._monotonic() - started, 1),
            "actions": actions,
            "failures": failures,
            "lanes": [
                [lane.player_power, lane.opponent_power] for lane in final.lanes
            ] if final is not None else None,
            "result": result_info,
            "queen_blood": final_payload,
        }
        self._record_action(**{key: value for key, value in summary.items() if key != "queen_blood"})
        profile = self.queen_blood_profile()
        if profile is not None and opponent_key is not None and outcome == "match_finished":
            try:
                profile.update_opponent(opponent_key, match={
                    "won": won, "score": score, "final_lanes": last_turn_lanes,
                    "actions": len(actions),
                })
            except OSError:
                pass
        return summary

    QUEEN_BLOOD_RESULT_SAMPLES = 12
    QUEEN_BLOOD_RESULT_SAMPLE_SECONDS = 0.25
    # The whole finish, result close included, stays well inside the bridge's
    # 25 s request timeout (runtime_bridge.DEFAULT_RUNTIME_REQUEST_TIMEOUT_SECONDS).
    # An unconfirmed close leaves the screen for a fresh observation and tap.
    QUEEN_BLOOD_FINISH_BUDGET_SECONDS = 18.0

    def _await_queen_blood_result_screen(self) -> tuple[Any, QueenBloodState]:
        """The result screen as fresh samples show it, read over about 3 s.

        One sample could miss it: live 2026-10-04 an observation showed the
        Defeat screen (phase=result, shown) and the finish called straight
        after refused it, leaving the match unrecorded. Refusal names the
        last reason, so a stuck screen can be diagnosed.
        """
        reason = "queen_blood_telemetry_unavailable"
        for sample in range(self.QUEEN_BLOOD_RESULT_SAMPLES):
            if sample:
                self._sleeper(self.QUEEN_BLOOD_RESULT_SAMPLE_SECONDS)
            state = self.telemetry.observe()
            payload = self._queen_blood_payload(state)
            if payload is None:
                reason = "queen_blood_telemetry_unavailable"
                continue
            try:
                queen_blood = QueenBloodState.from_dict(payload)
            except QueenBloodError as exc:
                reason = f"queen_blood_payload_invalid: {exc}"
                continue
            if self._queen_blood_result_shown(state):
                return state, queen_blood
            reason = f"phase={queen_blood.phase}, active={queen_blood.active}"
        raise SafetyError(f"the Queen's Blood result screen is not shown ({reason})")

    def finish_queens_blood_match(self, opponent: str | None = None, close_result: bool = True) -> dict[str, Any]:
        """Record the match on the result screen, then close the screen.

        For matches played with the agent's own moves (the stream's way): the
        score comes from the rules model on the board (the result screen
        keeps every card; else the last in-turn board a harness call saw),
        because the result widget reads the same after wins and losses. The
        score is withheld if a board card has unknown ownership; that result
        stays open and the match is retained for a fresh observation and retry.
        The result and duration go to the public stats (overlay record, streak,
        TOTAL MATCH), the current-match marker is cleared, and the opponent's
        record in the save profile gains the match. All of that is persisted
        before the result screen is closed, and the close stays within the
        call's budget; the post-match dialogue plays without input, so the
        call returns once the result screen has closed or the budget is spent. A valid post-result snapshot showing no card-game UI also
        expires the probe request; an unobserved transition leaves it active.
        """
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        finish_started = self._monotonic()
        try:
            state, queen_blood = self._await_queen_blood_result_screen()
            match = self._queen_blood_match or self._queen_blood_match_from_marker()
            tracked_opponent = match.get("opponent")
            name = opponent or tracked_opponent
            board_source = "result_screen_board"
            # The result screen keeps every card but reads each lane total as
            # 0, and the rules model trusts displayed totals (live 2026-09-30 a
            # 16-0 win was scored as a 0-0 draw), so score its cards alone.
            payload = dict(self._queen_blood_payload(state) or {})
            board = dict(payload.get("board") or {})
            board.pop("lanes", None)
            payload["board"] = board
            scored = QueenBloodState.from_dict(payload)
            if not any(cell.card_present for cell in queen_blood.board):
                board_source = "last_turn_board"
                scored = None
                if self._queen_blood_last_turn_payload is not None:
                    try:
                        scored = QueenBloodState.from_dict(self._queen_blood_last_turn_payload)
                    except QueenBloodError:
                        scored = None
            score = won = None
            lanes = None
            unresolved_owners = [
                {"row": cell.row, "column": cell.column, "card_id": cell.card_id,
                 "owner": cell.owner}
                for cell in scored.board
                if cell.card_present and cell.owner not in {"player", "opponent"}
            ] if scored is not None else []
            if scored is not None and not unresolved_owners:
                model = QueenBloodBoardModel.from_state(scored)
                lanes = [[player, opponent] for player, opponent in zip(model.player_lanes, model.opponent_lanes)]
                player_score, opponent_score = model.lane_points()
                score = {"player": player_score, "opponent": opponent_score,
                         "source": f"{board_source}_rules_model"}
                won = player_score > opponent_score
            elapsed = None
            if isinstance(match.get("started"), (int, float)):
                elapsed = round(self._monotonic() - match["started"], 1)
            elif isinstance(match.get("started_at"), (int, float)):
                elapsed = round(self._wall_clock().timestamp() - match["started_at"], 1)
            opponent_cards = sorted({
                cell.card_id for cell in (scored.board if scored is not None else ())
                if cell.card_present and cell.owner == "opponent" and cell.card_id
            })
            deck = match.get("deck_evidence") or MatchDeck()
            deck.observe(queen_blood)
            if scored is not None:
                deck.observe(scored)
            summary = {
                "ok": score is not None,
                "action": "finish_queens_blood_match",
                "outcome": "match_finished" if score is not None else "result_unscored",
                "opponent": name,
                "won": won,
                "draw": score is not None and score["player"] == score["opponent"],
                "score": score,
                "elapsed_seconds": elapsed,
                "opponent_cards": opponent_cards,
                "left_result_screen": None,
                "probe_released": None,
                "result_close_inputs": [],
                "result_exit_sequence": None,
                **deck.record(),
            }
            if score is None:
                summary.update(
                    reason="card_ownership_unavailable" if unresolved_owners else "scoring_board_unavailable",
                    unresolved_cells=unresolved_owners,
                    requires_fresh_observation=True,
                    left_result_screen=False,
                )
            self._record_action(**summary)
            profile = self.queen_blood_profile()
            if score is not None and self._public_stream_event_dir is not None:
                try:
                    self._write_queen_blood_history(summary, scored, lanes, profile)
                except Exception:
                    pass  # History is advisory; it never blocks finishing a match.
            profile_opponent = tracked_opponent if isinstance(tracked_opponent, str) and tracked_opponent else name
            if profile is not None and profile_opponent and score is not None:
                try:
                    profile.update_opponent(str(profile_opponent), card_ids=opponent_cards or None, match={
                        "won": won,
                        "score": score,
                        "elapsed_seconds": elapsed,
                        "moves": "agent",
                        "source": "finish_queens_blood_match",
                        # Only live-stream results can count towards the
                        # campaign, and a win only once it is saved.
                        "mode": self._queen_blood_record_mode(),
                    })
                except OSError:
                    pass
            if score is not None:
                self._queen_blood_match = None
                self._queen_blood_last_turn_payload = None
            # The result is persisted above before any close input: a close
            # that outlasts the bridge request timeout kills the worker, and
            # live 2026-10-12 that lost a win the game had already dismissed
            # (#325). The close is bounded by the call's own budget.
            if close_result and score is not None:
                left, close_state, close_inputs = self._close_queen_blood_result(
                    finish_started + self.QUEEN_BLOOD_FINISH_BUDGET_SECONDS)
                summary.update(
                    left_result_screen=left,
                    probe_released=self._release_queen_blood_probe_after_result(close_state) if left else None,
                    result_close_inputs=close_inputs,
                    result_exit_sequence=close_state.sequence if left and close_state is not None else None,
                )
                if (isinstance(self._last_action, dict)
                        and self._last_action.get("action") == "finish_queens_blood_match"):
                    self._last_action.update(
                        {key: summary[key] for key in ("left_result_screen", "probe_released",
                                                       "result_close_inputs", "result_exit_sequence")})
            # For the narration line reacting to the result; not part of the record.
            return {**summary, "reaction": result_reaction(score)}
        finally:
            try:
                self.input.release_all()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def _write_queen_blood_history(self, summary: dict[str, Any], board: Any, lanes: Any,
                                   profile: QueenBloodProfile | None) -> None:
        """Keep one detailed record per finished match for the public match
        history page (owner, 2026-09-30): margin, lanes, cards on the board,
        deck, the catalogue opponent id, and the verified driver (model and
        effort) who played it, so each model owns its own results."""
        def cards(owner: str) -> list[dict[str, Any]]:
            placed = []
            for cell in board.board:
                if not (cell.card_present and cell.owner == owner):
                    continue
                reference = catalogue_card(cell.card_id) if cell.card_id else None
                placed.append({"card_id": cell.card_id, "name": reference.name if reference else None,
                               "row": cell.row, "column": cell.column, "power": cell.power})
            return placed

        score = summary["score"]
        mine, theirs = cards("player"), cards("opponent")
        # The catalogue row is the opponent's identity for the "beat every
        # opponent" benchmark; display names repeat (Card Puzzle, Red XIII).
        name = summary.get("opponent")
        opponent_id = None
        try:
            from .qb_opponents import opponent as catalogue_opponent

            row = catalogue_opponent(str(name)) if name else None
            if row is not None:
                opponent_id = row.get("id")
                name = row.get("display_name") or name
        except Exception:
            pass  # Unknown or ambiguous: the page shows the name if reviewed, else Unknown.
        state_dir = self._public_stream_event_dir.parent
        driver, _results_dir, history_dir = match_record_target(state_dir)
        record = {
            "version": 2,
            "at": datetime.now(timezone.utc).isoformat(),
            "opponent": name,
            "opponent_id": opponent_id if isinstance(opponent_id, str) else None,
            "driver": driver,
            "result": "draw" if summary.get("draw") else ("win" if summary.get("won") else "loss"),
            "score": {"player": score["player"], "opponent": score["opponent"]},
            "margin": score["player"] - score["opponent"],
            "lanes": lanes,
            "duration_seconds": summary.get("elapsed_seconds"),
            "cards_played": len(mine),
            "opponent_cards_played": len(theirs),
            "player_cards": mine,
            "opponent_cards": theirs,
            "deck": summary.get("deck"),
            "save_profile": profile.profile_id if profile is not None else None,
        }
        directory = state_dir / history_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time() * 1000)}-{uuid.uuid4()}.json"
            temporary = directory / f".{name}.tmp"
            temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
            os.replace(temporary, directory / name)
        except OSError:
            pass  # History is advisory; the match result stands.

    def _queen_blood_match_from_marker(self) -> dict[str, Any]:
        """The match in progress as the public current-match marker has it.

        A worker restart mid-match loses the in-memory record; the marker
        (written at the mulligan) survives with its start time and the
        catalogue opponent name."""
        if self._public_stream_event_dir is None:
            return {}
        try:
            marker = json.loads(
                (self._public_stream_event_dir.parent / "public-qb-current.json").read_text(encoding="utf-8"))
            started_at = datetime.fromisoformat(str(marker["at"]).replace("Z", "+00:00")).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            return {}
        if marker.get("active") is not True:
            return {}
        opponent_id = marker.get("opponent_id")
        name = marker.get("opponent")
        tracked = (
            opponent_id if isinstance(opponent_id, str) and opponent_id
            else name if isinstance(name, str) and name != "Unknown"
            else None
        )
        return {"opponent": tracked, "started_at": started_at}

    @classmethod
    def _queen_blood_result_shown(cls, state: Any) -> bool:
        payload = cls._queen_blood_payload(state)
        current = cls._queen_blood_state_or_none(state)
        info = payload.get("result") if isinstance(payload, dict) else None
        return bool(current is not None and current.active and (
            current.phase == "result" or (isinstance(info, dict) and info.get("shown") is True)))

    def observe(self) -> dict[str, Any]:
        state = self.telemetry.observe()
        result = state.to_dict()
        age = state.age_seconds()
        live = state.source == "simulation" or age <= self.max_telemetry_age
        captured_running = state.running
        result["running"] = bool(captured_running and live)
        result["telemetry"] = {
            "live": live,
            "age_seconds": age,
            "last_captured_running": captured_running,
        }
        # Retain the original top-level field for older clients.
        result["age_seconds"] = age
        control = self.control_state()
        if control is not None:
            result["control"] = control
        return result

    def observe_map_poi(self, poi_id: str, seconds: int = 900) -> dict[str, Any]:
        from .map_poi import observe_poi
        return observe_poi(self, poi_id, seconds=seconds)

    def release_map_poi(self, request_id: str) -> dict[str, Any]:
        from .map_poi import release_poi
        return release_poi(self, request_id)

    def observe_map_pins(
        self, categories: list[str] | None = None, include_hidden: bool = False
    ) -> dict[str, Any]:
        """Read every pin on the open area map, with world coordinates.

        On demand only: one map-query request, answered once by the exporter.
        Each pin is also registered as a short-lived map target.
        """
        from .map_query import observe_map_pins
        return observe_map_pins(self, categories, include_hidden)

    def map_point_to_world(
        self,
        x: float | None = None,
        y: float | None = None,
        space: str = "map_local",
        z: float | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Convert a map-local point, screen pixel or the cursor to a map target."""
        from .map_query import map_point_to_world
        return map_point_to_world(self, x, y, space=space, z=z, label=label)

    def get_map_target(self, target_id: str) -> dict[str, Any]:
        from .map_query import get_map_target
        return get_map_target(self, target_id)

    def travel_to_map_spot(self, spot: str, execute: bool = False) -> dict[str, Any]:
        """Fast travel to a map spot named by key or display name; dry-run
        (select, check the prompt, close the map) unless ``execute``."""
        from .map_spot_travel import travel_to_map_spot
        return travel_to_map_spot(self, spot, execute=bool(execute))

    def load_save_slot(self, slot: Any, execute: bool = False, steam_id: str | None = None) -> dict[str, Any]:
        """Load a save slot (number, "autosave" or profile alias) from the field
        through Main Menu > Save / Load > Load; dry-run (highlight the slot's
        row, then close the menus) unless ``execute``."""
        from .save_slot_load import load_save_slot
        return load_save_slot(self, slot, execute=bool(execute), steam_id=steam_id)

    def read_save_menu(self, include_texts: bool = True) -> dict[str, Any]:
        """The save files and one read of the main menu / Save-Load texts; no input."""
        from .save_slot_load import read_save_menu
        return read_save_menu(self, include_texts=bool(include_texts))

    def sweep_route(self, points: list[dict[str, float]], radius_cm: float | None = None,
                    half_height_cm: float | None = None, lift_cm: float | None = None,
                    query: str | None = None, max_step_cm: float | None = None) -> dict[str, Any]:
        """Sweep Cloud's capsule along feet-level route points; read-only."""
        from . import route_sweep
        return route_sweep.request_route_sweep(
            self, points,
            radius_cm=float(radius_cm if radius_cm is not None else route_sweep.CAPSULE_RADIUS_CM),
            half_height_cm=float(half_height_cm if half_height_cm is not None else route_sweep.CAPSULE_HALF_HEIGHT_CM),
            lift_cm=float(lift_cm if lift_cm is not None else route_sweep.STEP_LIFT_CM),
            query=str(query or route_sweep.DEFAULT_QUERY),
            max_step_cm=float(max_step_cm if max_step_cm is not None else route_sweep.MAX_STEP_CM))

    def plan_npc_approach(self, start: dict[str, float], target: dict[str, float],
                          stop_distance_m: float = 2.0, stage: bool = False,
                          front: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Where to stop short of an NPC along the navmesh route, or with
        ``stage`` where to walk toward one beyond Cloud's capture; read-only.
        ``front``: spots in front of an NPC behind a counter, best first."""
        from .npc_approach import plan_approach_in_process
        return plan_approach_in_process(self, start, target, float(stop_distance_m), stage=bool(stage),
                                        front=list(front) if front else None)

    def cutscene_state(self) -> dict[str, Any]:
        """Whether a cutscene is playing, from the exporter's sequence players; read-only."""
        from .cutscene import read_cutscene_state, state_path
        return read_cutscene_state(state_path(self))

    def skip_cutscene(self, timeout_seconds: float = 8.0) -> dict[str, Any]:
        """Skip the cinema playing now through the game's own Paused dialog.

        Live 2026-10-05 (Chadley's combat simulator intro, EV_COMON_2110):
        Menu (Escape) pauses a skippable scene and opens Paused with Resume
        highlighted above Skip; Down and Confirm skip it. Nothing more is
        pressed unless that dialog's texts are seen: a scene that cannot be
        skipped (the simulator's VR transition, EV_COMON_2112) opens none.
        Succeeds only when the scene is seen to stop.
        """
        from .cutscene import read_cutscene_state, state_path
        timeout_seconds = float(timeout_seconds)
        if not 1.0 <= timeout_seconds <= 30.0:
            raise SafetyError("timeout_seconds must be between 1 and 30")
        path = state_path(self)
        before = read_cutscene_state(path)
        if not before.get("active"):
            return {"ok": False, "reason": "no_cutscene", "cutscene": before, "input_sent": False}
        scenes = sorted(item.get("sequence") for item in before["scenes"])
        if not self._action_lock.acquire(blocking=False):
            return {"ok": False, "reason": "busy", "scenes": scenes,
                    "detail": "another input action is executing", "input_sent": False}
        try:
            result = self._skip_cutscene_locked(path, scenes, timeout_seconds)
        finally:
            try:
                self.input.release_all()
            finally:
                self._action_lock.release()
        if result.get("ok"):
            self._record_action(action="skip_cutscene", **result)
        return result

    def _skip_cutscene_locked(self, path: Path, scenes: list[str], timeout_seconds: float) -> dict[str, Any]:
        from .cutscene import read_cutscene_state

        def playing() -> bool:
            now = read_cutscene_state(path)
            return now.get("observed") is True and any(
                item.get("sequence") in scenes for item in now.get("scenes") or [])

        started = self._monotonic()
        presses = [self.tap("menu", CUTSCENE_KEY_MS)]
        # Down and Confirm are for the Paused dialog only, seen by its own
        # texts: the game's pause flag stays clear while it is open (live
        # 2026-10-05). Had the scene ended as Menu landed, Menu opened the
        # main menu instead: close it and stop rather than confirm one of its
        # items.
        # The quick read watches for the dialog until the scene ends (live,
        # waiting on after the VR transition ended held the simulator back
        # ~3 s); one full read confirms its absence before anything is
        # called unskippable.
        dialog = {"open": False}

        def dialog_or_ended() -> bool:
            dialog["open"] = self._cinema_pause_dialog_open()
            return dialog["open"] or not playing()

        self._wait_for(dialog_or_ended, CUTSCENE_PAUSE_WAIT_SECONDS)
        if not dialog["open"]:
            if self.telemetry.observe().in_menu:
                presses.append(self.tap("cancel", CUTSCENE_KEY_MS))
                return {"ok": False, "reason": "scene_ended", "scenes": scenes, "presses": presses,
                        "detail": "the scene ended as Menu was pressed; the menu it opened was closed",
                        "input_sent": True}
            if not playing():
                return {"ok": False, "reason": "scene_ended", "scenes": scenes, "presses": presses,
                        "detail": "the scene ended before any Paused dialog showed", "input_sent": True}
            if not self._cinema_pause_dialog_open(full=True):
                return {"ok": False, "reason": "not_skippable", "scenes": scenes, "presses": presses,
                        "detail": "Menu opened no Paused dialog with Skip", "input_sent": True}
        if not playing():
            presses.append(self.tap("cancel", CUTSCENE_KEY_MS))
            return {"ok": False, "reason": "scene_ended", "scenes": scenes, "presses": presses,
                    "detail": "the scene ended as Menu was pressed; the menu it opened was closed",
                    "input_sent": True}
        presses.append(self.tap("down", CUTSCENE_KEY_MS))
        self._sleeper(CUTSCENE_MENU_GAP_SECONDS)
        presses.append(self.tap("confirm", CUTSCENE_KEY_MS))

        def stopped() -> bool:
            return read_cutscene_state(path).get("observed") is True and not playing()

        if not self._wait_for(stopped, timeout_seconds):
            return {"ok": False, "reason": "skip_not_confirmed", "scenes": scenes, "presses": presses,
                    "cutscene": read_cutscene_state(path), "input_sent": True}
        return {"ok": True, "skipped": scenes, "seconds": round(self._monotonic() - started, 2),
                "presses": presses, "input_sent": True}

    def set_cutscene_autoskip(self, mode: str) -> dict[str, Any]:
        """Skip cinemas automatically: "stream" (while OBS streams or a
        rehearsal records; the default), "always" or "off"."""
        if mode not in CUTSCENE_AUTOSKIP_MODES:
            raise SafetyError("mode must be one of " + ", ".join(CUTSCENE_AUTOSKIP_MODES))
        self._cutscene_autoskip = mode
        if mode != "off":
            self._start_cutscene_watch()
        return self.cutscene_autoskip_status()

    def cutscene_autoskip_status(self) -> dict[str, Any]:
        """The automatic skip mode, whether it applies now, and recent skips.

        ``blocked`` says why the watcher is holding off, while it is: it
        sends no key into a game it has no control session for, or that is
        not in front.
        """
        thread = self._cutscene_watch_thread
        return {"mode": self._cutscene_autoskip, "watching": thread is not None and thread.is_alive(),
                "applies_now": self._cutscene_autoskip_applies(),
                "blocked": dict(self._cutscene_blocked) if self._cutscene_blocked else None,
                "unskippable": sorted(self._cutscene_unskippable),
                "refused_once": sorted(set(self._cutscene_refusals) - self._cutscene_unskippable),
                "recent": list(self._cutscene_skips)}

    def _cutscene_autoskip_applies(self) -> bool:
        if self._cutscene_autoskip == "always":
            return True
        directory = self._public_stream_event_dir
        if self._cutscene_autoskip != "stream" or directory is None:
            return False
        return stream_is_live(directory.parent) or rehearsal_is_live(directory.parent)

    def _start_cutscene_watch(self) -> None:
        if self._cutscene_watch_thread is not None and self._cutscene_watch_thread.is_alive():
            return
        self._cutscene_watch_stop.clear()
        self._cutscene_watch_thread = Thread(target=self._cutscene_watch, name="cutscene-autoskip", daemon=True)
        self._cutscene_watch_thread.start()

    def stop_cutscene_watch(self) -> None:
        self._cutscene_watch_stop.set()

    def _cutscene_watch(self) -> None:
        from .cutscene import read_cutscene_state, state_path
        delay = CUTSCENE_WATCH_SECONDS
        while not self._cutscene_watch_stop.wait(delay):
            try:
                self._cutscene_watch_once(read_cutscene_state(state_path(self)))
                failing = False
            except Exception as exc:  # the watcher must outlive one bad read
                self._note_cutscene_skip({"ok": False, "reason": "watch_error", "detail": str(exc)[:200]})
                failing = True
            # A failure repeats at the same rate only if it can change: back
            # off while one is outstanding or the watcher is held off, and
            # return to the quick poll the moment neither is.
            delay = (CUTSCENE_BLOCKED_WATCH_SECONDS if failing or self._cutscene_blocked
                     else CUTSCENE_WATCH_SECONDS)

    def _cutscene_watch_once(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """One watcher step: skip a playing cinema when the mode applies."""
        if not state.get("active") or not self._cutscene_autoskip_applies():
            return None
        blocker = self._background_input_blocker()
        if blocker is not None:
            self._note_cutscene_blocked(blocker)
            return None
        if self._cutscene_blocked is not None:
            self._cutscene_blocked = None
            self._note_cutscene_skip({"ok": True, "reason": "control_restored",
                                      "detail": "input is possible again; skipping resumes"})
        return self.skip_known_cutscene(state)

    def _background_input_blocker(self) -> str | None:
        """Why a background watcher must not press keys now, or None.

        The watcher runs on no agent's say-so, so it needs what an agent
        action needs and could not get past: an open control session and a
        game in front of the keyboard.  Neither is a lock that retrying
        changes, so the watcher reports it once and waits.
        """
        if not getattr(self.input, "is_real", False):
            return None
        control = self.control_state()
        if control is not None and not control.get("session_open"):
            return f"no_control_session:{control.get('state')}: {control.get('reason')}"
        try:
            if not self._environment.game_process_exists():
                return "game_not_running"
            if not self._input_reachable():
                return "game_not_in_foreground"
        except Exception as exc:
            return f"game_window_unreadable: {exc}"[:200]
        return None

    def _note_cutscene_blocked(self, reason: str) -> None:
        """Record that the watcher is held off, once for each distinct reason."""
        if self._cutscene_blocked is not None and self._cutscene_blocked["reason"] == reason:
            self._cutscene_blocked["suppressed"] += 1
            return
        self._cutscene_blocked = {"reason": reason, "since": datetime.now(timezone.utc).isoformat(),
                                  "suppressed": 0}
        self._note_cutscene_skip({"ok": False, "reason": "control_unavailable", "detail": reason[:200]})

    def skip_known_cutscene(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """Skip the cinema ``state`` reports playing, unless another action is
        running or it has refused Skip: once within CUTSCENE_RETRY_SECONDS,
        or twice. None when nothing was tried."""
        if not state.get("active"):
            return None
        scenes = sorted(item.get("sequence") for item in state.get("scenes") or [])
        now = self._monotonic()

        def waiting(scene: str) -> bool:
            refused = self._cutscene_refusals.get(scene)
            return scene in self._cutscene_unskippable or (
                refused is not None and now - refused < CUTSCENE_RETRY_SECONDS)

        if not scenes or all(waiting(scene) for scene in scenes) or self._action_lock.locked():
            return None
        result = self.skip_cutscene()
        if result.get("reason") == "busy":
            return None
        if result.get("reason") == "not_skippable":
            for scene in scenes:
                if scene in self._cutscene_refusals:
                    self._cutscene_unskippable.add(scene)
                else:
                    self._cutscene_refusals[scene] = self._monotonic()
        self._note_cutscene_skip(result)
        return result

    def _note_cutscene_skip(self, result: dict[str, Any]) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(),
                 **{key: result.get(key) for key in ("ok", "reason", "skipped", "scenes", "seconds", "detail")
                    if result.get(key) is not None}}
        previous = self._cutscene_skips[-1] if self._cutscene_skips else None
        if (previous is not None and entry.get("ok") is False and previous.get("ok") is False
                and all(previous.get(key) == entry.get(key) for key in ("reason", "scenes", "detail"))):
            # The same failure again: count it, so a fault that repeats cannot
            # fill the log and push out what happened before it.
            previous["repeats"] = int(previous.get("repeats", 1)) + 1
            previous["last_at"] = entry["at"]
            return
        self._cutscene_skips = (self._cutscene_skips + [entry])[-CUTSCENE_SKIP_LOG:]

    def _cinema_pause_dialog_open(self, full: bool = False) -> bool:
        """The cinema's Paused dialog shows, with its Skip button: Pause_00's
        title "$menu_pause_0000_0000" and a Com_Btn "$menu_pause_0002_0001"
        (Resume is "$menu_pause_0002_0000"). Read from the menu texts (one
        object scan), or with ``full`` every text (1-3.5 s on the game
        thread, 7-9 s during a conversation, live 2026-10-06)."""
        from .map_query import request_snapshot
        try:
            response, _ = request_snapshot(self, heights=False, flags=("alltexts",) if full else ("menutexts",),
                                           require_map=False)
            if not full and response.get("texts_scope") != "menu":
                response, _ = request_snapshot(self, heights=False, flags=("alltexts",), require_map=False)
        except Exception:
            return False
        texts = [(str(item.get("path", "")), str(item.get("text", "")))
                 for item in response.get("texts") or [] if isinstance(item, dict)]
        title = any(".Pause_00." in path and text == CUTSCENE_PAUSE_TITLE for path, text in texts)
        skip = any(".Com_Btn_" in path and text == CUTSCENE_PAUSE_SKIP for path, text in texts)
        return title and skip

    def _wait_for(self, condition: Callable[[], bool], seconds: float) -> bool:
        deadline = self._monotonic() + seconds
        while True:
            if condition():
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleeper(CUTSCENE_POLL_SECONDS)

    def read_field_prompt(self) -> dict[str, Any]:
        """The field interaction prompt shown now (e.g. Talk), read from its text."""
        from .field_prompt import read_field_prompt
        return read_field_prompt(self)

    def open_door(self, observe_seconds: float = 10.0) -> dict[str, Any]:
        """Press interact at a door whose "Open" prompt is shown, and watch.

        Refused unless the field prompt offers the door action, so the key
        cannot start a conversation or anything else. Opening plays a scene:
        live at Nibelheim Cloud stood still for about 4 s while the door
        swung, then walked himself through. Cloud's feet are sampled until he
        has come through and stopped, for at most ``observe_seconds``.
        """
        from .field_prompt import read_field_prompt
        observe_seconds = float(observe_seconds)
        if not 0.0 <= observe_seconds <= 15.0:
            raise SafetyError("observe_seconds must be between 0 and 15")
        if not getattr(self.input, "is_real", False):
            raise SafetyError("opening a door requires the Windows backend")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            self._assert_real_movement_allowed()
            before = read_field_prompt(self)
            if not before.get("door") or before.get("talk"):
                raise SafetyError(f"no door prompt is shown: {before.get('commands')}")
            self._ensure_game_foreground()  # the interaction key is keyboard-only
            pressed = self.input.use_interaction(DOOR_INTERACTION_MILLISECONDS)
            started = self._monotonic()
            samples: list[dict[str, Any]] = []
            outcome = "observe_limit"
            while True:
                elapsed = self._monotonic() - started
                player = getattr(self.telemetry.observe(), "player", None)
                locomotion = getattr(player, "locomotion", None)
                feet = locomotion.feet_position if locomotion is not None else getattr(player, "position", None)
                if feet is not None:
                    samples.append({"t": round(elapsed, 2), "position": {"x": feet.x, "y": feet.y, "z": feet.z}})
                if _door_walk_finished(samples):
                    outcome = "walked_through"
                    break
                if elapsed >= observe_seconds:
                    break
                self._sleeper(DOOR_SAMPLE_SECONDS)
            after = read_field_prompt(self)
            result = {"ok": True, "action": "open_door", "before": before, "input": pressed, "outcome": outcome,
                      "seconds": round(self._monotonic() - started, 2), "prompt_cleared": not after.get("door"),
                      "samples": samples or [{"position": after.get("player_position")}]}
            self._record_action(**result)
            return result
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def use_field_action(self, command: str = "check", observe_seconds: float = 4.0,
                         hold_ms: int | None = None) -> dict[str, Any]:
        """Press interact at a field prompt such as "Y Check", and watch.

        ``command`` is the prompt's key (``$level_action_check``) or its last
        word (``check``). Refused unless the field prompt shows exactly that
        command, so the key cannot start something else; Talk, Open and
        Terrain Action have their own operations (talk_to_npc, open_door,
        terrain_action). Live at Nibelheim (2026-10-13) Cloud's front door
        offered only "Check", which no operation could press. Watches until a
        conversation, menu, pause (a tutorial tip), loading screen, blank
        field feed (a scene), or Cloud walking somewhere and stopping, for at
        most ``observe_seconds``; ``outcome`` names what was seen, and
        ``no_change`` means nothing was. ``hold_ms`` (500-4000) holds
        interact for a "Hold" prompt (live: the Mt. Nibel wall lever, which a
        tap only nudged).
        """
        from .conversation_state import dialogue_active
        from .field_prompt import DOOR_KEY, TALK_KEY, TERRAIN_ACTION_KEY, read_field_prompt
        key = str(command or "").strip()
        if not key:
            raise SafetyError("command is required, e.g. check")
        if not key.startswith("$"):
            key = FIELD_ACTION_KEY_PREFIX + key.casefold()
        dedicated = {TALK_KEY: "talk_to_npc", DOOR_KEY: "open_door", TERRAIN_ACTION_KEY: "terrain_action"}
        if key in dedicated:
            raise SafetyError(f"{key} has its own operation: {dedicated[key]}")
        if not key.startswith(FIELD_ACTION_KEY_PREFIX):
            raise SafetyError(f"only interact prompts ({FIELD_ACTION_KEY_PREFIX}*) can be used: {key}")
        observe_seconds = float(observe_seconds)
        if not 0.0 <= observe_seconds <= 15.0:
            raise SafetyError("observe_seconds must be between 0 and 15")
        if hold_ms is not None and (type(hold_ms) is not int or not 500 <= hold_ms <= 4000):
            raise SafetyError("hold_ms must be a whole number of milliseconds, 500-4000")
        press_ms = FIELD_ACTION_INTERACTION_MILLISECONDS if hold_ms is None else hold_ms
        if not getattr(self.input, "is_real", False):
            raise SafetyError("a field action requires the Windows backend")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            self._assert_real_movement_allowed()
            before = read_field_prompt(self)
            shown = set(before.get("commands") or [])
            if key not in shown:
                raise SafetyError(f"no {key} prompt is shown: {before.get('commands')}")
            if len(shown) > 1:
                raise SafetyError(f"several prompts are shown, so interact is ambiguous: {before.get('commands')}")
            self._ensure_game_foreground()  # the interaction key is keyboard-only
            pressed = self.input.use_interaction(press_ms)
            started = self._monotonic()
            samples: list[dict[str, Any]] = []
            outcome, blank = None, 0
            while outcome is None:
                elapsed = self._monotonic() - started
                state = self.telemetry.observe()
                player = getattr(state, "player", None)
                locomotion = getattr(player, "locomotion", None)
                feet = locomotion.feet_position if locomotion is not None else getattr(player, "position", None)
                if feet is not None:
                    samples.append({"t": round(elapsed, 2), "position": {"x": feet.x, "y": feet.y, "z": feet.z}})
                # A single blank read happens between frames; a scene or a
                # conversation keeps the feed blank.
                blank = blank + 1 if feet is None or getattr(state, "map_id", None) is None else 0
                if dialogue_active(state):
                    outcome = "dialogue"
                elif getattr(state, "loading", False):
                    outcome = "loading"
                elif getattr(state, "in_menu", False):
                    outcome = "menu"
                elif getattr(state, "paused", False):
                    outcome = "paused"
                elif blank >= FIELD_ACTION_BLANK_SAMPLES:
                    outcome = "field_feed_blank"
                elif _door_walk_finished(samples):
                    outcome = "moved"
                elif elapsed >= observe_seconds:
                    break
                else:
                    self._sleeper(DOOR_SAMPLE_SECONDS)
            after = read_field_prompt(self) if outcome in (None, "moved") else None
            if outcome is None:
                outcome = "prompt_cleared" if key not in (after.get("commands") or []) else "no_change"
            result = {"ok": True, "action": "use_field_action", "command": key, "before": before, "input": pressed,
                      "outcome": outcome, "seconds": round(self._monotonic() - started, 2),
                      "commands_after": after.get("commands") if after is not None else None,
                      "samples": samples[-10:]}
            self._record_action(**result)
            return result
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def prewarm_navigation(self, x: float, y: float, target_x: float | None = None,
                           target_y: float | None = None, z: float | None = None,
                           target_z: float | None = None) -> dict[str, Any]:
        """Start building the route validator and planning graph for the
        capture covering (x, y), at height z when given.

        Read-only and returns at once; the builds run in the background into
        the validator's warm pool (see validator_prewarm.prewarm_artifact)
        and the topology provider's graph memo (prewarm_graph). Live, a cold
        graph cost the first route in each region 9-11 s (#196).
        """
        import threading
        from .live_route_adapter import (DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH, LIVE_EXECUTABLE_SHA256,
                                         current_validator_source_sha256)
        from .topology_cache import select_library_manifest
        resources = getattr(self, "_validated_navigation_resources", None)
        validator = resources[2] if isinstance(resources, tuple) and len(resources) > 2 else None
        if not callable(getattr(validator, "prewarm_artifact", None)):
            return {"ok": True, "state": "skipped", "reason": "no_prewarm_capable_validator"}
        target = (float(target_x), float(target_y)) if target_x is not None and target_y is not None else None
        if target is not None and target_z is not None:
            target = (*target, float(target_z))
        position = (float(x), float(y)) if z is None else (float(x), float(y), float(z))
        selected, decision = select_library_manifest(
            Path(DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH).parent, world_id="EndGame", map_id="EndGame",
            executable_sha256=LIVE_EXECUTABLE_SHA256, position=position, target=target,
            validator_source_sha256=current_validator_source_sha256())
        if selected is None:
            return {"ok": True, "state": "skipped", "reason": "no_covering_capture"}
        threading.Thread(target=validator.prewarm_artifact, args=(selected,), name="ff7r-validator-prewarm",
                         daemon=True).start()
        result = {"ok": True, "state": "started", "manifest": str(selected)}
        provider = resources[1]
        if callable(getattr(provider, "prewarm_graph", None)):
            def build_graph() -> None:
                try:
                    status = provider.prewarm_graph(position, target=target)
                except Exception as exc:
                    status = {"state": "failed", "reason": type(exc).__name__, "detail": str(exc)[:256]}
                setattr(self, "_topology_graph_prewarm_status", status)

            threading.Thread(target=build_graph, name="ff7r-graph-prewarm", daemon=True).start()
            result["graph_prewarm"] = "started"
        return result

    def explore_view(self, radius_m: float = 60.0, px_per_m: float | None = None, center_x: float | None = None,
                     center_y: float | None = None, max_candidates: int = 8, show_islands: bool = True,
                     include_pois: bool = True, wait_seconds: float = 12.0,
                     output_dir: str | None = None) -> dict[str, Any]:
        """Top-down PNG of the navmesh round the player with candidate destinations.

        Read-only, no input. See explore_mode: ``explore_goto_async`` walks to a
        candidate, a world point or a pixel of the view.
        """
        from .explore_mode import explore_view
        return explore_view(self, radius_m=radius_m, px_per_m=px_per_m, center_x=center_x, center_y=center_y,
                            max_candidates=max_candidates, show_islands=show_islands, include_pois=include_pois,
                            wait_seconds=wait_seconds, output_dir=output_dir)

    def terrain_action(self, milliseconds: int = 150, settle_seconds: float = 2.5) -> dict[str, Any]:
        """Press the Terrain Action button while the field prompt offers it.

        Refused otherwise, so the dodge button cannot fire a dodge; the
        generic tap_button allows only menu buttons, which refused every
        Terrain Action an approach tried (live at Kalm's Golden Plumes wagon).
        """
        from .field_prompt import read_field_prompt
        milliseconds = validate_duration(milliseconds)
        settle_seconds = float(settle_seconds)
        if not 0.0 <= settle_seconds <= 10.0:
            raise SafetyError("settle_seconds must be between 0 and 10")
        if not getattr(self.input, "is_real", False):
            raise SafetyError("a Terrain Action requires the Windows backend")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            self._assert_real_movement_allowed()
            before = read_field_prompt(self)
            if not before.get("terrain_action"):
                raise SafetyError(f"no Terrain Action prompt is shown: {before.get('commands')}")
            self._ensure_game_foreground()
            self.input.tap("dodge", milliseconds)
            self._sleeper(settle_seconds)
            after = read_field_prompt(self)
            result = {"ok": True, "action": "terrain_action", "from": before.get("player_position"),
                      "to": after.get("player_position"), "commands_after": after.get("commands")}
            self._record_action(**result)
            return result
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def talk_to_npc(self, milliseconds: int = 150, settle_seconds: float = 1.5,
                    fast_forward_dialogue: bool = False, timeout_seconds: float = 90.0,
                    expected_guide_moogle: dict[str, Any] | None = None) -> dict[str, Any]:
        """Talk, optionally advance verified lines until field return or a menu.

        Use talk_to_npc_async for fast forwarding. Generic Talk never chooses
        an answer or a menu item, so an interactive UI returns to the caller.
        """
        from .field_prompt import read_field_prompt
        milliseconds = validate_duration(milliseconds)
        settle_seconds = float(settle_seconds)
        if not 0.0 <= settle_seconds <= 10.0:
            raise SafetyError("settle_seconds must be between 0 and 10")
        if type(fast_forward_dialogue) is not bool:
            raise SafetyError("fast_forward_dialogue must be a boolean")
        timeout_seconds = float(timeout_seconds)
        if not 1.0 <= timeout_seconds <= 120.0:
            raise SafetyError("timeout_seconds must be between 1 and 120")
        if not getattr(self.input, "is_real", False):
            raise SafetyError("talking requires the Windows backend")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            self._assert_real_movement_allowed()
            scan = self.scan_character_inventory() if expected_guide_moogle is not None else None
            before = read_field_prompt(self)
            if not before.get("talk"):
                raise SafetyError(f"no Talk prompt is shown: {before.get('commands')}")
            admission = None
            if expected_guide_moogle is not None:
                from .guide_moogle import verify_talk_target
                admission = verify_talk_target(scan, before, expected_guide_moogle)
                # Scanning/reading takes time. Recheck the normal input gate
                # and the player binding immediately before the Talk pulse.
                from .entity_interaction import validate_sample, require_stable
                state = self._assert_real_movement_allowed()
                current = state.to_dict()
                current["telemetry"] = {"live": state.age_seconds() <= 0.5, "age_seconds": state.age_seconds()}
                validate_sample(current, scan["sample"]["sequence"] - 1, baseline=scan["sample"])
                actor = {"id": admission["actor_id"], "kind": "npc", "position": admission["position"]}
                require_stable(scan["sample"], current, actor, actor)
                admission = verify_talk_target({**scan, "sample": current}, before, expected_guide_moogle)
            self._ensure_game_foreground()
            self.input.use_interaction(milliseconds)
            from .npc_dialogue import DialogueForwarder
            trace: list[dict[str, Any]] = []
            inputs: list[dict[str, Any]] = []
            forwarder = DialogueForwarder(self, fast_forward_dialogue, trace, inputs)
            forwarder.talk_started()
            deadline = self._monotonic() + timeout_seconds
            self._sleeper(settle_seconds)
            while forwarder.enabled and self._monotonic() < deadline:
                advanced = forwarder.advance(deadline)
                if (forwarder.last_value or {}).get("interactive_ui"):
                    forwarder.stop("interactive_ui_requires_decision")
                if not advanced and forwarder.enabled:
                    self._sleeper(0.3)
            if forwarder.enabled:
                forwarder.stop("conversation_timeout")
            state = self.observe()
            result = {"ok": True, "action": "talk", "at": before.get("player_position"),
                      "in_menu": state.get("in_menu"), "paused": state.get("paused")}
            if admission is not None:
                result["guide_moogle_admission"] = admission
            if fast_forward_dialogue:
                result.update(inputs=inputs, trace=trace)
            self._record_action(**result)
            return result
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def use_guide_moogle(self, destination: str, execute: bool = False,
                         expected_target: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fast travel within the Gold Saucer by a Guide Moogle.

        Stand at a Guide Moogle (its Talk prompt shown) or with its menu open.
        Talks, highlights ``destination`` (a zone name such as "Ghost Square"),
        checks the highlight and confirms. Returns while the screen fades, with
        the arrival point to wait for; it does not wait for the load.
        """
        from .guide_moogle import use
        if not getattr(self.input, "is_real", False):
            raise SafetyError("Guide Moogle travel requires the Windows backend")
        result = use(self, destination, execute=bool(execute), sleep=self._sleeper, expected_target=expected_target)
        if result.get("execute") is not False:
            self._record_action(action="guide_moogle", **result)
        return result

    def enter_combat_simulator(self, execute: bool = False, skip_scenes: bool = True,
                               timeout_seconds: float = 60.0) -> dict[str, Any]:
        """Enter Chadley's combat simulator from beside him (his Talk prompt
        shown) or with his menu open: talk, wait out his greeting, pick Use
        Combat Simulator (checked by its help text) and wait for the
        simulator's screen, skipping the scenes on the way that allow it.
        A dry run unless ``execute``."""
        from .combat_simulator import enter
        timeout_seconds = float(timeout_seconds)
        if not 5.0 <= timeout_seconds <= 120.0:
            raise SafetyError("timeout_seconds must be between 5 and 120")
        if not getattr(self.input, "is_real", False):
            raise SafetyError("the combat simulator requires the Windows backend")
        result = enter(self, execute=bool(execute), skip_scenes=bool(skip_scenes),
                       timeout_seconds=timeout_seconds, sleep=self._sleeper, clock=self._monotonic)
        if result.get("execute") is not False:
            self._record_action(action="enter_combat_simulator", **result)
        return result

    def _press_simulator_button(self, button: str, milliseconds: int = 100) -> dict[str, Any]:
        """One press on Chadley's menu or the combat simulator's screen, for
        a caller that has just read that screen by its texts.

        Neither screen sets the game's menu or pause flag, and after a
        simulator battle the arena counts as loaded gameplay, so the general
        menu guard refused Cancel there (live 2026-10-06). Combat, a target,
        the foreground and arming are still checked.
        """
        if button not in ("confirm", "cancel", "up", "down"):
            raise SafetyError("only confirm, cancel, up and down are enabled on the simulator screens")
        milliseconds = validate_duration(milliseconds)
        if not getattr(self.input, "is_real", False):
            raise SafetyError("the combat simulator requires the Windows backend")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            state = self._assert_action_allowed(allow_paused=True)
            if state.in_combat or state.target is not None:
                raise SafetyError("simulator input blocked during combat or with a selected target")
            if self._session_lease is not None:
                self._rearm_active_session()
            self._ensure_game_foreground(force=True)
            self.input.tap(button, milliseconds)
            self.input.release_all()
            result = {"ok": True, "action": "simulator_tap", "button": button}
            self._record_action(**result)
            return result
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def leave_combat_simulator(self, close_menu: bool = True) -> dict[str, Any]:
        """Leave the combat simulator's screen for Chadley's menu, and with
        ``close_menu`` close that too."""
        from .combat_simulator import leave
        if not getattr(self.input, "is_real", False):
            raise SafetyError("the combat simulator requires the Windows backend")
        result = leave(self, close_menu=bool(close_menu), sleep=self._sleeper, clock=self._monotonic)
        self._record_action(action="leave_combat_simulator", **result)
        return result

    def tap_shop_button(self, button: str, milliseconds: int = 150,
                        settle_seconds: float = 0.4) -> dict[str, Any]:
        """One menu press on an open shop screen; refused with no shop shown.

        The shop leaves the game neither paused nor in a menu, so the
        general menu guard refuses its input (live 2026-09-30, Kalm).
        """
        from .shop import read_screen_texts, shop_name
        shop = shop_name(read_screen_texts(self)["texts"])
        if shop is None:
            raise SafetyError("no shop screen is shown")
        return self._press_shop_button(button, shop, milliseconds, settle_seconds)

    def _press_shop_button(self, button: str, shop: str | None, milliseconds: int = 150,
                           settle_seconds: float = 0.4) -> dict[str, Any]:
        """A shop press for a caller that has just read the shop open.

        Reading the shop's texts costs seconds when the machine is busy
        (live 2026-09-30, 20 s a read): the purchase flow reads once per
        press, after it, not again before.
        """
        from .shop import SHOP_BUTTONS
        if button not in SHOP_BUTTONS:
            raise SafetyError(f"only shop menu buttons are enabled: {sorted(SHOP_BUTTONS)}")
        if not shop:
            raise SafetyError("no shop screen is shown")
        milliseconds = validate_duration(milliseconds)
        settle_seconds = float(settle_seconds)
        if not 0.0 <= settle_seconds <= 5.0:
            raise SafetyError("settle_seconds must be between 0 and 5")
        if not getattr(self.input, "is_real", False):
            raise SafetyError("shop input requires the Windows backend")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            state = self._assert_action_allowed(allow_paused=True)
            if state.in_combat:
                raise SafetyError("shop input blocked during combat")
            if self._session_lease is not None:
                self._rearm_active_session()
            self._ensure_game_foreground(force=True)
            if button == "cancel":
                # The shop backs out on right-click; Backspace does nothing
                # there (live 2026-09-30, Kalm).
                handles = getattr(self._environment, "game_window_handles", lambda: [])()
                if not handles:
                    raise SafetyError("the game window was not found for the right-click")
                self.input.mouse_click_in_game_window("right", milliseconds, handles[0]["hwnd"])
            else:
                self.input.tap(button, milliseconds)
            self.input.release_all()
            self._sleeper(settle_seconds)
            result = {"ok": True, "action": "shop_tap", "button": button, "shop": shop}
            self._record_action(**result)
            return result
        finally:
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    def read_shop(self) -> dict[str, Any]:
        """The open shop's screen (id, menu, tab, highlighted item, gil); no input."""
        from .shop import read_shop
        return read_shop(self).summary()

    def buy_shop_item(self, item: str, execute: bool = False, close_after: bool = True) -> dict[str, Any]:
        """Buy one of ``item`` (item id or name) from the open shop.

        The vendor catalogue gives the price and name the purchase is checked
        against when the open shop lists it; a dry run unless ``execute``.
        """
        from .navigation_values import MissionError
        from .qb_card_vendors import shop_stock_entry
        from .shop import buy_item, close_shop, read_shop
        screen = read_shop(self)
        if not screen.open:
            raise MissionError("shop_not_open")
        entry = shop_stock_entry(screen.shop, item)
        item_id = entry["item_id"] if entry else str(item).strip()
        gil_priced = bool(entry) and entry.get("currency") == "gil"
        plan = {"shop": screen.shop, "item_id": item_id, "catalogue": entry, "gil": screen.gil}
        if not execute:
            return {"ok": True, "dry_run": True, **plan}
        result = buy_item(self, item_id, price=entry["price"] if gil_priced else None,
                          name=entry["name"] if entry else None)
        self._record_action(action="shop_buy", shop=screen.shop, item_id=item_id, paid=result.get("paid"))
        if close_after:
            # Closing is part of the job: on its own it can outrun the
            # request deadline when the machine is busy.
            try:
                result["closed"] = close_shop(self)
            except MissionError as exc:
                result["closed"] = {"ok": False, "error": str(exc)}
        return {**result, "catalogue": entry}

    def close_shop(self) -> dict[str, Any]:
        """Back out of the open shop with Cancel."""
        from .shop import close_shop
        return close_shop(self)

    def read_screen_texts(self, contains: str | None = None) -> dict[str, Any]:
        """Every visible text with its widget path; no input."""
        from .shop import read_screen_texts
        return read_screen_texts(self, contains)

    def retry_after_game_over(self, execute: bool = False) -> dict[str, Any]:
        """On the Game Over screen, confirm the preselected Retry from Last
        Battle and check the screen closed. Without ``execute`` it only reads
        the screen."""
        from .game_over import retry_last_battle
        if type(execute) is not bool:
            raise SafetyError("execute must be a boolean")
        result = retry_last_battle(self, execute, self._tap_game_over_confirm)
        if result["input_sent"]:
            self._record_action(action="retry_after_game_over", ok=result["ok"],
                                rejection_reason=None if result["ok"] else result["outcome"])
        return result

    def read_dialogue_wheel(self) -> dict[str, Any]:
        """The dialogue answer wheel shown now: its question, each answer's
        text and bearing, and the answer the cursor highlights; no input."""
        from .dialogue_choice import DialogueChoiceError, read_wheel
        try:
            wheel = read_wheel(self)
        except DialogueChoiceError as exc:
            return {"ok": False, "decision": str(exc), "input_sent": False}
        return {"ok": True, "input_sent": False, **wheel}

    def choose_dialogue_option(self, answer: str | None = None, index: int | None = None,
                               execute: bool = False, max_presses: int = 6) -> dict[str, Any]:
        """Pick a dialogue wheel answer by its text (or Answer cell ``index``).

        Reads the wheel natively, steps the cursor with the arrow keys until
        the probe shows it on that answer, re-reads it, then confirms and
        checks the wheel has closed. Never confirms an unverified highlight.
        Without ``execute`` it only reports the plan.
        """
        from .dialogue_choice import DialogueChoiceError, match_answer, read_wheel, step_toward
        if (answer is None) == (index is None):
            raise SafetyError("give exactly one of answer or index")
        if not 0 <= int(max_presses) <= 12:
            raise SafetyError("max_presses must be between 0 and 12")
        presses: list[dict[str, Any]] = []

        def report(outcome: str, wheel: dict[str, Any], ok: bool, **extra: Any) -> dict[str, Any]:
            return {"ok": ok, "outcome": outcome, "execute": bool(execute), "presses": presses,
                    "question": wheel.get("question"),
                    "answers": [{k: a.get(k) for k in ("index", "text", "bearing")} for a in wheel.get("answers") or []],
                    "highlighted": wheel.get("highlighted"), "cursor_angle": wheel.get("cursor_angle"), **extra}

        def press(button: str) -> None:
            for _ in range(2):
                try:
                    self.tap(button, 150)
                    break
                except SafetyError as exc:
                    if "minimum delay" not in str(exc):
                        raise
                    self._sleeper(self._minimum_action_delay)
            self._last_action_finished = self._monotonic()
            presses.append({"button": button})

        try:
            wheel = read_wheel(self)
            if not wheel["shown"]:
                return report("wheel_not_shown", wheel, False)
            target = match_answer(wheel, answer, None if index is None else int(index))
            if not execute:
                return report("would_choose", wheel, True, target=target)
            for _ in range(int(max_presses) + 1):
                if not wheel["shown"]:
                    return report("wheel_closed_before_confirm", wheel, False, target=target)
                if wheel["highlighted"] is None:
                    return report("highlight_unverified", wheel, False, target=target,
                                  reason=wheel.get("highlight_reason"))
                if wheel["highlighted"] == target["index"]:
                    break
                current = next(a for a in wheel["answers"] if a["index"] == wheel["highlighted"])
                before = wheel["highlighted"]
                press(step_toward(current, target))
                deadline = self._monotonic() + 2.0
                while True:
                    self._sleeper(0.3)
                    wheel = read_wheel(self)
                    if wheel["highlighted"] != before or self._monotonic() >= deadline:
                        break
            else:
                return report("target_not_reached", wheel, False, target=target)
            if wheel["highlighted"] != target["index"]:
                return report("target_not_reached", wheel, False, target=target)
            press("confirm")
            deadline = self._monotonic() + 3.0
            while self._monotonic() < deadline:
                self._sleeper(0.3)
                after = read_wheel(self)
                if not after["shown"]:
                    self._record_action(action="choose_dialogue_option", answer=target.get("text"),
                                        index=target.get("index"), question=wheel.get("question"))
                    return report("chosen", wheel, True, target=target)
            return report("confirm_unverified", after, False, target=target)
        except DialogueChoiceError as exc:
            return {"ok": False, "outcome": "refused", "decision": str(exc), "presses": presses,
                    "input_sent": bool(presses)}

    def close_tips(self, max_tips: int = 8) -> dict[str, Any]:
        """Close the tutorial tip panel shown, and each queued behind it, with
        Cancel (one press per tip, each confirmed by its title changing)."""
        return self._tips.close(max_tips)

    def set_tips_autoclose(self, mode: str) -> dict[str, Any]:
        """Close tutorial tip panels automatically: "stream" (while OBS
        streams or a rehearsal records; the default), "always" or "off"."""
        return self._tips.set_mode(mode)

    def tips_autoclose_status(self) -> dict[str, Any]:
        """The tips auto-close mode, whether it applies now, and the recent
        closes with the tip keys dismissed."""
        return self._tips.status()

    def read_ui_roots(self, details: bool = False) -> dict[str, Any]:
        """Fresh root visibility diagnostics; never permission to dismiss UI."""
        from .map_query import request_snapshot
        if not isinstance(details, bool):
            raise SafetyError("details must be a boolean")
        flags = ("uiroots", "uirootdetails") if details else ("uiroots",)
        response, _ = request_snapshot(self, heights=False, flags=flags, require_map=False)
        roots = response.get("ui_roots")
        if not isinstance(roots, dict):
            raise SafetyError("UI root census is unavailable; update the exporter before investigating overlays")
        return {"ok": roots.get("observed") is True, "roots": roots,
                "source": "ue4ss_map_query", "request_id": response["request_id"],
                "source_sequence": response["source_sequence"], "timestamp_utc": response.get("timestamp_utc"),
                "input_sent": False, "input_authorized": False}

    def read_npc_dialogue(self) -> dict[str, Any]:
        """Fresh ordinary-line/choice diagnostics; this call never sends input."""
        from .npc_dialogue import read_dialogue
        return read_dialogue(self)

    def _map_menu_key(self, button: str, milliseconds: int) -> None:
        """One keyboard press on the map menus, which only take input focused."""
        if getattr(self.input, "is_real", False) and self._session_lease is not None:
            self._rearm_active_session()
        self._ensure_game_foreground(force=True)
        self.input.tap(button, milliseconds)
        self.input.release_all()

    def fast_travel_via_map(
        self,
        target_id: str,
        execute: bool = False,
        max_cursor_steps: int = 12,
        arrival_radius_m: float = 80.0,
        require_fast_travel_icon: bool = True,
    ) -> dict[str, Any]:
        """Fast travel to a map pin target; dry-run unless ``execute`` is true."""
        from .map_fast_travel import fast_travel_via_map
        return fast_travel_via_map(
            self, target_id, execute=execute, max_cursor_steps=max_cursor_steps,
            arrival_radius_m=arrival_radius_m, require_fast_travel_icon=require_fast_travel_icon,
        )

    def _map_cursor_pulse(self, x: float, y: float, milliseconds: int) -> dict[str, Any]:
        """One bounded map-cursor pulse (WASD / left stick) while the map is open."""
        x, y = normalise_axes(x, y)
        milliseconds = validate_duration(milliseconds)
        if x == 0.0 and y == 0.0:
            raise SafetyError("map cursor pulse requires a direction")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            state = self._assert_action_allowed(allow_paused=True)
            if not state.paused and not state.in_menu:
                raise SafetyError("map cursor input requires the map to be open")
            if state.in_combat or state.target is not None:
                raise SafetyError("map cursor input blocked during combat or with a selected target")
            if getattr(self.input, "is_real", False):
                if state.source == "simulation":
                    raise SafetyError("map cursor input requires live game telemetry")
                if not self._environment.game_process_exists():
                    raise SafetyError("FFVII Rebirth process was not found")
                if not self._input_reachable():
                    raise SafetyError("FFVII Rebirth is not the foreground window")
                armed, _ = self._arm.status() if self._arm else (False, 0.0)
                if not armed:
                    raise SafetyError("real input is not manually armed")
            self.input.move(x, y, milliseconds)
            result = {"ok": True, "sequence": state.sequence, "action": "map_cursor_pulse",
                      "x": x, "y": y, "milliseconds": milliseconds, "mode": "map"}
            self._record_action(**result)
            return result
        finally:
            self._action_lock.release()

    def scan_character_candidates(self, seconds: int = 60) -> dict[str, Any]:
        from .character_locator import scan_character_candidates
        return scan_character_candidates(self, seconds=seconds)

    def observe_combat(self, timeout_seconds: float = 3.0) -> dict[str, Any]:
        """Read one request-bound battle snapshot; never sends or authorizes input.

        Actual ATB, command UI and results remain unavailable until their read
        contracts are verified. An inactive battle is not a victory receipt.
        """
        from .combat import observe_combat
        return observe_combat(self, timeout_seconds=timeout_seconds)

    def read_objective_progress(self, story_flags: list[str] | None = None) -> dict[str, Any]:
        """Read declared story flags and chapter progress without game input."""
        from .objective_progress import read_objective_progress
        return read_objective_progress(self, [] if story_flags is None else story_flags)

    def get_active_objective(self, activity_type: str = 'main_story') -> dict[str, Any]:
        """Discover an evidenced current MSQ step and its exact/macro target."""
        from .live_objective import get_active_objective
        return get_active_objective(self, activity_type)

    def scan_character_inventory(self, seconds: int = 60) -> dict[str, Any]:
        from .character_locator import scan_character_inventory
        return scan_character_inventory(self, seconds=seconds)

    def scan_nearby_actors(self, radius_m: float = 60.0, contains: Any = None, limit: int = 60) -> dict[str, Any]:
        """Every actor within ``radius_m`` (at most 60) of Cloud, nearest
        first: class, identity, position. ``contains`` (a word or a list)
        keeps those whose class or name contains any of them, ignoring case.
        One request-only snapshot; no input. For things the character and
        field-action scans do not list (live: the Mt. Nibel mako clouds)."""
        from .map_query import request_snapshot
        radius = float(radius_m)
        if not 1.0 <= radius <= 60.0:
            raise SafetyError("radius_m must be between 1 and 60")
        if not 1 <= int(limit) <= 300:
            raise SafetyError("limit must be between 1 and 300")
        words = [contains] if isinstance(contains, str) else list(contains or [])
        if any(not isinstance(word, str) or not word.strip() for word in words):
            raise SafetyError("contains must be a word or a list of words")
        response, _ = request_snapshot(self, heights=False, flags=("actorsnear",), require_map=False)
        near = response.get("actors_near")
        if not isinstance(near, dict):
            return {"ok": False, "reason": "actor_scan_unsupported_by_exporter", "input_sent": False}
        lowered = [word.strip().casefold() for word in words]
        actors = [item for item in near.get("actors") or [] if isinstance(item, dict)
                  and float(item.get("distance_cm") or 0) <= radius * 100
                  and (not lowered or any(word in (str(item.get("class")) + " " + str(item.get("id"))).casefold()
                                          for word in lowered))]
        classes: dict[str, int] = {}
        for item in actors:
            classes[str(item.get("class"))] = classes.get(str(item.get("class")), 0) + 1
        return {"ok": not near.get("reason"), "reason": near.get("reason"), "input_sent": False,
                "radius_m": radius, "scanned": near.get("scanned"), "truncated": near.get("truncated"),
                "count": len(actors), "classes": dict(sorted(classes.items(), key=lambda kv: -kv[1])),
                "actors": actors[:int(limit)]}

    def scan_actor_inventory(self, seconds: int = 60) -> dict[str, Any]:
        from .character_locator import scan_actor_inventory
        return scan_actor_inventory(self, seconds=seconds)

    def scan_traversal_gimmicks(self, seconds: int = 60) -> dict[str, Any]:
        """Scan the loaded levels' traversal gimmicks; a complete scan is also
        merged into the evidence routes cross them by (gimmick_links.py)."""
        from .character_locator import scan_traversal_gimmicks
        from .gimmick_links import record_scan
        result = scan_traversal_gimmicks(self, seconds=seconds)
        snapshot = result.get("snapshot") if isinstance(result, dict) else None
        path = self.traversal_gimmick_evidence_path()
        if isinstance(snapshot, dict) and snapshot.get("complete") is True and path is not None:
            result["evidence"] = record_scan(path, snapshot.get("gimmicks") or [], map_id=snapshot.get("map_id"))
        return result

    def traversal_gimmick_evidence_path(self) -> Path | None:
        """Where scanned gimmicks are kept: beside the live telemetry."""
        from .gimmick_links import evidence_path
        telemetry_path = getattr(self.telemetry, "path", None)
        return evidence_path(Path(telemetry_path).parent) if telemetry_path else None

    def scan_character_target(self, selector: str, seconds: int = 60) -> dict[str, Any]:
        from .character_locator import scan_character_target
        return scan_character_target(self, selector, seconds=seconds)

    def resolve_world_target(
        self,
        name: str,
        seconds: int = 60,
        identity_index_path: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a player-facing name to one live actor and current XYZ.

        This is intentionally on-demand.  It performs one detailed character
        inventory snapshot and never sends input or treats static placement as
        an active location.
        """
        from .world_target_resolver import resolve_world_target
        return resolve_world_target(
            self,
            name,
            seconds=seconds,
            identity_index_path=identity_index_path,
        )

    def release_character_scan(self, request_id: str) -> dict[str, Any]:
        from .character_locator import release_character_scan
        return release_character_scan(self, request_id)

    def map_telemetry(self) -> dict[str, Any]:
        """Return map-relevant telemetry without requiring a screenshot.

        The runtime currently exports world position, camera orientation, map
        identity, and nearby actor/interaction data.  UI-level map markers are
        optional and are reported only when a producer supplies ``map_state``;
        the response makes that boundary explicit instead of treating a
        screenshot-derived marker as telemetry.
        """
        from dataclasses import asdict

        state = self.telemetry.observe()
        live = state.source == "simulation" or state.age_seconds() <= self.max_telemetry_age
        landmarks = self._runtime_landmarks(state.map_id)
        routes = (
            self._known_world_store.list_routes(state.map_id)
            if self._use_recorded_routes and self._known_world_store is not None and state.map_id
            else []
        )
        page_context = (
            state.map_state.get("page")
            if isinstance(state.map_state, dict) else None
        )
        map_ui_meta = (
            state.map_state.get("_map_ui_meta")
            if isinstance(state.map_state, dict) else None
        )
        map_ui_available = (
            isinstance(page_context, dict)
            and isinstance((state.map_state or {}).get("markers"), list)
            and (map_ui_meta is not None or page_context is not None)
        )
        map_ui_source = (
            "runtime_map_ui_sidecar" if map_ui_meta is not None
            else "runtime_map_state" if map_ui_available
            else "not_exposed"
        )
        return {
            "ok": True,
            "map_id": state.map_id,
            "sequence": state.sequence,
            "telemetry_live": live,
            "telemetry_age_seconds": state.age_seconds(),
            "paused": state.paused,
            "in_menu": state.in_menu,
            "player": asdict(state.player) if state.player is not None else None,
            "camera": asdict(state.camera) if state.camera is not None else None,
            "map_state": state.map_state,
            "page_context": page_context,
            "map_ui_telemetry_available": map_ui_available,
            "poi_observation": (state.map_state or {}).get("poi_observation"),
            "nearby_actors": [asdict(item) for item in state.nearby_actors],
            "nearby_interactables": [asdict(item) for item in state.nearby_interactables],
            "known_landmarks": [asdict(item) for item in landmarks],
            "known_routes": [asdict(item) for item in routes],
            "recorded_route_runtime_use": self._use_recorded_routes,
            "sources": {
                "world_position": "runtime_telemetry",
                "camera_orientation": "runtime_telemetry",
                "map_identity": "runtime_telemetry",
                "nearby_world_actors": "runtime_sidecar",
                "map_ui_markers": map_ui_source,
                "map_ui_page": map_ui_source,
                "known_landmarks": "agent_known_world_store",
            },
        }

    def map_ui_telemetry(self) -> dict[str, Any]:
        """Return only the validated, read-only map overlay snapshot."""
        result = self.map_telemetry()
        available = bool(result.get("map_ui_telemetry_available"))
        state = result.get("map_state") if available else None
        meta = state.get("_map_ui_meta") if isinstance(state, dict) else None
        return {
            "ok": True,
            "available": available,
            "map_id": result.get("map_id"),
            "sequence": result.get("sequence"),
            "telemetry_live": result.get("telemetry_live"),
            "telemetry_age_seconds": result.get("telemetry_age_seconds"),
            "paused": result.get("paused"),
            "in_menu": result.get("in_menu"),
            "map_state": state,
            "page_context": state.get("page") if isinstance(state, dict) else None,
            "markers": state.get("markers") if isinstance(state, dict) else [],
            "provenance": meta,
            "source": "runtime_map_ui_sidecar" if available else "not_exposed",
        }
    def _tap_map_control(self, button: str, milliseconds: int, action: str) -> dict[str, Any]:
        if button not in MAP_INPUT_BUTTONS:
            raise SafetyError("unsupported map control")
        if int(milliseconds) < MAP_BUTTON_MINIMUM_MS:
            raise SafetyError("map controls require a hold of at least 500 ms")
        if getattr(self.input, "is_real", False):
            state = self._assert_real_menu_input_allowed(button, milliseconds)
        else:
            state = self._assert_action_allowed(allow_paused=True)
        if not state.paused and not state.in_menu:
            raise SafetyError("map controls require the map or another paused menu to be open")
        self.input.tap(button, milliseconds)
        result = {
            "ok": True,
            "sequence": state.sequence,
            "action": action,
            "button": button,
            "mode": "map",
            "control_scope": (
                "page_local"
                if button in {"map_selection_prev", "map_selection_next"}
                else "map_surface"
            ),
            "milliseconds": milliseconds,
        }
        self._record_action(**result)
        return result

    def map_selection(self, direction: str, milliseconds: int = 500) -> dict[str, Any]:
        direction = str(direction).strip().lower()
        if direction in {"previous", "prev", "left", "back"}:
            return self._tap_map_control("map_selection_prev", milliseconds, "map_selection_previous")
        if direction in {"next", "right", "forward"}:
            return self._tap_map_control("map_selection_next", milliseconds, "map_selection_next")
        raise SafetyError("map selection direction must be previous or next")

    # Compatibility alias for the first map experiment. Z/X are page-local,
    # so this legacy name must not be used to infer POI semantics.
    def map_poi(self, direction: str, milliseconds: int = 500) -> dict[str, Any]:
        return self.map_selection(direction, milliseconds)

    def map_tab(self, direction: str, milliseconds: int = 500) -> dict[str, Any]:
        direction = str(direction).strip().lower()
        if direction in {"previous", "prev", "left", "back"}:
            return self._tap_map_control("map_tab_prev", milliseconds, "map_tab_previous")
        if direction in {"next", "right", "forward"}:
            return self._tap_map_control("map_tab_next", milliseconds, "map_tab_next")
        raise SafetyError("map tab direction must be previous or next")

    def toggle_map_layer(self, milliseconds: int = 500) -> dict[str, Any]:
        return self._tap_map_control("map_layer_toggle", milliseconds, "map_layer_toggle")

    def toggle_map_era(self, milliseconds: int = 500) -> dict[str, Any]:
        return self._tap_map_control("map_era_toggle", milliseconds, "map_era_toggle")

    def view_map_corresponding(self, milliseconds: int = 500) -> dict[str, Any]:
        return self._tap_map_control(
            "map_view_corresponding", milliseconds, "map_view_corresponding"
        )

    def drop_map_pin(self, milliseconds: int = 500) -> dict[str, Any]:
        return self._tap_map_control("map_drop_pin", milliseconds, "map_drop_pin")

    def capture_game_window(self, backend: str = "window") -> dict[str, Any]:
        """Capture the allow-listed game window or its verified OBS source."""
        if backend not in {"window", "obs"}:
            raise ValueError("capture backend must be window or obs")
        if not getattr(self.input, "is_real", False):
            raise SafetyError("game-window capture requires the Windows backend")
        phase_allowed, phase_acquired = self._acquire_local_episode_phase()
        if not phase_allowed:
            raise SafetyError("another input action is executing")
        try:
            return self._capture_game_window_under_action(backend)
        finally:
            self._release_local_episode_phase(phase_acquired)

    def _capture_game_window_under_action(self, backend: str = "window") -> dict[str, Any]:
        """Capture while the caller already owns the action lock; never send input."""
        process_ids = self._environment.game_process_ids()
        # Process discovery may be slow. Read the bound telemetry after
        # that work so its age describes the upcoming capture.
        state = self.telemetry.observe()
        output_dir = self._screenshot_dir
        if output_dir is None:
            raise SafetyError("screenshot output directory is not configured")
        try:
            capture_kwargs = {}
            if backend == "window" and self._background_input_enabled():
                render_windows = getattr(self._environment, "game_render_window_handles", None)
                if not callable(render_windows):
                    raise CaptureError("background capture requires a game render-window enumerator")
                windows = render_windows()
                if len(windows) != 1:
                    raise CaptureError("background game window identity is ambiguous")
                capture_kwargs["background_hwnd"] = windows[0]["hwnd"]
            if backend == "obs":
                result = capture_obs_game_source(process_ids, output_dir, sequence=state.sequence)
                if self._environment.game_process_ids() != process_ids:
                    raise CaptureError("game process identity changed during OBS capture; evidence preserved")
            else:
                result = capture_foreground_game_window(
                    process_ids,
                    output_dir,
                    sequence=state.sequence,
                    **capture_kwargs,
                )
        except CaptureError as exc:
            raise SafetyError(str(exc)) from exc
        prune_screenshots(output_dir, SCREENSHOT_KEEP)
        startup_frame = result.get("startup_frame")
        render_classifier_label = (
            startup_frame.get("label")
            if isinstance(startup_frame, dict)
            else None
        )
        telemetry_live = (
            state.source == "simulation"
            or state.age_seconds() <= self.max_telemetry_age
        )
        if (
            telemetry_live
            and state.running
            and not state.paused
            and not state.in_menu
            and state.map_id is not None
            and state.player is not None
            and state.player.position is not None
            and isinstance(startup_frame, dict)
            # Telemetry reports a loaded world while the loading screen is
            # still up (live 2026-09-28), so it never overrides that frame.
            and render_classifier_label != "loading"
        ):
            # The brightness classifier is intentionally conservative but
            # cannot distinguish every dark gameplay composition from the
            # title/menu layout. Fresh spatial telemetry is authoritative
            # for the opposite case: a live loaded world is never a
            # startup boundary.
            result["startup_frame"] = {
                **startup_frame,
                "render_classifier_label": render_classifier_label,
                "label": "gameplay",
                "telemetry_override": "fresh_loaded_gameplay",
            }
        result.update({
            "telemetry_sequence": state.sequence,
            "map_id": state.map_id,
            "telemetry_age_seconds": state.age_seconds(),
            "camera_bearing_degrees": (
                state.camera.rotation.yaw
                if state.camera is not None and state.camera.rotation is not None
                else None
            ),
        })
        return result

    def _capture_blocker_visual_evidence(self, reason: str) -> dict[str, Any]:
        """Capture a fresh frame whenever telemetry vetoes a movement corridor.

        The frame is evidence for a later visual review; this method deliberately
        does not interpret it or authorize a detour.  Keeping capture here means
        every coordinator-level physical blocker follows the same fail-closed
        workflow, including callers that did not explicitly request a screenshot.
        """
        try:
            evidence = self.capture_game_window()
            if not isinstance(evidence, dict):
                evidence = {"ok": False, "reason": "game capture returned a non-object result"}
            evidence["interpretation"] = "captured_only_not_semantically_classified"
        except Exception as exc:
            evidence = {"ok": False, "reason": str(exc)}
        return {
            "required": True,
            "reason": reason,
            "capture": evidence,
            "detour_authorized": False,
            "next_step": "inspect the fresh frame and submit structured visual_review before a detour",
        }

    def navigation_context(
        self,
        target_id: str | None = None,
        target_kind: str | None = None,
    ) -> dict[str, Any]:
        """Return read-only navigation data and the next target-relative decision."""
        from dataclasses import asdict

        state = self.telemetry.observe()
        candidates = navigation_candidates(state)
        landmarks = self._runtime_landmarks(state.map_id)
        selected = select_navigation_target(state, target_id, target_kind)
        if selected is None and target_id is not None:
            stored = next(
                (item for item in landmarks if item.landmark_id == target_id),
                None,
            )
            if stored is not None:
                selected = NavigationTarget.from_landmark(stored, state)
                candidates.append(selected)
        decision = decide_navigation_step(selected) if selected is not None else None
        routes = (
            self._known_world_store.list_routes(state.map_id)
            if self._use_recorded_routes and self._known_world_store is not None and state.map_id
            else []
        )
        return {
            "map_id": state.map_id,
            "location": state.location,
            "sequence": state.sequence,
            "telemetry_age_seconds": state.age_seconds(),
            "telemetry_live": state.source == "simulation"
            or state.age_seconds() <= self.max_telemetry_age,
            "player": asdict(state.player) if state.player is not None else None,
            "camera": asdict(state.camera) if state.camera is not None else None,
            "navigation": state.navigation,
            "candidates": [asdict(item) for item in candidates],
            "selected_target_id": selected.target_id if selected is not None else None,
            "decision": asdict(decision) if decision is not None else None,
            "known_landmarks": [asdict(item) for item in landmarks],
            "known_routes": [asdict(item) for item in routes],
            "recent_navigation_trace": list(self._navigation_trace[-16:]),
            "heading_blockers": [asdict(item) for item in self._map_heading_blockers(state.map_id)],
        }

    def plan_navigation_route(
        self,
        target_id: str | None = None,
        target_kind: str | None = None,
        map_scale_m_per_pixel: float | None = None,
    ) -> dict[str, Any]:
        """Return a route envelope before any movement input is sent."""
        from dataclasses import asdict

        state = self.telemetry.observe()
        selected = select_navigation_target(state, target_id, target_kind)
        landmarks = self._runtime_landmarks(state.map_id)
        if selected is None and target_id is not None:
            stored = next((item for item in landmarks if item.landmark_id == target_id), None)
            if stored is not None:
                selected = NavigationTarget.from_landmark(stored, state)
        if selected is None:
            raise SafetyError("route planning requires a visible or known target")
        routes = (
            self._known_world_store.list_routes(state.map_id)
            if self._use_recorded_routes and self._known_world_store is not None and state.map_id
            else []
        )
        plan = plan_route(state, selected, routes, map_scale_m_per_pixel)
        if not self._use_recorded_routes and plan.path_preference == "known_route":
            raise SafetyError("recorded route use is disabled for runtime navigation")
        return {"ok": True, "route_plan": asdict(plan)}

    def _refresh_coordinator_capabilities(self) -> dict[str, Any]:
        """Re-arm one coordinator phase through an explicit live session lease."""
        if not getattr(self.input, "is_real", False):
            return {"ok": True, "mode": "simulation"}
        if self._session_lease is None:
            return {
                "ok": False,
                "mode": "manual_arm_only",
                "reason": "evidence-fusion coordination requires an explicit control session to re-arm bounded phases",
            }
        status = self._session_lease.status()
        if not status or not status.get("active"):
            return {
                "ok": False,
                "mode": "session_required",
                "reason": "control session is not active",
                "session": status,
            }
        try:
            seconds = self._rearm_active_session()
            return {
                "ok": True,
                "mode": "leased_rearm",
                "armed_seconds": seconds,
                "session": self._session_lease.status(),
            }
        except Exception as exc:
            return {"ok": False, "mode": "leased_rearm", "reason": str(exc)}

    def _latched_stop_message(self) -> str:
        """Why input is refused: closing a control session latches the stop
        too, and the usual cause is a session a route or task closed (#193)."""
        lease = self._session_lease
        if getattr(self.input, "is_real", False) and lease is not None:
            try:
                status = lease.status()
            except Exception:
                status = None
            if not status or not status.get("active"):
                return "emergency stop is latched: no control session is open; open_control_session first"
        return "emergency stop is latched"

    def _rearm_active_session(self) -> int:
        """Re-arm before an ordinary bounded action while a control session is open.

        Every real action still latches the stop broker when it finishes.  An
        open control session is the explicit authorization to continue, so a
        subsequent action should re-arm itself instead of requiring an
        external reset-stop command between every movement or camera pulse.
        The session lasts as long as the game does and needs no renewing;
        closing or stopping it, or the game exiting, disables this path, so
        an emergency stop remains effective outside an open session.
        """
        if not getattr(self.input, "is_real", False) or self._session_lease is None:
            return 0
        status = self._session_lease.status()
        if not status or not status.get("active"):
            if status and status.get("state") == "GAME_EXITED":
                # The game this session was for has gone: nothing re-arms, and
                # what the last action left armed lapses with it.
                for broker in (self._arm, self._bench_arm, self._camera_arm):
                    if broker is not None:
                        broker.latch_stop()
                with self._lock:
                    self._stopped = True
            return 0
        if status.get("lifetime") != LIFETIME_GAME:
            # Written before sessions lasted for the game's life, so still
            # on a wall-clock lease: adopt it rather than let it lapse.
            self._session_lease.heartbeat()
        seconds = 120
        if self._arm is not None:
            self._arm.reset_stop()
            self._arm.arm(seconds)
        if self._bench_arm is not None:
            self._bench_arm.reset_stop()
        if self._camera_arm is not None:
            self._camera_arm.reset_stop()
            self._camera_arm.arm(seconds)
        with self._lock:
            self._stopped = False
        return seconds

    def _wait_for_coordinator_cooldown(self) -> None:
        """Absorb the normal inter-action gap between coordinator phases."""
        remaining = self._minimum_action_delay - (
            self._monotonic() - self._last_action_finished
        )
        if remaining > 0.0:
            self._sleeper(remaining)

    def _wait_for_coordinator_navigation(self, minimum_sequence: int) -> Any | None:
        """Wait briefly for camera-relative navigation evidence after a turn."""
        deadline = self._monotonic() + 2.0
        latest = None
        while self._monotonic() < deadline:
            try:
                candidate = self.telemetry.observe()
            except Exception:
                candidate = None
            if candidate is not None:
                latest = candidate
                payload = candidate.navigation if isinstance(candidate.navigation, dict) else {}
                directions = payload.get("directions")
                if (
                    candidate.sequence > minimum_sequence
                    and isinstance(directions, dict)
                    and any(isinstance(item, dict) for item in directions.values())
                ):
                    return candidate
            self._sleeper(0.1)
        return latest

    def _wait_for_long_range_walkability(
        self,
        minimum_sequence: int,
        direction: str = "forward",
    ) -> Any | None:
        """Wait for the low-frequency character-volume sample for a bounded hold.

        The cheap line traces update frequently, but they cannot authorize a
        long movement segment. The same sample is also useful for a short
        segment when a line trace ends at a distant object and otherwise
        cannot establish route continuity. Waiting here avoids treating a
        scheduled capsule sample as an immediate wall.
        """
        deadline = self._monotonic() + 3.5
        latest = None
        while self._monotonic() < deadline:
            try:
                candidate = self.telemetry.observe()
            except Exception:
                candidate = None
            if candidate is not None:
                latest = candidate
                payload = candidate.navigation if isinstance(candidate.navigation, dict) else {}
                directions = payload.get("directions")
                evidence = directions.get(direction) if isinstance(directions, dict) else None
                capsule = fresh_capsule_probe(payload, evidence) if isinstance(evidence, dict) else None
                if candidate.sequence > minimum_sequence and capsule is not None:
                    return candidate
            self._sleeper(0.1)
        return latest

    def _wait_for_visual_review_capsule(self, minimum_sequence: int) -> Any | None:
        """Wait for one fresh capsule cycle before a reviewed detour decision.

        Visual detours are deliberately short and infrequent. Waiting for the
        existing low-frequency capsule sample avoids treating a scheduled
        sample as a wall, without adding a continuous probe or increasing the
        UE4SS polling rate.
        """
        deadline = self._monotonic() + 3.5
        latest = None
        while self._monotonic() < deadline:
            try:
                candidate = self.telemetry.observe()
            except Exception:
                candidate = None
            if candidate is not None:
                latest = candidate
                payload = candidate.navigation if isinstance(candidate.navigation, dict) else {}
                directions = payload.get("directions")
                if candidate.sequence > minimum_sequence and isinstance(directions, dict):
                    if any(
                        isinstance(evidence, dict)
                        and fresh_capsule_probe(payload, evidence) is not None
                        for evidence in directions.values()
                    ):
                        return candidate
            self._sleeper(0.1)
        return latest

    def navigate_next_segment(
        self,
        target_id: str | None = None,
        target_kind: str | None = None,
        target_position: dict[str, float] | None = None,
        route_bearing_degrees: float | None = None,
        route_source: str = "unavailable",
        route_segment_start: dict[str, float] | None = None,
        route_segment_end: dict[str, float] | None = None,
        maximum_ms: int = ADAPTIVE_TRAVEL_COORDINATOR_DEFAULT_MS,
        maximum_distance_m: float = ADAPTIVE_TRAVEL_COORDINATOR_DEFAULT_DISTANCE_METRES,
        survey: bool = True,
        capture_screenshot: bool = False,
        visual_reviewed: bool = False,
        visual_review: dict[str, Any] | None = None,
        movement_vector: dict[str, float] | None = None,
        sprint: bool = False,
    ) -> dict[str, Any]:
        """Fuse live evidence, then execute one verified movement segment.

        This is deliberately a coordinator, not a Chadley macro.  It can use
        a supplied route leg, a runtime navmesh/occupancy route exposed in
        ``navigation``, or a target vector as a low-confidence tie-breaker.
        It never reads human traversal recordings.  A real-game call requires
        an explicit control-session lease so survey, camera alignment, and
        movement can each be re-armed without weakening the one-shot safety
        gates.
        """
        from dataclasses import asdict

        try:
            maximum_ms = int(maximum_ms)
            maximum_distance_m = float(maximum_distance_m)
        except (TypeError, ValueError) as exc:
            raise SafetyError("coordinator movement limits are invalid") from exc
        if not 100 <= maximum_ms <= ADAPTIVE_TRAVEL_MAXIMUM_MS:
            raise SafetyError(
                f"coordinator maximum_ms must be between 100 and {ADAPTIVE_TRAVEL_MAXIMUM_MS}"
            )
        if not 0.5 <= maximum_distance_m <= ADAPTIVE_TRAVEL_MAX_DISTANCE_METRES:
            raise SafetyError(
                f"coordinator maximum_distance_m must be between 0.5 and {ADAPTIVE_TRAVEL_MAX_DISTANCE_METRES}"
            )
        vector_axes: tuple[float, float] | None = None
        if movement_vector is not None:
            if not isinstance(movement_vector, dict):
                raise SafetyError("movement_vector must contain numeric x and y")
            try:
                vector_axes = normalise_axes(
                    movement_vector["x"], movement_vector["y"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SafetyError("movement_vector must contain numeric x and y") from exc
            vector_x, vector_y = vector_axes
            if vector_y < 0.70 or abs(vector_x) > 0.60:
                raise SafetyError(
                    "visual vector detours must be forward-biased with lateral input between -0.60 and 0.60"
                )
            if maximum_distance_m > LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES:
                raise SafetyError(
                    "visual vector detours are limited to 3 m per reviewed segment"
                )
        if route_bearing_degrees is not None and route_source in {"", "unavailable", "route_required"}:
            route_source = "caller_route_leg"

        state = self.telemetry.observe()
        live = state.source == "simulation" or state.age_seconds() <= self.max_telemetry_age
        result: dict[str, Any] = {
            "ok": False,
            "action": "navigate_next_segment",
            "runtime_route_policy": "human_recordings_excluded",
            "sequence_before": state.sequence,
            "map_id": state.map_id,
            "survey": None,
            "visual_evidence": None,
            "blocker_review": None,
            "map_evidence": None,
            "decision": None,
            "camera_alignment": None,
            "movement": None,
            "movement_vector_requested": (
                {"x": vector_axes[0], "y": vector_axes[1]}
                if vector_axes is not None else None
            ),
            "movement_vector_authorized": False,
            "sprint_requested": bool(sprint),
            "verification": None,
            "replan_required": False,
        }
        if not state.running or not live:
            result.update({
                "decision": "not_ready",
                "reason": "gameplay telemetry is not running and fresh",
                "replan_required": True,
            })
            self._record_action(**result)
            return result
        if state.paused or state.in_menu or state.in_combat or state.target is not None:
            result.update({
                "decision": "blocked_by_gameplay_state",
                "reason": "pause, menu, combat, or selected target blocks navigation",
                "replan_required": True,
            })
            self._record_action(**result)
            return result
        if state.player is None or state.player.position is None:
            result.update({
                "decision": "perception_required",
                "reason": "player spatial telemetry is unavailable",
                "replan_required": True,
            })
            self._record_action(**result)
            return result

        # A coordinator step has multiple bounded phases.  On Windows, do
        # not allow it to partially execute unless the caller has explicitly
        # opened the renewable control session.
        capability = self._refresh_coordinator_capabilities()
        result["capability"] = capability
        if not capability.get("ok"):
            result.update({
                "decision": "control_session_required",
                "reason": capability.get("reason"),
                "replan_required": False,
            })
            self._record_action(**result)
            return result

        if survey:
            try:
                result["survey"] = self.survey_camera(60.0, 80)
                if not result["survey"].get("ok"):
                    result.update({
                        "decision": "camera_survey_required",
                        "reason": result["survey"].get("reason") or "camera survey did not produce a verified turn",
                        "replan_required": True,
                    })
                    self._record_action(**result)
                    return result
                state = self.telemetry.observe()
            except Exception as exc:
                result["survey"] = {"ok": False, "reason": str(exc)}
                result.update({
                    "decision": "camera_survey_required",
                    "reason": str(exc),
                    "replan_required": True,
                })
                self._record_action(**result)
                return result

        if capture_screenshot:
            try:
                result["visual_evidence"] = self.capture_game_window()
                result["visual_evidence"]["interpretation"] = "captured_only_not_semantically_classified"
            except Exception as exc:
                result["visual_evidence"] = {"ok": False, "reason": str(exc)}
            result.update({
                "decision": "visual_review_required",
                "reason": "inspect this frame, then resume with structured visual_review; movement was not attempted",
                "replan_required": True,
            })
            self._record_action(**result)
            return result

        try:
            result["map_evidence"] = self.map_telemetry()
        except Exception as exc:
            result["map_evidence"] = {"ok": False, "reason": str(exc)}

        selected = self._planning_target(state, target_id)
        goal = None
        if target_position is not None:
            try:
                goal = Vec3.from_dict(target_position)
            except (KeyError, TypeError, ValueError) as exc:
                raise SafetyError("target_position must contain numeric x, y and z") from exc
        elif selected is not None:
            goal = selected.interaction_position or selected.world_position
        goal_bearing = (
            world_bearing_degrees(state.player.position, goal)
            if goal is not None else None
        )

        resolved_route: dict[str, Any] | None = None
        if route_bearing_degrees is None and selected is not None:
            routes = (
                self._known_world_store.list_routes(state.map_id)
                if self._use_recorded_routes and self._known_world_store is not None and state.map_id
                else []
            )
            route_plan = plan_route(state, selected, routes)
            resolved_route = asdict(route_plan)
            if route_plan.legs and route_plan.path_preference != "route_required":
                try:
                    route_bearing_degrees = float(route_plan.legs[0]["bearing_degrees"])
                    route_source = route_plan.route_source
                    route_segment_start = route_segment_start or route_plan.legs[0].get("from_position")
                    route_segment_end = route_segment_end or route_plan.legs[0].get("to_position")
                except (KeyError, TypeError, ValueError):
                    route_bearing_degrees = None
        result["route_plan"] = resolved_route

        reviewed_bearing_degrees = None
        visual_review_precheck_ok = False
        if isinstance(visual_review, dict):
            try:
                reviewed_bearing_degrees = float(visual_review["reviewed_bearing_degrees"])
            except (KeyError, TypeError, ValueError):
                reviewed_bearing_degrees = None
            if reviewed_bearing_degrees is not None:
                precheck_ok, precheck_reason = self._validate_visual_review(
                    visual_review,
                    None,
                )
                result["visual_review_precheck"] = {
                    "accepted": precheck_ok,
                    "reason": precheck_reason,
                }
                visual_review_precheck_ok = precheck_ok
                if not precheck_ok:
                    reviewed_bearing_degrees = None
        if reviewed_bearing_degrees is not None:
            review_capsule_state = self._wait_for_visual_review_capsule(int(state.sequence))
            if review_capsule_state is not None and review_capsule_state.sequence > state.sequence:
                result["visual_review_capsule_wait"] = {
                    "minimum_sequence": state.sequence,
                    "sequence": review_capsule_state.sequence,
                    "fresh_capsule_observed": True,
                }
                state = review_capsule_state
        fusion = fuse_direction_evidence(
            state,
            route_bearing_degrees=route_bearing_degrees,
            goal_bearing_degrees=goal_bearing,
            route_source=route_source,
            blockers=self._map_heading_blockers(state.map_id),
            visual_review_bearing_degrees=reviewed_bearing_degrees,
            movement_horizon_m=(
                min(maximum_distance_m, LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES)
                + 0.75
                if maximum_distance_m <= LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES
                else None
            ),
        )
        result["decision"] = fusion.to_dict()
        selected_direction = fusion.selected_direction
        selected_bearing_degrees = fusion.selected_bearing_degrees
        vector_detour = False
        vector_side_direction = None
        vector_side_evidence = None
        vector_side_capsule = None
        if (
            vector_axes is not None
            and visual_review_precheck_ok
            and reviewed_bearing_degrees is not None
            and (fusion.selected_direction in {None, "forward"})
        ):
            vector_side_direction = "left" if vector_axes[0] < 0.0 else "right"
            navigation_payload = state.navigation if isinstance(state.navigation, dict) else {}
            directions = navigation_payload.get("directions")
            if isinstance(directions, dict):
                candidate_side = directions.get(vector_side_direction)
                if isinstance(candidate_side, dict):
                    vector_side_evidence = candidate_side
                    vector_side_capsule = fresh_capsule_probe(
                        navigation_payload, candidate_side
                    )
            side_clearance = None
            if isinstance(vector_side_evidence, dict):
                try:
                    side_clearance = float(
                        vector_side_evidence.get("clear_distance_m")
                    )
                except (TypeError, ValueError):
                    side_clearance = None
            side_walkable = bool(
                isinstance(vector_side_capsule, dict)
                and not vector_side_capsule.get(
                    "blocking_hit", vector_side_capsule.get("hit", False)
                )
                and (
                    vector_side_capsule.get("corridor_confirmed") is True
                    or vector_side_capsule.get("walkable_surface_hit") is True
                )
            )
            side_blocked = bool(
                isinstance(vector_side_evidence, dict)
                and _navigation_obstacle_within_horizon(
                    navigation_payload,
                    vector_side_evidence,
                    LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES,
                )
            )
            vector_detour = bool(
                isinstance(vector_side_evidence, dict)
                and vector_side_capsule is not None
                and side_walkable
                and not side_blocked
                and side_clearance is not None
                and side_clearance >= 1.0
            )
            if vector_detour:
                selected_direction = "forward_diagonal"
                selected_bearing_degrees = reviewed_bearing_degrees
                decision = fusion.to_dict()
                decision.update({
                    "outcome": "visual_vector_detour_selected",
                    "selected_direction": selected_direction,
                    "selected_bearing_degrees": selected_bearing_degrees,
                    "reason": "fresh side capsule evidence authorizes a short reviewed diagonal detour",
                })
                result["decision"] = decision
                result["movement_vector_authorized"] = True
                result["vector_detour_evidence"] = {
                    "side_direction": vector_side_direction,
                    "side": dict(vector_side_evidence),
                    "side_capsule": dict(vector_side_capsule),
                }
        if vector_axes is not None and not vector_detour:
            result.update({
                "decision": "visual_vector_detour_replan",
                "reason": (
                    "the requested diagonal detour lacks a matching visual review and fresh "
                    "non-blocking walkable side capsule"
                ),
                "replan_required": True,
            })
            self._record_action(**result)
            return result
        if selected_direction is None:
            blocker_reason = (
                "local corridor is blocked or uncertain; inspect the fresh frame for an avoidable side path"
            )
            result["blocker_review"] = self._capture_blocker_visual_evidence(blocker_reason)
            result["visual_evidence"] = result["blocker_review"]["capture"]
            result.update({
                "reason": fusion.reason,
                "replan_required": fusion.outcome in {
                    "blocked_or_uncertain",
                    "perception_required",
                    "route_deviation_replan",
                },
            })
            self._record_action(**result)
            return result

        # The vector is relative to the camera.  Keep the camera aligned to
        # the reviewed travel heading, while retaining the actual movement
        # bearing for post-action corridor verification.
        movement_bearing_degrees = selected_bearing_degrees
        if vector_detour and vector_axes is not None and movement_bearing_degrees is not None:
            movement_bearing_degrees = normalise_degrees(
                movement_bearing_degrees
                + math.degrees(math.atan2(vector_axes[0], vector_axes[1]))
            )

        # Align the camera to the selected corridor, not blindly to the final
        # target.  The cardinal travel action can then be one forward hold.
        rearm = self._refresh_coordinator_capabilities()
        result["camera_capability"] = rearm
        if not rearm.get("ok"):
            result.update({"decision": "control_session_required", "reason": rearm.get("reason")})
            self._record_action(**result)
            return result
        self._wait_for_coordinator_cooldown()
        alignment = self.align_camera_to_bearing(
            selected_bearing_degrees,
            tolerance_degrees=3.0,
            maximum_steps=8,
        )
        result["camera_alignment"] = alignment
        if not alignment.get("ok"):
            result.update({
                "decision": "camera_alignment_required",
                "reason": alignment.get("rejection_reason") or alignment.get("outcome"),
                "replan_required": True,
            })
            self._record_action(**result)
            return result

        post_alignment_sequence = int(alignment.get("post_sequence") or state.sequence)
        long_range_requested = bool(
            getattr(self.input, "is_real", False)
            and maximum_distance_m > LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES
        )
        post_alignment = (
            self._wait_for_long_range_walkability(post_alignment_sequence)
            if long_range_requested
            else self._wait_for_coordinator_navigation(post_alignment_sequence)
        )
        # Short real-input segments normally use the cheap line traces. If
        # the post-alignment sample lacks a fresh character-volume result,
        # however, the coordinator cannot distinguish a clear corridor, a
        # distant endpoint, or a near capsule obstacle. Obtain one fresh
        # sample before authorising the bounded hold. Keep this conditional so
        # already-confirmed short paths retain their low-latency path.
        post_alignment_navigation = (
            post_alignment.navigation
            if post_alignment is not None and isinstance(post_alignment.navigation, dict)
            else {}
        )
        post_alignment_directions = post_alignment_navigation.get("directions")
        post_alignment_forward = (
            post_alignment_directions.get("forward")
            if isinstance(post_alignment_directions, dict)
            else None
        )
        has_fresh_forward_capsule = (
            fresh_capsule_probe(post_alignment_navigation, post_alignment_forward)
            is not None
            if isinstance(post_alignment_forward, dict)
            else False
        )
        bounded_walkability_requested = bool(
            getattr(self.input, "is_real", False)
            and not long_range_requested
            and maximum_distance_m <= LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES
            and not has_fresh_forward_capsule
            and isinstance(post_alignment_forward, dict)
        )
        if bounded_walkability_requested:
            bounded_walkability = self._wait_for_long_range_walkability(
                int(getattr(post_alignment, "sequence", post_alignment_sequence))
            )
            if bounded_walkability is not None:
                bounded_navigation = (
                    bounded_walkability.navigation
                    if isinstance(bounded_walkability.navigation, dict)
                    else {}
                )
                bounded_directions = bounded_navigation.get("directions")
                bounded_forward = (
                    bounded_directions.get("forward")
                    if isinstance(bounded_directions, dict)
                    else None
                )
                if (
                    isinstance(bounded_forward, dict)
                    and fresh_capsule_probe(bounded_navigation, bounded_forward) is not None
                ):
                    post_alignment = bounded_walkability
                    post_alignment_navigation = bounded_navigation
                    post_alignment_directions = bounded_directions
                    post_alignment_forward = bounded_forward
        result["walkability_wait"] = {
            "requested": bool(long_range_requested or bounded_walkability_requested),
            "mode": (
                "long_range"
                if long_range_requested
                else "bounded_short_segment"
                if bounded_walkability_requested
                else "none"
            ),
            "fresh_capsule_observed": bool(
                post_alignment is not None
                and isinstance(post_alignment.navigation, dict)
                and isinstance(
                    fresh_capsule_probe(
                        post_alignment.navigation,
                        (
                            post_alignment.navigation.get("directions", {}).get("forward")
                            if isinstance(post_alignment.navigation.get("directions"), dict)
                            else None
                        ),
                    ),
                    dict,
                )
            ),
            "sequence": getattr(post_alignment, "sequence", None),
        }
        forward_evidence = (
            post_alignment_directions.get("forward")
            if isinstance(post_alignment_directions, dict)
            else None
        )
        post_vector_side_evidence = (
            post_alignment_directions.get(vector_side_direction)
            if vector_detour
            and vector_side_direction is not None
            and isinstance(post_alignment_directions, dict)
            else None
        )
        corridor_evidence = (
            post_vector_side_evidence
            if vector_detour
            else forward_evidence
        )
        corridor_direction = vector_side_direction if vector_detour else "forward"
        if not isinstance(corridor_evidence, dict):
            result.update({
                "decision": "post_alignment_perception_required",
                "reason": (
                    "fresh lateral navigation evidence was not observed for the reviewed vector detour"
                    if vector_detour
                    else "fresh camera-relative navigation evidence was not observed"
                ),
                "post_alignment_sequence": getattr(post_alignment, "sequence", None),
                "replan_required": True,
            })
            self._record_action(**result)
            return result
        try:
            post_alignment_clearance = float(corridor_evidence.get("clear_distance_m"))
        except (TypeError, ValueError):
            post_alignment_clearance = None
        capsule_probe = fresh_capsule_probe(post_alignment_navigation, corridor_evidence)
        forward_capsule_probe = (
            fresh_capsule_probe(post_alignment_navigation, forward_evidence)
            if isinstance(forward_evidence, dict)
            else None
        )
        result["post_alignment_navigation"] = {
            "sequence": getattr(post_alignment, "sequence", None),
            "probe_kind": post_alignment_navigation.get("probe_kind"),
            "corridor_direction": corridor_direction,
            "forward": dict(forward_evidence) if isinstance(forward_evidence, dict) else None,
            "corridor": dict(corridor_evidence),
            "side": (
                dict(post_vector_side_evidence)
                if isinstance(post_vector_side_evidence, dict) else None
            ),
        }
        forward_center_blocked = bool(
            isinstance(forward_evidence, dict)
            and _navigation_obstacle_within_horizon(
                post_alignment_navigation,
                forward_evidence,
                min(maximum_distance_m, LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES),
            )
        )
        corridor_blocked = _navigation_obstacle_within_horizon(
            post_alignment_navigation,
            corridor_evidence,
            min(maximum_distance_m, LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES) + 0.75,
        )
        # A diagonal detour may legitimately have a companion or foliage in
        # the camera-centre ray. It still fails closed if the measured lateral
        # corridor is blocked, or if the forward capsule itself reports a
        # character-sized blocking hit that the diagonal cannot be trusted to
        # avoid.
        vector_center_exception = bool(
            vector_detour
            and forward_center_blocked
            and isinstance(forward_capsule_probe, dict)
            and not forward_capsule_probe.get(
                "blocking_hit", forward_capsule_probe.get("hit", False)
            )
        )
        if (
            corridor_blocked
            or post_alignment_clearance is None
            or post_alignment_clearance < 1.0
            or (forward_center_blocked and not vector_center_exception)
        ):
            blocker_reason = (
                "fresh post-alignment corridor is blocked; inspect the frame before choosing a detour"
            )
            result["blocker_review"] = self._capture_blocker_visual_evidence(blocker_reason)
            result["visual_evidence"] = result["blocker_review"]["capture"]
            result.update({
                "decision": "post_alignment_obstacle_replan",
                "reason": (
                    "the freshly measured lateral detour corridor is blocked after camera alignment"
                    if vector_detour and corridor_blocked
                    else "the freshly measured forward corridor is blocked after camera alignment"
                ),
                "replan_required": True,
            })
            self._record_action(**result)
            return result

        probe_kind = str(
            post_alignment_navigation.get("probe_kind")
            or post_alignment_navigation.get("source")
            or ""
        )
        walkability_verified = bool(
            capsule_probe is not None
            and (
                capsule_probe.get(
                    "corridor_confirmed", not capsule_probe.get("hit", False)
                ) is True
                or (
                    capsule_probe.get("walkable_surface_hit") is True
                    and capsule_probe.get("blocking_hit", False) is False
                )
            )
        ) or any(
            marker in probe_kind.lower()
            for marker in ("swept", "walkable_surface", "navmesh_corridor")
        )
        short_segment_horizon_m = (
            min(maximum_distance_m, LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES)
            + 0.75
            if maximum_distance_m <= LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES
            else None
        )
        bounded_capsule_clearance = _bounded_capsule_clearance_m(
            capsule_probe,
            short_segment_horizon_m,
        )
        if bounded_capsule_clearance is not None and not corridor_blocked:
            walkability_verified = True
        verified_clearance = _verified_corridor_clearance_m(
            capsule_probe,
            probe_kind,
            post_alignment_clearance,
        )
        if verified_clearance is None:
            verified_clearance = bounded_capsule_clearance
        requires_visual_review = not walkability_verified
        if visual_reviewed and visual_review is None:
            result.update({
                "decision": "visual_review_required",
                "reason": "visual_reviewed is only a compatibility flag; provide structured visual_review evidence",
                "replan_required": True,
            })
            self._record_action(**result)
            return result
        if requires_visual_review and visual_review is None:
            try:
                result["visual_evidence"] = self.capture_game_window()
                result["visual_evidence"]["interpretation"] = "captured_only_not_semantically_classified"
            except Exception as exc:
                result["visual_evidence"] = {"ok": False, "reason": str(exc)}
            result.update({
                "decision": "visual_review_required",
                "reason": "line-trace evidence does not establish route continuity; inspect the aligned frame and resume with structured visual_review",
                "corridor_check": {
                    "required": True,
                    "method": "aligned_visual_review_plus_local_clearance",
                    "probe_kind": probe_kind or "unknown",
                },
                "replan_required": True,
            })
            self._record_action(**result)
            return result
        if visual_review is not None:
            review_ok, review_reason = self._validate_visual_review(
                visual_review,
                selected_bearing_degrees,
            )
            result["visual_review"] = dict(visual_review)
            if not review_ok:
                result.update({
                    "decision": "visual_review_rejected",
                    "reason": review_reason,
                    "replan_required": True,
                })
                self._record_action(**result)
                return result
            result["corridor_check"] = {
                "required": requires_visual_review,
                "method": "aligned_visual_review_plus_local_clearance"
                if requires_visual_review else "telemetry_walkability_verified",
                "probe_kind": probe_kind or "unknown",
                "accepted": True,
            }

        # Sprint is intentionally narrower than ordinary adaptive movement:
        # a line trace or screenshot can show immediate clearance but cannot
        # prove that the whole faster segment is walkable. Require a fresh
        # capsule/swept/navmesh corridor result before allowing it.
        if sprint and not walkability_verified:
            result.update({
                "decision": "sprint_requires_walkability",
                "reason": "sprint requires fresh capsule, swept, walkable-surface, or navmesh corridor evidence",
                "replan_required": True,
            })
            self._record_action(**result)
            return result

        rearm = self._refresh_coordinator_capabilities()
        result["movement_capability"] = rearm
        if not rearm.get("ok"):
            result.update({"decision": "control_session_required", "reason": rearm.get("reason")})
            self._record_action(**result)
            return result
        self._wait_for_coordinator_cooldown()
        clearance = post_alignment_clearance
        navigation_payload = post_alignment_navigation
        movement_distance_limit = maximum_distance_m
        movement_policy = (
            "visual_reviewed_vector_detour"
            if vector_detour
            else "walkability_verified"
            if walkability_verified
            else "line_trace_reassessment_only"
        )
        if not walkability_verified and "line_trace" in probe_kind.lower():
            movement_distance_limit = min(
                movement_distance_limit,
                LINE_TRACE_UNVERIFIED_MAX_DISTANCE_METRES,
            )
        if walkability_verified and verified_clearance is not None:
            # A short capsule result must not be used to authorize a longer
            # line-trace-only distance. Long holds are available only up to
            # the distance covered by the walkability evidence itself.
            movement_distance_limit = min(movement_distance_limit, verified_clearance)
        planned_distance = min(
            movement_distance_limit,
            max(0.5, float(clearance or movement_distance_limit) - 0.75),
        )
        result["movement_policy"] = {
            "probe_kind": probe_kind or "unknown",
            "walkability_verified": walkability_verified,
            "policy": movement_policy,
            "corridor_direction": corridor_direction,
            "requested_distance_m": maximum_distance_m,
            "bounded_distance_m": planned_distance,
            "walkability_clearance_m": verified_clearance,
            "sprint": bool(sprint),
        }
        predictive_plan = {
            "coordinator": "evidence_fusion",
            "selected_direction": selected_direction,
            "selected_bearing_degrees": selected_bearing_degrees,
            "movement_bearing_degrees": movement_bearing_degrees,
            "planned_distance_m": planned_distance,
            "clearance_m": clearance,
        }
        if vector_detour and vector_axes is not None:
            predictive_plan.update({
                "vector_detour": True,
                "movement_vector": {"x": vector_axes[0], "y": vector_axes[1]},
                "side_direction": vector_side_direction,
            })
            movement = self.travel_vector_chunk(
                vector_axes[0],
                vector_axes[1],
                maximum_ms,
                planned_distance,
                predictive_plan=predictive_plan,
                sprint=sprint,
            )
        else:
            movement = self.travel_direction_chunk(
                "forward",
                maximum_ms,
                planned_distance,
                predictive_plan=predictive_plan,
                sprint=sprint,
            )
        result["movement"] = movement
        post = self.telemetry.observe()
        verification: dict[str, Any] = {
            "sequence_after": post.sequence,
            "telemetry_live": post.source == "simulation" or post.age_seconds() <= self.max_telemetry_age,
            "map_unchanged": post.map_id == state.map_id,
            "post_position": asdict(post.player.position) if post.player and post.player.position else None,
            "movement_observed": bool(movement.get("horizontal_displacement", 0.0) > ADAPTIVE_FORWARD_RELEASE_DISPLACEMENT_METRES),
        }
        if post.player and post.player.position and state.player.position:
            verification["verified_displacement_m"] = horizontal_distance_m(state.player.position, post.player.position)
        if goal is not None and post.player and post.player.position:
            verification["goal_distance_before_m"] = horizontal_distance_m(state.player.position, goal)
            verification["goal_distance_after_m"] = horizontal_distance_m(post.player.position, goal)
            verification["goal_progress_m"] = verification["goal_distance_before_m"] - verification["goal_distance_after_m"]
        if route_segment_start is not None and route_segment_end is not None and post.player and post.player.position:
            try:
                verification["cross_track_after_m"] = cross_track_distance_m(
                    post.player.position,
                    Vec3.from_dict(route_segment_start),
                    Vec3.from_dict(route_segment_end),
                )
            except (KeyError, TypeError, ValueError):
                verification["cross_track_after_m"] = None
        if route_segment_end is not None and post.player and post.player.position and state.player.position:
            try:
                route_waypoint = Vec3.from_dict(route_segment_end)
                verification["waypoint_distance_before_m"] = horizontal_distance_m(
                    state.player.position, route_waypoint
                )
                verification["waypoint_distance_after_m"] = horizontal_distance_m(
                    post.player.position, route_waypoint
                )
                verification["waypoint_progress_m"] = (
                    verification["waypoint_distance_before_m"]
                    - verification["waypoint_distance_after_m"]
                )
            except (KeyError, TypeError, ValueError):
                verification["waypoint_progress_m"] = None
        post_navigation = post.navigation if isinstance(post.navigation, dict) else {}
        post_probe_kind = str(post_navigation.get("probe_kind") or post_navigation.get("source") or "")
        post_directions = post_navigation.get("directions")
        post_corridor: dict[str, Any] | None = None
        if isinstance(post_directions, dict) and movement_bearing_degrees is not None:
            ranked_post: list[tuple[float, dict[str, Any]]] = []
            for raw in post_directions.values():
                if not isinstance(raw, dict):
                    continue
                try:
                    bearing = float(raw["bearing_degrees"])
                except (KeyError, TypeError, ValueError):
                    continue
                error = abs((bearing - float(movement_bearing_degrees) + 180.0) % 360.0 - 180.0)
                ranked_post.append((error, raw))
            if ranked_post:
                post_corridor = min(ranked_post, key=lambda item: item[0])[1]
        if post_corridor is not None and "line_trace" in post_probe_kind.lower():
            try:
                verification["post_corridor_clearance_m"] = float(post_corridor.get("clear_distance_m"))
            except (TypeError, ValueError):
                verification["post_corridor_clearance_m"] = None
            verification["post_corridor_hit"] = bool(post_corridor.get("hit", False))
            verification["post_corridor_obstacle"] = bool(
                _navigation_obstacle_within_horizon(
                    post_navigation,
                    post_corridor,
                    planned_distance,
                )
                or (
                    verification["post_corridor_clearance_m"] is not None
                    and verification["post_corridor_clearance_m"] < 1.0
                )
            )
            verification["post_corridor_obstacle_horizon_m"] = max(
                1.0, float(planned_distance)
            )
        visual_detour_reviewed = (
            route_source == "visual_detour" and visual_review is not None
        )
        verification["progress_policy"] = (
            "bounded_visual_vector_detour"
            if vector_detour
            else "bounded_visual_detour_regression"
            if visual_detour_reviewed
            else "positive_route_or_goal_progress"
        )
        route_progress_ok = True
        if route_segment_end is not None:
            route_progress_ok = bool(
                verification.get("waypoint_progress_m") is not None
                and verification["waypoint_progress_m"] >= MIN_ROUTE_PROGRESS_METRES
            )
        elif goal is not None:
            goal_progress = verification.get("goal_progress_m")
            route_progress_ok = bool(
                goal_progress is not None
                and (
                    goal_progress >= MIN_GOAL_PROGRESS_METRES
                    or (
                        visual_detour_reviewed or vector_detour
                        and goal_progress >= -MAX_VISUAL_DETOUR_GOAL_REGRESSION_METRES
                    )
                )
            )
        else:
            route_progress_ok = False
        cross_track_ok = True
        if route_segment_start is not None and route_segment_end is not None:
            cross_track_ok = bool(
                verification.get("cross_track_after_m") is not None
                and verification["cross_track_after_m"] <= ROUTE_CROSS_TRACK_TOLERANCE_METRES
            )
        verification["route_progress_ok"] = route_progress_ok
        verification["cross_track_ok"] = cross_track_ok
        if verification.get("post_corridor_obstacle"):
            blocker_reason = (
                "post-movement corridor is blocked; inspect the fresh frame before choosing the next detour"
            )
            result["blocker_review"] = self._capture_blocker_visual_evidence(blocker_reason)
            result["visual_evidence"] = result["blocker_review"]["capture"]
        verification["accepted"] = bool(
            verification["telemetry_live"]
            and verification["map_unchanged"]
            and verification["movement_observed"]
            and route_progress_ok
            and cross_track_ok
            and not verification.get("post_corridor_obstacle", False)
        )
        result["verification"] = verification
        result["ok"] = verification["accepted"]
        result["replan_required"] = not verification["accepted"]
        result["decision"] = (
            "segment_verified"
            if result["ok"]
            else "post_movement_obstacle_replan"
            if verification.get("post_corridor_obstacle")
            else "movement_unverified_replan"
        )
        result["sequence_after"] = post.sequence
        self._record_action(**result)
        return result

    def run_local_navigation_episode(
        self,
        target_position: dict[str, float] | None = None,
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
    ) -> dict[str, Any]:
        """Run several bounded coordinator cycles in one service call.

        The executor itself is MCP-independent.  This service method owns the
        action lock for the whole episode, while each reused coordinator step
        continues to perform its normal telemetry, foreground, session, and
        neutralisation checks.  No MCP operation is called from the loop.
        """
        limits = LocalNavigationLimits(
            maximum_seconds=maximum_seconds,
            maximum_distance_m=maximum_distance_m,
            arrival_radius_m=arrival_radius_m,
            maximum_cycles=maximum_cycles,
            maximum_segment_ms=maximum_segment_ms,
            maximum_segment_distance_m=maximum_segment_distance_m,
        ).validate()
        if route_bearing_degrees is not None:
            try:
                route_bearing_degrees = float(route_bearing_degrees)
            except (TypeError, ValueError) as exc:
                raise SafetyError("route_bearing_degrees must be numeric") from exc
            if not math.isfinite(route_bearing_degrees):
                raise SafetyError("route_bearing_degrees must be finite")

        initial = self.telemetry.observe()
        resolved_target = target_position
        selected = self._planning_target(initial, target_id) if target_id else None
        if resolved_target is None and selected is not None:
            position = selected.interaction_position or selected.world_position
            resolved_target = asdict(position)
        if resolved_target is not None:
            try:
                resolved_target = asdict(Vec3.from_dict(resolved_target))
            except (KeyError, TypeError, ValueError) as exc:
                raise SafetyError("target_position must contain numeric x, y and z") from exc

        if route_bearing_degrees is None and target_id is not None:
            inferred_route = self._route_leg_for_target(initial, target_id, None)
            inferred_bearing = inferred_route.get("bearing")
            if inferred_bearing is not None:
                route_bearing_degrees = inferred_bearing
                route_source = inferred_route.get("route_source") or "runtime_route"

        # A prior bounded action normally latches the one-shot arm.  An
        # active control session is the explicit authority to re-arm once for
        # this bounded episode; no controller is recreated or reconnected.
        if getattr(self.input, "is_real", False) and self._session_lease is not None:
            self._refresh_coordinator_capabilities()
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        self._local_episode_owner = get_ident()

        service = self

        class _ServiceEpisodeDriver:
            def observe(self) -> Any:
                return service.telemetry.observe()

            def safety_reason(self, state: Any, map_id: str | None) -> str | None:
                return service._local_navigation_safety_reason(state, map_id)

            def execute_segment(
                self,
                state: Any,
                cycle_index: int,
                segment_ms: int,
                segment_distance_m: float,
            ) -> dict[str, Any]:
                del cycle_index
                cycle_route_bearing = route_bearing_degrees
                cycle_route_source = route_source
                cycle_route_start = None
                cycle_route_end = None
                if target_id is not None:
                    resolved_route = service._route_leg_for_target(
                        state,
                        target_id,
                        route_bearing_degrees,
                    )
                    cycle_route_bearing = resolved_route.get("bearing")
                    cycle_route_source = resolved_route.get("route_source") or route_source
                    cycle_route_start = resolved_route.get("route_segment_start")
                    cycle_route_end = resolved_route.get("route_segment_end")
                    if cycle_route_bearing is None:
                        return {
                            "ok": False,
                            "replan_required": True,
                            "decision": "route_required",
                            "reason": "the current player position has no applicable walkable route leg",
                            "route_plan": resolved_route.get("route_plan"),
                        }
                decision = service.navigate_next_segment(
                    target_id=target_id,
                    target_position=resolved_target,
                    route_bearing_degrees=cycle_route_bearing,
                    route_source=cycle_route_source,
                    route_segment_start=cycle_route_start,
                    route_segment_end=cycle_route_end,
                    maximum_ms=segment_ms,
                    maximum_distance_m=segment_distance_m,
                    survey=False,
                    capture_screenshot=False,
                    sprint=False,
                )
                decision["route_bearing_degrees"] = cycle_route_bearing
                decision["route_source"] = cycle_route_source
                decision["route_segment_start"] = cycle_route_start
                decision["route_segment_end"] = cycle_route_end
                return decision

            def neutralise(self) -> None:
                service.input.release_all()
                if service._arm:
                    service._arm.latch_stop()
                if service._bench_arm:
                    service._bench_arm.latch_stop()
                if service._camera_arm:
                    service._camera_arm.latch_stop()
                with service._lock:
                    service._stopped = True
                service._last_action_finished = service._monotonic()

            def capture_terminal(self) -> dict[str, Any]:
                return service.capture_game_window()

        try:
            executor = LocalNavigationExecutor(
                _ServiceEpisodeDriver(),
                clock=self._monotonic,
                trace_sink=lambda record: self._record_action(**record),
            )
            return executor.run(
                target_position=resolved_target,
                target_id=target_id,
                route_bearing_degrees=route_bearing_degrees,
                limits=limits,
                capture_on_terminal_uncertainty=capture_on_terminal_uncertainty,
            )
        finally:
            self._local_episode_owner = None
            self._action_lock.release()

    def _local_navigation_safety_reason(
        self,
        state: Any,
        map_id: str | None,
    ) -> str | None:
        """Return a stable episode outcome before any segment is sent."""
        real = bool(getattr(self.input, "is_real", False))
        if real and self._session_lease is not None:
            status = self._session_lease.status()
            if not status or not status.get("active"):
                return "control_session_lost"
        if real:
            if self._arm is not None:
                _, _, stopped = self._arm.snapshot()
                if stopped:
                    return "emergency_stop"
            with self._lock:
                if self._stopped:
                    return "emergency_stop"
            if not self._environment.game_process_exists():
                return "game_process_lost"
            if not self._input_reachable():
                return "foreground_lost"
        if not getattr(state, "running", False):
            return "game_process_lost" if real else "gameplay_state_changed"
        if getattr(state, "source", "unknown") != "simulation":
            try:
                if state.age_seconds() > self.max_telemetry_age:
                    return "telemetry_stale"
            except (AttributeError, TypeError):
                return "telemetry_stale"
        if getattr(state, "in_menu", False) or getattr(state, "paused", False):
            return "menu_opened" if getattr(state, "in_menu", False) else "gameplay_state_changed"
        if getattr(state, "in_combat", False) or getattr(state, "target", None) is not None:
            return "combat_started"
        if map_id is not None and getattr(state, "map_id", None) != map_id:
            return "gameplay_state_changed"
        player = getattr(state, "player", None)
        if player is None or getattr(player, "position", None) is None:
            return "telemetry_stale"
        return None

    def diagnose_navigation_stall(
        self,
        intended_direction: str = "forward",
        displacement_m: float | None = None,
        vertical_change_m: float | None = None,
    ) -> dict[str, Any]:
        """Rank recovery inputs from the latest telemetry without acting."""
        from dataclasses import asdict

        state = self.telemetry.observe()
        self._remember_motion_state(state)
        diagnosis = diagnose_navigation_stall(
            state,
            intended_direction=intended_direction,
            displacement_m=displacement_m,
            vertical_change_m=vertical_change_m,
        )
        return {"ok": True, "diagnosis": asdict(diagnosis), "sequence": state.sequence}

    def _predictive_candidates(self, state: Any) -> list[HeadingCandidate]:
        payload = state.navigation if isinstance(state.navigation, dict) else None
        directions = payload.get("directions") if payload else None
        if not isinstance(directions, dict):
            return []
        camera_yaw = state.camera.rotation.yaw if state.camera is not None and state.camera.rotation is not None else state.player.rotation.yaw if state.player is not None and state.player.rotation is not None else 0.0
        default_bearings = {"forward": camera_yaw, "backward": camera_yaw + 180.0, "left": camera_yaw - 90.0, "right": camera_yaw + 90.0}
        candidates: list[HeadingCandidate] = []
        for direction in ("forward", "left", "right", "backward"):
            item = directions.get(direction)
            if not isinstance(item, dict):
                continue
            try:
                clear = item.get("clear_distance_m", item.get("distance_m"))
                clear_value = float(clear) if clear is not None else None
                bearing = float(item.get("bearing_degrees", default_bearings[direction]))
                progress = float(item.get("progress_m", max(0.0, clear_value or 0.0)))
            except (TypeError, ValueError):
                continue
            if clear_value is not None and not math.isfinite(clear_value) or not math.isfinite(bearing) or not math.isfinite(progress):
                continue
            candidates.append(HeadingCandidate(direction, bearing, clear_value, max(0.0, progress), str(item.get("confidence", payload.get("confidence", "unknown"))), {key: item[key] for key in ("hit", "probe_kind", "sample_sequence") if key in item}))
        return candidates

    def plan_predictive_movement(self, requested_distance_m: float = DEFAULT_PLANNED_DISTANCE_METRES, target_bearing_degrees: float | None = None, maximum_ms: int = ADAPTIVE_TRAVEL_MAXIMUM_MS, *, target_id: str | None = None) -> dict[str, Any]:
        """Plan one clearance-gated hold toward a verified route segment."""
        state = self.telemetry.observe()
        self._remember_motion_state(state)
        if state.player is None or state.player.position is None:
            raise SafetyError("predictive movement requires player spatial telemetry")
        selected = self._planning_target(state, target_id)
        resolved_route = route_source = None
        target_bearing = target_bearing_degrees
        # An explicit bearing is already a caller-supplied route segment. Do
        # not let an unrelated nearby interactable turn that verified segment
        # back into a final-target route requirement.
        if target_id is not None or target_bearing is None:
            inferred_target_id = target_id or (selected.target_id if selected is not None else None)
            target_bearing, resolved_route, route_source = self._route_segment_for_target(state, inferred_target_id, target_bearing)
        if target_bearing is None:
            return {"ok": False, "decision": "route_required", "reason": "target coordinates are not a traversable route; provide a navmesh, occupancy-grid, or recorded walkable path", "sequence": state.sequence, "target_id": target_id or (selected.target_id if selected is not None else None), "route_plan": resolved_route}
        velocity = estimate_velocity(self._motion_samples)
        speed = velocity.speed_mps if velocity.speed_mps > 0.0 else None
        candidates = self._predictive_candidates(state)
        if not candidates:
            return {"ok": False, "decision": "perception_required", "reason": "navigation telemetry has no candidate clearances", "sequence": state.sequence, "velocity": asdict(velocity)}
        choice = choose_heading(candidates, float(target_bearing), stopping_buffer_m=stopping_buffer_m(speed or 2.0), current_position=state.player.position, recent_blockers=self._map_heading_blockers(state.map_id))
        if choice.direction is None or choice.clear_distance_m is None:
            return {"ok": False, "decision": "blocked_or_uncertain", "sequence": state.sequence, "velocity": asdict(velocity), "heading_choice": asdict(choice)}
        plan = plan_motion_hold(requested_distance_m, choice.clear_distance_m, speed, maximum_ms=maximum_ms)
        return {"ok": plan.outcome == "planned", "sequence": state.sequence, "direction": choice.direction, "target_bearing_degrees": float(target_bearing), "target_id": target_id or (selected.target_id if selected is not None else None), "route_source": route_source, "route_plan": resolved_route, "velocity": asdict(velocity), "heading_choice": asdict(choice), "motion_plan": asdict(plan)}

    def travel_planned_chunk(self, requested_distance_m: float = DEFAULT_PLANNED_DISTANCE_METRES, target_bearing_degrees: float | None = None, maximum_ms: int = ADAPTIVE_TRAVEL_MAXIMUM_MS, *, target_id: str | None = None) -> dict[str, Any]:
        """Execute one predictive plan, refusing blind movement."""
        plan = self.plan_predictive_movement(requested_distance_m, target_bearing_degrees, maximum_ms, target_id=target_id)
        if not plan.get("ok"):
            return plan
        motion = plan["motion_plan"]
        return self.travel_direction_chunk(str(plan["direction"]), int(motion["hold_ms"]), float(motion["planned_distance_m"]), predictive_plan=plan)

    def navigation_metrics(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        if self._action_log is not None:
            try:
                with self._action_log.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            value = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(value, dict):
                            records.append(value)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise SafetyError(f"cannot read navigation action log: {exc}") from exc
        return {"ok": True, "source": str(self._action_log) if self._action_log is not None else None, "metrics": replay_navigation_metrics(records), "chadley_benchmark": self._chadley_benchmark.status()}

    def _resolve_recording_goal(self, route_id: str, goal_id: str | None = None) -> tuple[str | None, dict[str, float] | None]:
        requested = goal_id
        if requested is None and route_id == PHYSICAL_CHADLEY_ROUTE:
            requested = PHYSICAL_CHADLEY_LANDMARK_ID
        if requested and self._known_world_store is not None:
            try:
                for landmark in self._known_world_store.list_landmarks():
                    if landmark.landmark_id == requested:
                        position = landmark.interaction_position or landmark.position
                        return requested, {"x": position.x, "y": position.y, "z": position.z}
            except Exception:
                # The route still has a safe built-in fallback for the known
                # Chadley landmark. A malformed optional store must not make
                # benchmark bookkeeping impossible.
                pass
        if requested == PHYSICAL_CHADLEY_LANDMARK_ID:
            return requested, {"x": PHYSICAL_CHADLEY_POSITION.x, "y": PHYSICAL_CHADLEY_POSITION.y, "z": PHYSICAL_CHADLEY_POSITION.z}
        return requested, None

    def start_chadley_benchmark(self, route_id: str = PHYSICAL_CHADLEY_ROUTE, benchmark_id: str | None = None, goal_id: str | None = None) -> dict[str, Any]:
        try:
            resolved_goal_id, goal_position = self._resolve_recording_goal(route_id, goal_id)
            setup = {
                "input_backend": type(self.input).__name__,
                "telemetry_backend": type(self.telemetry).__name__,
                "recorded_routes_enabled": self._use_recorded_routes,
                "runtime_route_policy": "human_recordings_excluded",
            }
            return self._chadley_benchmark.start(route_id, self.telemetry.observe(), benchmark_id, resolved_goal_id, goal_position, setup)
        except BenchmarkError as exc:
            raise SafetyError(str(exc)) from exc

    def mark_chadley_benchmark_phase(self, phase: str, at: datetime | str | None = None) -> dict[str, Any]:
        """Persist one ordered benchmark phase boundary without sending input."""
        try:
            return self._chadley_benchmark.mark_phase(phase, at)
        except BenchmarkError as exc:
            raise SafetyError(str(exc)) from exc

    def start_human_traversal_recording(self, route_id: str = PHYSICAL_CHADLEY_ROUTE, recording_id: str | None = None, goal_id: str | None = None) -> dict[str, Any]:
        try:
            resolved_goal_id, goal_position = self._resolve_recording_goal(route_id, goal_id)
            return self._human_recording.start(route_id, self.telemetry.observe(), recording_id, resolved_goal_id, goal_position)
        except (RecordingError, TelemetryError) as exc:
            raise SafetyError(str(exc)) from exc

    def sample_human_traversal_recording(self, include_screenshot: bool = False, note: str | None = None) -> dict[str, Any]:
        try:
            screenshot = None
            if include_screenshot:
                try:
                    screenshot = self.capture_game_window()
                except Exception as exc:
                    screenshot = {"ok": False, "error": str(exc)}
            return self._human_recording.sample(self.telemetry.observe(), screenshot, note)
        except (RecordingError, TelemetryError) as exc:
            raise SafetyError(str(exc)) from exc

    def human_traversal_recording_status(self) -> dict[str, Any]:
        try:
            return self._human_recording.status()
        except RecordingError as exc:
            raise SafetyError(str(exc)) from exc

    def finish_human_traversal_recording(self, outcome: str = "completed", note: str | None = None) -> dict[str, Any]:
        try:
            return self._human_recording.finish(outcome, note)
        except RecordingError as exc:
            raise SafetyError(str(exc)) from exc

    def run_human_traversal_sampler(self, interval_seconds: float = 2.0) -> dict[str, Any]:
        """Sample an active human recording until it is finalized."""
        try:
            return HumanTraversalSampler(
                self._human_recording,
                self.telemetry.observe,
                interval_seconds,
            ).run()
        except RecordingError as exc:
            raise SafetyError(str(exc)) from exc

    def chadley_benchmark_status(self) -> dict[str, Any]:
        try:
            try:
                state = self.telemetry.observe()
            except TelemetryError as exc:
                result = self._chadley_benchmark.status()
                result["observation_error"] = str(exc)
                return result
            return self._chadley_benchmark.status(state)
        except BenchmarkError as exc:
            raise SafetyError(str(exc)) from exc

    def finish_chadley_benchmark(self, outcome: str = "completed", note: str | None = None) -> dict[str, Any]:
        try:
            try:
                state = self.telemetry.observe()
            except TelemetryError:
                state = None
            return self._chadley_benchmark.finish(outcome, state, note)
        except BenchmarkError as exc:
            raise SafetyError(str(exc)) from exc

    def discard_chadley_benchmark(self) -> dict[str, Any]:
        try:
            return self._chadley_benchmark.discard()
        except BenchmarkError as exc:
            raise SafetyError(str(exc)) from exc

    def remember_landmark(
        self,
        landmark_id: str,
        label: str,
        kind: str = "landmark",
        target_id: str | None = None,
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        """Persist the current player position or a telemetry target as a landmark."""
        from dataclasses import asdict

        if self._known_world_store is None:
            raise SafetyError("known-world persistence is not configured")
        if not landmark_id.strip() or not label.strip() or not kind.strip():
            raise SafetyError("landmark identity fields cannot be empty")
        state = self.telemetry.observe()
        if state.source != "simulation" and state.age_seconds() > self.max_telemetry_age:
            raise SafetyError("cannot record a landmark from stale telemetry")
        if state.map_id is None or state.player is None or state.player.position is None:
            raise SafetyError("landmark requires a map and player spatial telemetry")

        target = select_navigation_target(state, target_id=target_id) if target_id else None
        if target_id is not None and target is None:
            raise SafetyError("requested landmark target is not present in telemetry")
        position = (
            target.interaction_position or target.world_position
            if target is not None else state.player.position
        )
        interaction_position = target.interaction_position if target is not None else None
        metadata = {"anchor": "target" if target is not None else "player"}
        if target is not None and target.metadata:
            metadata["target_metadata"] = target.metadata
        if target is not None:
            metadata["target_id"] = target.target_id
        landmark = Landmark(
            landmark_id=landmark_id.strip(),
            label=label.strip(),
            kind=(target.kind if target is not None else kind.strip()),
            map_id=state.map_id,
            position=position,
            interaction_position=interaction_position,
            heading_yaw=state.player.rotation.yaw if state.player.rotation else None,
            source="telemetry",
            confidence="target_observed" if target is not None else "position_observed",
            evidence_refs=(evidence_ref,) if evidence_ref else (),
            metadata=metadata,
        )
        saved = self._known_world_store.upsert_landmark(landmark)
        return {"ok": True, "landmark": asdict(saved)}

    def _acquire_local_episode_phase(self) -> tuple[bool, bool]:
        """Acquire a nested phase unless the current episode owns the lock.

        The second flag records whether this call acquired the underlying
        lock and therefore must release it.  A normal caller that finds the
        lock busy is rejected; only the owning episode thread may reuse the
        existing public coordinator phase methods.
        """
        if self._local_episode_owner == get_ident():
            return True, False
        acquired = self._action_lock.acquire(blocking=False)
        return acquired, acquired

    def _release_local_episode_phase(self, acquired: bool) -> None:
        if acquired:
            self._action_lock.release()

    def _assert_action_allowed(
        self,
        *,
        allow_paused: bool = False,
        allow_queen_blood_dialogue_transition: bool = False,
        allow_queen_blood_mulligan: bool = False,
        allow_stale_queen_blood_transition: bool = False,
        allow_swimming: bool = False,
        observed_state: Any | None = None,
    ) -> Any:
        self._assert_one_off_visit_unchanged()
        # A live lease is the explicit continuation authority for this
        # session. Re-arm here so direct MCP actions have the same reusable
        # lifecycle as the multi-phase coordinator.
        if getattr(self.input, "is_real", False) and self._session_lease is not None:
            try:
                self._rearm_active_session()
            except Exception as exc:
                raise SafetyError(f"control-session rearm failed: {exc}") from exc
        if getattr(self.input, "is_real", False) and self._arm:
            _, _, stopped = self._arm.snapshot()
            with self._lock:
                self._stopped = stopped
        with self._lock:
            if self._stopped:
                raise SafetyError(self._latched_stop_message())
        state = observed_state if observed_state is not None else self.telemetry.observe()
        if not state.running:
            raise SafetyError("game is not running")
        freshness_limit = (
            self._queen_blood_freshness_limit()
            if allow_queen_blood_dialogue_transition or allow_queen_blood_mulligan
            else self.max_telemetry_age
        )
        if state.source != "simulation" and state.age_seconds() >= freshness_limit:
            # The QB dialogue/deck transition can briefly stop advancing the
            # generic state feed while the dedicated producer still exposes
            # the explicit card UI.  Permit only the bounded QB continuation
            # path to confirm that already-identified UI; all other input
            # remains fail-closed on stale telemetry.
            allow_stale_queen_blood_transition = (
                (
                    allow_queen_blood_dialogue_transition
                    and (
                        self._queen_blood_dialogue_transition_active(state)
                        or self._queen_blood_prematch_visible(state)
                    )
                )
                or (
                    allow_queen_blood_mulligan
                    and self._queen_blood_mulligan_ready(state)
                )
                or (
                    allow_stale_queen_blood_transition
                    and allow_queen_blood_dialogue_transition
                )
            )
            if not allow_stale_queen_blood_transition:
                raise SafetyError("telemetry is stale")
        allow_dialogue_transition = (
            allow_queen_blood_dialogue_transition
            and self._queen_blood_dialogue_transition_active(state)
        )
        # Once the dedicated producer has identified the Queen's Blood
        # pre-match screen, generic world/player spatial telemetry is expected
        # to disappear while the game owns the full-screen card UI.  Admit
        # this explicit pre-match signal through the normal gameplay-settling
        # gate so a retry can press F3 without requiring a stale world pose.
        allow_queen_blood_prematch = (
            allow_queen_blood_dialogue_transition
            and self._queen_blood_prematch_visible(state)
        )
        if (
            getattr(self.input, "is_real", False)
            and not allow_paused
            and not allow_dialogue_transition
            and not allow_queen_blood_prematch
            and self._settled_gameplay_samples > 1
            and not (allow_swimming and self._swimming(state))
        ):
            state = self._ensure_settled_gameplay(state)
        if state.paused and not allow_paused:
            raise SafetyError("game is paused")
        return state

    @staticmethod
    def _swimming(state: Any) -> bool:
        """Loaded, placed and in the field, but not grounded: Cloud swimming.

        Swimming never settles (live 2026-10-01, a Grasslands river), so the
        settling gate would refuse the very stick input that swims him out.
        """
        player = getattr(state, "player", None)
        locomotion = getattr(player, "locomotion", None)
        return (state.map_id is not None and player is not None and player.position is not None
                and locomotion is not None and locomotion.grounded is False
                and not state.in_menu and not state.paused)

    def _gameplay_identity(self, state: Any) -> tuple[Any, ...]:
        """Return the identity that must remain stable during post-load settling."""
        actor_id = (
            state.player.actor_id
            if state.player is not None else None
        )
        world_id = getattr(state, "world_id", None)
        session_id = getattr(state, "session_id", None)
        for attribute in ("navigation_diagnostics", "navigation", "map_state"):
            payload = getattr(state, attribute, None)
            if not isinstance(payload, dict):
                continue
            if world_id is None:
                world_id = payload.get("world_id", payload.get("world"))
            if session_id is None:
                session_id = payload.get("session_id", payload.get("session"))
        try:
            process_ids = tuple(sorted(
                (int(pid), str(name))
                for pid, name in self._environment.game_process_ids().items()
            ))
        except Exception:
            process_ids = (bool(self._environment.game_process_exists()),)
        return (state.map_id, world_id, session_id, actor_id, process_ids)

    def _ensure_settled_gameplay(
        self,
        initial_state: Any,
        timeout_seconds: float = GAMEPLAY_SETTLE_TIMEOUT_SECONDS,
        max_telemetry_age_seconds: float | None = None,
    ) -> Any:
        """Wait for consecutive advancing world samples before real input.

        UE4SS can publish a fresh player snapshot while the save is still
        transitioning into the playable world.  This gate is deliberately
        low-rate and consumes no input.  Once established, the proof is
        maintained by session-health observations and is reacquired only
        after an actual invalidation.
        """
        freshness_limit = self._settling_freshness_limit(max_telemetry_age_seconds)
        state = initial_state
        # A proof established under the generic service threshold cannot
        # satisfy a stricter movement-specific gate.  Force the normal
        # multi-sample sequence in that case; this does not alter the global
        # threshold used by unrelated startup/readiness callers.
        if (
            self._settled_gameplay_freshness_limit is not None
            and self._settled_gameplay_freshness_limit > freshness_limit
        ):
            self._invalidate_settled_gameplay("movement_freshness_reacquisition_required")
        elif (
            self._settled_gameplay_proof_present()
            and self._settled_gameplay_freshness_limit is not None
            and state.source != "simulation"
            and state.age_seconds() > self._settled_gameplay_freshness_limit
        ):
            self._invalidate_settled_gameplay("telemetry_stale")
        previous_sequence: int | None = None
        previous_identity: tuple[Any, ...] | None = None
        settled_samples = 0
        first_sample = True
        deadline = self._monotonic() + max(1.0, min(30.0, float(timeout_seconds)))
        latest_reason = "gameplay state is not settled"
        while self._monotonic() < deadline:
            if not first_sample:
                state = self.telemetry.observe()
            first_sample = False

            age = state.age_seconds()
            live = state.source == "simulation" or age <= freshness_limit
            player_spatial = bool(
                state.player is not None and state.player.position is not None
            )
            process_exists = self._environment.game_process_exists()
            foreground = self._input_reachable()
            if not state.running:
                latest_reason = "game is not running"
                self._invalidate_settled_gameplay("game_not_running")
                previous_sequence = None
                previous_identity = None
                settled_samples = 0
            elif not live:
                latest_reason = "telemetry is stale during gameplay transition"
                self._invalidate_settled_gameplay("telemetry_stale")
                previous_sequence = None
                previous_identity = None
                settled_samples = 0
            elif process_exists is False:
                latest_reason = "game process disappeared during gameplay transition"
                self._invalidate_settled_gameplay("process_missing")
                previous_sequence = None
                previous_identity = None
                settled_samples = 0
            elif foreground is False:
                latest_reason = "game is not foreground during gameplay transition"
                self._invalidate_settled_gameplay("foreground_missing")
                previous_sequence = None
                previous_identity = None
                settled_samples = 0
            elif state.paused:
                latest_reason = "gameplay is paused"
                self._invalidate_settled_gameplay("paused")
                previous_sequence = None
                previous_identity = None
                settled_samples = 0
            elif state.in_menu:
                latest_reason = "a game menu is still open"
                self._invalidate_settled_gameplay("menu")
                previous_sequence = None
                previous_identity = None
                settled_samples = 0
            elif state.map_id is None or not player_spatial:
                latest_reason = "player spatial telemetry is not loaded"
                self._invalidate_settled_gameplay("player_not_loaded")
                previous_sequence = None
                previous_identity = None
                settled_samples = 0
            elif (getattr(state.player, "locomotion", None) is not None
                  and state.player.locomotion.grounded is False):
                latest_reason = "measured character is not grounded"
                self._invalidate_settled_gameplay("physical_ground_not_ready")
                previous_sequence = None
                previous_identity = None
                settled_samples = 0
            else:
                current_identity = self._gameplay_identity(state)
                if (
                    self._settled_gameplay_identity == current_identity
                    and self._settled_gameplay_sequence is not None
                    and state.sequence >= self._settled_gameplay_sequence
                    and self._settled_gameplay_freshness_limit is not None
                    and self._settled_gameplay_freshness_limit <= freshness_limit
                ):
                    if state.sequence > self._settled_gameplay_sequence:
                        self._record_settled_gameplay_proof(
                            state,
                            "continuity_refreshed",
                            freshness_max_age_seconds=freshness_limit,
                        )
                    return state
                advancing = (
                    previous_sequence is not None
                    and state.sequence > previous_sequence
                )
                matching_identity = (
                    previous_identity is not None
                    and current_identity == previous_identity
                )
                if previous_sequence is None or not matching_identity:
                    settled_samples = 1
                elif advancing:
                    settled_samples += 1
                # A repeated sequence is not a new settling sample.  Retain
                # the candidate count, but keep waiting for a real producer
                # advancement before authorizing input.
                if settled_samples >= self._settled_gameplay_samples:
                    self._record_settled_gameplay_proof(
                        state,
                        "multi_sample_established",
                        freshness_max_age_seconds=freshness_limit,
                    )
                    return state
                previous_identity = current_identity
                latest_reason = (
                    f"waiting for advancing settled samples "
                    f"({settled_samples}/{self._settled_gameplay_samples})"
                )
                previous_sequence = state.sequence
            self._sleeper(GAMEPLAY_SETTLE_POLL_SECONDS)
        self._invalidate_settled_gameplay("settling_timeout")
        raise SafetyError(
            f"gameplay_not_settled: {latest_reason}; no input was sent"
        )

    def _assert_real_movement_allowed(self, require_armed: bool = True, allow_swimming: bool = False) -> Any:
        state = self._assert_action_allowed(allow_swimming=allow_swimming)
        if state.source == "simulation":
            raise SafetyError("real movement requires live game telemetry")
        if state.in_menu:
            raise SafetyError("game Main Menu is open")
        if state.target is not None:
            raise SafetyError("movement blocked because a target is present")
        if not self._environment.game_process_exists():
            raise SafetyError("FFVII Rebirth process was not found")
        if not self._input_reachable():
            raise SafetyError("FFVII Rebirth is not the foreground window")
        if require_armed:
            armed, _ = self._arm.status() if self._arm else (False, 0.0)
            if not armed:
                raise SafetyError("real input is not manually armed")
        if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
            raise SafetyError("minimum delay between movement calls has not elapsed")
        return state

    def say_on_stream(self, text: str, tone: str | None = None) -> dict[str, Any]:
        """Ask the live streamer to say one short line aloud (voice only, never chat).

        ``tone`` sets how it is said: neutral (default), calm, confident,
        excited, tense, concerned, relieved, surprised, frustrated or amused.

        Rules: 4-320 characters on one line; letters, digits, spaces and
        . , ! ? ' : ; ( ) - – — only; no web addresses, code-like words, backend
        details, support or donation wording, or profanity. At most one line per 8 s, 60 per
        10 minutes, no repeat or near-repeat within 5 minutes (``repetitive``:
        rephrase), while OBS is streaming or a rehearsal is recording.
        Use this throughout gameplay: travel, navigation, conversations, menu
        choices, shopping, rest, combat and Queen's Blood. Summarise the next
        decision and react to observed outcomes. One explanation may cover a
        bounded action's repeated inputs. ``rate_limited`` includes
        ``retry_after_seconds``; ``ok`` means queued, not heard.
        Describe what is on screen and why you are choosing an approach; never
        read out internal state, local files, logs or story beyond what viewers
        can already see.
        """
        return self._agent_speech.say(text, tone)

    def start_queens_blood_recording(self, run_id: str, duration_seconds: float = QB_RECORDING_MAX_SECONDS,
                                    max_bytes: int = 67108864, sample_hz: float = 10) -> dict[str, Any]:
        state_dir = self._public_stream_event_dir.parent if self._public_stream_event_dir else None
        driver = match_record_target(state_dir)[0] if state_dir else None
        assignment = self._queen_blood_runs.assignment(driver) if self._queen_blood_runs and driver else None
        mode = "rehearsal" if state_dir and match_record_target(state_dir)[2] == "qb-match-history-rehearsal" else "live"
        status = None
        if getattr(self.input, "is_real", False):
            status = self.get_queens_blood_run_status()
            if not status.get("run_loaded") or not isinstance(assignment, dict) or status.get("profile_id") != assignment["profile_id"]:
                raise SafetyError("live recording requires the model's verified loaded working save")
        return self._qb_recorder.start(run_id, duration_seconds=duration_seconds,
                                       max_bytes=max_bytes, sample_hz=sample_hz,
                                       metadata={"driver": driver, "assignment": assignment, "mode": mode,
                                                 "input_backend": type(self.input).__name__,
                                                 "loaded_save": status.get("loaded_save") if status else None,
                                                 "loaded_save_verified": bool(status and status.get("loaded_save_verified")),
                                                 "run_loaded": bool(status and status.get("run_loaded")),
                                                 "profile_id": status.get("profile_id") if status else None})

    def get_queens_blood_recording_status(self) -> dict[str, Any]:
        return self._qb_recorder.status()

    def get_production_event_status(self) -> dict[str, Any]:
        """Read the bounded best-effort event writer counters."""
        if self._production_event_sink is None:
            status = {"enabled": False, "reason": "production_state_directory_unavailable"}
        else:
            status = {"enabled": True, **self._production_event_sink.status()}
        narration_sink = getattr(self, "_live_narration_event_sink", None)
        status["live_narration"] = narration_sink.status() if narration_sink is not None else {"enabled": False}
        return status

    def _production_state_dir(self) -> Path | None:
        event_dir = getattr(self, "_public_stream_event_dir", None)
        return Path(event_dir).parent if event_dir is not None else state_dir_from_env()

    @staticmethod
    def _require_feature_validation() -> None:
        if not feature_validation_runtime():
            raise SafetyError("production development events require FeatureValidation runtime")

    def register_production_test_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """Persist explicit feature metadata for a later verified MCP test run."""
        self._require_feature_validation()
        state_dir = self._production_state_dir()
        normalized = feature_test_context(context)
        if state_dir is None or normalized is None:
            raise SafetyError("a valid feature test context and production state directory are required")
        from .public_stream_events import match_record_target
        driver = match_record_target(state_dir)[0]
        if not isinstance(driver, dict) or not driver.get("driver") or not driver.get("model_id") or not driver.get("effort"):
            raise SafetyError("production test context requires a declared driver, model, and effort")
        write_json_atomic(state_dir / "production" / "feature-test-context.json", normalized)
        return {"ok": True, "registered": True, "feature": normalized["feature"],
                "featureVersion": normalized["featureVersion"]}

    def begin_production_test(self) -> dict[str, Any]:
        """Capture the pre-action monitor boundary for a declared test context."""
        self._require_feature_validation()
        state_dir = self._production_state_dir()
        if state_dir is None:
            raise SafetyError("production state directory is unavailable")
        from .production_events import _read_json
        context_path = state_dir / "production" / "feature-test-context.json"
        context = feature_test_context(_read_json(context_path))
        if context is None:
            raise SafetyError("register_production_test_context must be called first")
        active_path = state_dir / "production" / "feature-test-active.json"
        prior = _read_json(active_path)
        if prior and prior.get("state") == "running":
            raise SafetyError("a production feature test is already running")
        started = self._wall_clock().isoformat().replace("+00:00", "Z")
        active = {"state": "running", "context": context, "startedAt": started,
                  "startSnapshot": read_recording_snapshot(state_dir), "routeResult": None}
        write_json_atomic(active_path, active)
        return {"ok": True, "started": True, "feature": context["feature"], "startedAt": started}

    def production_test_route_started(self) -> None:
        """Refresh the start boundary immediately before an MCP route action."""
        state_dir = self._production_state_dir()
        if state_dir is None:
            return
        from .production_events import _read_json
        path = state_dir / "production" / "feature-test-active.json"
        active = _read_json(path)
        if not active or active.get("state") != "running":
            return
        active["startedAt"] = self._wall_clock().isoformat().replace("+00:00", "Z")
        active["startSnapshot"] = read_recording_snapshot(state_dir)
        active["routeStarted"] = True
        write_json_atomic(path, active)

    def production_test_route_completed(self, result: dict[str, Any]) -> None:
        """Persist only a compact nested adapter outcome for end-test verification."""
        state_dir = self._production_state_dir()
        if state_dir is None:
            return
        from .production_events import _read_json
        path = state_dir / "production" / "feature-test-active.json"
        active = _read_json(path)
        if not active or active.get("state") != "running":
            return
        run = result.get("run") if isinstance(result.get("run"), dict) else {}
        timeline = run.get("timeline") if isinstance(run.get("timeline"), list) else []
        movement_observed = any(isinstance(item, dict) and (
            item.get("actual_command_sent") is True or item.get("movement_command_sent") is True
        ) for item in timeline)
        route_result = {"operation": "run_live_route_adapter", "runState": run.get("state"),
                        "reason": run.get("reason"), "movementObserved": movement_observed,
                        "routeTarget": result.get("route_target_position"),
                        "completedAt": self._wall_clock().isoformat().replace("+00:00", "Z")}
        evidence_path = state_dir / "production" / "evidence" / ("feature-test-" + uuid.uuid4().hex + ".json")
        write_json_atomic(evidence_path, route_result)
        active.update(routeResult=route_result, routeEvidenceRef=str(evidence_path),
                      endSnapshot=read_recording_snapshot(state_dir))
        write_json_atomic(path, active)

    def end_production_test(self) -> dict[str, Any]:
        """Emit from the retained nested route result, never caller-claimed success."""
        self._require_feature_validation()
        state_dir = self._production_state_dir()
        if state_dir is None:
            raise SafetyError("production state directory is unavailable")
        from .production_events import _read_json
        active_path = state_dir / "production" / "feature-test-active.json"
        active = _read_json(active_path)
        if not active or active.get("state") != "running":
            raise SafetyError("no production feature test is running")
        context = feature_test_context(active.get("context"))
        route = active.get("routeResult") if isinstance(active.get("routeResult"), dict) else None
        if context is None or route is None:
            raise SafetyError("the active test has no completed harness route result")
        result = "unknown"
        observed = str(route.get("runState") or "route outcome unavailable")
        ended_at = route.get("completedAt") or self._wall_clock().isoformat().replace("+00:00", "Z")
        if route.get("runState") == "ARRIVED" and route.get("movementObserved") is True:
            if context.get("expectTalkPrompt") is True:
                try:
                    from .field_prompt import read_field_prompt
                    prompt = read_field_prompt(self)
                    if prompt.get("talk") is True:
                        result = "verified"
                        observed = "ARRIVED after movement; fresh Talk prompt observed"
                    else:
                        result = "unknown"
                        observed = "ARRIVED after movement; Talk prompt not observed"
                except Exception:
                    observed = "ARRIVED after movement; Talk prompt observation unavailable"
                ended_at = self._wall_clock().isoformat().replace("+00:00", "Z")
                active["endSnapshot"] = read_recording_snapshot(state_dir)
            else:
                result = "verified"
                observed = "ARRIVED after observed movement and adapter final verification"
        elif route.get("runState") in {"FAILED", "STOPPED", "DISARMED", "REPLAN_REQUIRED"} and route.get("reason"):
            result = "failed"
            observed = str(route["reason"])
        runtime_identity: dict[str, Any] = {}
        try:
            from .runtime_identity import collect_identity
            runtime_identity = collect_identity(process_role="mcp-worker", module_path=__file__)
        except Exception:
            # Missing source identity lowers metadata completeness; it never
            # changes the nested route result classification.
            runtime_identity = {}
        report = {"ok": result == "verified", "state": "ARRIVED" if result == "verified" else route.get("runState"),
                  "dry_run": False, "execute": True, "route_state": "ARRIVED" if result == "verified" else None,
                  "movement_attempted": route.get("movementObserved") is True,
                  "reason": route.get("reason"), "observed_behavior": observed,
                  "started_at_utc": active.get("startedAt"), "ended_at_utc": ended_at,
                  "evidence_refs": [active["routeEvidenceRef"]] if active.get("routeEvidenceRef") else [],
                  "runtime_identity": runtime_identity}
        # The only override is generated here from nested adapter evidence.
        event = emit_development_outcome(state_dir=state_dir, context=context, report=report,
                                         event_type="npc_approach_outcome" if context.get("expectTalkPrompt")
                                         else "navigation_mission_outcome",
                                         start_snapshot=active.get("startSnapshot"),
                                         end_snapshot=active.get("endSnapshot"),
                                         verified_result_override=result)
        sink = getattr(self, "_production_event_sink", None)
        if event is None or sink is None:
            accepted = False
            exclusion_recorded = False
            reason = "event_sink_unavailable"
        elif known_live_stream(event):
            accepted = False
            exclusion_recorded = sink.emit_excluded(event, "fresh_obs_stream_active")
            reason = "active_stream_excluded"
        else:
            accepted = sink.emit(event)
            exclusion_recorded = False
            reason = None if accepted else "event_queue_rejected"
        active.update(state="ended", endedAt=report["ended_at_utc"], eventId=event.get("eventId") if event else None,
                      result=result, accepted=accepted, exclusionRecorded=exclusion_recorded)
        write_json_atomic(active_path, active)
        return {"ok": True, "accepted": accepted, "exclusionRecorded": exclusion_recorded, "reason": reason,
                "eventId": event.get("eventId") if event else None, "result": result,
                "intervalUncertain": event.get("interval", {}).get("uncertain", True) if event else True,
                "status": sink.status() if sink is not None else None}

    def _emit_production_qb_event(self, event_type: str, state: Any, *, cues: list[dict[str, Any]] | None = None,
                                  finish: dict[str, Any] | None = None,
                                  projection: dict[str, Any] | None = None,
                                  projection_change: int | float | None = None) -> None:
        """Emit only notable commentary deltas and the rules-verified finish record."""
        sink = self._production_event_sink
        if not getattr(self.input, "is_real", False):
            return
        match = self._queen_blood_match or {}
        story_id = match.get("story_id")
        if not story_id:
            return
        if event_type == "queens_blood_notable_exchange":
            self._emit_live_narration_qb_exchange(str(story_id), state, cues or [], projection, projection_change)
        if sink is None:
            return
        state_dir = self._public_stream_event_dir.parent
        try:
            driver = match_record_target(state_dir)[0]
            model = driver.get("model_id") if isinstance(driver, dict) else None
            effort = driver.get("effort") if isinstance(driver, dict) else None
            opponent = (finish or {}).get("opponent") or match.get("opponent")
            verified_exchange = event_type == "queens_blood_notable_exchange" and state is not None and bool(cues)
            gameplay: dict[str, Any] = {
                "mission": "queens_blood", "opponent": str(opponent) if opponent else None,
                "result": "verified" if (finish and isinstance(finish.get("score"), dict)) or verified_exchange else "unknown",
            }
            if model:
                gameplay["model"] = model
            if effort:
                gameplay["effort"] = effort
            if finish and isinstance(finish.get("score"), dict):
                gameplay.update(score=finish["score"], won=finish.get("won"))
            if verified_exchange and isinstance(projection, dict):
                player_score, opponent_score = projection.get("player_score"), projection.get("opponent_score")
                if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                       for value in (player_score, opponent_score)):
                    gameplay["score"] = {"player": player_score, "opponent": opponent_score,
                                         "source": "verified_board_projection"}
            if verified_exchange and isinstance(projection_change, (int, float)) and not isinstance(projection_change, bool):
                gameplay["projectionChange"] = projection_change
            cue_items: list[dict[str, Any]] = []
            for cue_index, cue in enumerate(cues or []):
                if not isinstance(cue, dict):
                    continue
                kind = cue.get("kind")
                lane = cue.get("lane")
                label = str(kind or "notable_exchange")
                lane_number = {"top": 0, "middle": 1, "bottom": 2}.get(lane, lane)
                cue_id = hashlib.sha256(f"{story_id}:{label}:{cue_index}:{self._production_qb_event_counter}".encode("utf-8")).hexdigest()
                item: dict[str, Any] = {"cueId": cue_id, "label": label[:120]}
                for source, target in (("kind", "kind"), ("from", "from"), ("to", "to"),
                                       ("swing", "swing"), ("card", "card"), ("card_id", "cardId"),
                                       ("column", "column"), ("power", "power"),
                                       ("player_power", "playerPower"), ("opponent_power", "opponentPower"),
                                       ("count", "count")):
                    value = cue.get(source)
                    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                        item[target] = value
                if isinstance(lane_number, int) and not isinstance(lane_number, bool) and 0 <= lane_number <= 2:
                    item["lane"] = lane_number
                if isinstance(cue.get("player_score"), (int, float)) and isinstance(cue.get("opponent_score"), (int, float)):
                    item["score"] = {"player": cue["player_score"], "opponent": cue["opponent_score"]}
                elif isinstance(projection, dict) and all(isinstance(projection.get(k), (int, float))
                                                          for k in ("player_score", "opponent_score")):
                    item["score"] = {"player": projection["player_score"], "opponent": projection["opponent_score"]}
                cue_items.append(item)
            if cue_items:
                gameplay["notableCues"] = cue_items[:12]
            recording = self._qb_recorder.status()
            if isinstance(recording.get("path"), str):
                gameplay["evidenceRefs"] = [recording["path"]]
            detail = {"gameplay": gameplay}
            source = event_type + ":" + (
                json.dumps(cue_items, sort_keys=True, separators=(",", ":"))
                if cue_items else "finish"
            ) + ":" + str(self._production_qb_event_counter)
            event = build_event(
                state_dir=state_dir, mode="gameplay", event_type=event_type,
                source_key=str(story_id) + ":" + source, story_id=str(story_id),
                related_event_ids=self._production_qb_event_ids if finish else None,
                details=detail,
            )
            if known_live_stream(event):
                sink.emit_excluded(event, "fresh_obs_stream_active")
                return
            if sink.emit(event):
                self._production_qb_event_counter += 1
                if not finish:
                    self._production_qb_event_ids.append(event["eventId"])
                else:
                    self._production_qb_event_ids = []
        except Exception:
            # Event capture is advisory and cannot alter game input/results.
            return

    def stop_queens_blood_recording(self) -> dict[str, Any]:
        return self._qb_recorder.stop()

    def _record_action(self, **values: Any) -> None:
        visit = getattr(self, "_one_off_visit_binding", None)
        if visit is not None:
            values = {**values, "one_off_visit": True, "visited_save": visit["loaded_save"]}
        record = {
            "timestamp_utc": self._wall_clock().isoformat().replace("+00:00", "Z"),
            **values,
        }
        self._last_action = record
        if visit is None and values.get("action") in {"initiate_queens_blood_match", "finish_queens_blood_match",
                                     "place_queens_blood_card", "pass_queens_blood_turn"}:
            self._record_campaign_action(record)
        self._qb_recorder.capture("action", record)
        if visit is not None:
            return  # One-off inspection cannot write campaign or public match facts.
        if values.get("action") == "finish_queens_blood_match" and values.get("ok") is True:
            if isinstance(values.get("score"), dict) and values.get("outcome") == "match_finished":
                self._emit_production_qb_event("match.finish", None, finish=values)
        elif values.get("action") in {"place_queens_blood_card", "pass_queens_blood_turn"}:
            if values.get("ok") is False or values.get("outcome") == "placement_unverified":
                self._emit_production_qb_event("input.qb.placement-uncertain", None,
                                               cues=[{"kind": str(values.get("reason") or values.get("outcome") or "placement_error")}])
        payload = values.get("queen_blood")
        if self._queen_blood_match and values.get("action") == "place_queens_blood_card":
            self._queen_blood_match.setdefault("deck_evidence", MatchDeck()).placement(values)
        self._observe_match_deck(payload)
        if (values.get("action") in {"place_queens_blood_card", "pass_queens_blood_turn", "observe_queens_blood"}
                and isinstance(payload, dict) and payload.get("phase") in {"player_turn", "opponent_turn"}):
            self._queen_blood_last_turn_payload = payload
        if values.get("action") in {"initiate_queens_blood_match", "play_queens_blood_match",
                                    "finish_queens_blood_match"}:
            # A new or finished match has no exchange to react to: the last
            # board belongs to the previous match (#326).
            self._queen_blood_commentary_baseline = None
        if values.get("action") in {"place_queens_blood_card", "pass_queens_blood_turn"}:
            # A failed input did not start an opponent exchange. Drop any old
            # baseline too, so the retry cannot repeat a previous pass cue.
            # A slow read can already hold the opponent's reply (#357).
            self._queen_blood_commentary_baseline = (
                exchange_baseline(self._queen_blood_pre_move_payload, payload)
                if isinstance(payload, dict) and values.get("ok") is True
                and values.get("verified") is True else None
            )
            self._queen_blood_pre_move_payload = None
            self._queen_blood_commentary_after_pass = values.get("action") == "pass_queens_blood_turn"
        if self._public_stream_event_dir is not None:
            public_kind = public_event_for_action(values)
            if public_kind == "queens_blood_started" or values.get("action") in {
                    "play_queens_blood_match", "finish_queens_blood_match"}:
                try:
                    write_public_match_state(
                        self._public_stream_event_dir.parent / "public-qb-current.json", values,
                    )
                except OSError:
                    pass  # Public stats must never block gameplay.
            if public_kind is not None:
                if public_kind in {"queens_blood_won", "queens_blood_lost", "queens_blood_draw"}:
                    try:
                        state_dir = self._public_stream_event_dir.parent
                        driver, results_dir, _history_dir = match_record_target(state_dir)
                        write_public_match_result(state_dir / results_dir, values, driver=driver)
                    except OSError:
                        pass  # Stats are advisory; the action result remains authoritative.
                try:
                    now = time.monotonic()
                    cooldown = 45.0 if public_kind in {"exploring", "navigating", "combat_action", "queens_blood_turn"} else 0.0
                    if now - self._public_stream_last_emitted.get(public_kind, float("-inf")) >= cooldown:
                        try:
                            write_public_event(self._public_stream_event_dir, public_kind)
                        except OSError:
                            pass  # The asynchronous narrator intake is independent.
                        self._emit_live_narration_action_event(values, public_kind, cooldown)
                        self._public_stream_last_emitted[public_kind] = now
                except OSError:
                    pass  # Public narration must never block gameplay.
        if self._action_log is not None:
            try:
                self._action_log.parent.mkdir(parents=True, exist_ok=True)
                with self._action_log.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, sort_keys=True) + "\n")
            except OSError:
                pass

    def _tap_battle_tip_button(self, button: str, milliseconds: int) -> dict[str, Any]:
        """Cancel on a battle tutorial the tips closer has just read, or Confirm
        on the battle pause its Escape opened. The only menu input allowed in
        combat: battle tips hold the fight until closed (live 2026-10-20)."""
        if button not in {"cancel", "confirm"}:
            raise SafetyError("only Cancel and Confirm close a battle tip")
        if not getattr(self.input, "is_real", False):
            return self.tap(button, milliseconds)
        state = self._assert_real_menu_input_allowed(button, milliseconds, allow_combat_tip=True)
        self.input.tap(button, milliseconds)
        result = {"ok": True, "sequence": state.sequence, "action": "tap", "button": button,
                  "mode": "battle_tip"}
        self._record_action(**result)
        return result

    def grab_move(self, x: float | None = None, y: float | None = None, milliseconds: int = 1500,
                  segments: list[Any] | None = None, stick: bool = False) -> dict[str, Any]:
        """Drag the field object the prompt offers to Grab (<button=catch>,
        Ctrl): holds Grab for the whole pull and moves Cloud with it, either
        (x, y, milliseconds) or ``segments`` [[x, y, ms], ...] (camera-relative,
        at most GRAB_MAX_SEGMENTS, each up to 1.5 s), then lets go. Refused
        unless a grab prompt (a ``*_catch`` command) is shown."""
        from .field_prompt import read_field_prompt
        if segments is None:
            if x is None or y is None:
                raise SafetyError("give x and y, or segments")
            segments = [[x, y, milliseconds]]
        if not isinstance(segments, list) or not 1 <= len(segments) <= GRAB_MAX_SEGMENTS:
            raise SafetyError(f"segments must be 1-{GRAB_MAX_SEGMENTS} [x, y, milliseconds] items")
        checked = []
        for segment in segments:
            if not isinstance(segment, (list, tuple)) or len(segment) != 3:
                raise SafetyError("each segment is [x, y, milliseconds]")
            sx, sy = normalise_axes(segment[0], segment[1])
            if sx == 0.0 and sy == 0.0:
                raise SafetyError("each segment needs a direction")
            checked.append((sx, sy, validate_duration(segment[2])))
        if not getattr(self.input, "is_real", False):
            self.input.grab_move(checked, stick=stick)
            return {"ok": True, "simulated": True, "segments": len(checked)}
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            self._assert_real_movement_allowed()
            prompt = read_field_prompt(self)
            commands = [str(command) for command in prompt.get("commands") or []]
            catch = next((command for command in commands if command.endswith("_catch")), None)
            if catch is None:
                raise SafetyError(f"no grab prompt is shown: {commands}")
            before = prompt.get("player_position")
            self._ensure_game_foreground()  # the Grab key is keyboard-only
            self.input.grab_move(checked, stick=stick is True)
            after = read_field_prompt(self)
            result = {"ok": True, "action": "grab_move", "command": catch, "segments": len(checked),
                      "milliseconds": sum(segment[2] for segment in checked), "from": before,
                      "to": after.get("player_position"), "commands_after": after.get("commands")}
            if isinstance(before, dict) and isinstance(result["to"], dict):
                result["moved_m"] = round(math.hypot(result["to"]["x"] - before["x"],
                                                     result["to"]["y"] - before["y"]) / 100.0, 2)
            self._record_action(**{k: v for k, v in result.items() if k not in ("from", "to")})
            return result
        finally:
            try:
                self.input.release_all()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def climb_move(self, x: float, y: float, milliseconds: int = 800) -> dict[str, Any]:
        """Move along a climbing wall: one camera-relative stick push while
        Cloud hangs on it (live 2026-10-20, the Mt. Nibel cave wall: walking
        into its yellow holds starts the climb, and every move was then
        refused as "not grounded"). Refused unless Cloud is off the ground
        and still across two fresh samples, so it never steers a jump or a
        fall. Reports where he went and whether he is back on the ground."""
        x, y = normalise_axes(x, y)
        milliseconds = validate_duration(milliseconds)
        if x == 0.0 and y == 0.0:
            raise SafetyError("climb_move needs a direction")
        if not getattr(self.input, "is_real", False):
            self.input.move(x, y, milliseconds)
            return {"ok": True, "simulated": True}
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            # The settling gate refuses anyone off the ground; the swimming
            # exception admits it, and the stillness check below narrows it.
            first = self._assert_real_movement_allowed(allow_swimming=True)
            if first.in_combat:
                raise SafetyError("climb_move is refused in a fight")
            before = self._climbing_position(first)
            time.sleep(CLIMB_STILL_INTERVAL_SECONDS)
            second = self.telemetry.observe()
            held = self._climbing_position(second)
            if second.sequence <= first.sequence:
                raise SafetyError("climb_move needs two fresh samples")
            if math.dist(before, held) > CLIMB_STILL_MAX_CM:
                raise SafetyError("Cloud is moving through the air, not hanging on a wall")
            self.input.move(x, y, milliseconds)
            time.sleep(CLIMB_SETTLE_SECONDS)
            after = self.telemetry.observe()
            player = after.player
            position = player.position if player is not None else None
            locomotion = getattr(player, "locomotion", None)
            result: dict[str, Any] = {
                "ok": True, "action": "climb_move", "x": x, "y": y, "milliseconds": milliseconds,
                "from": {"x": held[0], "y": held[1], "z": held[2]},
                "to": None if position is None else {"x": position.x, "y": position.y, "z": position.z},
                "grounded_after": None if locomotion is None else locomotion.grounded,
                "controller_state": getattr(player, "controller_state", None),
            }
            if position is not None:
                result["moved_m"] = round(math.dist(held, (position.x, position.y, position.z)) / 100.0, 2)
                result["climbed_m"] = round((position.z - held[2]) / 100.0, 2)
            self._record_action(**{k: v for k, v in result.items() if k not in ("from", "to")})
            return result
        finally:
            try:
                self.input.release_all()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    @staticmethod
    def _climbing_position(state: Any) -> tuple[float, float, float]:
        player = state.player
        position = player.position if player is not None else None
        locomotion = getattr(player, "locomotion", None)
        if position is None:
            raise SafetyError("Cloud's position is not reported")
        grounded = getattr(locomotion, "grounded", None)
        if grounded is True:
            raise SafetyError("Cloud is on the ground: walk with move_stick or explore_goto")
        # Halfway up the wall ground contact went unreported; the character
        # state still read the climb's.
        if grounded is None and getattr(player, "controller_state", None) != CLIMB_CONTROLLER_STATE:
            raise SafetyError("Cloud's ground contact is not reported and he is not climbing")
        return (position.x, position.y, position.z)

    def battle_menu_tap(self, button: str, milliseconds: int = 120) -> dict[str, Any]:
        """One bounded press of the battle command menu: open it
        (``battle_command_menu``), move (up/down), choose (confirm) or back
        out (cancel). Only in a fight; field menus keep their own gate."""
        if button not in BATTLE_MENU_BUTTONS:
            raise SafetyError("only the battle command menu buttons are enabled")
        if not getattr(self.input, "is_real", False):
            return self.tap(button, milliseconds)
        state = self._assert_real_menu_input_allowed(button, milliseconds, allow_combat_tip=True)
        if not state.in_combat:
            raise SafetyError("battle menu input needs a fight in progress")
        self.input.tap(button, milliseconds)
        result = {"ok": True, "sequence": state.sequence, "action": "tap", "button": button,
                  "mode": "battle_menu"}
        self._record_action(**result)
        return result

    def read_field_menu(self, raw: bool = False) -> dict[str, Any]:
        """The field (party) menu now, no input: which screen, the highlighted
        main-menu entry, the Spells and Inventory lists and their highlights,
        and the party's HP and MP. ``raw`` adds every menu text and list."""
        from .field_menu import read_field_menu
        if type(raw) is not bool:
            raise SafetyError("raw must be a boolean")
        return read_field_menu(self, raw=raw)

    def cast_field_spell(self, spell: str, caster: str = "cloud", target: str = "cloud",
                         until_hp: int | None = None, max_casts: int = 10) -> dict[str, Any]:
        """Cast a spell outside a fight through Main Menu > Spells: caster,
        spell and target are each checked on screen before Confirm. The
        first cast measures the heal; the casts needed to reach ``until_hp``
        (default: full) follow back to back, within the caster's MP, then the
        menu is closed. Names: cloud, sephiroth; cure (or a "$..." key)."""
        from .field_menu import cast_field_spell
        self._assert_field_menu_allowed()
        if until_hp is not None and (type(until_hp) is not int or until_hp < 1):
            raise SafetyError("until_hp must be a positive whole number")
        if type(max_casts) is not int or not 1 <= max_casts <= 10:
            raise SafetyError("max_casts must be 1-10")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            result = cast_field_spell(self, spell, caster, target, until_hp, max_casts)
        finally:
            self._action_lock.release()
        self._record_action(**{k: v for k, v in result.items() if k != "presses"})
        return result

    def use_field_item(self, item: str, target: str = "cloud", until_hp: int | None = None,
                       until_mp: int | None = None, count: int | None = None) -> dict[str, Any]:
        """Use an item outside a fight through Main Menu > Inventory > Items:
        item and target checked on screen before Confirm. ``count`` uses it
        that many times; otherwise the first use measures it and the uses
        needed to reach ``until_hp`` or ``until_mp`` (default: full HP, or
        full MP for an MP item given until_mp) follow. Names: mega-potion,
        ether, phoenix down (or a "$..." key)."""
        from .field_menu import use_field_item
        self._assert_field_menu_allowed()
        for name, value in (("until_hp", until_hp), ("until_mp", until_mp), ("count", count)):
            if value is not None and (type(value) is not int or value < 1):
                raise SafetyError(f"{name} must be a positive whole number")
        if until_hp is not None and until_mp is not None:
            raise SafetyError("give until_hp or until_mp, not both")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            result = use_field_item(self, item, target, until_hp, until_mp, count)
        finally:
            self._action_lock.release()
        self._record_action(**{k: v for k, v in result.items() if k != "presses"})
        return result

    def run_field_menu(self, action: str, **arguments: Any) -> dict[str, Any]:
        """The work of field_menu_async: ``action`` cast_spell (spell,
        caster, target, until_hp, max_casts) or use_item (item, target,
        until_hp | until_mp | count)."""
        if action == "cast_spell":
            return self.cast_field_spell(**arguments)
        if action == "use_item":
            return self.use_field_item(**arguments)
        raise SafetyError("action must be cast_spell or use_item")

    def _assert_field_menu_allowed(self) -> None:
        if not getattr(self.input, "is_real", False):
            raise SafetyError("the field menu requires the Windows backend")
        state = self.telemetry.observe()
        if state.in_combat:
            raise SafetyError("the field menu is not used in a fight: use use_battle_command")
        if state.in_menu or state.paused:
            raise SafetyError("close the open menu first")

    def read_battle_command_menu(self) -> dict[str, Any]:
        """The battle command menu now: open, list title, entries, the
        highlighted index and entry, and its help text. No input."""
        from .battle_command import parse_command_menu
        from .map_query import request_snapshot
        response, _ = request_snapshot(self, heights=False, flags=("menutexts", "savecells"), require_map=False)
        texts = [item for item in response.get("texts") or [] if isinstance(item, dict)]
        return parse_command_menu(texts, [item for item in response.get("save_lists") or [] if isinstance(item, dict)])

    def _battle_vitals(self) -> dict[str, Any] | None:
        """Party and enemy HP/MP from one combat snapshot, keyed by actor."""
        try:
            combat = (self.observe_combat(timeout_seconds=3.0) or {}).get("combat") or {}
        except Exception:
            return None
        if not combat.get("in_battle"):
            return None
        return {"party": {str(m.get("actor_id")): {"hp": m.get("hp"), "mp": m.get("mp")}
                          for m in combat.get("party") or [] if isinstance(m, dict)},
                "enemies": {str(e.get("actor_id")): {"hp": e.get("hp")}
                            for e in combat.get("enemies") or [] if isinstance(e, dict)}}

    def use_battle_command(self, path: Any = None, execute: bool = False,
                           effect_seconds: float = 3.0, target: str | None = None,
                           hold_on_enemy_skill: bool = False) -> dict[str, Any]:
        """Issue an ATB command by name in a fight: ``path`` is the menu
        entries in order, e.g. ["Items", "Mega-Potion"] or ["Abilities",
        "Braver"]. Opens the Commands Menu, moves to each entry, verifies the
        highlight natively before each Confirm and reports how it ended
        (``command_not_accepted`` when an entry is unavailable, e.g. not
        enough ATB; the menu is then closed). Without ``execute`` it reads
        the menu only."""
        from .battle_command import MAX_PATH, use_command
        if (not isinstance(path, list) or not 1 <= len(path) <= MAX_PATH
                or any(not isinstance(name, str) or not name.strip() for name in path)):
            raise SafetyError(f"path must be 1-{MAX_PATH} menu entry names")
        if type(execute) is not bool:
            raise SafetyError("execute must be a boolean")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise SafetyError("target must be an entry name")
        if type(hold_on_enemy_skill) is not bool:
            raise SafetyError("hold_on_enemy_skill must be a boolean")
        if execute and not self.telemetry.observe().in_combat:
            raise SafetyError("battle commands need a fight in progress")
        if not 0 <= float(effect_seconds) <= 10:
            raise SafetyError("effect_seconds must be between 0 and 10")
        # A command can be knocked out of its animation after the menu closes
        # (live: a Fire cast interrupted by a Velociwing left it untouched,
        # and the ATB is spent all the same), so the vitals before and after
        # are reported, never assumed.
        before = self._battle_vitals() if execute else None
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            result = use_command(self.read_battle_command_menu, lambda button: self.battle_menu_tap(button, 120),
                                 self._sleeper, path, execute, target, hold_on_enemy_skill)
        finally:
            try:
                self.input.release_all()
            finally:
                self._action_lock.release()
        if result["outcome"] == "menu_closed_after_command" and before is not None:
            self._sleeper(float(effect_seconds))
            after = self._battle_vitals()
            result["effect"] = {"before": before, "after": after, "seconds": float(effect_seconds),
                                "changed": after is None or after != before}
        if result["input_sent"]:
            self._record_action(action="use_battle_command", ok=result["ok"],
                                rejection_reason=None if result["ok"] else result["outcome"])
        return result

    def _tap_game_over_confirm(self, button: str, milliseconds: int) -> dict[str, Any]:
        """Confirm on the Game Over screen retry_after_game_over has just read.
        The fight's telemetry still reports combat there (live 2026-10-20), so
        ordinary menu input is refused."""
        if button != "confirm":
            raise SafetyError("only Confirm is pressed on the Game Over screen")
        if not getattr(self.input, "is_real", False):
            return self.tap(button, milliseconds)
        state = self._assert_real_menu_input_allowed(button, milliseconds, allow_combat_tip=True)
        self.input.tap(button, milliseconds)
        result = {"ok": True, "sequence": state.sequence, "action": "tap", "button": button,
                  "mode": "game_over"}
        self._record_action(**result)
        return result

    def tap(self, button: str, milliseconds: int = 500) -> dict[str, Any]:
        if button == "map" and int(milliseconds) < MAP_BUTTON_MINIMUM_MS:
            raise SafetyError("map controls require a hold of at least 500 ms")
        if getattr(self.input, "is_real", False):
            state = self._assert_real_menu_input_allowed(button, milliseconds)
            self.input.tap(button, milliseconds)
            result = {
                "ok": True, "sequence": state.sequence, "action": "tap",
                "button": button, "mode": "menu",
            }
            self._record_action(**result)
            return result
        state = self._assert_action_allowed(allow_paused=True)
        self.input.tap(button, milliseconds)
        return {"ok": True, "sequence": state.sequence, "action": "tap", "button": button}

    @staticmethod
    def _queen_blood_payload(state: Any) -> dict[str, Any] | None:
        payload = getattr(state, "queen_blood", None)
        return payload if isinstance(payload, dict) else None

    def _tap_queen_blood_confirm(
        self,
        milliseconds: int,
        *,
        allow_stale_queen_blood_transition: bool = False,
        keyboard: bool = False,
    ) -> None:
        """Send a QB menu confirmation through the normal real-input guard.

        ``keyboard`` forces the focused keyboard path: the field dialogue and
        the pre-match popups ignore the virtual pad (live 2026-09-25, pad A on
        a highlighted "Challenge to a match?" Yes did nothing).

        The live dialogue widget accepts the public guarded menu-input path
        reliably because it refreshes the active session lease before sending
        Enter.  Keep simulation tests on the direct backend path.
        """
        if getattr(self.input, "is_real", False) and self._session_lease is not None:
            self._assert_real_menu_input_allowed(
                "confirm",
                milliseconds,
                allow_queen_blood_dialogue_transition=True,
                allow_stale_queen_blood_transition=allow_stale_queen_blood_transition,
                background_pad=not keyboard,
            )
        self._queen_blood_press("confirm", milliseconds, keyboard=keyboard)

    @classmethod
    def _queen_blood_begin_popup(cls, state: Any) -> bool | None:
        """True/False when the probe reports the Begin Match popup, else None."""
        payload = cls._queen_blood_payload(state)
        popups = payload.get("popups") if isinstance(payload, dict) else None
        value = popups.get("begin_confirm") if isinstance(popups, dict) else None
        return value if isinstance(value, bool) else None

    @classmethod
    def _queen_blood_prematch_visible(cls, state: Any) -> bool:
        """Return whether the producer identifies the pre-match UI.

        The card-game probe is intentionally additive and may expose either a
        direct boolean or a small UI descriptor while its runtime field names
        are being reconciled.  Only those explicit producer fields authorize
        the F3 press; an arbitrary menu or screenshot never does.
        """
        payload = cls._queen_blood_payload(state)
        if payload is None:
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        # ``match_screen`` identifies the QB UI, not specifically the deck
        # selector.  Once the producer has an active match phase, the screen
        # is no longer pre-match even though that UI flag remains true.
        if queen_blood.active and queen_blood.phase in QUEEN_BLOOD_MATCH_PHASES:
            return False
        if queen_blood.begin_match_available or queen_blood.match_screen:
            return True
        if queen_blood.screen in QUEEN_BLOOD_PREMATCH_SCREENS:
            return True
        # Accept the original initiate-branch aliases while old producers are
        # being retired, but only after the canonical payload has validated.
        if payload.get("pre_match") is True:
            return True
        screen = str(payload.get("screen", payload.get("ui_state", ""))).strip().lower()
        if screen in QUEEN_BLOOD_PREMATCH_SCREENS:
            return True
        ui = payload.get("ui")
        if isinstance(ui, dict):
            if ui.get("begin_match_available") is True or ui.get("start_match_available") is True:
                return True
            ui_screen = str(ui.get("screen", ui.get("state", ""))).strip().lower()
            if ui_screen in QUEEN_BLOOD_PREMATCH_SCREENS:
                return True
        phase = str(payload.get("phase", "")).strip().lower()
        return payload.get("active") is True and phase in QUEEN_BLOOD_PREMATCH_ACTIVE_PHASES

    @classmethod
    def _queen_blood_mulligan_ready(cls, state: Any) -> bool:
        """Return whether the producer has exposed the drawn mulligan hand."""
        payload = cls._queen_blood_payload(state)
        if payload is None:
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        return (
            queen_blood.active
            and queen_blood.phase == "dealing"
            and queen_blood.screen == "mulligan"
            and len(queen_blood.hand) >= 5
        )

    @classmethod
    def _queen_blood_started(cls, state: Any, before_payload: dict[str, Any]) -> bool:
        payload = cls._queen_blood_payload(state)
        if payload is None or payload == before_payload:
            return False
        try:
            queen_blood = QueenBloodState.from_dict(payload)
        except QueenBloodError:
            return False
        return queen_blood.active and queen_blood.phase in QUEEN_BLOOD_MATCH_PHASES

    @classmethod
    def _queen_blood_dialogue_active(cls, state: Any) -> bool:
        interaction = getattr(state, "interaction", None)
        return (
            isinstance(interaction, dict)
            and str(interaction.get("state", "")).strip().lower()
            in QUEEN_BLOOD_DIALOGUE_STATES
        ) or cls._queen_blood_talk_wheel_shown(state)

    @classmethod
    def _queen_blood_dialogue_transition_active(cls, state: Any) -> bool:
        """Recognize the game UI transition where the generic map feed blanks.

        During the Turner conversation the game temporarily publishes no map,
        player pose, or generic interaction record while the dialogue widget is
        on screen.  This state is only admitted as a continuation signal when
        the game is not loading, in combat, or targeting, and the QB producer
        has not reported a different screen.
        """
        if (
            getattr(state, "map_id", None) is not None
            or getattr(state, "loading", False)
            or getattr(state, "in_combat", False)
            or getattr(state, "target", None) is not None
        ):
            return False
        payload = cls._queen_blood_payload(state)
        return payload is None or (
            isinstance(payload, dict)
            and payload.get("active") is not True
            and payload.get("match_screen") is not True
            and payload.get("begin_match_available") is not True
        )

    @classmethod
    def _queen_blood_talk_wheel(cls, state: Any) -> dict[str, Any] | None:
        payload = cls._queen_blood_payload(state)
        wheel = payload.get("talk_menu") if isinstance(payload, dict) else None
        return wheel if isinstance(wheel, dict) else None

    @classmethod
    def _queen_blood_talk_wheel_supported(cls, state: Any) -> bool:
        """The probe publishes the dialogue wheel (it may not be found yet)."""
        return cls._queen_blood_talk_wheel(state) is not None

    @classmethod
    def _queen_blood_talk_wheel_shown(cls, state: Any) -> bool:
        wheel = cls._queen_blood_talk_wheel(state)
        return bool(wheel and wheel.get("observed") and wheel.get("shown"))

    def _run_queen_blood_dialogue(
        self,
        latest: Any,
        deadline: float,
        inputs: list[dict[str, Any]],
        trace: list[dict[str, Any]],
        dialogue_forwarder: Any = None,
    ) -> Any:
        """Drive the NPC dialogue to the deck screen with inputs gated on the wheel.

        Live 2026-09-25 the game lagged so much that a timed confirm landed
        after the line had advanced on its own and picked the wheel's default
        (No after a win), ending the conversation. Here nothing is pressed
        on unverified lines; the optional Talk-scoped forwarder uses fresh ordinary
        subtitle evidence and sends nothing on the wheel. Left and one Confirm are sent
        only while the wheel is shown, and a Confirm is repeated only if the
        wheel is still shown after the response window. A conversation that
        ends without the wheel is followed by another Talk.
        """
        wheel_deadline = min(deadline, self._monotonic() + QUEEN_BLOOD_WHEEL_WAIT_SECONDS)
        talks = 1
        confirms = 0
        field_since: float | None = None
        first_observation = True
        while self._monotonic() < wheel_deadline:
            # Initiation's wait already acquired a fresh wheel. Preserve that
            # handoff instead of requiring another native lookup to agree.
            if first_observation:
                first_observation = False
            else:
                latest = self.telemetry.observe()
            if self._queen_blood_prematch_visible(latest):
                if dialogue_forwarder is not None:
                    dialogue_forwarder.stop("prematch_visible")
                return latest
            if self._queen_blood_talk_wheel_shown(latest):
                if dialogue_forwarder is not None:
                    dialogue_forwarder.wheel_seen()
                field_since = None
                if confirms >= QUEEN_BLOOD_WHEEL_CONFIRM_ATTEMPTS:
                    raise SafetyError("the dialogue wheel did not accept Yes")
                # The wheel cursor points at the highlighted answer: live,
                # 100 degrees on No (right) and 280 after Left, on Yes. Only
                # confirm while it points left; otherwise press Left again.
                # This freshly acquired sample already verifies Yes. Do not
                # discard it for an unnecessary second lookup, which can miss
                # the same visible wheel during native path rediscovery.
                selected = (latest if self._queen_blood_wheel_on_yes(latest) is True
                            else self._wait_for_wheel_yes(wheel_deadline))
                if selected is None:
                    continue
                wheel = self._queen_blood_talk_wheel(selected) or {}
                if (callable(getattr(self.telemetry, "request_queen_blood_dialogue", None))
                        and self._queen_blood_wheel_on_yes(selected) is not True):
                    raise SafetyError("Queen's Blood Yes selection is unverified")
                self._tap_queen_blood_confirm(400, keyboard=True)
                if dialogue_forwarder is not None:
                    dialogue_forwarder.choice_handled()
                request_probe = getattr(self.telemetry, "request_queen_blood_probe", None)
                if callable(request_probe) and request_probe() is False:
                    raise SafetyError("Queen's Blood scanner request failed after confirming Yes")
                confirms += 1
                inputs.append({
                    "button": "confirm", "reason": "dialogue_wheel_yes", "attempt": confirms,
                    "cursor": wheel.get("cursor"), "answers": wheel.get("answers"),
                    "clock": round(self._wall_clock().timestamp(), 3),
                })
                response_deadline = min(wheel_deadline, self._monotonic() + QUEEN_BLOOD_WHEEL_RESPONSE_SECONDS)
                while self._monotonic() < response_deadline:
                    self._sleeper(0.5)
                    sample = self.telemetry.observe()
                    if self._queen_blood_prematch_visible(sample):
                        return sample
                    if not self._queen_blood_talk_wheel_shown(sample):
                        break
                continue
            if dialogue_forwarder is not None and dialogue_forwarder.advance(wheel_deadline):
                field_since = None
                continue
            if self._queen_blood_dialogue_or_transition_active(latest):
                field_since = None
            else:
                # Back on the field with no wheel: after a grace period the
                # conversation has ended (or never opened); talk again.
                now = self._monotonic()
                field_since = field_since if field_since is not None else now
                if now - field_since >= QUEEN_BLOOD_DIALOGUE_ENDED_SECONDS:
                    if talks >= QUEEN_BLOOD_MAX_TALKS:
                        raise SafetyError("the NPC conversation ended without the match prompt")
                    self._ensure_game_foreground()
                    talk_input = self.input.use_interaction(QUEEN_BLOOD_INTERACTION_MILLISECONDS)
                    if dialogue_forwarder is not None:
                        dialogue_forwarder.talk_started()
                    talks += 1
                    inputs.append({"button": "interact", "reason": "talk_again", "attempt": talks, **talk_input})
                    field_since = None
                    wheel_deadline = min(deadline, self._monotonic() + QUEEN_BLOOD_WHEEL_WAIT_SECONDS)
            self._sleeper(0.4)
        # Live at Rolf (#180) the telemetry had stopped (202 s stale; only a
        # game restart recovered), so no wheel could have been observed.
        final = self.telemetry.observe()
        age = final.age_seconds()
        if final.source != "simulation" and age >= self.max_telemetry_age:
            raise SafetyError(
                f"the dialogue wheel did not appear before the timeout: telemetry stopped "
                f"({age:.0f} s stale), so the game may have hung")
        if confirms:
            # Live 2026-10-12 (#328): Yes was accepted and the game showed
            # deck select, but the probe never reported it. Say so, rather
            # than that the wheel never appeared.
            raise SafetyError(QUEEN_BLOOD_DECK_AFTER_YES_TIMEOUT)
        raise SafetyError("the dialogue wheel did not appear before the timeout")

    @classmethod
    def _queen_blood_wheel_on_yes(cls, state: Any) -> bool | None:
        """True/False when the wheel cursor points left (Yes) or right; None if unknown."""
        wheel = cls._queen_blood_talk_wheel(state) or {}
        cursor = wheel.get("cursor")
        angle = cursor.get("angle") if isinstance(cursor, dict) else None
        if type(angle) not in (int, float) or not math.isfinite(angle):
            return None
        return 180.0 < float(angle) % 360.0 < 360.0

    def _wait_for_wheel_yes(self, deadline: float) -> Any:
        """Press Left until the shown wheel's cursor is on Yes; the sample, or None."""
        for _attempt in range(3):
            sample = self.telemetry.observe()
            if not self._queen_blood_talk_wheel_shown(sample):
                return None
            if self._queen_blood_wheel_on_yes(sample) is not False:
                if self._queen_blood_wheel_on_yes(sample) is True:
                    return sample
            self._queen_blood_press("queen_blood_left", 300, keyboard=True)
            settle_until = min(deadline, self._monotonic() + 2.0)
            while self._monotonic() < settle_until:
                self._sleeper(0.3)
                sample = self.telemetry.observe()
                if self._queen_blood_wheel_on_yes(sample) is True:
                    return sample
                if not self._queen_blood_talk_wheel_shown(sample):
                    return None
            if self._queen_blood_wheel_on_yes(sample) is None:
                # Older probe without a cursor angle: fall back to one Left.
                return sample
        return None

    def _wait_for_wheel_no(self, deadline: float, initial: Any = None) -> Any:
        """Press Right until the shown wheel's cursor is verifiably on No."""
        sample = initial
        for _attempt in range(3):
            # The native wheel path can disappear between consumer reads.
            # Discovery is throttled to five seconds. Keep the fresh handoff,
            # and allow that bounded rediscovery without input on a missing UI.
            rediscovery_deadline = min(deadline, self._monotonic() + 6.0)
            while True:
                if sample is None:
                    sample = self.telemetry.observe()
                if self._queen_blood_talk_wheel_shown(sample):
                    break
                if has_field_telemetry(sample) or self._monotonic() >= rediscovery_deadline:
                    return None
                self._sleeper(0.3)
                sample = None
            if self._queen_blood_wheel_on_yes(sample) is False:
                return sample
            if self._monotonic() >= deadline:
                return None
            self._queen_blood_press("queen_blood_right", 300, keyboard=True)
            settle_until = min(deadline, self._monotonic() + 2.0)
            while self._monotonic() < settle_until:
                self._sleeper(0.3)
                sample = self.telemetry.observe()
                if self._queen_blood_talk_wheel_shown(sample) and self._queen_blood_wheel_on_yes(sample) is False:
                    return sample
                if has_field_telemetry(sample):
                    return None
        return None

    def _finish_open_conversation(
        self,
        initial: Any,
        deadline: float,
        inputs: list[dict[str, Any]],
        trace: list[dict[str, Any]],
        require_opening: bool = False,
        dialogue_forwarder: Any = None,
    ) -> tuple[Any, bool]:
        """Decline a visible Queen's Blood wheel, then wait for settled field state.

        The optional Talk-scoped forwarder can advance verified ordinary lines,
        including closing replies, and sends nothing on a wheel. A No confirmation is sent only after
        the wheel and its cursor angle identify No explicitly, at most
        ``QUEEN_BLOOD_WHEEL_CONFIRM_ATTEMPTS`` times, each followed by a
        response window: the probe samples the wheel about once a second, so
        it stays visible after a confirm that worked, and a stray Confirm on
        the field would talk to the NPC again.

        With ``require_opening`` (decline right after Talk) the field is only
        accepted once the conversation has been seen to open; the dialogue can
        appear many seconds after the key, and returning before it does would
        leave it open behind the caller.
        """
        latest = initial
        first_observation = True
        no_selected = False
        confirms = 0
        opened = False
        open_deadline = min(deadline, self._monotonic() + QUEEN_BLOOD_CONVERSATION_OPEN_SECONDS)
        while self._monotonic() < deadline:
            if first_observation:
                first_observation = False
            else:
                latest = self.telemetry.observe()
            payload = self._queen_blood_payload(latest) or {}
            if payload.get("active") is True or self._queen_blood_prematch_visible(latest):
                raise SafetyError("Queen's Blood match UI is already open; refusing decline recovery")
            wheel_shown = self._queen_blood_talk_wheel_shown(latest)
            if (wheel_shown or self._queen_blood_dialogue_or_transition_active(latest)
                    or not has_field_telemetry(latest)):
                opened = True
            if wheel_shown:
                if dialogue_forwarder is not None:
                    dialogue_forwarder.wheel_seen()
                if confirms >= QUEEN_BLOOD_WHEEL_CONFIRM_ATTEMPTS:
                    raise SafetyError("the dialogue wheel did not accept No")
                # As with Yes, use the fresh cursor proof already in hand.
                # A redundant lookup can miss this same visible wheel during
                # native path rediscovery and incorrectly refuse the decline.
                selected = (latest if self._queen_blood_wheel_on_yes(latest) is False
                            else self._wait_for_wheel_no(deadline, initial=latest))
                if selected is None:
                    latest = self.telemetry.observe()
                    if self._queen_blood_talk_wheel_shown(latest) and self._queen_blood_wheel_on_yes(latest) is False:
                        selected = latest
                    elif self._queen_blood_talk_wheel_shown(latest):
                        raise SafetyError("the dialogue wheel did not expose a verifiable No selection")
                    else:
                        continue
                wheel = self._queen_blood_talk_wheel(selected) or {}
                if self._queen_blood_wheel_on_yes(selected) is not False:
                    raise SafetyError("the dialogue wheel cursor is not verifiably on No")
                self._tap_queen_blood_confirm(400, keyboard=True)
                if dialogue_forwarder is not None:
                    dialogue_forwarder.choice_handled()
                confirms += 1
                inputs.append({
                    "button": "confirm",
                    "reason": "dialogue_wheel_no",
                    "attempt": confirms,
                    "cursor": wheel.get("cursor"),
                    "answers": wheel.get("answers"),
                    "clock": round(self._wall_clock().timestamp(), 3),
                })
                no_selected = True
                response_deadline = min(deadline, self._monotonic() + QUEEN_BLOOD_WHEEL_RESPONSE_SECONDS)
                while self._monotonic() < response_deadline:
                    self._sleeper(0.5)
                    if not self._queen_blood_talk_wheel_shown(self.telemetry.observe()):
                        break
                continue

            if has_field_telemetry(latest) and not self._queen_blood_dialogue_active(latest):
                if require_opening and not opened:
                    if self._monotonic() >= open_deadline:
                        raise SafetyError(
                            "conversation_not_observed: the conversation did not open after Talk"
                        )
                    trace.append({"event": "waiting_for_conversation_to_open",
                                  "sequence": getattr(latest, "sequence", None)})
                    self._sleeper(0.3)
                    continue
                remaining = max(1.0, min(30.0, deadline - self._monotonic()))
                settled = self._ensure_settled_gameplay(latest, timeout_seconds=remaining)
                latest = self.telemetry.observe()
                payload = self._queen_blood_payload(latest) or {}
                if (
                    has_field_telemetry(latest)
                    and not self._queen_blood_dialogue_active(latest)
                    and not self._queen_blood_talk_wheel_shown(latest)
                    and payload.get("active") is not True
                    and not self._queen_blood_prematch_visible(latest)
                ):
                    trace.append({
                        "event": "conversation_finished",
                        "sequence": latest.sequence,
                        "settled_sequence": settled.sequence,
                        "map_id": latest.map_id,
                    })
                    if dialogue_forwarder is not None:
                        dialogue_forwarder.stop("conversation_finished")
                    return latest, no_selected

            if dialogue_forwarder is not None and dialogue_forwarder.advance(deadline):
                continue
            # A blank world feed never authorizes input. The optional forwarder
            # requires a separate fresh ordinary-line/advance-arrow UI signal.
            trace.append({
                "event": "waiting_for_conversation",
                "sequence": getattr(latest, "sequence", None),
                "map_id": getattr(latest, "map_id", None),
            })
            self._sleeper(0.3)
        raise SafetyError(
            "conversation did not return to settled field gameplay before timeout; no verifiable "
            "Queen's Blood wheel was handled, so check capture_game_window for an unreadable prompt"
            if not no_selected else
            "conversation did not return to settled field gameplay before timeout after No was selected"
        )

    def finish_conversation(self, timeout_seconds: float = 120.0) -> dict[str, Any]:
        """Finish an open conversation, choosing No only on a verified QB wheel."""
        if not getattr(self.input, "is_real", False):
            raise SafetyError("conversation recovery requires the Windows backend")
        try:
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid conversation timeout") from exc
        if not 1.0 <= timeout_seconds <= QUEEN_BLOOD_MAX_TIMEOUT_SECONDS:
            raise SafetyError(f"timeout_seconds must be between 1 and {QUEEN_BLOOD_MAX_TIMEOUT_SECONDS:g}")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        started = self._monotonic()
        dialogue_requested = False
        try:
            self._ensure_game_foreground()
            initial = self._assert_action_allowed(allow_queen_blood_dialogue_transition=True)
            if initial.in_combat or initial.target is not None:
                raise SafetyError("conversation recovery is blocked during combat or targeting")
            payload = self._queen_blood_payload(initial) or {}
            if payload.get("active") is True or self._queen_blood_prematch_visible(initial):
                raise SafetyError("Queen's Blood match UI is active; conversation recovery refused")
            dialogue_probe = getattr(self.telemetry, "request_queen_blood_dialogue", None)
            if callable(dialogue_probe):
                requested = dialogue_probe()
                if requested is False:
                    raise SafetyError("Queen's Blood dialogue request failed")
                dialogue_requested = bool(requested)
                # The preflight sample preceded this operation's read lease.
                # Refresh it before treating a cursor as a reusable No proof.
                initial = self._queen_blood_observe()
            armed, _ = self._arm.status() if self._arm else (False, 0.0)
            if not armed:
                raise SafetyError("conversation recovery input is not manually armed")
            inputs: list[dict[str, Any]] = []
            trace: list[dict[str, Any]] = []
            final, no_selected = self._finish_open_conversation(
                initial, started + timeout_seconds, inputs, trace
            )
            position = final.player.position if final.player else None
            result = {
                "ok": True,
                "action": "finish_conversation",
                "outcome": "declined" if no_selected else "finished",
                "settled_position": ({"x": position.x, "y": position.y, "z": position.z}
                                     if position is not None else None),
                "map_id": final.map_id,
                "inputs": inputs,
                "trace": trace,
            }
            self._record_action(**result)
            return result
        finally:
            if dialogue_requested:
                release = getattr(self.telemetry, "release_queen_blood_probe", None)
                if callable(release):
                    release()
            self.input.release_all()
            self._last_action_finished = self._monotonic()
            self._action_lock.release()

    @classmethod
    def _queen_blood_dialogue_or_transition_active(cls, state: Any) -> bool:
        return cls._queen_blood_dialogue_active(state) or cls._queen_blood_dialogue_transition_active(state)

    @staticmethod
    def _queen_blood_host_nearby(snapshot: Any, target_id: str) -> dict[str, Any] | None:
        """The one card-game actor the scan saw near where the opponent stands.

        Some hosts report no actor name to the probe (live 2026-09-29 Nene
        was MG2005_00_UniqueCardGameKalm02_C with actor_name ""), so the name
        scan missed and the full inventory fallback cost about 10 s each time.
        The scan's nearby list still has the actor, on the catalogue position.
        """
        nearby = snapshot.get("nearby") if isinstance(snapshot, dict) else None
        if not isinstance(nearby, list):
            return None
        try:
            from .qb_opponents import opponent

            record = opponent(target_id)
        except Exception:
            return None
        points = [
            candidate["xyz"] for candidate in record.get("position_candidates") or []
            if isinstance(candidate, dict) and isinstance(candidate.get("xyz"), list)
            and len(candidate["xyz"]) >= 2
        ]
        matches = []
        for actor in nearby:
            if not isinstance(actor, dict) or actor.get("is_player") is True:
                continue
            identity = f"{actor.get('class', '')} {actor.get('id', '')}".casefold()
            position = actor.get("position")
            if "cardgame" not in identity or not isinstance(position, dict):
                continue
            try:
                x, y = float(position["x"]), float(position["y"])
            except (KeyError, TypeError, ValueError):
                continue
            if any(math.hypot(x - float(point[0]), y - float(point[1])) <= QUEEN_BLOOD_HOST_MATCH_CM
                   for point in points):
                matches.append(actor)
        if len(matches) == 1:
            return matches[0]
        # The Moore Twins share a nameless character class and stand 63 cm
        # apart, so both fit the normal 150 cm host envelope. Corroborate an
        # exact resident host placement; never rank actors by player distance.
        if len(matches) != 2 or any(
            actor.get("is_player") is not False or actor.get("actor_name")
            or not isinstance(actor.get("id"), str) or not actor["id"].strip()
            or not isinstance(actor.get("class"), str) or "cardgame" not in actor["class"].casefold()
            for actor in matches
        ):
            return None
        if matches[0]["id"] == matches[1]["id"] or matches[0].get("class") != matches[1].get("class"):
            return None
        host_id = (record.get("host") or {}).get("object_id") or record.get("id")
        resident_points = [
            point["xyz"] for point in record.get("position_candidates") or []
            if point.get("method") == "resident_initialize_transform"
            and point.get("ambiguous") is False and point.get("duplicates") == 0
            and (point.get("table_source") or {}).get("row") == host_id
            and isinstance(point.get("xyz"), list) and len(point["xyz"]) == 3
        ]
        exact = []
        for actor in matches:
            try:
                xyz = [float(actor["position"][axis]) for axis in ("x", "y", "z")]
                if not all(math.isfinite(value) for value in xyz):
                    return None
                for point in resident_points:
                    anchor = [float(value) for value in point]
                    if not all(math.isfinite(value) for value in anchor):
                        continue
                    other = matches[1] if actor is matches[0] else matches[0]
                    separation = math.hypot(float(other["position"]["x"]) - anchor[0], float(other["position"]["y"]) - anchor[1])
                    if math.hypot(xyz[0] - anchor[0], xyz[1] - anchor[1]) <= 5.0 and abs(xyz[2] - anchor[2]) <= 100.0 and separation >= 50.0:
                        exact.append(actor)
                        break
            except (KeyError, TypeError, ValueError):
                return None
        return exact[0] if len(exact) == 1 else None

    def _resolve_queens_blood_target(
        self,
        state: Any,
        target_id: str | None,
        target_kind: str,
        deadline: float,
    ) -> tuple[dict[str, Any], Any]:
        """Resolve a QB opponent when the generic state feed omits its affordance.

        The generic surroundings producer may expose a card gamer only through
        the on-demand character inventory.  Use that source only for an
        explicit NPC selector, and synthesize the minimum interaction record
        after checking the static identity and live actor evidence.  The
        returned state is sampled after the inventory request so the spatial
        admission check is current.
        """
        from .entity_interaction import ready, resolve_entity

        # The normal world-target resolver first walks the static identity
        # index and can spend the whole bridge request budget proving a target
        # that the on-demand UE4SS character probe has already identified.  A
        # Queen's Blood initiation target is already a caller-supplied actor
        # selector, so admit the exact live actor directly when the bounded
        # actor-name probe returns it.  Keep the older static resolver below as
        # a fallback for selectors that are not runtime actor names.
        if target_id and target_kind.strip().casefold() == "npc":
            actor_selector = target_id
            # A canonical field-object ID is not a runtime actor name. Resolve
            # only the catalogue's exact canonical binding, never an alias guess.
            if target_id.startswith("field_object:"):
                from .navigation_values import MissionError
                try:
                    from .qb_opponents import opponent
                    record = opponent(target_id)
                    name = record.get("actor_name")
                    if record.get("canonical_id") == target_id and isinstance(name, str) and name.strip():
                        actor_selector = name.strip()
                except (MissionError, KeyError, ValueError, TypeError):
                    pass
            try:
                from .character_locator import scan_character_target

                fast_scan = scan_character_target(
                    self,
                    f"actor_name:{actor_selector}",
                    seconds=10,
                )
                snapshot = fast_scan.get("snapshot") if isinstance(fast_scan, dict) else None
                actor = snapshot.get("target") if isinstance(snapshot, dict) else None
                if not (
                    isinstance(snapshot, dict)
                    and snapshot.get("complete") is True
                    and isinstance(actor, dict)
                    and str(actor.get("actor_name", "")).strip().casefold()
                    == actor_selector.strip().casefold()
                ):
                    actor = self._queen_blood_host_nearby(snapshot, target_id)
                if isinstance(actor, dict) and actor.get("is_player") is not True:
                    position = actor.get("position")
                    live_id = actor.get("id")
                    if isinstance(position, dict) and isinstance(live_id, str) and live_id.strip():
                        current = self.telemetry.observe()
                        if (
                            current.map_id == state.map_id
                            and current.player.actor_id == state.player.actor_id
                            and current.sequence > state.sequence
                        ):
                            target = {
                                "id": live_id,
                                "kind": "npc",
                                "name": target_id,
                                "position": position,
                                "usable": True,
                                "visible": True,
                                "prompt": "Talk",
                                "opened": False,
                                "source": "ue4ss_character_target",
                            }
                            if ready(
                                current.to_dict(),
                                target,
                                max_distance_m=QUEEN_BLOOD_NPC_INTERACTION_RADIUS_M,
                            ):
                                return target, current
            except Exception:
                # The exact probe is advisory; retain the existing static
                # identity path if it cannot produce a complete live target.
                pass

        sample = state.to_dict()
        try:
            return resolve_entity(sample, target_id, target_kind), state
        except Exception as observed_exc:
            if not target_id or target_kind.strip().casefold() != "npc":
                raise SafetyError(
                    f"Queen's Blood NPC target is unavailable: {observed_exc}"
                ) from observed_exc

            remaining = deadline - self._monotonic()
            if remaining < 1.0:
                raise SafetyError("Queen's Blood NPC target resolution exceeded the action deadline") from observed_exc
            # A freshly loaded scene can publish settled player telemetry before
            # the named NPC has been instantiated into the actor inventory.  A
            # single long scan then returns ``known_but_not_currently_instantiated``
            # even though the actor appears moments later.  Retry bounded scans
            # until the action deadline so the caller does not need to intervene.
            located = None
            last_resolution_error: Exception = observed_exc
            resolution_attempt = 0
            while located is None:
                remaining = deadline - self._monotonic()
                # resolve_world_target delegates to live inventory probes
                # whose request contract requires at least ten seconds.
                # Never turn the final few seconds of the action deadline
                # into an invalid one-to-five-second retry.
                if remaining < 10.0:
                    raise SafetyError(
                        "Queen's Blood NPC target resolution exceeded the action deadline"
                    ) from last_resolution_error
                scan_seconds = max(10, min(60, int(remaining)))
                resolution_attempt += 1
                try:
                    candidate = self.resolve_world_target(target_id, seconds=scan_seconds)
                except Exception as resolve_exc:
                    last_resolution_error = resolve_exc
                else:
                    if isinstance(candidate, dict) and candidate.get("status") == "located":
                        located = candidate
                        break
                    status = candidate.get("status") if isinstance(candidate, dict) else None
                    last_resolution_error = RuntimeError(
                        f"named live actor was not located ({status or 'invalid_resolution'})"
                    )
                if deadline - self._monotonic() < 10.0:
                    raise SafetyError(
                        "Queen's Blood NPC target resolution exceeded the action deadline"
                    ) from last_resolution_error
                self._sleeper(min(0.5, max(0.0, deadline - self._monotonic())))

            identity = located.get("identity")
            candidates = identity.get("candidates") if isinstance(identity, dict) else None
            candidate = candidates[0] if isinstance(candidates, list) and len(candidates) == 1 else None
            actor = located.get("live_actor")
            actor_name = actor.get("actor_name") if isinstance(actor, dict) else None
            static_actor_name = candidate.get("actor_name") if isinstance(candidate, dict) else None
            canonical_id = located.get("canonical_id")
            card_gamer_evidence = " ".join(
                str(value)
                for value in (actor_name, static_actor_name, canonical_id)
                if isinstance(value, str)
            ).casefold()
            if candidate is None or not isinstance(actor, dict) or actor.get("is_player") is True:
                raise SafetyError("Queen's Blood NPC target is unavailable: live identity evidence is incomplete") from observed_exc
            if "cardgamer" not in card_gamer_evidence and "card_gamer" not in card_gamer_evidence:
                raise SafetyError(
                    "Queen's Blood NPC target is unavailable: named actor is not a card-game opponent"
                ) from observed_exc
            live_id = actor.get("id")
            position = located.get("position")
            if not isinstance(live_id, str) or not live_id.strip() or not isinstance(position, dict):
                raise SafetyError("Queen's Blood NPC target is unavailable: live actor pose is incomplete") from observed_exc

            target = {
                "id": live_id,
                "kind": "npc",
                "name": target_id,
                "position": position,
                "usable": True,
                "visible": True,
                "prompt": "Talk",
                "opened": False,
                "source": "world_target_resolver",
                "canonical_id": canonical_id,
            }
            current = self.telemetry.observe()
            if (
                current.map_id != state.map_id
                or current.player.actor_id != state.player.actor_id
                or current.sequence <= state.sequence
            ):
                raise SafetyError("Queen's Blood NPC target resolution crossed a world or telemetry boundary") from observed_exc
            if not ready(
                current.to_dict(),
                target,
                max_distance_m=QUEEN_BLOOD_NPC_INTERACTION_RADIUS_M,
            ):
                raise SafetyError(
                    "Queen's Blood NPC is not usable, visible, in range, or facing the player"
                ) from observed_exc
            return target, current

    def _wait_for_queen_blood_sample(
        self,
        previous_sequence: int,
        deadline: float,
        predicate: Any,
        trace: list[dict[str, Any]],
        *,
        return_latest_on_timeout: bool = False,
        allow_stale_queen_blood_transition: bool = False,
    ) -> Any:
        """Read a fresh advancing sample before the next bounded input."""
        latest = None
        # The fixed poll bound keeps a broken test clock or producer from
        # turning a gameplay action into an unbounded wait.
        for _ in range(600):
            if self._monotonic() >= deadline:
                break
            latest = self.telemetry.observe()
            age = latest.age_seconds()
            if not latest.running:
                raise SafetyError("game telemetry reports that the game is stopped")
            if (
                latest.source != "simulation"
                and age > self._queen_blood_freshness_limit()
                and not allow_stale_queen_blood_transition
            ):
                raise SafetyError("Queen's Blood telemetry is stale")
            if latest.sequence > previous_sequence:
                item = {
                    "sequence": latest.sequence,
                    "interaction_state": (
                        latest.interaction.get("state")
                        if isinstance(latest.interaction, dict) else None
                    ),
                    "queen_blood": latest.queen_blood,
                }
                trace.append(item)
                if predicate(latest):
                    return latest
                previous_sequence = latest.sequence
            self._sleeper(min(0.1, max(0.01, deadline - self._monotonic())))
        if return_latest_on_timeout:
            return latest
        raise SafetyError("Queen's Blood state did not advance before the action deadline")

    def _one_off_visit_receipt(self) -> dict[str, Any]:
        from .recording_driver import validate_recording_driver
        run = self.get_queens_blood_run_status()
        driver, loaded, assignment = run.get("driver"), run.get("loaded_save"), run.get("assignment")
        if (not run.get("loaded_save_verified") or not isinstance(loaded, dict)
                or not loaded.get("load_id") or not isinstance(loaded.get("process"), dict)
                or not loaded["process"].get("pid") or not loaded["process"].get("token")
                or not isinstance(assignment, dict) or run.get("run_loaded")
                or type(loaded.get("slot")) is not int or loaded.get("slot") == assignment.get("slot")
                or loaded.get("steam_id") != assignment.get("steam_id")
                or run.get("profile_id") is not None or not isinstance(driver, dict)
                or not driver.get("run_id")):
            raise SafetyError("one-off visit requires a verified nonassigned loaded save and declared run")
        try:
            recording = validate_recording_driver(driver.get("model_id"), driver.get("effort"))
        except ValueError as exc:
            raise SafetyError("one-off visit requires exact declared model and effort") from exc
        if recording is None:
            raise SafetyError("one-off visit requires exact declared model and effort")
        session = self._session_lease.status() if self._session_lease is not None else None
        if (not isinstance(session, dict) or session.get("active") is not True
                or session.get("owner_matches") is not True or session.get("recording_driver") != recording
                or str(session.get("game_pid")) != str(loaded["process"].get("pid"))):
            raise SafetyError("one-off visit requires a matching owned attributed control session")
        return deepcopy({"driver": driver, "assignment": assignment, "loaded_save": loaded,
                         "session": {key: session.get(key) for key in
                                     ("session_marker", "owner_id", "controller_pid", "game_pid", "opened_at", "client_id")}})

    def _assert_one_off_visit_unchanged(self) -> None:
        binding = getattr(self, "_one_off_visit_binding", None)
        if binding is not None and self._one_off_visit_receipt() != binding:
            raise SafetyError("one-off visit loaded save or declared run changed")

    def initiate_queens_blood_match(
        self,
        target_id: str | None = None,
        target_kind: str = "npc",
        dialogue_confirmations: int = 8,
        confirmation_milliseconds: int = 500,
        begin_milliseconds: int = 500,
        timeout_seconds: float = 120.0,
        stop_at_deck_select: bool = False,
        decline: bool = False,
        one_off_visit: bool = False,
        fast_forward_dialogue: bool = True,
    ) -> dict[str, Any]:
        """Interact with an NPC and start or decline its Queen's Blood match.

        With ``stop_at_deck_select`` the call returns ``deck_select_ready``
        once the pre-match deck screen is up, so a deck can be chosen or
        edited for this opponent; calling it again from that screen skips
        the conversation and begins the match.

        This is a single bounded transaction: one NPC interaction when the
        caller is still in the world, repeated default-option confirmations
        through dialogue, and up to three complete ``F3``/``Yes`` begin-match
        attempts. An identified card after a begin attempt is the mulligan
        transition signal; the prompt banner is not required.

        ``decline`` interacts with the named NPC if needed, selects No only
        when the talk wheel and No cursor position are observed, and waits for
        settled field telemetry without opening the match.
        """
        if not getattr(self.input, "is_real", False):
            raise SafetyError("Queen's Blood match initiation requires the Windows backend")
        if type(fast_forward_dialogue) is not bool:
            raise SafetyError("fast_forward_dialogue must be a boolean")
        visit_binding = None
        if one_off_visit:
            if (one_off_visit is not True or not target_id or target_kind.strip().casefold() != "npc"
                    or type(stop_at_deck_select) is not bool or type(decline) is not bool
                    or stop_at_deck_select == decline):
                raise SafetyError("one-off visit requires a named NPC and exactly deck-select stop or decline; Begin is forbidden")
            visit_binding = self._one_off_visit_receipt()
        campaign_assignment = None
        if self._queen_blood_runs is not None:
            run = self.get_queens_blood_run_status()
            if run["assignment"] and not run["run_loaded"] and not one_off_visit:
                raise SafetyError("the declared model's assigned save is not loaded; use start_model_run_async")
            if run["run_loaded"] and not one_off_visit:
                campaign_assignment = run.get("assignment")
        try:
            dialogue_confirmations = int(dialogue_confirmations)
            confirmation_milliseconds = validate_duration(confirmation_milliseconds)
            begin_milliseconds = validate_duration(begin_milliseconds)
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid Queen's Blood match initiation parameters") from exc
        if not 1 <= dialogue_confirmations <= QUEEN_BLOOD_MAX_DIALOGUE_CONFIRMATIONS:
            raise SafetyError(
                "dialogue_confirmations must be between 1 and "
                f"{QUEEN_BLOOD_MAX_DIALOGUE_CONFIRMATIONS}"
            )
        if not 1.0 <= timeout_seconds <= QUEEN_BLOOD_MAX_TIMEOUT_SECONDS:
            raise SafetyError(
                f"timeout_seconds must be between 1 and {QUEEN_BLOOD_MAX_TIMEOUT_SECONDS:g}"
            )
        if decline and (not target_id or target_kind.strip().casefold() != "npc"):
            raise SafetyError("decline mode requires a named Queen's Blood NPC target")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")

        self._one_off_visit_binding = visit_binding
        started = self._monotonic()
        deadline = started + timeout_seconds
        trace: list[dict[str, Any]] = []
        inputs: list[dict[str, Any]] = []
        from .npc_dialogue import DialogueForwarder
        forwarder = DialogueForwarder(self, fast_forward_dialogue, trace, inputs)
        pre_state = None
        target = None
        interaction_sent = False
        handoff: dict[str, Any] = {"event": "npc_interaction_handoff", "phases_ms": {}, "input_sent": False}
        handoff_mark = started

        def handoff_lap(phase: str) -> None:
            nonlocal handoff_mark
            now = self._monotonic()
            handoff["phases_ms"][phase] = round((now - handoff_mark) * 1000.0, 3)
            handoff_mark = now

        trace.append(handoff)
        interaction_attempts = 0
        dialogue_transition_grace = False
        dialogue_settle_pending = False
        match_screen_state = None
        borrowed_from: int | None = None
        final_state = None
        dialogue_requested = False
        try:
            self._queen_blood_mulligan_inferred_sequence = None
            self._queen_blood_mulligan_inferred_hand = None
            self._queen_blood_mulligan_inferred_basis = None
            self._queen_blood_turn_one_inferred_sequence = None
            self._queen_blood_turn_one_inferred_payload = None
            # Talk, the field dialogue and Begin Match use the focused
            # keyboard (the pad is unreliable there while unfocused), and the
            # checks below require focus: take it before anything else. In
            # background mode the window that had focus gets it back when
            # initiation ends (live, the focused game then ignored the pad).
            if self._queen_blood_background_input and not self._background_input_enabled():
                borrowed_from = self._foreground_to_restore()
            self._ensure_game_foreground()
            handoff_lap("foreground")
            pre_state = self._assert_action_allowed(
                allow_queen_blood_dialogue_transition=(
                    bool(target_id) and target_kind.strip().casefold() == "npc"
                )
            )
            if pre_state.source == "simulation":
                raise SafetyError("Queen's Blood match initiation requires live game telemetry")
            if pre_state.in_combat or pre_state.target is not None:
                raise SafetyError("Queen's Blood match initiation is blocked during combat or targeting")
            if not self._environment.game_process_exists():
                raise SafetyError("FFVII Rebirth process was not found")
            if not self._queen_blood_input_reachable():
                raise SafetyError("FFVII Rebirth is not the foreground window")
            armed, _ = self._arm.status() if self._arm else (False, 0.0)
            if not armed:
                raise SafetyError("Queen's Blood input is not manually armed")
            handoff_lap("readiness_and_ownership")

            if self._queen_blood_prematch_visible(pre_state):
                if one_off_visit and decline:
                    raise SafetyError("one-off decline cannot begin from an open deck screen")
                match_screen_state = pre_state
            elif not self._queen_blood_dialogue_or_transition_active(pre_state):
                # Reuse the same explicit affordance rules as the entity
                # mission, but keep the whole sequence under one action lock
                # so the interaction and dialogue cannot be interleaved.
                from .entity_interaction import entities, ready

                target, pre_state = self._resolve_queens_blood_target(
                    pre_state,
                    target_id,
                    target_kind,
                    deadline,
                )
                handoff_lap("target_resolution_and_facing")
                sample = pre_state.to_dict()
                if not ready(
                    sample,
                    target,
                    max_distance_m=QUEEN_BLOOD_NPC_INTERACTION_RADIUS_M,
                ):
                    raise SafetyError("Queen's Blood NPC is not usable, visible, in range, or facing the player")
                competitors = [
                    item for item in entities(sample)
                    if item["id"] != target["id"] and ready(sample, item, require_facing=False)
                ]
                if competitors:
                    raise SafetyError("Queen's Blood NPC interaction is ambiguous")
                handoff_lap("affordance_and_competitors")
                # Talk needs only its answer wheel. Full card discovery starts
                # in _run_queen_blood_dialogue after Yes has been confirmed.
                request_probe = getattr(self.telemetry, "request_queen_blood_dialogue", None)
                if callable(request_probe):
                    if request_probe() is False:
                        raise SafetyError("Queen's Blood dialogue request failed")
                    dialogue_requested = True
                # The interaction key opens the dialogue widget asynchronously,
                # and an NPC can still be settling into the live world after the
                # target resolver has found it.  Retry the same explicit
                # interaction a few times when neither dialogue nor the card UI
                # appears.  Once either signal exists, never send another
                # interaction key; the rest of the transaction owns the dialogue
                # and pre-match transitions.
                latest = pre_state
                for interaction_attempt in range(QUEEN_BLOOD_MAX_INTERACTION_ATTEMPTS):
                    self._ensure_game_foreground()  # the interaction key is keyboard-only
                    if not interaction_sent:
                        handoff_lap("final_foreground")
                        handoff["first_input_after_start_ms"] = round((self._monotonic() - started) * 1000.0, 3)
                    self._assert_one_off_visit_unchanged()
                    interaction_input = self.input.use_interaction(QUEEN_BLOOD_INTERACTION_MILLISECONDS)
                    forwarder.talk_started()
                    handoff["input_sent"] = True
                    interaction_attempts += 1
                    inputs.append({
                        "button": "interact",
                        "milliseconds": QUEEN_BLOOD_INTERACTION_MILLISECONDS,
                        "attempt": interaction_attempts,
                        **interaction_input,
                    })
                    interaction_sent = True
                    dialogue_settle_pending = True
                    dialogue_transition_grace = True
                    self._sleeper(
                        min(
                            QUEEN_BLOOD_DIALOGUE_SETTLE_SECONDS,
                            max(0.0, deadline - self._monotonic()),
                        )
                    )
                    latest = self.telemetry.observe()
                    if self._queen_blood_prematch_visible(latest):
                        match_screen_state = latest
                        break
                    if self._queen_blood_dialogue_active(latest):
                        break
                    if forwarder.present():
                        # Ordinary subtitles are positive opening evidence;
                        # do not repeat Talk while waiting for the choice wheel.
                        break
                    if interaction_attempt + 1 >= QUEEN_BLOOD_MAX_INTERACTION_ATTEMPTS:
                        break
                    retry_deadline = min(
                        deadline,
                        self._monotonic() + QUEEN_BLOOD_INTERACTION_RESPONSE_SECONDS,
                    )
                    try:
                        latest = self._wait_for_queen_blood_sample(
                            latest.sequence,
                            retry_deadline,
                            lambda state: self._queen_blood_dialogue_active(state)
                            or self._queen_blood_prematch_visible(state),
                            trace,
                        )
                        if self._queen_blood_prematch_visible(latest):
                            match_screen_state = latest
                            break
                        if self._queen_blood_dialogue_active(latest):
                            break
                    except SafetyError as exc:
                        if str(exc) not in {
                            "Queen's Blood state did not advance before the action deadline",
                            "Queen's Blood telemetry is stale",
                        }:
                            raise
                        # A stale sample during NPC construction is a retryable
                        # transition, not proof that the interaction failed.
                        # The next bounded attempt obtains the fresh sample;
                        # do not consume an additional state here or skip a
                        # dialogue sample in the same transaction.
                        dialogue_transition_grace = True

            if match_screen_state is None:
                if not dialogue_requested:
                    request_dialogue = getattr(self.telemetry, "request_queen_blood_dialogue", None)
                    if callable(request_dialogue):
                        if request_dialogue() is False:
                            raise SafetyError("Queen's Blood dialogue request failed")
                        dialogue_requested = True
                latest = self.telemetry.observe()
                if decline:
                    if self._queen_blood_prematch_visible(latest):
                        raise SafetyError("Queen's Blood match screen is already open; refusing decline")
                    final, no_selected = self._finish_open_conversation(
                        latest, deadline, inputs, trace, require_opening=True, dialogue_forwarder=forwarder
                    )
                    if not no_selected:
                        # Nothing was declined: a visit must not count as done.
                        raise SafetyError(
                            "no_match_prompt: the conversation ended without the Queen's Blood prompt"
                        )
                    position = final.player.position if final.player else None
                    result = {
                        "ok": True,
                        "action": "initiate_queens_blood_match",
                        "outcome": "declined",
                        "target": target,
                        "opponent": target_id,
                        "interaction_sent": interaction_sent,
                        "settled_position": ({"x": position.x, "y": position.y, "z": position.z}
                                             if position is not None else None),
                        "map_id": final.map_id,
                        "inputs": inputs,
                        "trace": trace,
                    }
                    if one_off_visit:
                        self._assert_one_off_visit_unchanged()
                        result.update(one_off_visit=True, visited_save=visit_binding["loaded_save"])
                    self._record_action(**result)
                    return result
                if self._queen_blood_dialogue_transition_active(pre_state):
                    dialogue_transition_grace = True
                # A fresh subtitle read already proves the conversation is on
                # screen. The dialogue loop below advances its lines and waits
                # for the wheel itself. Live 2026-10-12 this grace wait instead
                # held the forwarder off for up to 30 s per Talk while the NPC
                # read every line out (Talk to Yes 33 s, three clicks in about
                # fifteen initiations), so no dialogue was ever skipped.
                subtitles_seen = (
                    forwarder.enabled
                    and forwarder.seen_dialogue
                    and self._queen_blood_talk_wheel_supported(latest)
                )
                if (
                    dialogue_transition_grace
                    and not subtitles_seen
                    and not self._queen_blood_prematch_visible(latest)
                    and not self._queen_blood_dialogue_active(latest)
                ):
                    # The generic feed can expose only a transition while the
                    # dialogue widget or deck UI is still constructing.  Give
                    # the producer a bounded chance to publish pre-match before
                    # sending a default confirmation into an unready widget.
                    pre_match_or_dialogue_deadline = min(
                        deadline,
                        self._monotonic() + QUEEN_BLOOD_POST_DIALOGUE_GRACE_SECONDS,
                    )
                    try:
                        latest = self._wait_for_queen_blood_sample(
                            latest.sequence,
                            pre_match_or_dialogue_deadline,
                            lambda state: self._queen_blood_prematch_visible(state)
                            or self._queen_blood_dialogue_active(state),
                            trace,
                        )
                    except SafetyError as exc:
                        if str(exc) != "Queen's Blood state did not advance before the action deadline":
                            raise
                    if self._queen_blood_prematch_visible(latest):
                        match_screen_state = latest
                previous_sequence = latest.sequence
                if match_screen_state is None and self._queen_blood_talk_wheel_supported(latest):
                    match_screen_state = self._run_queen_blood_dialogue(
                        latest, deadline, inputs, trace, dialogue_forwarder=forwarder)
                for confirmation_index in range(dialogue_confirmations):
                    if match_screen_state is not None:
                        break
                    if self._queen_blood_prematch_visible(latest):
                        match_screen_state = latest
                        break
                    if not self._queen_blood_dialogue_or_transition_active(latest) and not dialogue_transition_grace:
                        raise SafetyError("Queen's Blood dialogue is no longer active before the match screen")
                    if dialogue_settle_pending or dialogue_transition_grace:
                        self._sleeper(
                            min(
                                QUEEN_BLOOD_DIALOGUE_SETTLE_SECONDS,
                                max(0.0, deadline - self._monotonic()),
                            )
                        )
                        dialogue_settle_pending = False
                    # A rematch asks "Challenge to a match?" with Yes on the
                    # left and No on the right, and No highlighted (live
                    # 2026-09-25). Left selects Yes and does nothing on a
                    # plain dialogue line or a Yes-first prompt.
                    self._queen_blood_press("queen_blood_left", 200, keyboard=True)
                    self._sleeper(0.3)
                    inputs.append({
                        "button": "queen_blood_left",
                        "milliseconds": 200,
                        "reason": "dialogue_select_yes",
                        "index": confirmation_index + 1,
                    })
                    self._tap_queen_blood_confirm(confirmation_milliseconds, keyboard=True)
                    inputs.append({
                        "button": "confirm",
                        "milliseconds": confirmation_milliseconds,
                        "reason": "dialogue_default_yes",
                        "index": confirmation_index + 1,
                    })
                    try:
                        latest = self._wait_for_queen_blood_sample(
                            previous_sequence,
                            deadline,
                            lambda state: self._queen_blood_dialogue_or_transition_active(state)
                            or self._queen_blood_prematch_visible(state),
                            trace,
                        )
                    except SafetyError as exc:
                        if str(exc) not in {
                            "Queen's Blood telemetry is stale",
                            "Queen's Blood state did not advance before the action deadline",
                        }:
                            raise
                        # A confirm can be consumed by the dialogue widget
                        # while the world/producer sequence is momentarily
                        # unchanged.  Treat that as the same bounded retryable
                        # transition as a stale sample; the next confirmation
                        # attempt remains gated by the explicit dialogue or
                        # transition signal.
                        dialogue_transition_grace = True
                        latest = self.telemetry.observe()
                    # Do not stack confirmations onto a changing dialogue
                    # widget.  The next loop iteration will re-check the
                    # pre-match signal first, then wait before pressing again.
                    dialogue_settle_pending = True
                    previous_sequence = latest.sequence
                if match_screen_state is None and self._queen_blood_prematch_visible(latest):
                    match_screen_state = latest
                if match_screen_state is None:
                    # The dialogue confirmations can finish before the card
                    # UI has completed its asynchronous deck construction.
                    # Observe for a short bounded grace period without sending
                    # another key; F3 remains gated by the producer signal.
                    post_dialogue_deadline = min(
                        deadline,
                        self._monotonic() + QUEEN_BLOOD_POST_DIALOGUE_GRACE_SECONDS,
                    )
                    try:
                        latest = self._wait_for_queen_blood_sample(
                            latest.sequence,
                            post_dialogue_deadline,
                            self._queen_blood_prematch_visible,
                            trace,
                        )
                        match_screen_state = latest
                    except SafetyError as exc:
                        if str(exc) != "Queen's Blood state did not advance before the action deadline":
                            raise
                if match_screen_state is None:
                    raise SafetyError(
                        "Queen's Blood match screen was not observed after the dialogue confirmation limit"
                    )

            if self._queen_blood_campaign is not None and campaign_assignment is not None:
                try:
                    self._queen_blood_campaign.record_available(campaign_assignment, target_id)
                except (OSError, ValueError):
                    pass  # Campaign persistence must not turn a verified match prompt into a gameplay failure.
            enemy = ((self._queen_blood_payload(match_screen_state) or {}).get("deck_screen") or {}).get("enemy")
            enemy_name_id = enemy.get("enemy_name") if isinstance(enemy, dict) else None
            profile = None if one_off_visit else self.queen_blood_profile()
            if profile is not None and target_id and isinstance(enemy_name_id, str):
                try:
                    profile.update_opponent(str(target_id), name_id=enemy_name_id)
                except OSError:
                    pass
            if stop_at_deck_select:
                result = {
                    "ok": True,
                    "action": "initiate_queens_blood_match",
                    "outcome": "deck_select_ready",
                    "target": target,
                    "opponent": target_id,
                    "interaction_sent": interaction_sent,
                    "pre_sequence": pre_state.sequence,
                    "match_screen_sequence": match_screen_state.sequence,
                    "queen_blood": match_screen_state.queen_blood,
                    "inputs": inputs,
                    "trace": trace,
                }
                if one_off_visit:
                    self._assert_one_off_visit_unchanged()
                    result.update(one_off_visit=True, visited_save=visit_binding["loaded_save"])
                self._record_action(**result)
                return result

            if one_off_visit:
                raise SafetyError("one-off visit cannot Begin Match")
            match_deck = MatchDeck()
            popup_state = None
            mulligan_deadline = min(
                deadline,
                self._monotonic() + QUEEN_BLOOD_MULLIGAN_GRACE_SECONDS,
            )
            previous_sequence = match_screen_state.sequence
            for begin_attempt in range(QUEEN_BLOOD_MAX_BEGIN_ATTEMPTS):
                attempt_sequence = previous_sequence
                # When the probe reports the "Start the match?" popup, F3 is
                # only sent while it is closed and Confirm only once it is
                # observed open: live, a Confirm that landed before the
                # popup was ready left it open for every retry.
                popup_open = self._queen_blood_begin_popup(
                    popup_state if popup_state is not None else match_screen_state
                )
                if popup_open is not True:
                    self._assert_deck_select_for_begin()
                    match_deck = self._match_deck_before_begin(
                        popup_state if popup_state is not None else match_screen_state)
                    self._ensure_game_foreground()  # F3 is keyboard-only
                    self.input.tap("queen_blood_begin_match", begin_milliseconds)
                    inputs.append({
                        "button": "queen_blood_begin_match",
                        "key": "F3",
                        "milliseconds": begin_milliseconds,
                        "attempt": begin_attempt + 1,
                    })
                if popup_open is None:
                    # No popup telemetry: F3 then Confirm as a fixed pair.
                    self._sleeper(
                        min(
                            QUEEN_BLOOD_DIALOGUE_SETTLE_SECONDS,
                            max(0.0, mulligan_deadline - self._monotonic()),
                        )
                    )
                elif popup_open is False:
                    opened = self._wait_for_queen_blood_sample(
                        previous_sequence,
                        min(mulligan_deadline, self._monotonic() + 6.0),
                        lambda sample: self._queen_blood_begin_popup(sample) is True,
                        trace,
                        return_latest_on_timeout=True,
                        allow_stale_queen_blood_transition=True,
                    )
                    if self._queen_blood_begin_popup(opened) is not True:
                        if opened is not None:
                            popup_state = opened
                            previous_sequence = opened.sequence
                        continue
                    # Let the popup finish opening before Yes.
                    self._sleeper(0.8)
                self._tap_queen_blood_confirm(
                    confirmation_milliseconds,
                    allow_stale_queen_blood_transition=True,
                    keyboard=True,
                )
                inputs.append({
                    "button": "confirm",
                    "milliseconds": confirmation_milliseconds,
                    "reason": "start_match_default_yes_after_begin_attempt",
                    "attempt": begin_attempt + 1,
                })
                # The match can start even when the transition read below
                # fails (a probe ticket timeout); keep this attempt's deck
                # evidence with it instead of losing it with the job.
                self._queen_blood_match = {
                    "opponent": target_id, "started": self._monotonic(),
                    "started_at": self._wall_clock().timestamp(), "deck_evidence": match_deck,
                }
                try:
                    popup_state = self._wait_for_queen_blood_sample(
                        previous_sequence,
                        min(
                            mulligan_deadline,
                            self._monotonic() + QUEEN_BLOOD_BEGIN_RETRY_SECONDS,
                        ),
                        self._queen_blood_has_identified_cards,
                        trace,
                        return_latest_on_timeout=True,
                        allow_stale_queen_blood_transition=True,
                    )
                except SafetyError as exc:
                    if str(exc) != "Queen's Blood telemetry is stale":
                        raise
                    # The pair has already been sent against a validated
                    # pre-match screen.  Re-observe the bounded transition;
                    # the next attempt remains limited to this same screen.
                    popup_state = self.telemetry.observe()
                if popup_state is None or popup_state.sequence <= attempt_sequence:
                    raise SafetyError(
                        "Queen's Blood telemetry did not advance after Begin Match; "
                        "refusing another input"
                    )
                if self._queen_blood_has_identified_cards(popup_state):
                    break
                if self._queen_blood_payload(popup_state) is None:
                    raise SafetyError(
                        "Queen's Blood sidecar is no longer fresh after Begin Match; "
                        "refusing another input"
                    )
                if not self._queen_blood_prematch_visible(popup_state):
                    raise SafetyError(
                        "Queen's Blood pre-match screen is no longer visible after "
                        "Begin Match; refusing another input"
                    )
                previous_sequence = popup_state.sequence
            if self._queen_blood_has_identified_cards(popup_state):
                self._queen_blood_mulligan_inferred_sequence = popup_state.sequence
                payload = self._queen_blood_payload(popup_state)
                raw_hand = payload.get("hand") if isinstance(payload, dict) else None
                self._queen_blood_mulligan_inferred_hand = (
                    [dict(card) for card in raw_hand if isinstance(card, dict)]
                    if isinstance(raw_hand, list) else None
                )
                self._queen_blood_mulligan_inferred_basis = "identified_hand_after_begin_match"
                inferred_payload = self._queen_blood_inferred_mulligan_payload(popup_state)
                result = {
                    "ok": True,
                    "action": "initiate_queens_blood_match",
                    "outcome": "mulligan_ready",
                    "state_inference": "identified_hand_after_begin_match",
                    "target": target,
                    "opponent": target_id,
                    "interaction_sent": interaction_sent,
                    "dialogue_confirmations_sent": sum(
                        item.get("reason") == "dialogue_default_yes" for item in inputs
                    ),
                    "pre_sequence": pre_state.sequence,
                    "match_screen_sequence": match_screen_state.sequence,
                    "post_sequence": popup_state.sequence,
                    "queen_blood": inferred_payload or popup_state.queen_blood,
                    "inputs": inputs,
                    "trace": trace,
                }
                match_payload = self._queen_blood_payload(popup_state) or {}
                native_match_id = match_payload.get("match_id") or match_payload.get("matchId")
                self._queen_blood_match["story_id"] = str(
                    native_match_id or f"qb-{target_id}-{uuid.uuid4().hex}"
                )
                self._production_qb_event_ids = []
                self._production_qb_event_counter = 0
                self._queen_blood_last_turn_payload = None
                self._observe_match_deck(result["queen_blood"])
                result.update(match_deck.record())
                self._record_action(**result)
                return result
            raise SafetyError(
                "Queen's Blood hand identities did not appear after "
                f"{QUEEN_BLOOD_MAX_BEGIN_ATTEMPTS} Begin Match attempts"
            )
        except Exception as exc:
            yes_confirmed = str(exc) == QUEEN_BLOOD_DECK_AFTER_YES_TIMEOUT
            if yes_confirmed or str(exc).startswith("the dialogue wheel did not appear before the timeout"):
                # A live wheel can survive a missed UE4SS widget scan. Do not
                # assert the NPC declined or retry Talk/Confirm into that UI.
                # Capture while this transaction still owns the action lock.
                try:
                    fresh = self.telemetry.observe()
                    age = fresh.age_seconds()
                    is_fresh = fresh.source == "simulation" or age < self.max_telemetry_age
                    wheel = self._queen_blood_talk_wheel(fresh) or {}
                    live = is_fresh and fresh.running and not getattr(fresh, "loading", False)
                    wheel_visible = live and self._queen_blood_talk_wheel_shown(fresh)
                    dialogue_open = live and self._queen_blood_dialogue_or_transition_active(fresh)
                    deck_visible = live and self._queen_blood_prematch_visible(fresh)
                    observation = {
                        "sequence": fresh.sequence,
                        "age_seconds": age,
                        "fresh": is_fresh,
                        "dialogue_active": self._queen_blood_dialogue_active(fresh),
                        "transition_active": self._queen_blood_dialogue_transition_active(fresh),
                        "wheel_observed": wheel.get("observed") is True,
                        "wheel_shown": wheel_visible,
                        "wheel_cursor": wheel.get("cursor"),
                    }
                except Exception as observation_error:
                    wheel_visible = dialogue_open = deck_visible = False
                    observation = {"fresh": False, "error": str(observation_error)}
                try:
                    capture = self._capture_game_window_under_action()
                    capture["interpretation"] = "captured_only_not_semantically_classified"
                except Exception as capture_error:
                    capture = {"ok": False, "reason": str(capture_error)}
                pending = bool(wheel_visible or dialogue_open or deck_visible)
                outcome = ("deck_select_pending_review" if deck_visible else
                           "dialogue_pending_review" if pending else "dialogue_timeout")
                evidence = {
                    "at": queen_blood_now(),
                    "operation": "initiate_queens_blood_match",
                    "target_id": target_id,
                    "error": str(exc),
                    "interaction_sent": interaction_sent,
                    "telemetry": observation,
                    "screenshot": capture,
                    "trace": trace,
                }
                if (not pending and self._queen_blood_campaign is not None
                        and campaign_assignment is not None):
                    try:
                        self._queen_blood_campaign.record_blocked(campaign_assignment, target_id, evidence)
                    except (OSError, ValueError):
                        pass
                result = {
                    "ok": False,
                    "action": "initiate_queens_blood_match",
                    "outcome": outcome,
                    "prompt_state": ("deck_select_visible" if deck_visible else
                                     "wheel_visible" if wheel_visible else
                                     "dialogue_or_transition_active" if dialogue_open else "unverified"),
                    "opponent": target_id,
                    "target": target,
                    "interaction_sent": interaction_sent,
                    "inputs": inputs,
                    "trace": trace,
                    "telemetry_evidence": observation,
                    "visual_evidence": capture,
                    "recovery_required": True,
                    "next_step": ("Deck selection is visible; inspect the live UI and resume from deck select."
                                  if deck_visible else
                                  "Yes was confirmed, so the game may already show deck select although "
                                  "the probe did not report it. Inspect the screenshot; do not send another "
                                  "Talk or Confirm."
                                  if yes_confirmed else
                                  "Inspect the screenshot and live wheel before confirming; "
                                  "do not send another Talk or blind Confirm."),
                    "rejection_reason": str(exc),
                    "campaign_state": None if pending else "blocked",
                }
                self._record_action(**result)
                return result
            campaign_state = classify_initiation_failure(str(exc))
            if (
                campaign_state is not None
                and self._queen_blood_campaign is not None
                and campaign_assignment is not None
            ):
                evidence = {
                    "at": queen_blood_now(),
                    "operation": "initiate_queens_blood_match",
                    "target_id": target_id,
                    "error": str(exc),
                    "interaction_sent": interaction_sent,
                    "pre_sequence": pre_state.sequence if pre_state is not None else None,
                    "match_screen_sequence": (
                        match_screen_state.sequence if match_screen_state is not None else None
                    ),
                    "trace": trace,
                }
                try:
                    if campaign_state == "locked":
                        self._queen_blood_campaign.record_locked(campaign_assignment, target_id, evidence)
                    else:
                        self._queen_blood_campaign.record_blocked(campaign_assignment, target_id, evidence)
                except (OSError, ValueError):
                    pass  # Preserve the original harness failure.
            self._record_action(
                action="initiate_queens_blood_match",
                ok=False,
                opponent=target_id,
                interaction_sent=interaction_sent,
                pre_sequence=pre_state.sequence if pre_state is not None else None,
                match_screen_sequence=(
                    match_screen_state.sequence if match_screen_state is not None else None
                ),
                inputs=inputs,
                trace=trace,
                rejection_reason=str(exc),
                campaign_state=campaign_state,
            )
            raise
        finally:
            if dialogue_requested and (decline or match_screen_state is None):
                release_probe = getattr(self.telemetry, "release_queen_blood_probe", None)
                if callable(release_probe):
                    release_probe()
            self._one_off_visit_binding = None
            if borrowed_from is not None:
                restore = getattr(self._environment, "restore_foreground_window", None)
                if callable(restore):
                    try:
                        restore(borrowed_from)
                    except Exception:
                        pass
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def startup_tap(
        self,
        button: str,
        milliseconds: int = 500,
        *,
        expected_labels: tuple[str, ...] = tuple(STARTUP_VISUAL_LABELS),
    ) -> dict[str, Any]:
        """Confirm a positively classified title/menu frame before telemetry exists.

        Rebirth can render its title prompt before UE4SS has published the
        first live snapshot.  This is intentionally a separate, very narrow
        path: it captures the foreground game window immediately before the
        input and accepts only the allow-listed startup labels/buttons.  No
        gameplay or general menu action may use this telemetry-free bypass.
        """
        if not getattr(self.input, "is_real", False):
            raise SafetyError("startup visual input requires the Windows backend")
        if button not in STARTUP_VISUAL_BUTTONS:
            raise SafetyError("startup visual input only permits confirm or cancel")
        milliseconds = validate_duration(milliseconds)
        labels = {str(label) for label in expected_labels}
        if not labels or not labels.issubset(STARTUP_VISUAL_LABELS):
            raise SafetyError("startup visual labels are not allow-listed")
        evidence = self.capture_game_window()
        frame = evidence.get("startup_frame")
        label = frame.get("label") if isinstance(frame, dict) else None
        if label not in labels:
            raise SafetyError(
                f"startup visual input refused: expected {sorted(labels)}, got {label!r}"
            )
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            if self._session_lease is not None and self._session_lease.status().get("active"):
                self._rearm_active_session()
            if self._stopped:
                raise SafetyError(self._latched_stop_message())
            if not self._environment.game_process_exists():
                raise SafetyError("FFVII Rebirth process was not found")
            if not self._input_reachable():
                raise SafetyError("FFVII Rebirth is not the foreground window")
            armed, _ = self._arm.status() if self._arm else (False, 0.0)
            if not armed:
                raise SafetyError("real input is not manually armed")
            if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
                raise SafetyError("minimum delay between menu actions has not elapsed")
            self.input.tap(button, milliseconds)
            result = {
                "ok": True,
                "sequence": evidence.get("telemetry_sequence"),
                "action": "startup_visual_tap",
                "button": button,
                "mode": "startup_visual",
                "startup_frame_label": label,
                "image_path": evidence.get("image_path"),
            }
            self._record_action(**result)
            self._last_action_finished = self._monotonic()
            return result
        finally:
            self._action_lock.release()

    def recover_session_control(
        self, milliseconds: int = 500,
        timeout_seconds: float = SESSION_RECOVERY_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Clear a stuck pause/menu state with at most two menu toggles.

        This is deliberately separate from ordinary menu navigation. It is a
        bounded recovery action, consumes the general arm before sending any
        key, and always leaves the emergency stop latched.
        """
        if not getattr(self.input, "is_real", False):
            raise SafetyError("session-control recovery requires the Windows backend")
        try:
            milliseconds = validate_duration(milliseconds)
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid session-control recovery options") from exc
        if not 1.0 <= timeout_seconds <= 5.0:
            raise SafetyError("session-control recovery timeout must be between 1 and 5 seconds")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        result: dict[str, Any] = {
            "ok": False,
            "outcome": "failed",
            "toggles": 0,
            "timeout_seconds": timeout_seconds,
        }
        pre = None
        latest = None
        error: Exception | None = None
        try:
            pre = self._assert_real_menu_input_allowed("menu", milliseconds)
            if pre.player is None or pre.player.position is None or not pre.map_id:
                raise SafetyError("session-control recovery requires loaded gameplay")
            initial_active = not pre.paused and not pre.in_menu
            result["pre_sequence"] = pre.sequence
            result["pre_paused"] = pre.paused
            result["pre_in_menu"] = pre.in_menu
            if self._arm:
                self._arm.clear()

            deadline = self._monotonic() + timeout_seconds
            previous_sequence = pre.sequence
            for _ in range(SESSION_RECOVERY_MAX_TOGGLES):
                self.input.tap("menu", milliseconds)
                result["toggles"] += 1
                self.input.release_all()
                transition_deadline = min(
                    deadline,
                    self._monotonic() + max(
                        SESSION_RECOVERY_POLL_SECONDS * 4,
                        timeout_seconds / SESSION_RECOVERY_MAX_TOGGLES,
                    ),
                )
                while self._monotonic() < transition_deadline:
                    candidate = self.telemetry.observe()
                    if candidate.sequence > previous_sequence:
                        latest = candidate
                        previous_sequence = candidate.sequence
                        if not candidate.paused and not candidate.in_menu:
                            if not initial_active or result["toggles"] >= 2:
                                break
                        if initial_active and result["toggles"] == 1:
                            break
                    self._sleeper(SESSION_RECOVERY_POLL_SECONDS)
                if latest is not None and not latest.paused and not latest.in_menu:
                    if not initial_active or result["toggles"] >= 2:
                        break
                if self._monotonic() >= deadline:
                    break

            if latest is None:
                latest = self.telemetry.observe()
            telemetry_ready = bool(
                latest.running
                and (
                    latest.source == "simulation"
                    or latest.age_seconds() <= self.max_telemetry_age
                )
            )
            recovered = telemetry_ready and not latest.paused and not latest.in_menu
            result.update({
                "ok": recovered,
                "outcome": "recovered" if recovered else "still_blocked",
                "post_sequence": latest.sequence,
                "post_paused": latest.paused,
                "post_in_menu": latest.in_menu,
                "post_telemetry_age_seconds": latest.age_seconds(),
            })
        except Exception as exc:
            error = exc
            result["outcome"] = "safety_gate_rejected"
            result["rejection_reason"] = str(exc)
        finally:
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                if self._bench_arm:
                    self._bench_arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        result["emergency_stop"] = True
        self._record_action(action="recover_session_control", **result)
        if error is not None:
            raise error
        return result

    def probe_forward_movement(
        self, milliseconds: int = MOVEMENT_PROBE_DEFAULT_MS
    ) -> dict[str, Any]:
        """Hold one bounded adaptive forward probe and classify displacement."""
        if not getattr(self.input, "is_real", False):
            raise SafetyError("movement probe requires the Windows backend")
        try:
            milliseconds = int(milliseconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("movement probe duration must be an integer") from exc
        if not 100 <= milliseconds <= MOVEMENT_PROBE_MAX_MS:
            raise SafetyError(
                f"movement probe duration must be between 100 and {MOVEMENT_PROBE_MAX_MS} ms"
            )
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        result: dict[str, Any] = {
            "ok": False,
            "outcome": "failed",
            "milliseconds": milliseconds,
        }
        error: Exception | None = None
        try:
            pre = self._diagnostic_movement_gate_with_retry(require_armed=True)
            if pre.player is None or pre.player.position is None:
                raise SafetyError("movement probe requires player spatial telemetry")
            if self._arm:
                self._arm.clear()
            latest = pre

            def stop_requested() -> str | None:
                nonlocal latest
                try:
                    candidate = self.telemetry.observe()
                except Exception:
                    return None
                if candidate.sequence <= latest.sequence:
                    return None
                latest = candidate
                if candidate.player is None or candidate.player.position is None:
                    return None
                displacement = math.hypot(
                    candidate.player.position.x - pre.player.position.x,
                    candidate.player.position.y - pre.player.position.y,
                ) / 100.0
                if displacement > ADAPTIVE_FORWARD_RELEASE_DISPLACEMENT_METRES:
                    return "movement_observed"
                return None

            adaptive = self.input.adaptive_forward(
                stop_requested,
                maximum_ms=milliseconds,
                poll_seconds=0.025,
            )
            self.input.release_all()
            post = self._diagnostic_advanced_state(pre.sequence, latest)
            displacement = math.hypot(
                post.player.position.x - pre.player.position.x,
                post.player.position.y - pre.player.position.y,
            ) / 100.0
            result.update({
                "ok": displacement > ADAPTIVE_FORWARD_RELEASE_DISPLACEMENT_METRES,
                "outcome": (
                    "movement_observed"
                    if displacement > ADAPTIVE_FORWARD_RELEASE_DISPLACEMENT_METRES
                    else "no_movement"
                ),
                "pre_sequence": pre.sequence,
                "post_sequence": post.sequence,
                "horizontal_displacement": displacement,
                "pre_position": {
                    "x": pre.player.position.x,
                    "y": pre.player.position.y,
                    "z": pre.player.position.z,
                },
                "post_position": {
                    "x": post.player.position.x,
                    "y": post.player.position.y,
                    "z": post.player.position.z,
                },
                "release_reason": adaptive["release_reason"],
                "actual_hold_ms": adaptive["actual_hold_ms"],
            })
        except Exception as exc:
            error = exc
            result["outcome"] = "safety_gate_rejected"
            result["rejection_reason"] = str(exc)
        finally:
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                if self._bench_arm:
                    self._bench_arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        result["emergency_stop"] = True
        self._record_action(action="probe_forward_movement", **result)
        if error is not None:
            raise error
        return result

    def travel_forward_chunk(
        self,
        maximum_ms: int = ADAPTIVE_TRAVEL_DEFAULT_MS,
        target_distance_m: float | None = None,
    ) -> dict[str, Any]:
        """Hold forward for a measured travel chunk, stopping only at a goal or safety change.

        Unlike the short movement probe, this deliberately does not release on the
        first small displacement. The caller chooses a bounded time budget and may
        optionally provide the approximate horizontal distance to cover. Telemetry
        still stops the hold if the game pauses, changes map, enters combat, or the
        requested distance is reached.
        """
        try:
            maximum_ms = int(maximum_ms)
        except (TypeError, ValueError) as exc:
            raise SafetyError("travel chunk duration must be an integer") from exc
        if not 100 <= maximum_ms <= ADAPTIVE_TRAVEL_MAXIMUM_MS:
            raise SafetyError(
                f"travel chunk duration must be between 100 and {ADAPTIVE_TRAVEL_MAXIMUM_MS} ms"
            )
        if target_distance_m is not None:
            try:
                target_distance_m = float(target_distance_m)
            except (TypeError, ValueError) as exc:
                raise SafetyError("target_distance_m must be numeric") from exc
            if not math.isfinite(target_distance_m) or not 0.1 <= target_distance_m <= ADAPTIVE_TRAVEL_MAX_DISTANCE_METRES:
                raise SafetyError(
                    f"target_distance_m must be between 0.1 and {ADAPTIVE_TRAVEL_MAX_DISTANCE_METRES} metres"
                )
        self._require_guarded_benchmark_movement(None)
        if not getattr(self.input, "is_real", False):
            raise SafetyError("adaptive travel requires the Windows backend")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        result: dict[str, Any] = {
            "ok": False,
            "outcome": "failed",
            "maximum_ms": maximum_ms,
            "target_distance_m": target_distance_m,
        }
        error: Exception | None = None
        try:
            pre = self._diagnostic_movement_gate_with_retry(require_armed=True)
            if pre.player is None or pre.player.position is None:
                raise SafetyError("adaptive travel requires player spatial telemetry")
            if self._arm:
                self._arm.clear()
            latest = pre
            baseline_map_id = pre.map_id

            def stop_requested() -> str | None:
                nonlocal latest
                try:
                    candidate = self.telemetry.observe()
                except Exception as exc:
                    if self._is_permission_read_error(exc):
                        return None
                    raise
                if candidate.sequence <= latest.sequence:
                    return None
                latest = candidate
                if not candidate.running or candidate.paused or candidate.in_menu:
                    return "gameplay_state_changed"
                if candidate.map_id != baseline_map_id:
                    return "map_changed"
                if candidate.in_combat or candidate.target is not None:
                    return "combat_or_target_detected"
                if candidate.player is None or candidate.player.position is None:
                    return None
                displacement = math.hypot(
                    candidate.player.position.x - pre.player.position.x,
                    candidate.player.position.y - pre.player.position.y,
                ) / 100.0
                if target_distance_m is not None and displacement >= target_distance_m:
                    return "travel_distance_reached"
                return None

            adaptive = self.input.adaptive_forward(
                stop_requested,
                maximum_ms=maximum_ms,
                poll_seconds=ADAPTIVE_TRAVEL_POLL_SECONDS,
            )
            self.input.release_all()
            try:
                post = self._diagnostic_advanced_state(pre.sequence, latest)
            except SafetyError:
                if latest.sequence <= pre.sequence or latest.player is None or latest.player.position is None:
                    raise
                post = latest
            displacement = math.hypot(
                post.player.position.x - pre.player.position.x,
                post.player.position.y - pre.player.position.y,
            ) / 100.0
            release_reason = str(adaptive["release_reason"])
            result.update({
                "ok": displacement > ADAPTIVE_FORWARD_RELEASE_DISPLACEMENT_METRES,
                "outcome": release_reason,
                "pre_sequence": pre.sequence,
                "post_sequence": post.sequence,
                "horizontal_displacement": displacement,
                "pre_position": {
                    "x": pre.player.position.x,
                    "y": pre.player.position.y,
                    "z": pre.player.position.z,
                },
                "post_position": {
                    "x": post.player.position.x,
                    "y": post.player.position.y,
                    "z": post.player.position.z,
                },
                "release_reason": release_reason,
                "actual_hold_ms": adaptive["actual_hold_ms"],
            })
        except Exception as exc:
            error = exc
            result["outcome"] = "safety_gate_rejected"
            result["rejection_reason"] = str(exc)
        finally:
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                if self._bench_arm:
                    self._bench_arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        result["emergency_stop"] = True
        self._record_action(action="travel_forward_chunk", **result)
        if error is not None:
            raise error
        return result

    def _require_guarded_benchmark_movement(
        self,
        predictive_plan: dict[str, Any] | None,
    ) -> None:
        """Keep benchmark movement on the evidence-fusion path.

        Raw movement remains useful for diagnostics and manual recovery, but
        an active benchmark must measure the reusable navigation policy.  A
        direct W/S/A/D call can otherwise bypass the fresh capsule veto and
        turn a visually plausible rock branch into a benchmark action.
        """
        try:
            active = bool(self._chadley_benchmark.status().get("active"))
        except BenchmarkError as exc:
            raise SafetyError(f"cannot read benchmark guard state: {exc}") from exc
        if not active:
            return
        if not isinstance(predictive_plan, dict):
            raise SafetyError(
                "active Chadley benchmark requires navigate_next_segment or "
                "travel_planned_chunk evidence; raw movement is blocked"
            )
        coordinator = predictive_plan.get("coordinator")
        if coordinator != "evidence_fusion" and not predictive_plan.get("motion_plan"):
            raise SafetyError(
                "active Chadley benchmark movement lacks coordinator/predictive evidence"
            )

    def travel_direction_chunk(
        self,
        direction: str,
        maximum_ms: int = ADAPTIVE_TRAVEL_DEFAULT_MS,
        target_distance_m: float | None = None,
        *,
        predictive_plan: dict[str, Any] | None = None,
        sprint: bool = False,
    ) -> dict[str, Any]:
        """Hold one cardinal W/S/A/D direction for a bounded travel chunk.

        ``sprint`` adds the configured sprint modifier for the duration of the
        same adaptive hold; ordinary calls remain non-sprinting.
        """
        vectors = {
            "forward": (0.0, 1.0), "w": (0.0, 1.0),
            "backward": (0.0, -1.0), "back": (0.0, -1.0), "s": (0.0, -1.0),
            "left": (-1.0, 0.0), "a": (-1.0, 0.0),
            "right": (1.0, 0.0), "d": (1.0, 0.0),
        }
        key = str(direction).strip().lower()
        if key not in vectors:
            raise SafetyError("direction must be forward, backward, left, or right")
        try:
            maximum_ms = int(maximum_ms)
        except (TypeError, ValueError) as exc:
            raise SafetyError("travel chunk duration must be an integer") from exc
        if not 100 <= maximum_ms <= ADAPTIVE_TRAVEL_MAXIMUM_MS:
            raise SafetyError(
                f"travel chunk duration must be between 100 and {ADAPTIVE_TRAVEL_MAXIMUM_MS} ms"
            )
        if target_distance_m is not None:
            try:
                target_distance_m = float(target_distance_m)
            except (TypeError, ValueError) as exc:
                raise SafetyError("target_distance_m must be numeric") from exc
            if not math.isfinite(target_distance_m) or not 0.1 <= target_distance_m <= ADAPTIVE_TRAVEL_MAX_DISTANCE_METRES:
                raise SafetyError(
                    f"target_distance_m must be between 0.1 and {ADAPTIVE_TRAVEL_MAX_DISTANCE_METRES} metres"
                )
        self._require_guarded_benchmark_movement(predictive_plan)
        if not getattr(self.input, "is_real", False) or not hasattr(self.input, "adaptive_move"):
            raise SafetyError("adaptive cardinal travel requires the Windows backend")
        phase_allowed, phase_acquired = self._acquire_local_episode_phase()
        if not phase_allowed:
            raise SafetyError("another input action is executing")
        result: dict[str, Any] = {
            "ok": False, "outcome": "failed", "direction": (
                "forward" if key in {"forward", "w"} else
            "backward" if key in {"backward", "back", "s"} else
                "left" if key in {"left", "a"} else "right"
            ), "maximum_ms": maximum_ms, "target_distance_m": target_distance_m,
            "sprint": bool(sprint),
        }
        error: Exception | None = None
        try:
            pre = self._diagnostic_movement_gate_with_retry(require_armed=True)
            if pre.player is None or pre.player.position is None:
                raise SafetyError("adaptive travel requires player spatial telemetry")
            if self._arm:
                self._arm.clear()
            latest = pre
            baseline_map_id = pre.map_id

            def stop_requested() -> str | None:
                nonlocal latest
                candidate = self.telemetry.observe()
                if candidate.sequence <= latest.sequence:
                    return None
                latest = candidate
                if not candidate.running or candidate.paused or candidate.in_menu:
                    return "gameplay_state_changed"
                if candidate.map_id != baseline_map_id:
                    return "map_changed"
                if candidate.in_combat or candidate.target is not None:
                    return "combat_or_target_detected"
                if candidate.player is None or candidate.player.position is None:
                    return None
                displacement = math.hypot(
                    candidate.player.position.x - pre.player.position.x,
                    candidate.player.position.y - pre.player.position.y,
                ) / 100.0
                if target_distance_m is not None and displacement >= target_distance_m:
                    return "travel_distance_reached"
                return None

            adaptive = self.input.adaptive_move(
                *vectors[key], stop_requested,
                maximum_ms=maximum_ms,
                poll_seconds=ADAPTIVE_TRAVEL_POLL_SECONDS,
                sprint=sprint,
            )
            self.input.release_all()
            post = self._diagnostic_advanced_state(pre.sequence, latest)
            displacement = math.hypot(
                post.player.position.x - pre.player.position.x,
                post.player.position.y - pre.player.position.y,
            ) / 100.0
            result.update({
                "ok": displacement > ADAPTIVE_FORWARD_RELEASE_DISPLACEMENT_METRES,
                "outcome": str(adaptive["release_reason"]),
                "pre_sequence": pre.sequence,
                "post_sequence": post.sequence,
                "horizontal_displacement": displacement,
                "pre_position": {"x": pre.player.position.x, "y": pre.player.position.y, "z": pre.player.position.z},
                "post_position": {"x": post.player.position.x, "y": post.player.position.y, "z": post.player.position.z},
                "release_reason": adaptive["release_reason"],
                "actual_hold_ms": adaptive["actual_hold_ms"],
                "sprint": bool(adaptive.get("sprint", sprint)),
            })
            if predictive_plan is not None:
                result["predictive_plan"] = predictive_plan
        except Exception as exc:
            error = exc
            result["outcome"] = "safety_gate_rejected"
            result["rejection_reason"] = str(exc)
        finally:
            try:
                self.input.release_all()
            finally:
                if self._local_episode_owner != get_ident():
                    if self._arm:
                        self._arm.latch_stop()
                    if self._bench_arm:
                        self._bench_arm.latch_stop()
                    with self._lock:
                        self._stopped = True
                self._last_action_finished = self._monotonic()
                self._release_local_episode_phase(phase_acquired)
        result["emergency_stop"] = True
        self._record_navigation_trace(result, predictive_plan)
        self._record_action(action="travel_direction_chunk", **result)
        if error is not None:
            raise error
        return result

    def travel_sprint_chunk(
        self,
        direction: str,
        maximum_ms: int = ADAPTIVE_TRAVEL_DEFAULT_MS,
        target_distance_m: float | None = None,
    ) -> dict[str, Any]:
        """Convenience wrapper for an explicitly sprinting cardinal chunk."""
        return self.travel_direction_chunk(
            direction,
            maximum_ms,
            target_distance_m,
            sprint=True,
        )

    def travel_vector_chunk(
        self,
        x: float,
        y: float,
        maximum_ms: int = ADAPTIVE_TRAVEL_DEFAULT_MS,
        target_distance_m: float | None = None,
        *,
        predictive_plan: dict[str, Any] | None = None,
        sprint: bool = False,
    ) -> dict[str, Any]:
        """Hold a normalized W/S/A/D vector, optionally sprinting."""
        try:
            x, y = normalise_axes(x, y)
            maximum_ms = int(maximum_ms)
        except (TypeError, ValueError) as exc:
            raise SafetyError("travel vector parameters are invalid") from exc
        if abs(x) < 0.2 and abs(y) < 0.2:
            raise SafetyError("travel vector requires a non-zero direction")
        if not 100 <= maximum_ms <= ADAPTIVE_TRAVEL_MAXIMUM_MS:
            raise SafetyError(
                f"travel vector duration must be between 100 and {ADAPTIVE_TRAVEL_MAXIMUM_MS} ms"
            )
        if target_distance_m is not None:
            try:
                target_distance_m = float(target_distance_m)
            except (TypeError, ValueError) as exc:
                raise SafetyError("target_distance_m must be numeric") from exc
            if not math.isfinite(target_distance_m) or not 0.1 <= target_distance_m <= ADAPTIVE_TRAVEL_MAX_DISTANCE_METRES:
                raise SafetyError(
                    f"target_distance_m must be between 0.1 and {ADAPTIVE_TRAVEL_MAX_DISTANCE_METRES} metres"
                )
        self._require_guarded_benchmark_movement(predictive_plan)
        if not getattr(self.input, "is_real", False) or not hasattr(self.input, "adaptive_move"):
            raise SafetyError("adaptive vector travel requires the Windows backend")
        phase_allowed, phase_acquired = self._acquire_local_episode_phase()
        if not phase_allowed:
            raise SafetyError("another input action is executing")
        result: dict[str, Any] = {
            "ok": False,
            "outcome": "failed",
            "action": "travel_vector_chunk",
            "x": x,
            "y": y,
            "maximum_ms": maximum_ms,
            "target_distance_m": target_distance_m,
            "sprint": bool(sprint),
        }
        error: Exception | None = None
        try:
            pre = self._diagnostic_movement_gate_with_retry(require_armed=True)
            if pre.player is None or pre.player.position is None:
                raise SafetyError("adaptive vector travel requires player spatial telemetry")
            if self._arm:
                self._arm.clear()
            latest = pre
            baseline_map_id = pre.map_id

            def stop_requested() -> str | None:
                nonlocal latest
                candidate = self.telemetry.observe()
                if candidate.sequence <= latest.sequence:
                    return None
                latest = candidate
                if not candidate.running or candidate.paused or candidate.in_menu:
                    return "gameplay_state_changed"
                if candidate.map_id != baseline_map_id:
                    return "map_changed"
                if candidate.in_combat or candidate.target is not None:
                    return "combat_or_target_detected"
                if candidate.player is None or candidate.player.position is None:
                    return None
                displacement = math.hypot(
                    candidate.player.position.x - pre.player.position.x,
                    candidate.player.position.y - pre.player.position.y,
                ) / 100.0
                if target_distance_m is not None and displacement >= target_distance_m:
                    return "travel_distance_reached"
                return None

            adaptive = self.input.adaptive_move(
                x,
                y,
                stop_requested,
                maximum_ms=maximum_ms,
                poll_seconds=ADAPTIVE_TRAVEL_POLL_SECONDS,
                sprint=sprint,
            )
            self.input.release_all()
            post = self._diagnostic_advanced_state(pre.sequence, latest)
            displacement = math.hypot(
                post.player.position.x - pre.player.position.x,
                post.player.position.y - pre.player.position.y,
            ) / 100.0
            result.update({
                "ok": displacement > ADAPTIVE_FORWARD_RELEASE_DISPLACEMENT_METRES,
                "outcome": str(adaptive["release_reason"]),
                "pre_sequence": pre.sequence,
                "post_sequence": post.sequence,
                "horizontal_displacement": displacement,
                "pre_position": {"x": pre.player.position.x, "y": pre.player.position.y, "z": pre.player.position.z},
                "post_position": {"x": post.player.position.x, "y": post.player.position.y, "z": post.player.position.z},
                "release_reason": adaptive["release_reason"],
                "actual_hold_ms": adaptive["actual_hold_ms"],
                "keys": adaptive.get("keys"),
                "sprint": bool(adaptive.get("sprint", sprint)),
            })
            if predictive_plan is not None:
                result["predictive_plan"] = predictive_plan
        except Exception as exc:
            error = exc
            result["outcome"] = "safety_gate_rejected"
            result["rejection_reason"] = str(exc)
        finally:
            try:
                self.input.release_all()
            finally:
                if self._local_episode_owner != get_ident():
                    if self._arm:
                        self._arm.latch_stop()
                    if self._bench_arm:
                        self._bench_arm.latch_stop()
                    with self._lock:
                        self._stopped = True
                self._last_action_finished = self._monotonic()
                self._release_local_episode_phase(phase_acquired)
        result["emergency_stop"] = True
        self._record_navigation_trace(result, predictive_plan)
        self._record_action(**result)
        if error is not None:
            raise error
        return result

    def _assert_real_menu_input_allowed(
        self,
        button: str,
        milliseconds: int,
        *,
        allow_queen_blood_dialogue_transition: bool = False,
        allow_stale_queen_blood_transition: bool = False,
        background_pad: bool = False,
        allow_combat_tip: bool = False,
    ) -> Any:
        if button not in MENU_INPUT_BUTTONS:
            raise SafetyError("only bounded menu navigation buttons are enabled")
        milliseconds = validate_duration(milliseconds)
        if (button == "map" or button in MAP_INPUT_BUTTONS) and milliseconds < MAP_BUTTON_MINIMUM_MS:
            raise SafetyError("map controls require a hold of at least 500 ms")
        state = self._assert_action_allowed(
            allow_paused=True,
            allow_queen_blood_dialogue_transition=allow_queen_blood_dialogue_transition,
            allow_stale_queen_blood_transition=allow_stale_queen_blood_transition,
        )
        if state.source == "simulation":
            raise SafetyError("menu input requires live game telemetry")
        if (state.in_combat or state.target is not None) and not allow_combat_tip:
            raise SafetyError("menu input blocked during combat or with a selected target")
        loaded_gameplay = (
            state.map_id is not None
            and state.player is not None
            and getattr(state.player, "position", None) is not None
        )
        if (
            loaded_gameplay
            and not state.in_menu
            and not state.paused
            and button not in {"menu", "map"}
            and not allow_queen_blood_dialogue_transition
            and not allow_combat_tip
        ):
            raise SafetyError("menu input blocked during loaded gameplay")
        if not self._environment.game_process_exists():
            raise SafetyError("FFVII Rebirth process was not found")
        if not (
            self._input_reachable()
            or (background_pad and self._queen_blood_pad_route(button))
        ):
            raise SafetyError("FFVII Rebirth is not the foreground window")
        armed, _ = self._arm.status() if self._arm else (False, 0.0)
        if not armed:
            raise SafetyError("real input is not manually armed")
        if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
            raise SafetyError("minimum delay between menu actions has not elapsed")
        return state

    def _assert_real_combat_action_allowed(self, button: str, milliseconds: int) -> Any:
        if button not in TARGET_GATED_COMBAT_BUTTONS:
            raise SafetyError("only the target-gated attack button is enabled")
        milliseconds = validate_duration(milliseconds)
        state = self._assert_action_allowed()
        if state.source == "simulation":
            raise SafetyError("combat action requires a live game telemetry source")
        if state.in_menu:
            raise SafetyError("combat action blocked because a menu is open")
        if state.target is None:
            raise SafetyError("combat action requires a selected target")
        if not self._environment.game_process_exists():
            raise SafetyError("FFVII Rebirth process was not found")
        if not self._input_reachable():
            raise SafetyError("FFVII Rebirth is not the foreground window")
        armed, _ = self._arm.status() if self._arm else (False, 0.0)
        if not armed:
            raise SafetyError("real input is not manually armed")
        if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
            raise SafetyError("minimum delay between combat actions has not elapsed")
        return state

    def combat_tap(self, button: str = "attack", milliseconds: int = 80) -> dict[str, Any]:
        """Perform one target-gated attack probe; arbitrary real buttons remain disabled."""
        if not self._action_lock.acquire(blocking=False):
            self._record_action(
                action="combat_tap", button=button, milliseconds=milliseconds,
                ok=False, rejection_reason="another input action is executing",
            )
            raise SafetyError("another input action is executing")
        state = None
        try:
            state = self._assert_real_combat_action_allowed(button, milliseconds)
            self.input.tap(button, milliseconds)
            self._record_action(
                action="combat_tap", button=button, milliseconds=milliseconds,
                ok=True, rejection_reason=None, pre_sequence=state.sequence,
                target_id=state.target.id if state.target else None,
            )
        except Exception as exc:
            self._record_action(
                action="combat_tap", button=button, milliseconds=milliseconds,
                ok=False, rejection_reason=str(exc),
            )
            raise
        finally:
            try:
                self.input.release_all()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        post_state = self.telemetry.observe()
        damage_result = None
        damage_result_status = "not_exposed_by_telemetry"
        pre_target = state.target
        post_target = post_state.target
        if pre_target and post_target and pre_target.id == post_target.id:
            if pre_target.hp is not None and post_target.hp is not None:
                damage_result = {
                    "target_id": pre_target.id,
                    "before_hp": pre_target.hp,
                    "after_hp": post_target.hp,
                    "delta": pre_target.hp - post_target.hp,
                }
                damage_result_status = "observed" if damage_result["delta"] > 0 else "no_decrease_observed"
            else:
                damage_result_status = "target_present_hp_unavailable"
        return {
            "ok": True,
            "action": "combat_tap",
            "button": button,
            "milliseconds": milliseconds,
            "pre_sequence": state.sequence,
            "post_sequence": post_state.sequence,
            "target_id": state.target.id if state.target else None,
            "damage_result": damage_result,
            "damage_result_status": damage_result_status,
        }

    def move(self, x: float, y: float, milliseconds: int = 250, swimming: bool = False) -> dict[str, Any]:
        """One stick pulse. ``swimming`` admits it while Cloud swims (not
        grounded), which the settling gate otherwise refuses; every other
        guard still applies."""
        requested_x, requested_y, requested_ms = x, y, milliseconds
        swimming = swimming is True
        try:
            x, y = normalise_axes(x, y)
            milliseconds = validate_duration(milliseconds)
        except Exception as exc:
            self._record_action(
                action="move", x=requested_x, y=requested_y,
                milliseconds=requested_ms, ok=False, rejection_reason=str(exc)
            )
            raise
        if getattr(self.input, "is_real", False) and x == 0.0 and y == 0.0:
            self._record_action(
                action="move", x=x, y=y, milliseconds=milliseconds, ok=False,
                rejection_reason="real movement requires a non-zero direction",
            )
            raise SafetyError("real movement requires a non-zero direction")
        self._require_guarded_benchmark_movement(None)
        if not self._action_lock.acquire(blocking=False):
            self._record_action(action="move", x=x, y=y, milliseconds=milliseconds,
                                ok=False, rejection_reason="another input action is executing")
            raise SafetyError("another input action is executing")
        state = None
        try:
            state = (
                self._assert_real_movement_allowed(allow_swimming=swimming)
                if getattr(self.input, "is_real", False)
                else self._assert_action_allowed(allow_swimming=swimming)
            )
            self.input.move(x, y, milliseconds)
            self._record_action(action="move", x=x, y=y, milliseconds=milliseconds,
                                ok=True, rejection_reason=None)
        except Exception as exc:
            self._record_action(action="move", x=x, y=y, milliseconds=milliseconds,
                                ok=False, rejection_reason=str(exc))
            raise
        finally:
            try:
                self.input.release_all()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        if isinstance(self.telemetry, SimulationTelemetry) and y > 0:
            self.telemetry.apply_forward_progress(y * milliseconds / 500.0)
        return {"ok": True, "sequence": state.sequence, "action": "move",
                "x": x, "y": y, "milliseconds": milliseconds}

    def move_direction(self, direction: str, milliseconds: int = 1000) -> dict[str, Any]:
        """Use a semantic cardinal key for recovery and path following."""
        vectors = {
            "forward": (0.0, 1.0), "w": (0.0, 1.0),
            "backward": (0.0, -1.0), "back": (0.0, -1.0), "s": (0.0, -1.0),
            "left": (-1.0, 0.0), "a": (-1.0, 0.0),
            "right": (1.0, 0.0), "d": (1.0, 0.0),
        }
        key = str(direction).strip().lower()
        if key not in vectors:
            raise SafetyError("direction must be forward, backward, left, or right")
        result = self.move(*vectors[key], milliseconds)
        result["direction"] = "forward" if key in {"forward", "w"} else (
            "backward" if key in {"backward", "back", "s"} else
            "left" if key in {"left", "a"} else "right"
        )
        result["control"] = {"forward": "w", "backward": "s", "left": "a", "right": "d"}[result["direction"]]
        return result

    def survey_camera(
        self,
        turn_degrees: float = 60.0,
        milliseconds: int = 80,
    ) -> dict[str, Any]:
        """Turn in bounded <=400-pixel pulses and report telemetry after each."""
        try:
            turn_degrees = float(turn_degrees)
            milliseconds = validate_duration(milliseconds)
        except (TypeError, ValueError) as exc:
            raise SafetyError("camera survey parameters are invalid") from exc
        if not math.isfinite(turn_degrees) or not 5.0 <= abs(turn_degrees) <= 180.0:
            raise SafetyError("camera survey turn must be between 5 and 180 degrees")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        try:
            state = (
                self._assert_real_movement_allowed()
                if getattr(self.input, "is_real", False)
                else self._assert_action_allowed()
            )
            if getattr(self.input, "is_real", False):
                armed, _ = self._camera_arm.status() if self._camera_arm else (False, 0.0)
                if not armed:
                    raise SafetyError("camera survey is not manually armed")
                if self._camera_arm:
                    self._camera_arm.clear()
            requested_abs = abs(turn_degrees)
            sign = 1 if turn_degrees > 0 else -1
            pulses: list[int] = []
            estimated_input_per_degree = (
                CALIBRATED_SURVEY_DELTA_PER_DEGREE
                if getattr(self.input, "is_real", False)
                else CAMERA_DELTA_PER_DEGREE
            )
            if state.camera_rotation is None:
                result = {
                    "ok": False,
                    "action": "survey_camera",
                    "requested_degrees": turn_degrees,
                    "estimated_pixels": 0,
                    "pulses": pulses,
                    "pre_sequence": state.sequence,
                    "post_sequence": state.sequence,
                    "pre_camera_yaw": None,
                    "post_camera_yaw": None,
                    "observed_turn_degrees": None,
                    "minimum_observed_turn_degrees": max(
                        CAMERA_SURVEY_MIN_EFFECT_DEGREES,
                        abs(turn_degrees) * CAMERA_SURVEY_MIN_EFFECT_FRACTION,
                    ),
                    "maximum_observed_turn_degrees": abs(turn_degrees) * CAMERA_SURVEY_MAX_EFFECT_FRACTION,
                    "target_tolerance_degrees": CAMERA_SURVEY_TARGET_TOLERANCE_DEGREES,
                    "observations": [],
                    "telemetry_source": state.source,
                    "reason": "camera yaw telemetry is unavailable for survey verification",
                }
                self._record_action(**result)
                return result
            observations: list[dict[str, Any]] = []
            last_sequence = state.sequence
            latest = state
            failure_reason: str | None = None
            observed_signed = 0.0
            while (
                failure_reason is None
                and len(pulses) < CAMERA_SURVEY_MAX_PULSES
                and requested_abs - (sign * observed_signed) > CAMERA_SURVEY_TARGET_TOLERANCE_DEGREES
            ):
                remaining = max(0.0, requested_abs - (sign * observed_signed))
                if not pulses:
                    pulse_abs = CAMERA_SURVEY_INITIAL_ABS_DELTA
                else:
                    pulse_abs = int(round(
                        remaining
                        * max(estimated_input_per_degree, 1.0)
                        * CAMERA_SURVEY_PULSE_SAFETY_FACTOR
                    ))
                pulse_abs = max(1, min(
                    CAMERA_SURVEY_MAX_ABS_DELTA
                    if not pulses else CAMERA_SURVEY_FOLLOWUP_MAX_ABS_DELTA,
                    pulse_abs,
                ))
                pulse = sign * pulse_abs
                pulses.append(pulse)
                pulse_record: dict[str, Any] = {"pulse": pulse, "attempts": []}
                pulse_verified = False
                pulse_change_signed: float | None = None
                for attempt in range(CAMERA_SURVEY_MAX_RETRIES + 1):
                    before_pulse = latest
                    if getattr(self.input, "is_real", False):
                        self.input.calibrate_camera_horizontal(pulse)
                    else:
                        self.input.look(pulse, 0, milliseconds)
                    self.input.release_all()
                    deadline = self._monotonic() + CAMERA_SURVEY_PULSE_TIMEOUT_SECONDS
                    candidate = latest
                    last_candidate_yaw = (
                        before_pulse.camera_rotation.yaw
                        if before_pulse.camera_rotation else None
                    )
                    changed_seen = False
                    best_observed_change_signed: float | None = None
                    stable_samples = 0
                    while self._monotonic() < deadline:
                        candidate = self.telemetry.observe()
                        if candidate.sequence > last_sequence:
                            previous_yaw = last_candidate_yaw
                            last_sequence = candidate.sequence
                            latest = candidate
                            signed_yaw_change = (
                                normalise_degrees(candidate.camera_rotation.yaw - before_pulse.camera_rotation.yaw)
                                if candidate.camera_rotation and before_pulse.camera_rotation
                                else None
                            )
                            incremental_yaw_change = (
                                normalise_degrees(candidate.camera_rotation.yaw - previous_yaw)
                                if candidate.camera_rotation and previous_yaw is not None
                                else None
                            )
                            if (
                                signed_yaw_change is not None
                                and abs(signed_yaw_change) >= CAMERA_MEANINGFUL_CHANGE_DEGREES
                            ):
                                changed_seen = True
                                if (
                                    best_observed_change_signed is None
                                    or abs(signed_yaw_change) > abs(best_observed_change_signed)
                                ):
                                    best_observed_change_signed = signed_yaw_change
                            if changed_seen:
                                if (
                                    incremental_yaw_change is not None
                                    and abs(incremental_yaw_change) <= CAMERA_SETTLE_ENVELOPE_DEGREES
                                ):
                                    stable_samples += 1
                                else:
                                    stable_samples = 0
                                if stable_samples >= CAMERA_SETTLE_MIN_ADVANCING_SAMPLES:
                                    pulse_change_signed = signed_yaw_change
                                    pulse_verified = True
                                    break
                            last_candidate_yaw = candidate.camera_rotation.yaw if candidate.camera_rotation else last_candidate_yaw
                        self._sleeper(POST_PULSE_POLL_SECONDS)
                    # A delayed telemetry sample can prove that the pulse
                    # already moved the camera without satisfying the normal
                    # settle envelope before the bounded timeout.  Do not
                    # resend the same input: that was observed live to turn a
                    # requested 60 degrees into roughly 110 degrees.  The
                    # measured displacement is enough to size the next pulse;
                    # the caller can use the aligner if a stable frame is
                    # still required.
                    if (
                        not pulse_verified
                        and changed_seen
                        and best_observed_change_signed is not None
                        and best_observed_change_signed * sign > 0
                    ):
                        pulse_change_signed = best_observed_change_signed
                        pulse_verified = True
                        pulse_record["verification_mode"] = "advanced_yaw_without_settle"
                    pulse_record["attempts"].append({
                        "attempt": attempt + 1,
                        "sequence": candidate.sequence,
                        "camera_yaw": candidate.camera_rotation.yaw if candidate.camera_rotation else None,
                        "signed_yaw_change_degrees": pulse_change_signed,
                        "player_position": (
                            {"x": candidate.player.position.x, "y": candidate.player.position.y, "z": candidate.player.position.z}
                            if candidate.player and candidate.player.position else None
                        ),
                        "verified_yaw_change": pulse_verified,
                    })
                    if pulse_verified:
                        break
                if not pulse_verified:
                    failure_reason = "camera survey pulse produced no advancing yaw telemetry"
                    observations.append(pulse_record)
                    break
                if pulse_change_signed is None or pulse_change_signed * sign <= 0:
                    failure_reason = "camera survey moved in the opposite direction to the requested turn"
                    observations.append(pulse_record)
                    break
                observed_signed += pulse_change_signed
                measured_input_per_degree = abs(pulse) / abs(pulse_change_signed)
                # Camera response is not stable enough to average across
                # pulses.  Use the latest settled measurement so the next
                # pulse follows the current camera state.
                estimated_input_per_degree = measured_input_per_degree
                pulse_record["cumulative_observed_turn_degrees"] = abs(observed_signed)
                observations.append(pulse_record)
            if (
                failure_reason is None
                and requested_abs - (sign * observed_signed) > CAMERA_SURVEY_TARGET_TOLERANCE_DEGREES
            ):
                failure_reason = "camera survey pulse limit reached before the requested turn"
            post = self.telemetry.observe()
            pre_yaw = state.camera_rotation.yaw if state.camera_rotation else None
            post_yaw = post.camera_rotation.yaw if post.camera_rotation else None
            observed_turn = (
                abs(normalise_degrees(post_yaw - pre_yaw))
                if pre_yaw is not None and post_yaw is not None
                else None
            )
            minimum_observed_turn = max(
                CAMERA_SURVEY_MIN_EFFECT_DEGREES,
                abs(turn_degrees) * CAMERA_SURVEY_MIN_EFFECT_FRACTION,
            )
            maximum_observed_turn = abs(turn_degrees) * CAMERA_SURVEY_MAX_EFFECT_FRACTION
            target_minimum_turn = max(
                minimum_observed_turn,
                requested_abs - CAMERA_SURVEY_TARGET_TOLERANCE_DEGREES,
            )
            target_maximum_turn = min(
                maximum_observed_turn,
                requested_abs + CAMERA_SURVEY_TARGET_TOLERANCE_DEGREES,
            )
            survey_ok = bool(
                failure_reason is None
                and observed_turn is not None
                and observed_turn >= target_minimum_turn
                and observed_turn <= target_maximum_turn + CAMERA_SURVEY_RESULT_EPSILON_DEGREES
            )
            result = {
                "ok": survey_ok,
                "action": "survey_camera",
                "requested_degrees": turn_degrees,
                "estimated_pixels": sum(pulses),
                "pulses": pulses,
                "pre_sequence": state.sequence,
                "post_sequence": post.sequence,
                "pre_camera_yaw": pre_yaw,
                "post_camera_yaw": post_yaw,
                "observed_turn_degrees": observed_turn,
                "observed_turn_degrees_signed": (
                    normalise_degrees(post_yaw - pre_yaw)
                    if pre_yaw is not None and post_yaw is not None
                    else None
                ),
                "minimum_observed_turn_degrees": minimum_observed_turn,
                "maximum_observed_turn_degrees": maximum_observed_turn,
                "target_minimum_turn_degrees": target_minimum_turn,
                "target_maximum_turn_degrees": target_maximum_turn,
                "target_tolerance_degrees": CAMERA_SURVEY_TARGET_TOLERANCE_DEGREES,
                "observations": observations,
                "telemetry_source": post.source,
            }
            if not survey_ok:
                if failure_reason:
                    result["reason"] = failure_reason
                elif observed_turn is None:
                    result["reason"] = "camera yaw telemetry is unavailable for survey verification"
                elif observed_turn < minimum_observed_turn:
                    result["reason"] = (
                        f"camera survey undershot the requested turn: observed {observed_turn:.3f}° "
                        f"of {abs(turn_degrees):.3f}° requested"
                    )
                else:
                    result["reason"] = (
                        f"camera survey overshot the requested turn: observed {observed_turn:.3f}° "
                        f"of {abs(turn_degrees):.3f}° requested"
                    )
            self._record_action(**result)
            return result
        finally:
            try:
                self.input.release_all()
                if self._camera_arm:
                    self._camera_arm.clear()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def tap_vault(self, milliseconds: int = 120) -> dict[str, Any]:
        """Send one guarded space/dodge press for a possible terrain vault."""
        try:
            milliseconds = validate_duration(milliseconds)
        except Exception as exc:
            raise SafetyError("vault duration is invalid") from exc
        if not getattr(self.input, "is_real", False):
            raise SafetyError("terrain vault requires the Windows backend")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        state = None
        try:
            state = self._assert_real_movement_allowed()
            if self._arm:
                self._arm.clear()
            self.input.tap("dodge", milliseconds)
            self._record_action(
                action="tap_vault", ok=True, button="dodge",
                milliseconds=milliseconds, sequence=state.sequence,
            )
        except Exception as exc:
            self._record_action(
                action="tap_vault", ok=False, button="dodge",
                milliseconds=milliseconds, rejection_reason=str(exc),
            )
            raise
        finally:
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                if self._bench_arm:
                    self._bench_arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        return {
            "ok": True, "sequence": state.sequence, "action": "tap_vault",
            "button": "dodge", "milliseconds": milliseconds,
            "emergency_stop": True,
        }

    def look_camera(self, dx: float, dy: float, milliseconds: int = 80) -> dict[str, Any]:
        requested_dx, requested_dy, requested_ms = dx, dy, milliseconds
        try:
            dx, dy = validate_look_delta(dx, dy)
            milliseconds = validate_duration(milliseconds)
        except Exception as exc:
            self._record_action(
                action="look", dx=requested_dx, dy=requested_dy,
                milliseconds=requested_ms, ok=False, rejection_reason=str(exc)
            )
            raise
        if not self._action_lock.acquire(blocking=False):
            self._record_action(action="look", dx=dx, dy=dy,
                                milliseconds=milliseconds, ok=False,
                                rejection_reason="another input action is executing")
            raise SafetyError("another input action is executing")
        state = None
        try:
            state = (
                self._assert_real_movement_allowed()
                if getattr(self.input, "is_real", False)
                else self._assert_action_allowed()
            )
            self.input.look(dx, dy, milliseconds)
            self._record_action(action="look", dx=dx, dy=dy,
                                milliseconds=milliseconds, ok=True,
                                rejection_reason=None)
        except Exception as exc:
            self._record_action(action="look", dx=dx, dy=dy,
                                milliseconds=milliseconds, ok=False,
                                rejection_reason=str(exc))
            raise
        finally:
            try:
                self.input.release_all()
            finally:
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        return {"ok": True, "sequence": state.sequence, "action": "look",
                "dx": dx, "dy": dy, "milliseconds": milliseconds}

    def _camera_gate(
        self, state: Any, bench_id: str | None = None,
        bench_position: Any | None = None,
    ) -> Any:
        """Validate a live detected bench without requiring its current heading."""
        if not getattr(self.input, "is_real", False) or state.source == "simulation":
            raise SafetyError("camera alignment requires the Windows backend and live telemetry")
        if not state.running or state.age_seconds() >= self.max_telemetry_age:
            raise SafetyError("camera alignment requires fresh running telemetry")
        if state.paused or state.in_menu:
            raise SafetyError("camera alignment blocked by pause or Main Menu")
        if state.target is not None:
            raise SafetyError("camera alignment blocked because a target is present")
        if not self._environment.game_process_exists():
            raise SafetyError("FFVII Rebirth process was not found")
        if not self._input_reachable():
            raise SafetyError("FFVII Rebirth is not the foreground window")
        with self._lock:
            if self._stopped:
                raise SafetyError(self._latched_stop_message())
        bench = state.nearby_interactable
        if (
            bench is None or bench.kind != "bench" or bench.position is None
            or bench.relative is None
        ):
            raise SafetyError("detected bench is unavailable or lacks spatial telemetry")
        if bench_id is not None and bench.id != bench_id:
            raise SafetyError("detected bench identity changed")
        if bench_position is not None and math.sqrt(
            (bench.position.x - bench_position.x) ** 2
            + (bench.position.y - bench_position.y) ** 2
            + (bench.position.z - bench_position.z) ** 2
        ) > 1.0:
            raise SafetyError("detected bench position changed")
        if state.player is None or state.player.position is None:
            raise SafetyError("player spatial telemetry is unavailable")
        if state.camera_rotation is None:
            raise SafetyError("camera rotation telemetry is unavailable")
        if bench.relative.horizontal_distance > 5.0:
            raise SafetyError("detected bench is outside camera alignment range")
        return bench

    def _observe_camera_settling(
        self, pre: Any, bench_id: str, bench_position: Any,
        requested_delta: int,
    ) -> tuple[Any, dict[str, Any]]:
        """Wait for advancing, stable camera telemetry without movement input."""
        started = self._monotonic()
        deadline = started + CAMERA_SETTLE_TIMEOUT_SECONDS
        last_sequence = pre.sequence
        latest = pre
        changed_at: float | None = None
        stable_started: float | None = None
        stable_yaws: list[float] = []
        stable_samples = 0
        failure = "camera_settle_timeout"
        polls = 0
        while self._monotonic() < deadline and polls < 80:
            polls += 1
            try:
                state = self.telemetry.observe()
            except Exception as exc:
                if self._is_permission_read_error(exc) and latest.age_seconds() < self.max_telemetry_age:
                    self._sleeper(POST_PULSE_POLL_SECONDS)
                    continue
                raise SafetyError(f"camera telemetry read failed: {exc}") from exc
            if state.sequence <= last_sequence:
                self._sleeper(POST_PULSE_POLL_SECONDS)
                continue
            last_sequence = state.sequence
            latest = state
            self._camera_gate(state, bench_id, bench_position)
            displacement = math.sqrt(
                (state.player.position.x - pre.player.position.x) ** 2
                + (state.player.position.y - pre.player.position.y) ** 2
                + (state.player.position.z - pre.player.position.z) ** 2
            ) / 100.0
            if displacement > 0.05:
                failure = "player_moved_during_camera_correction"
                break
            yaw = state.camera_rotation.yaw
            change = abs(self._normalise_degrees(yaw - pre.camera_rotation.yaw))
            now = self._monotonic()
            if changed_at is None:
                if change < CAMERA_MEANINGFUL_CHANGE_DEGREES:
                    self._sleeper(POST_PULSE_POLL_SECONDS)
                    continue
                changed_at = now
                stable_started = now
                stable_yaws = [yaw]
                stable_samples = 1
            else:
                candidate = stable_yaws + [yaw]
                offsets = [self._normalise_degrees(value - candidate[0]) for value in candidate]
                envelope = max(offsets) - min(offsets)
                if envelope > CAMERA_SETTLE_ENVELOPE_DEGREES:
                    stable_started = now
                    stable_yaws = [yaw]
                    stable_samples = 1
                else:
                    stable_yaws = candidate
                    stable_samples += 1
                if (
                    stable_started is not None
                    and now - stable_started >= CAMERA_SETTLE_DURATION_SECONDS
                    and stable_samples >= CAMERA_SETTLE_MIN_ADVANCING_SAMPLES
                ):
                    failure = ""
                    break
            self._sleeper(POST_PULSE_POLL_SECONDS)
        final_yaw = latest.camera_rotation.yaw if latest.camera_rotation else None
        observed_change = (
            self._normalise_degrees(final_yaw - pre.camera_rotation.yaw)
            if final_yaw is not None else None
        )
        envelope = 0.0
        if stable_yaws:
            offsets = [self._normalise_degrees(value - stable_yaws[0]) for value in stable_yaws]
            envelope = max(offsets) - min(offsets)
        trace = {
            "pre_sequence": pre.sequence,
            "post_sequence": latest.sequence,
            "pre_camera_yaw": pre.camera_rotation.yaw,
            "final_camera_yaw": final_yaw,
            "requested_delta": requested_delta,
            "delivered_delta": requested_delta,
            "observed_yaw_change": observed_change,
            "first_yaw_change_seconds": now - started if changed_at else None,
            "settled_duration_seconds": (
                self._monotonic() - stable_started if stable_started is not None else 0.0
            ),
            "settled_envelope_degrees": envelope,
            "advancing_settled_samples": stable_samples,
            "player_displacement_metres": math.sqrt(
                (latest.player.position.x - pre.player.position.x) ** 2
                + (latest.player.position.y - pre.player.position.y) ** 2
                + (latest.player.position.z - pre.player.position.z) ** 2
            ) / 100.0 if latest.player and latest.player.position else None,
            "settled": failure == "",
            "failure_reason": failure or None,
            "poll_count": polls,
        }
        if failure:
            raise SafetyError(failure)
        return latest, trace

    def calibrate_camera_horizontal(self, delta: int) -> dict[str, Any]:
        """Send one manually armed, bounded calibrated horizontal correction."""
        delta = int(delta)
        if delta == 0 or abs(delta) > CAMERA_MAX_ABS_DELTA:
            raise SafetyError("camera delta must be non-zero and between -500 and 500")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        pre = None
        event = None
        try:
            pre = self._assert_action_allowed()
            if not getattr(self.input, "is_real", False) or pre.source == "simulation":
                raise SafetyError("camera calibration requires the Windows input backend and live telemetry")
            if pre.in_menu or pre.target is not None:
                raise SafetyError("camera calibration blocked by menu or target state")
            if not self._environment.game_process_exists():
                raise SafetyError("FFVII Rebirth process was not found")
            if not self._input_reachable():
                raise SafetyError("FFVII Rebirth is not the foreground window")
            armed, _ = self._camera_arm.status() if self._camera_arm else (False, 0.0)
            if not armed:
                raise SafetyError("camera calibration is not manually armed")
            if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
                raise SafetyError("input action cooldown has not elapsed")
            self._camera_arm.clear()
            event = self.input.calibrate_camera_horizontal(delta)
            self._record_action(
                action="calibrate_camera_horizontal", ok=True,
                horizontal_delta=delta, rejection_reason=None,
            )
        except Exception as exc:
            self._record_action(
                action="calibrate_camera_horizontal", ok=False,
                horizontal_delta=delta, rejection_reason=str(exc),
            )
            raise
        finally:
            try:
                if self._camera_arm:
                    self._camera_arm.clear()
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                if self._bench_arm:
                    self._bench_arm.latch_stop()
                if self._camera_arm:
                    self._camera_arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        post = self.telemetry.observe()
        return {
            "ok": True,
            "outcome": "camera_calibration_sent",
            "horizontal_delta": delta,
            "pre_sequence": pre.sequence,
            "post_sequence": post.sequence,
            "event": event,
            "pre_camera_yaw": pre.camera_rotation.yaw if pre.camera_rotation else None,
            "post_camera_yaw": post.camera_rotation.yaw if post.camera_rotation else None,
            "emergency_stop": True,
        }

    def align_camera_to_bearing(
        self,
        target_bearing_degrees: float,
        tolerance_degrees: float = 2.0,
        maximum_steps: int = 8,
        smooth: bool = False,
    ) -> dict[str, Any]:
        """Aim the camera at a world bearing using settled calibrated steps.

        ``smooth`` turns it in one eased sweep from the calibrated estimate
        instead, with no correction pulses (on stream several pulses looked
        janky): the outcome is aligned if that lands within the tolerance,
        otherwise the step limit with the error left.
        """
        try:
            target = float(target_bearing_degrees)
            tolerance = float(tolerance_degrees)
            steps_limit = int(maximum_steps)
        except (TypeError, ValueError) as exc:
            raise SafetyError("camera bearing alignment parameters are invalid") from exc
        if not math.isfinite(target):
            raise SafetyError("target bearing must be finite")
        if not math.isfinite(tolerance) or not 0.1 <= tolerance <= 15.0:
            raise SafetyError("camera bearing tolerance must be between 0.1 and 15 degrees")
        if not 1 <= steps_limit <= 8:
            raise SafetyError("camera bearing maximum_steps must be between 1 and 8")
        phase_allowed, phase_acquired = self._acquire_local_episode_phase()
        if not phase_allowed:
            raise SafetyError("another input action is executing")
        steps: list[dict[str, Any]] = []
        outcome = "camera_alignment_failed"
        latest = None
        error_reason: str | None = None
        try:
            latest = self._assert_action_allowed()
            if not getattr(self.input, "is_real", False) or latest.source == "simulation":
                raise SafetyError("camera bearing alignment requires live Windows input")
            if latest.in_menu or latest.target is not None:
                raise SafetyError("camera bearing alignment blocked by menu or target")
            if latest.camera_rotation is None:
                raise SafetyError("camera rotation telemetry is unavailable")
            if not self._environment.game_process_exists():
                raise SafetyError("FFVII Rebirth process was not found")
            if not self._input_reachable():
                raise SafetyError("FFVII Rebirth is not the foreground window")
            armed, _ = self._camera_arm.status() if self._camera_arm else (False, 0.0)
            if not armed:
                raise SafetyError("camera bearing alignment is not manually armed")
            if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
                raise SafetyError("input action cooldown has not elapsed")
            if self._camera_arm:
                self._camera_arm.clear()
            # The calibrated camera event is not linear across all live
            # camera states.  Start with the bench-derived estimate, then
            # measure the settled yaw change after each pulse and use that
            # response for the next correction.  This avoids repeatedly
            # overshooting a small residual error after a coarse turn.
            pixels_per_degree = CAMERA_DELTA_PER_DEGREE
            if smooth:
                latest = self._smooth_camera_sweep_to(target, tolerance, steps)
            for step_index in range(0 if smooth else steps_limit):
                latest = self.telemetry.observe()
                if latest.camera_rotation is None:
                    raise SafetyError("camera rotation telemetry disappeared")
                current_yaw = latest.camera_rotation.yaw
                error = self._normalise_degrees(target - current_yaw)
                if abs(error) <= tolerance:
                    outcome = "camera_aligned"
                    break
                delta = max(
                    -CAMERA_MAX_ABS_DELTA,
                    min(CAMERA_MAX_ABS_DELTA, round(error * pixels_per_degree)),
                )
                if delta == 0:
                    raise SafetyError("camera bearing correction rounded to zero")
                previous_sequence = latest.sequence
                previous_yaw = current_yaw
                self.input.calibrate_camera_horizontal(delta)
                self.input.release_all()
                deadline = self._monotonic() + CAMERA_SETTLE_TIMEOUT_SECONDS
                changed = False
                stable_samples = 0
                settled = None
                while self._monotonic() < deadline:
                    candidate = self.telemetry.observe()
                    if candidate.sequence <= previous_sequence:
                        self._sleeper(POST_PULSE_POLL_SECONDS)
                        continue
                    if candidate.camera_rotation is None:
                        raise SafetyError("camera rotation telemetry disappeared during correction")
                    candidate_yaw = candidate.camera_rotation.yaw
                    yaw_change = abs(self._normalise_degrees(candidate_yaw - previous_yaw))
                    if yaw_change > CAMERA_MEANINGFUL_CHANGE_DEGREES:
                        changed = True
                        stable_samples = 0
                    elif changed:
                        stable_samples += 1
                    latest = candidate
                    previous_sequence = candidate.sequence
                    previous_yaw = candidate_yaw
                    if changed and stable_samples >= CAMERA_SETTLE_MIN_ADVANCING_SAMPLES:
                        settled = candidate
                        break
                    self._sleeper(POST_PULSE_POLL_SECONDS)
                if settled is None:
                    raise SafetyError("camera correction did not settle on advancing telemetry")
                observed_change = self._normalise_degrees(
                    settled.camera_rotation.yaw - current_yaw
                )
                if abs(observed_change) >= CAMERA_MEANINGFUL_CHANGE_DEGREES:
                    measured_pixels_per_degree = abs(delta) / abs(observed_change)
                    # Keep adaptation bounded: a delayed/quantised sample
                    # must not turn the next pulse into an unbounded jump.
                    measured_pixels_per_degree = max(
                        3.0, min(30.0, measured_pixels_per_degree)
                    )
                    pixels_per_degree = (
                        pixels_per_degree + measured_pixels_per_degree
                    ) / 2.0
                post_error = self._normalise_degrees(target - settled.camera_rotation.yaw)
                steps.append({
                    "step": step_index + 1,
                    "horizontal_delta": delta,
                    "pre_camera_yaw": current_yaw,
                    "post_camera_yaw": settled.camera_rotation.yaw,
                    "post_heading_error_degrees": post_error,
                    "observed_yaw_change_degrees": observed_change,
                    "pixels_per_degree_for_next_step": pixels_per_degree,
                    "post_sequence": settled.sequence,
                    "stable_samples": stable_samples,
                })
            else:
                latest = self.telemetry.observe()
                if latest.camera_rotation is not None and abs(self._normalise_degrees(target - latest.camera_rotation.yaw)) <= tolerance:
                    outcome = "camera_aligned"
                else:
                    outcome = "camera_alignment_step_limit"
            if outcome != "camera_aligned" and latest is not None and latest.camera_rotation is not None:
                error_reason = f"bearing error remains {self._normalise_degrees(target - latest.camera_rotation.yaw):.3f} degrees"
        except Exception as exc:
            error_reason = str(exc)
            outcome = "camera_alignment_failed"
        finally:
            try:
                self.input.release_all()
            finally:
                if self._local_episode_owner != get_ident():
                    if self._arm:
                        self._arm.latch_stop()
                    if self._bench_arm:
                        self._bench_arm.latch_stop()
                    if self._camera_arm:
                        self._camera_arm.latch_stop()
                    with self._lock:
                        self._stopped = True
                self._last_action_finished = self._monotonic()
                self._release_local_episode_phase(phase_acquired)
        final_yaw = latest.camera_rotation.yaw if latest is not None and latest.camera_rotation is not None else None
        result = {
            "ok": outcome == "camera_aligned",
            "outcome": outcome,
            "target_bearing_degrees": target,
            "tolerance_degrees": tolerance,
            "final_camera_yaw": final_yaw,
            "final_heading_error_degrees": self._normalise_degrees(target - final_yaw) if final_yaw is not None else None,
            "steps": steps,
            "rejection_reason": error_reason,
            "emergency_stop": True,
        }
        self._record_action(action="align_camera_to_bearing", **result)
        return result

    def _smooth_camera_sweep_to(self, target: float, tolerance: float, steps: list[dict[str, Any]]) -> Any:
        """Turn the camera to ``target`` in one continuous, steered motion.

        Constant-speed mouse slices run back to back, the camera is read
        between them, and the speed eases off as the remaining error shrinks,
        so the turn stops on the target. Live 2026-10-05 a single open-loop
        sweep landed 14-42 degrees short: a sweep's turn per pixel grows with
        its size (pointer acceleration), so no fixed scale lands it. The
        scale here only sets the speed.
        """
        latest = self.telemetry.observe()
        if latest.camera_rotation is None:
            raise SafetyError("camera rotation telemetry disappeared")
        start_yaw = latest.camera_rotation.yaw
        error = self._normalise_degrees(target - start_yaw)
        if abs(error) <= tolerance:
            return latest
        move = getattr(self.input, "camera_move_linear", None)
        if not callable(move):
            raise SafetyError("a smooth camera turn needs the Windows mouse path")
        pixels_per_degree = getattr(self, "_sweep_pixels_per_degree", SMOOTH_SWEEP_PIXELS_PER_DEGREE)
        direction = 1.0 if error > 0 else -1.0
        slice_seconds = SMOOTH_SWEEP_SLICE_MS / 1000.0
        deadline = self._monotonic() + SMOOTH_SWEEP_MAX_SECONDS
        sent_total, slices, stop = 0, 0, "deadline"
        while self._monotonic() < deadline:
            remaining = self._normalise_degrees(target - latest.camera_rotation.yaw)
            if abs(remaining) <= tolerance:
                stop = "on_target"
                break
            if remaining * direction < 0:
                stop = "passed_target"
                break
            speed = max(SMOOTH_SWEEP_MIN_DEGREES_PER_SECOND,
                        min(SMOOTH_SWEEP_DEGREES_PER_SECOND, abs(remaining) * SMOOTH_SWEEP_GAIN))
            pixels = int(round(direction * min(speed * slice_seconds, abs(remaining)) * pixels_per_degree))
            if pixels == 0:
                stop = "slice_rounded_to_zero"
                break
            moved = move(pixels, SMOOTH_SWEEP_SLICE_MS)
            sent_total += int((moved or {}).get("sent_delta", pixels)) if isinstance(moved, dict) else pixels
            slices += 1
            observed = self.telemetry.observe()
            if observed.camera_rotation is None:
                raise SafetyError("camera rotation telemetry disappeared during the camera turn")
            latest = observed
        self.input.release_all()
        # Settled once the yaw holds still over a few advancing samples.
        previous_sequence, previous_yaw, stable = latest.sequence, latest.camera_rotation.yaw, 0
        settle_deadline = self._monotonic() + CAMERA_SETTLE_TIMEOUT_SECONDS
        while self._monotonic() < settle_deadline and stable < CAMERA_SETTLE_MIN_ADVANCING_SAMPLES:
            candidate = self.telemetry.observe()
            if candidate.sequence > previous_sequence and candidate.camera_rotation is not None:
                yaw = candidate.camera_rotation.yaw
                stable = stable + 1 if abs(self._normalise_degrees(yaw - previous_yaw)) <= CAMERA_MEANINGFUL_CHANGE_DEGREES else 0
                latest, previous_sequence, previous_yaw = candidate, candidate.sequence, yaw
            self._sleeper(POST_PULSE_POLL_SECONDS)
        final_yaw = latest.camera_rotation.yaw if latest.camera_rotation is not None else None
        turned = self._normalise_degrees(final_yaw - start_yaw) if final_yaw is not None else None
        if turned is not None and abs(turned) >= 10.0 and (turned > 0) == (sent_total > 0):
            measured = max(15.0, min(50.0, abs(sent_total) / abs(turned)))
            self._sweep_pixels_per_degree = (pixels_per_degree + measured) / 2.0
        steps.append({
            "step": 1, "smooth": True, "slices": slices, "stop": stop, "sent_delta": sent_total,
            "pixels_per_degree": round(pixels_per_degree, 2),
            "pre_camera_yaw": start_yaw, "post_camera_yaw": final_yaw,
            "post_heading_error_degrees": self._normalise_degrees(target - final_yaw) if final_yaw is not None else None,
            "settled": stable >= CAMERA_SETTLE_MIN_ADVANCING_SAMPLES,
        })
        return latest

    def align_camera_to_detected_bench(self) -> dict[str, Any]:
        """Aim the camera at the currently detected bench using telemetry feedback."""
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        pre = event = settle = post = None
        requested_delta: int | None = None
        try:
            pre = self._assert_action_allowed()
            bench = self._camera_gate(pre)
            armed, _ = self._camera_arm.status() if self._camera_arm else (False, 0.0)
            if not armed:
                raise SafetyError("camera alignment is not manually armed")
            if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
                raise SafetyError("input action cooldown has not elapsed")
            error = self._normalise_degrees(
                bench.relative.bearing_degrees - pre.camera_rotation.yaw
            )
            if abs(error) <= CAMERA_ALIGNMENT_DEADBAND_DEGREES:
                self._camera_arm.clear()
                return {
                    "ok": True,
                    "outcome": "already_aligned",
                    "bench_id": bench.id,
                    "heading_error_degrees": error,
                    "emergency_stop": False,
                }
            requested_delta = max(
                -CAMERA_MAX_ABS_DELTA,
                min(CAMERA_MAX_ABS_DELTA, round(error * CAMERA_DELTA_PER_DEGREE)),
            )
            self._camera_arm.clear()
            event = self.input.calibrate_camera_horizontal(requested_delta)
            self.input.release_all()
            post, settle = self._observe_camera_settling(
                pre, bench.id, bench.position, requested_delta
            )
            if post.camera_rotation is None or post.nearby_interactable is None:
                raise SafetyError("camera alignment telemetry lost the detected bench")
            post_bench = self._camera_gate(post, bench.id, bench.position)
            post_error = self._normalise_degrees(
                post_bench.relative.bearing_degrees - post.camera_rotation.yaw
            )
            yaw_change = self._normalise_degrees(
                post.camera_rotation.yaw - pre.camera_rotation.yaw
            )
            if yaw_change * requested_delta <= 0:
                raise SafetyError("camera correction moved in the wrong direction")
            if abs(post_error) >= abs(error) - 0.01:
                raise SafetyError("camera alignment did not improve")
            self._record_action(
                action="align_camera_to_detected_bench", ok=True,
                bench_id=bench.id, initial_error_degrees=error,
                post_error_degrees=post_error, horizontal_delta=requested_delta,
                rejection_reason=None,
            )
            return {
                "ok": True,
                "outcome": "camera_aligned",
                "bench_id": bench.id,
                "horizontal_delta": requested_delta,
                "calibration_delta_per_degree": CAMERA_DELTA_PER_DEGREE,
                "pre_sequence": pre.sequence,
                "post_sequence": post.sequence,
                "telemetry_advanced": post.sequence > pre.sequence,
                "initial_camera_yaw": pre.camera_rotation.yaw,
                "post_camera_yaw": post.camera_rotation.yaw,
                "initial_heading_error_degrees": error,
                "post_heading_error_degrees": post_error,
                "camera_settling": settle,
                "event": event,
                "emergency_stop": True,
            }
        except Exception as exc:
            self._record_action(
                action="align_camera_to_detected_bench", ok=False,
                horizontal_delta=requested_delta, rejection_reason=str(exc),
            )
            raise
        finally:
            try:
                if self._camera_arm:
                    self._camera_arm.clear()
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                if self._bench_arm:
                    self._bench_arm.latch_stop()
                if self._camera_arm:
                    self._camera_arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stopped = True
        if self._arm:
            self._arm.latch_stop()
        if self._bench_arm:
            self._bench_arm.latch_stop()
        if self._camera_arm:
            self._camera_arm.latch_stop()
        if self._session_lease is not None:
            try:
                self._session_lease.abort("emergency_stop")
            except Exception:
                # The emergency stop itself must remain successful even if a
                # stale/expired session record needs separate recovery.
                pass
        self.input.release_all()
        self._record_action(action="stop_all_input", ok=True, rejection_reason=None)
        return {"ok": True, "emergency_stop": True}

    def _diagnostic_movement_gate_with_retry(
        self, *, require_armed: bool, last_successful_state: Any | None = None
    ) -> Any:
        started = self._monotonic()
        deadline = started + 2.0
        while self._monotonic() < deadline:
            try:
                return self._assert_real_movement_allowed(require_armed=require_armed)
            except Exception as exc:
                if not self._is_permission_read_error(exc):
                    raise
                if last_successful_state is None:
                    raise SafetyError("telemetry read was denied before a fresh state was established") from exc
                estimated_age = (
                    last_successful_state.age_seconds()
                    + max(0.0, self._monotonic() - started)
                )
                if estimated_age >= self.max_telemetry_age:
                    raise SafetyError(
                        "telemetry became stale during transient permission errors"
                    ) from exc
                self._sleeper(0.1)
        raise SafetyError("telemetry permission error did not clear before retry deadline")

    def _diagnostic_advanced_state(self, previous_sequence: int,
                                   last_successful_state: Any) -> Any:
        deadline = self._monotonic() + 2.0
        newest = last_successful_state
        while self._monotonic() < deadline:
            newest = self._diagnostic_movement_gate_with_retry(
                require_armed=False, last_successful_state=newest
            )
            if newest.sequence > previous_sequence:
                if newest.player is None or newest.player.position is None:
                    raise SafetyError("player has no valid spatial telemetry")
                return newest
            self._sleeper(0.1)
        raise SafetyError("telemetry did not advance after diagnostic pulse")

    def diagnose_forward_pulses(
        self, *, extra_key_up_before_second: bool = False
    ) -> dict[str, Any]:
        """Two isolated, manually armed W pulses; never bench/navigation logic."""
        if not getattr(self.input, "is_real", False):
            raise SafetyError("forward-input diagnostic requires the Windows backend")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")
        result: dict[str, Any] = {
            "ok": False, "outcome": "failed", "pulses": [],
            "extra_key_up_before_second": bool(extra_key_up_before_second),
        }
        event_start = (
            self.input.event_record_count()
            if hasattr(self.input, "event_record_count") else 0
        )
        try:
            current = self._diagnostic_movement_gate_with_retry(require_armed=True)
            if current.player is None or current.player.position is None:
                raise SafetyError("player has no valid spatial telemetry")
            if self._arm:
                self._arm.clear()
            for pulse_number in (1, 2):
                if pulse_number == 2:
                    current = self._diagnostic_movement_gate_with_retry(
                        require_armed=False, last_successful_state=current
                    )
                    if extra_key_up_before_second:
                        if not hasattr(self.input, "diagnostic_key_up"):
                            raise SafetyError("backend cannot emit the diagnostic extra key-up")
                        if self.input.diagnostic_key_up("w") is not True:
                            raise SafetyError("explicit diagnostic W key-up failed")
                pre = current
                pressed_at = self._monotonic()
                self.input.move(0.0, 1.0, 250)
                released_at = self._monotonic()
                self.input.release_all()
                self._sleeper(1.0)
                current = self._diagnostic_advanced_state(pre.sequence, pre)
                displacement = math.hypot(
                    current.player.position.x - pre.player.position.x,
                    current.player.position.y - pre.player.position.y,
                ) / 100.0
                pulse = {
                    "pulse": pulse_number, "milliseconds": 250,
                    "pre_sequence": pre.sequence, "post_sequence": current.sequence,
                    "pre_position": {"x": pre.player.position.x,
                                     "y": pre.player.position.y,
                                     "z": pre.player.position.z},
                    "post_position": {"x": current.player.position.x,
                                      "y": current.player.position.y,
                                      "z": current.player.position.z},
                    "horizontal_displacement": displacement,
                    "key_down_monotonic": pressed_at,
                    "key_up_monotonic": released_at,
                    "neutral_interval_seconds": self._monotonic() - released_at,
                    "movement_observed": displacement > 0.05,
                }
                result["pulses"].append(pulse)
                if displacement <= 0.05:
                    result["outcome"] = (
                        "pulse_1_no_movement" if pulse_number == 1
                        else "pulse_2_no_movement"
                    )
                    break
            else:
                result["ok"] = True
                result["outcome"] = "both_pulses_moved"
        except Exception as exc:
            result["outcome"] = "safety_gate_rejected"
            result["rejection_reason"] = str(exc)
        finally:
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                if self._bench_arm:
                    self._bench_arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        result["input_events"] = (
            self.input.event_records_since(event_start)
            if hasattr(self.input, "event_records_since") else []
        )
        result["emergency_stop"] = True
        self._record_action(action="diagnose_forward_pulses", **result)
        return result

    def interact_with_entity(self, *, target_id: str, target_kind: str, expected_position: dict,
                             expected_map_id: str, expected_actor_id: str, expected_world: dict,
                             after_sequence: int, client_id: str, game_pid: int,
                             timeout_seconds: float = 10.0) -> dict[str, Any]:
        from .entity_interaction import interact_once
        return interact_once(self, target_id=target_id, target_kind=target_kind,
            expected_position=expected_position, expected_map_id=expected_map_id,
            expected_actor_id=expected_actor_id, expected_world=expected_world,
            after_sequence=after_sequence, client_id=client_id, game_pid=game_pid, timeout_seconds=timeout_seconds)

    def _assert_bench_interaction_allowed(self) -> Any:
        if not getattr(self.input, "is_real", False):
            raise SafetyError("bench interaction requires the Windows input backend")
        state = self._assert_action_allowed()
        if state.source == "simulation":
            raise SafetyError("bench interaction requires live game telemetry")
        if state.in_menu:
            raise SafetyError("game Main Menu is open")
        if state.target is not None:
            raise SafetyError("bench interaction blocked because a target is present")
        if not self._environment.game_process_exists():
            raise SafetyError("FFVII Rebirth process was not found")
        if not self._input_reachable():
            raise SafetyError("FFVII Rebirth is not the foreground window")
        bench = state.nearby_interactable
        if bench is None or bench.kind != "bench":
            raise SafetyError("no validated nearby bench")
        if not bench.usable:
            raise SafetyError("nearby bench is not currently usable")
        if bench.distance > 3.0:
            raise SafetyError("nearby bench is outside the validated interaction range")
        armed, _ = self._bench_arm.status() if self._bench_arm else (False, 0.0)
        if not armed:
            raise SafetyError("bench interaction is not manually armed")
        if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
            raise SafetyError("input action cooldown has not elapsed")
        return state

    def use_nearby_bench(self) -> dict[str, Any]:
        if not self._action_lock.acquire(blocking=False):
            self._record_action(action="use_nearby_bench", ok=False,
                                rejection_reason="another input action is executing")
            raise SafetyError("another input action is executing")
        pre = None
        press = None
        error: Exception | None = None
        try:
            pre = self._assert_bench_interaction_allowed()
            # Consume the capability before input so an exception can never leave it armed.
            self._bench_arm.clear()
            begin_lifecycle = getattr(self.telemetry, "begin_bench_interaction", None)
            if begin_lifecycle is not None:
                begin_lifecycle(pre)
            press = self.input.use_bench(250)
            self._record_action(
                action="use_nearby_bench", ok=True, rejection_reason=None,
                bench_id=pre.nearby_interactable.id,
                bench_distance=pre.nearby_interactable.distance,
                **press,
            )
        except Exception as exc:
            error = exc
            self._record_action(action="use_nearby_bench", ok=False,
                                rejection_reason=str(exc), milliseconds=250)
        finally:
            try:
                if self._bench_arm:
                    self._bench_arm.clear()
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                if self._bench_arm:
                    self._bench_arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        if error is not None:
            fail_lifecycle = getattr(self.telemetry, "fail_bench_interaction", None)
            if fail_lifecycle is not None and pre is not None:
                fail_lifecycle(pre.sequence, f"input_error: {error}")
            raise error
        post, lifecycle_trace = self._observe_bench_lifecycle()
        return {
            "ok": True,
            "outcome": "interaction_sent",
            "bench": {
                "id": pre.nearby_interactable.id,
                "distance": pre.nearby_interactable.distance,
            },
            "pre_sequence": pre.sequence,
            "post_sequence": post.sequence,
            "rest_state": post.rest.state,
            "rest": post.to_dict()["rest"],
            "lifecycle_trace": lifecycle_trace,
            "input": press,
            "emergency_stop": True,
        }

    def _assert_physical_chadley_interaction_allowed(
        self, route_id: str, visual_confirmation: str
    ) -> Any:
        if route_id != PHYSICAL_CHADLEY_ROUTE:
            raise SafetyError("unsupported physical Chadley route")
        if visual_confirmation != PHYSICAL_CHADLEY_CONFIRMATION:
            raise SafetyError("physical Chadley visual confirmation is required")
        if not getattr(self.input, "is_real", False):
            raise SafetyError("physical Chadley interaction requires the Windows input backend")
        state = self._assert_action_allowed()
        if state.source == "simulation":
            raise SafetyError("physical Chadley interaction requires live game telemetry")
        if state.paused or state.in_menu:
            raise SafetyError("physical Chadley interaction requires active gameplay")
        if state.in_combat or state.target is not None or state.nearby_enemies:
            raise SafetyError("physical Chadley interaction is blocked during combat or targeting")
        if state.map_id != "EndGame":
            raise SafetyError("physical Chadley route requires the EndGame map")
        if state.player is None or state.player.position is None:
            raise SafetyError("physical Chadley interaction requires player spatial telemetry")
        position = state.player.position
        for axis in ("x", "y", "z"):
            value = float(getattr(position, axis))
            lower, upper = PHYSICAL_CHADLEY_POSITION_BOUNDS[axis]
            if not math.isfinite(value) or not lower <= value <= upper:
                raise SafetyError("player is outside the validated Nibelheim upper-square envelope")
        if not self._environment.game_process_exists():
            raise SafetyError("FFVII Rebirth process was not found")
        if not self._input_reachable():
            raise SafetyError("FFVII Rebirth is not the foreground window")
        armed, _ = self._arm.status() if self._arm else (False, 0.0)
        if not armed:
            raise SafetyError("real input is not manually armed")
        if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
            raise SafetyError("minimum delay between input calls has not elapsed")
        return state

    def interact_with_physical_chadley(
        self,
        route_id: str = PHYSICAL_CHADLEY_ROUTE,
        visual_confirmation: str = PHYSICAL_CHADLEY_CONFIRMATION,
    ) -> dict[str, Any]:
        if not self._action_lock.acquire(blocking=False):
            self._record_action(
                action="interact_with_physical_chadley", ok=False,
                rejection_reason="another input action is executing",
            )
            raise SafetyError("another input action is executing")
        pre = None
        press = None
        error: Exception | None = None
        try:
            pre = self._assert_physical_chadley_interaction_allowed(
                route_id, visual_confirmation
            )
            # Consume the general real-input capability before sending the one-shot press.
            self._arm.clear()
            press = self.input.use_interaction(250)
            self._record_action(
                action="interact_with_physical_chadley", ok=True,
                rejection_reason=None, route_id=route_id,
                visual_confirmation=visual_confirmation, map_id=pre.map_id,
                sequence=pre.sequence, position={
                    "x": pre.player.position.x,
                    "y": pre.player.position.y,
                    "z": pre.player.position.z,
                }, **press,
            )
        except Exception as exc:
            error = exc
            self._record_action(
                action="interact_with_physical_chadley", ok=False,
                rejection_reason=str(exc), route_id=route_id,
                visual_confirmation=visual_confirmation, milliseconds=250,
            )
        finally:
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        if error is not None:
            raise error
        return {
            "ok": True,
            "outcome": "physical_chadley_interaction_sent",
            "route_id": route_id,
            "map_id": pre.map_id,
            "pre_sequence": pre.sequence,
            "position": {
                "x": pre.player.position.x,
                "y": pre.player.position.y,
                "z": pre.player.position.z,
            },
            "input": press,
            "emergency_stop": True,
        }

    def _observe_bench_lifecycle(self) -> tuple[Any, list[dict[str, Any]]]:
        """Observe the anchored lifecycle after input release without sending input."""
        if not hasattr(self.telemetry, "begin_bench_interaction"):
            post = self.telemetry.observe()
            return post, []

        deadline = self._monotonic() + BENCH_LIFECYCLE_OBSERVE_TIMEOUT_SECONDS
        trace: list[dict[str, Any]] = []
        last_sequence: int | None = None
        last_state = None
        post = None
        while self._monotonic() < deadline:
            try:
                observed = self.telemetry.observe()
            except TelemetryError as exc:
                trace.append({
                    "elapsed_seconds": (
                        BENCH_LIFECYCLE_OBSERVE_TIMEOUT_SECONDS
                        - max(0.0, deadline - self._monotonic())
                    ),
                    "read_ok": False,
                    "error": str(exc),
                })
                time.sleep(BENCH_LIFECYCLE_POLL_SECONDS)
                continue
            post = observed
            if observed.sequence == last_sequence:
                time.sleep(BENCH_LIFECYCLE_POLL_SECONDS)
                continue
            last_sequence = observed.sequence
            current_state = observed.rest.state
            if current_state != last_state:
                trace.append({
                    "sequence": observed.sequence,
                    "timestamp_utc": observed.timestamp_utc,
                    "state": current_state,
                    "controller_state": (
                        observed.player.controller_state if observed.player else None
                    ),
                    "interaction_id": observed.rest.interaction_id,
                    "state_identity": observed.rest.state_identity,
                    "bench_id": observed.rest.bench_id,
                    "failure_reason": observed.rest.failure_reason,
                })
                last_state = current_state
            if current_state in {
                "interaction_completed", "cancelled", "failed"
            }:
                return observed, trace
            time.sleep(BENCH_LIFECYCLE_POLL_SECONDS)

        if post is None:
            post = self.telemetry.observe()
        trace.append({
            "sequence": post.sequence,
            "timestamp_utc": post.timestamp_utc,
            "state": post.rest.state,
            "controller_state": post.player.controller_state if post.player else None,
            "reason": "bounded_observation_timeout",
        })
        return post, trace

    def _approach_gate(
        self, state: Any, expected_bench_id: str | None = None,
        expected_bench_position: Any | None = None,
    ) -> Any:
        if not getattr(self.input, "is_real", False):
            raise SafetyError("bench approach requires the Windows input backend")
        if not state.running:
            raise SafetyError("game is not running")
        if state.source == "simulation" or state.age_seconds() >= self.max_telemetry_age:
            raise SafetyError("bench approach requires fresh live telemetry")
        if state.paused:
            raise SafetyError("game is paused")
        if state.in_menu:
            raise SafetyError("game Main Menu is open")
        if state.target is not None:
            raise SafetyError("bench approach blocked because a target is present")
        if state.rest.state not in {"idle", "bench_detected"}:
            raise SafetyError("bench approach blocked by active rest state")
        if not self._environment.game_process_exists():
            raise SafetyError("FFVII Rebirth process was not found")
        if not self._input_reachable():
            raise SafetyError("FFVII Rebirth is not the foreground window")
        _, _, stopped = self._arm.snapshot() if self._arm else (False, 0.0, True)
        with self._lock:
            self._stopped = stopped
        if stopped:
            raise SafetyError(self._latched_stop_message())
        bench = state.nearby_interactable
        if bench is None or bench.kind != "bench":
            raise SafetyError("detected bench is unavailable")
        if expected_bench_id is not None and bench.id != expected_bench_id:
            raise SafetyError("detected bench identity changed")
        if bench.position is None or bench.relative is None:
            raise SafetyError("detected bench has no valid spatial telemetry")
        if expected_bench_position is not None and math.sqrt(
            (bench.position.x - expected_bench_position.x) ** 2 +
            (bench.position.y - expected_bench_position.y) ** 2 +
            (bench.position.z - expected_bench_position.z) ** 2
        ) > 1.0:
            raise SafetyError("detected bench position changed")
        if state.player is None or state.player.position is None:
            raise SafetyError("player has no valid spatial telemetry")
        if bench.relative.horizontal_distance > 5.0:
            raise SafetyError("detected bench is outside the maximum approach distance")
        if abs(bench.relative.heading_error_degrees) > MAX_APPROACH_HEADING_ERROR_DEGREES:
            raise SafetyError("heading is outside the allowed range")
        return bench

    def _post_pulse_state(
        self, previous: int, pre_position: Any,
        bench_id: str, bench_position: Any, pre_distance: float,
        pulse_number: int, pre_age: float, initial_state: Any | None = None,
    ) -> tuple[Any | None, str, list[dict[str, Any]]]:
        newest = None
        last_sequence = previous
        last_position = None
        movement_seen = False
        stable_samples = 0
        records: list[dict[str, Any]] = []
        started = self._monotonic()
        last_success_elapsed = 0.0
        last_success_age = pre_age
        last_motion_time = None
        provisional_settle_time = None
        provisional_settle_position = None
        post_settle_positions: list[Any] = []
        candidate_last_motion_time = None
        candidate_interval_positions: list[Any] = []
        candidate_advancing_samples = 0
        candidate_envelope_diameter = None
        candidate_settle_time = None
        candidate_settle_position = None
        poll_index = 0
        queued_state = initial_state
        deadline = started + POST_PULSE_SETTLE_TIMEOUT_SECONDS
        while self._monotonic() < deadline:
            poll_index += 1
            elapsed = self._monotonic() - started
            try:
                if queued_state is not None:
                    state = queued_state
                    queued_state = None
                else:
                    state = self.telemetry.observe()
            except Exception as exc:
                estimated_age = last_success_age + max(0.0, elapsed - last_success_elapsed)
                record = {
                    "event": "post_pulse_poll", "pulse": pulse_number,
                    "poll": poll_index, "elapsed_seconds": elapsed,
                    "read_success": False, "exception_type": type(exc).__name__,
                    "exception": str(exc), "permission_error": self._is_permission_read_error(exc),
                    "estimated_telemetry_age_seconds": estimated_age,
                    "stable_count": stable_samples, "last_motion_time": last_motion_time,
                    "qualification": "skipped_permission_error" if self._is_permission_read_error(exc)
                                     else "read_error_aborted",
                    "safety_gates": {
                        "windows_backend": None, "game_running": None,
                        "telemetry_fresh": estimated_age < self.max_telemetry_age,
                        "paused_clear": None, "main_menu_clear": None,
                        "target_clear": None, "rest_state_clear": None,
                        "process_exists": None, "foreground": None,
                        "emergency_stop_clear": None, "bench_present": None,
                        "bench_kind": None, "bench_identity_stable": None,
                        "bench_spatial_valid": None, "bench_position_stable": None,
                        "player_spatial_valid": None, "maximum_distance": None,
                        "heading_range": None, "all_passed": None,
                        "reason": "state unavailable because telemetry read failed",
                    },
                    "sequence": None, "timestamp_utc": None, "player": None,
                    "incremental_horizontal_displacement": None,
                    "total_horizontal_displacement": None,
                    "actor_root_bench_distance": None, "press_volume_distance": None,
                    "bench_bearing_degrees": None, "actor_yaw_degrees": None,
                    "heading_error_degrees": None, "movement_bearing_degrees": None,
                    "movement_vs_actor_yaw_degrees": None,
                    "movement_vs_press_bearing_degrees": None,
                    "provisional_settle_elapsed_seconds": provisional_settle_time,
                    "displacement_from_provisional_settle": None,
                    "rolling_post_settle_envelope_diameter": None,
                    "later_increment_gt_2cm": False,
                    "later_increment_gt_5cm": False,
                    "later_increment_gt_10cm": False,
                    "later_offset_gt_2cm": False,
                    "later_offset_gt_5cm": False,
                    "later_offset_gt_10cm": False,
                    "candidate_last_motion_time": candidate_last_motion_time,
                    "candidate_advancing_samples": candidate_advancing_samples,
                    "candidate_envelope_diameter": candidate_envelope_diameter,
                    "candidate_settle_elapsed_seconds": candidate_settle_time,
                    "candidate_settle_reached": False,
                }
                records.append(record)
                if not self._is_permission_read_error(exc):
                    self._write_approach_trace(records)
                    raise
                if estimated_age >= self.max_telemetry_age:
                    self._write_approach_trace(records)
                    return newest, "telemetry_stale", records
                remaining = deadline - self._monotonic()
                if remaining > 0:
                    self._sleeper(min(POST_PULSE_POLL_SECONDS, remaining))
                continue
            telemetry_age = state.age_seconds()
            last_success_elapsed = elapsed
            last_success_age = telemetry_age
            record = {
                "event": "post_pulse_poll", "pulse": pulse_number,
                "poll": poll_index, "elapsed_seconds": elapsed,
                "read_success": True, "sequence": state.sequence,
                "timestamp_utc": state.timestamp_utc, "telemetry_age_seconds": telemetry_age,
                "stable_count": stable_samples, "last_motion_time": last_motion_time,
                "provisional_settle_elapsed_seconds": provisional_settle_time,
            }
            player = state.player
            observed_bench = state.nearby_interactable
            if player and player.position:
                total_displacement = math.hypot(
                    player.position.x - pre_position.x,
                    player.position.y - pre_position.y,
                ) / 100.0
                dx = player.position.x - pre_position.x
                dy = player.position.y - pre_position.y
                movement_bearing = (
                    math.degrees(math.atan2(dy, dx)) if total_displacement > 0 else None
                )
                record["player"] = {
                    "x": player.position.x, "y": player.position.y, "z": player.position.z
                }
                record["total_horizontal_displacement"] = total_displacement
                record["actor_yaw_degrees"] = (
                    player.rotation.yaw if player.rotation else None
                )
                record["movement_bearing_degrees"] = movement_bearing
            else:
                total_displacement = None
                movement_bearing = None
                record.update({"player": None, "total_horizontal_displacement": None,
                               "actor_yaw_degrees": None,
                               "movement_bearing_degrees": None})
            if observed_bench:
                record["actor_root_bench_distance"] = observed_bench.distance
                if observed_bench.relative:
                    record["press_volume_distance"] = observed_bench.relative.horizontal_distance
                    record["bench_bearing_degrees"] = observed_bench.relative.bearing_degrees
                    record["heading_error_degrees"] = observed_bench.relative.heading_error_degrees
                else:
                    record.update({"press_volume_distance": None,
                                   "bench_bearing_degrees": None,
                                   "heading_error_degrees": None})
            else:
                record.update({"actor_root_bench_distance": None,
                               "press_volume_distance": None,
                               "bench_bearing_degrees": None,
                               "heading_error_degrees": None})
            if movement_bearing is not None and player and player.rotation:
                record["movement_vs_actor_yaw_degrees"] = self._normalise_degrees(
                    movement_bearing - player.rotation.yaw
                )
                record["movement_vs_press_bearing_degrees"] = (
                    self._normalise_degrees(
                        movement_bearing - observed_bench.relative.bearing_degrees
                    ) if observed_bench and observed_bench.relative else None
                )
            else:
                record["movement_vs_actor_yaw_degrees"] = None
                record["movement_vs_press_bearing_degrees"] = None
            record["displacement_from_provisional_settle"] = None
            record["rolling_post_settle_envelope_diameter"] = None
            record["later_increment_gt_2cm"] = False
            record["later_increment_gt_5cm"] = False
            record["later_increment_gt_10cm"] = False
            record["later_offset_gt_2cm"] = False
            record["later_offset_gt_5cm"] = False
            record["later_offset_gt_10cm"] = False
            record["candidate_last_motion_time"] = candidate_last_motion_time
            record["candidate_advancing_samples"] = candidate_advancing_samples
            record["candidate_envelope_diameter"] = candidate_envelope_diameter
            record["candidate_settle_elapsed_seconds"] = candidate_settle_time
            record["candidate_settle_reached"] = False
            if state.sequence > last_sequence:
                last_sequence = state.sequence
                newest = state
                try:
                    current_bench = self._approach_gate(state, bench_id, bench_position)
                    record["safety_gates"] = {
                        "windows_backend": True, "game_running": True,
                        "telemetry_fresh": True, "paused_clear": True,
                        "main_menu_clear": True, "target_clear": True,
                        "rest_state_clear": True, "process_exists": True,
                        "foreground": True, "emergency_stop_clear": True,
                        "bench_present": True, "bench_kind": True,
                        "bench_identity_stable": True, "bench_spatial_valid": True,
                        "bench_position_stable": True, "player_spatial_valid": True,
                        "maximum_distance": True, "heading_range": True,
                        "all_passed": True,
                    }
                except SafetyError as exc:
                    record["safety_gates"] = {"all_passed": False, "reason": str(exc)}
                    record["qualification"] = "safety_gate_changed"
                    records.append(record)
                    self._write_approach_trace(records)
                    # Return immediately so the caller can classify the changed gate.
                    return state, "gate_changed", records
                relative = current_bench.relative
                if current_bench.relative.horizontal_distance > pre_distance + 0.10:
                    record["qualification"] = "distance_increased"
                    records.append(record)
                    self._write_approach_trace(records)
                    return state, "distance_increased", records
                displacement = math.hypot(
                    state.player.position.x - pre_position.x,
                    state.player.position.y - pre_position.y,
                ) / 100.0
                incremental = None
                if displacement > 0.05 and not movement_seen:
                    movement_seen = True
                    last_motion_time = elapsed
                    candidate_last_motion_time = elapsed
                    candidate_interval_positions = [state.player.position]
                    candidate_advancing_samples = 0
                    candidate_envelope_diameter = 0.0
                if movement_seen and last_position is not None:
                    incremental = math.hypot(
                        state.player.position.x - last_position.x,
                        state.player.position.y - last_position.y,
                    ) / 100.0
                    if incremental <= 0.02:
                        stable_samples += 1
                        qualification = "stable_sample"
                    else:
                        stable_samples = 0
                        last_motion_time = elapsed
                        qualification = "motion_above_jitter_threshold"
                        candidate_last_motion_time = elapsed
                        candidate_interval_positions = [state.player.position]
                        candidate_advancing_samples = 0
                        candidate_envelope_diameter = 0.0
                    record["incremental_horizontal_displacement"] = incremental
                    record["stable_count"] = stable_samples
                    record["last_motion_time"] = last_motion_time
                    record["qualification"] = qualification
                    if stable_samples >= 3 and provisional_settle_time is None:
                        provisional_settle_time = elapsed
                        provisional_settle_position = state.player.position
                        post_settle_positions = [state.player.position]
                        record["provisional_settle_reached"] = True
                        record["provisional_settle_elapsed_seconds"] = elapsed
                        record["displacement_from_provisional_settle"] = 0.0
                        record["rolling_post_settle_envelope_diameter"] = 0.0
                    else:
                        record["provisional_settle_reached"] = False
                    if incremental <= 0.02 and candidate_last_motion_time is not None:
                        candidate_interval_positions.append(state.player.position)
                        candidate_advancing_samples += 1
                        candidate_envelope_diameter = max(
                            math.hypot(a.x - b.x, a.y - b.y) / 100.0
                            for a in candidate_interval_positions
                            for b in candidate_interval_positions
                        )
                        if (candidate_settle_time is None and
                                elapsed - candidate_last_motion_time >= 0.6 and
                                candidate_advancing_samples >= 3 and
                                candidate_envelope_diameter <=
                                BENCH_SETTLE_ENVELOPE_METRES):
                            candidate_settle_time = elapsed
                            candidate_settle_position = state.player.position
                            record["candidate_settle_reached"] = True
                    record["candidate_last_motion_time"] = candidate_last_motion_time
                    record["candidate_advancing_samples"] = candidate_advancing_samples
                    record["candidate_envelope_diameter"] = candidate_envelope_diameter
                    record["candidate_settle_elapsed_seconds"] = candidate_settle_time
                    if (record["candidate_settle_reached"] and
                            not self._extended_settling_diagnostic):
                        records.append(record)
                        self._write_approach_trace(records)
                        return state, "settled", records
                elif not movement_seen:
                    record["incremental_horizontal_displacement"] = incremental
                    record["qualification"] = "movement_not_yet_observed"
                else:
                    record["incremental_horizontal_displacement"] = incremental
                    record["qualification"] = "first_position_after_movement"
                last_position = state.player.position
                if (provisional_settle_position is not None and
                        state.player.position is not provisional_settle_position):
                    offset = math.hypot(
                        state.player.position.x - provisional_settle_position.x,
                        state.player.position.y - provisional_settle_position.y,
                    ) / 100.0
                    post_settle_positions.append(state.player.position)
                    envelope = max(
                        math.hypot(a.x - b.x, a.y - b.y) / 100.0
                        for a in post_settle_positions
                        for b in post_settle_positions
                    )
                    record["displacement_from_provisional_settle"] = offset
                    record["rolling_post_settle_envelope_diameter"] = envelope
                    record["later_increment_gt_2cm"] = bool(incremental and incremental > 0.02)
                    record["later_increment_gt_5cm"] = bool(incremental and incremental > 0.05)
                    record["later_increment_gt_10cm"] = bool(incremental and incremental > 0.10)
                    record["later_offset_gt_2cm"] = offset > 0.02
                    record["later_offset_gt_5cm"] = offset > 0.05
                    record["later_offset_gt_10cm"] = offset > 0.10
                if candidate_settle_position is not None:
                    candidate_offset = math.hypot(
                        state.player.position.x - candidate_settle_position.x,
                        state.player.position.y - candidate_settle_position.y,
                    ) / 100.0
                    record["displacement_from_candidate_settle"] = candidate_offset
                    record["later_candidate_increment_gt_2cm"] = bool(
                        incremental and incremental > 0.02
                    )
                    record["later_candidate_offset_gt_2cm"] = candidate_offset > 0.02
                else:
                    record["displacement_from_candidate_settle"] = None
                    record["later_candidate_increment_gt_2cm"] = False
                    record["later_candidate_offset_gt_2cm"] = False
            else:
                record["qualification"] = "duplicate_or_older_sequence"
                record["incremental_horizontal_displacement"] = None
                record["safety_gates"] = {
                    "all_passed": None,
                    "reason": "not evaluated because sequence was not newer",
                }
            records.append(record)
            remaining = deadline - self._monotonic()
            if remaining > 0:
                self._sleeper(min(POST_PULSE_POLL_SECONDS, remaining))
        self._write_approach_trace(records)
        if newest is None:
            return None, "no_sequence", records
        if candidate_settle_time is not None:
            return newest, "settled", records
        return newest, "unsettled" if movement_seen else "no_movement", records

    def _confirm_bench_usable(
        self, state: Any, bench_id: str, bench_position: Any
    ) -> tuple[Any, bool, int, str]:
        """Require the settled bench prompt across fresh advancing snapshots."""
        deadline = self._monotonic() + BENCH_USABILITY_CONFIRM_TIMEOUT_SECONDS
        last_sequence = state.sequence
        latest = state
        consecutive = 0
        last_success_time = self._monotonic()
        last_success_age = state.age_seconds()
        while self._monotonic() < deadline:
            try:
                candidate = self.telemetry.observe()
            except Exception as exc:
                elapsed = max(0.0, self._monotonic() - last_success_time)
                if (not self._is_permission_read_error(exc) or
                        last_success_age + elapsed >= self.max_telemetry_age):
                    raise
                self._sleeper(POST_PULSE_POLL_SECONDS)
                continue
            age = candidate.age_seconds()
            if age >= self.max_telemetry_age:
                raise SafetyError("telemetry is stale")
            last_success_time = self._monotonic()
            last_success_age = age
            if candidate.sequence <= last_sequence:
                self._sleeper(POST_PULSE_POLL_SECONDS)
                continue
            last_sequence = candidate.sequence
            latest = candidate
            bench = self._approach_gate(candidate, bench_id, bench_position)
            if not bench.usable:
                return latest, False, consecutive, "usable_did_not_persist"
            consecutive += 1
            if consecutive >= BENCH_USABILITY_CONFIRM_SAMPLES:
                return latest, True, consecutive, "stable_usable"
            self._sleeper(POST_PULSE_POLL_SECONDS)
        return latest, False, consecutive, "usability_confirmation_timeout"

    def approach_detected_bench(
        self, maximum_pulses: int = 1, stop_distance: float = 2.5
    ) -> dict[str, Any]:
        try:
            maximum_pulses = int(maximum_pulses)
            stop_distance = float(stop_distance)
        except (TypeError, ValueError) as exc:
            raise SafetyError("invalid bench approach options") from exc
        if not 1 <= maximum_pulses <= 8:
            raise SafetyError("maximum_pulses must be between 1 and 8")
        if not math.isfinite(stop_distance) or not 2.0 <= stop_distance <= 3.0:
            raise SafetyError("stop_distance must be between 2.0 and 3.0 metres")
        if not self._action_lock.acquire(blocking=False):
            raise SafetyError("another input action is executing")

        trace: list[dict[str, Any]] = []
        outcome = "safety_gate_rejected"
        bench_id: str | None = None
        initial_distance: float | None = None
        final_distance: float | None = None
        error_reason: str | None = None
        try:
            pre = self.telemetry.observe()
            bench = self._approach_gate(pre)
            armed, _ = self._arm.status() if self._arm else (False, 0.0)
            if not armed:
                raise SafetyError("real input is not manually armed")
            if self._monotonic() - self._last_action_finished < self._minimum_action_delay:
                raise SafetyError("input action cooldown has not elapsed")
            bench_id = bench.id
            bench_position = bench.position
            initial_distance = bench.relative.horizontal_distance
            final_distance = initial_distance
            # Consume movement arming before the first possible key press.
            self._arm.clear()
            if final_distance <= stop_distance and bench.usable:
                pre, usable_stable, _, _ = self._confirm_bench_usable(
                    pre, bench_id, bench_position
                )
                if usable_stable:
                    outcome = "bench_usable"
            if outcome != "bench_usable":
                cumulative_displacement = 0.0
                for pulse_index in range(maximum_pulses):
                    if pulse_index > 0:
                        bench = self._approach_gate(pre, bench_id, bench_position)
                    pre_distance = bench.relative.horizontal_distance
                    pre_position = pre.player.position
                    adaptive_latest = pre
                    adaptive_last_sequence = pre.sequence
                    adaptive_release_observation = None

                    def adaptive_stop_requested() -> str | None:
                        nonlocal adaptive_latest, adaptive_last_sequence
                        nonlocal adaptive_release_observation
                        try:
                            candidate = self.telemetry.observe()
                        except Exception as exc:
                            if self._is_permission_read_error(exc):
                                return None
                            self._write_approach_trace([{
                                "event": "adaptive_hold_poll",
                                "pulse": pulse_index + 1,
                                "read_success": False,
                                "exception_type": type(exc).__name__,
                                "exception": str(exc),
                                "stable_count": 0,
                                "qualification": "read_error_aborted",
                            }])
                            raise
                        candidate_bench = self._approach_gate(
                            candidate, bench_id, bench_position
                        )
                        if candidate.sequence <= adaptive_last_sequence:
                            return None
                        adaptive_last_sequence = candidate.sequence
                        adaptive_latest = candidate
                        dx = candidate.player.position.x - pre_position.x
                        dy = candidate.player.position.y - pre_position.y
                        displacement = math.hypot(dx, dy) / 100.0
                        if candidate_bench.usable:
                            adaptive_release_observation = candidate
                            return "bench_usable_observed"
                        if displacement > ADAPTIVE_FORWARD_RELEASE_DISPLACEMENT_METRES:
                            adaptive_release_observation = candidate
                            return "movement_observed"
                        return None

                    adaptive_result = self.input.adaptive_forward(
                        adaptive_stop_requested,
                        maximum_ms=ADAPTIVE_FORWARD_MAXIMUM_MS,
                        poll_seconds=ADAPTIVE_FORWARD_POLL_SECONDS,
                    )
                    self.input.release_all()
                    post, pulse_status, settling_records = self._post_pulse_state(
                        pre.sequence, pre_position, bench_id, bench_position
                        , pre_distance, pulse_index + 1, pre.age_seconds(),
                        adaptive_latest if adaptive_latest.sequence > pre.sequence else None,
                    )
                    if post is None:
                        outcome = "safety_gate_rejected"
                        error_reason = "telemetry sequence did not advance"
                        break
                    try:
                        post_bench = self._approach_gate(post, bench_id, bench_position)
                    except SafetyError as exc:
                        message = str(exc)
                        error_reason = message
                        if ("unavailable" in message or "identity changed" in message or
                                "position changed" in message):
                            outcome = "bench_lost"
                        elif "heading" in message:
                            outcome = "heading_out_of_range"
                        elif "emergency stop" in message:
                            outcome = "emergency_stopped"
                        else:
                            outcome = "safety_gate_rejected"
                        break
                    post_distance = post_bench.relative.horizontal_distance
                    post_position = post.player.position
                    usable_stable = False
                    usable_samples = 0
                    usability_reason = "settled_sample_not_usable"
                    if post_bench.usable:
                        post, usable_stable, usable_samples, usability_reason = (
                            self._confirm_bench_usable(post, bench_id, bench_position)
                        )
                        post_bench = self._approach_gate(post, bench_id, bench_position)
                        post_distance = post_bench.relative.horizontal_distance
                        post_position = post.player.position
                    displacement = math.hypot(
                        post_position.x - pre_position.x,
                        post_position.y - pre_position.y,
                    ) / 100.0
                    cumulative_displacement += displacement
                    movement_dx = post_position.x - pre_position.x
                    movement_dy = post_position.y - pre_position.y
                    movement_bearing = (
                        math.degrees(math.atan2(movement_dy, movement_dx))
                        if displacement > 0 else None
                    )
                    trace.append({
                        "pulse": pulse_index + 1,
                        "duration_ms": adaptive_result["actual_hold_ms"],
                        "maximum_duration_ms": adaptive_result["maximum_ms"],
                        "adaptive_release_reason": adaptive_result["release_reason"],
                        "adaptive_release_sequence": (
                            adaptive_release_observation.sequence
                            if adaptive_release_observation is not None else None
                        ),
                        "pre_sequence": pre.sequence,
                        "post_sequence": post.sequence,
                        "pre_distance": pre_distance,
                        "post_distance": post_distance,
                        "horizontal_displacement": displacement,
                        "heading_error_degrees": post_bench.relative.heading_error_degrees,
                        "movement_bearing_degrees": movement_bearing,
                        "movement_vs_actor_yaw_degrees": (
                            self._normalise_degrees(
                                movement_bearing - post.player.rotation.yaw
                            ) if movement_bearing is not None else None
                        ),
                        "movement_vs_press_bearing_degrees": (
                            self._normalise_degrees(
                                movement_bearing - post_bench.relative.bearing_degrees
                            ) if movement_bearing is not None else None
                        ),
                        "usable": post_bench.usable,
                        "usable_stable": usable_stable,
                        "usable_stable_samples": usable_samples,
                        "usability_reason": usability_reason,
                        "settled": pulse_status == "settled",
                        "settling_poll_count": len(settling_records),
                    })
                    final_distance = post_distance
                    if pulse_status == "unsettled":
                        outcome = "safety_gate_rejected"
                        error_reason = "movement did not settle before telemetry deadline"
                        break
                    if pulse_status == "telemetry_stale":
                        outcome = "safety_gate_rejected"
                        error_reason = "telemetry became stale during skipped reads"
                        break
                    if usable_stable:
                        outcome = "bench_usable"
                        break
                    if displacement <= 0.05:
                        outcome = "blocked_but_not_usable"
                        break
                    if post_distance > pre_distance + 0.10:
                        outcome = "distance_increased"
                        break
                    if cumulative_displacement > 6.0:
                        outcome = "pulse_limit_reached"
                        error_reason = "cumulative distance budget exhausted"
                        break
                    pre = post
                    if pulse_index + 1 < maximum_pulses:
                        self._sleeper(self._minimum_action_delay)
                else:
                    outcome = "pulse_limit_reached"
        except Exception as exc:
            error_reason = str(exc)
            if ("unavailable" in error_reason or "identity changed" in error_reason or
                    "position changed" in error_reason):
                outcome = "bench_lost"
            elif "heading" in error_reason:
                outcome = "heading_out_of_range"
            elif "emergency stop" in error_reason:
                outcome = "emergency_stopped"
            else:
                outcome = "safety_gate_rejected"
        finally:
            try:
                self.input.release_all()
            finally:
                if self._arm:
                    self._arm.latch_stop()
                if self._bench_arm:
                    self._bench_arm.latch_stop()
                with self._lock:
                    self._stopped = True
                self._last_action_finished = self._monotonic()
                self._action_lock.release()
        result = {
            "ok": outcome == "bench_usable",
            "outcome": outcome,
            "bench_id": bench_id,
            "initial_distance": initial_distance,
            "final_distance": final_distance,
            "stop_distance": stop_distance,
            "pulses": trace,
            "rejection_reason": error_reason,
            "emergency_stop": True,
        }
        self._record_action(action="approach_detected_bench", **result)
        return result

    def approach_and_interact(
        self,
        target_kind: str = "bench",
        interaction_distance: float = 1.5,
        max_steps: int = 8,
    ) -> dict[str, Any]:
        if getattr(self.input, "is_real", False):
            raise SafetyError("autonomous movement and interaction are disabled during Stage 4A")
        from dataclasses import asdict
        if not 1 <= max_steps <= 12:
            raise SafetyError("max_steps must be between 1 and 12")
        trace: list[dict[str, Any]] = []
        for _ in range(max_steps):
            state = self._assert_action_allowed()
            target = state.nearby_interactable
            if target and target.kind == target_kind and target.distance <= interaction_distance:
                result = self.tap("interact", 80)
                trace.append(result)
                return {"ok": True, "outcome": "interacted", "target": asdict(target), "trace": trace}
            result = self.move(0.0, 1.0, 250)
            trace.append(result)
        return {"ok": False, "outcome": "step_limit_reached", "trace": trace}
