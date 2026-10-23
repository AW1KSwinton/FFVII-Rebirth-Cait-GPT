from __future__ import annotations

import argparse
import json
from pathlib import Path

from .autonomy import (
    AutonomyError,
    DEFAULT_CLOSE_TIMEOUT_SECONDS,
    EvidenceBundle,
    FixtureBroker,
    LifecycleController,
)
from .factory import create_lifecycle_controller, create_service
from .safety import (
    ArmError, SessionLease, WindowsGameEnvironment, request_arm, request_arm_bench,
    request_arm_camera, request_reset_stop, request_session_close,
    request_session_heartbeat, request_session_open, single_gameplay_process,
)


def emit(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(prog="ff7r-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("observe")
    sub.add_parser("capture-game-window")
    close_game = sub.add_parser("close-game")
    close_game.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_CLOSE_TIMEOUT_SECONDS
    )
    sub.add_parser("status")
    sub.add_parser("session-health")
    recover_session = sub.add_parser("recover-session-control")
    recover_session.add_argument("--milliseconds", type=int, default=500)
    recover_session.add_argument("--timeout-seconds", type=float, default=3.0)
    movement_probe = sub.add_parser("probe-forward-movement")
    movement_probe.add_argument("--milliseconds", type=int, default=1000)
    vault = sub.add_parser("tap-vault")
    vault.add_argument("--milliseconds", type=int, default=120)
    travel_chunk = sub.add_parser("travel-forward-chunk")
    travel_chunk.add_argument("--maximum-ms", type=int, default=3000)
    travel_chunk.add_argument("--target-distance-m", type=float)
    travel_direction = sub.add_parser("travel-direction-chunk")
    travel_direction.add_argument("direction", choices=["forward", "backward", "left", "right"])
    travel_direction.add_argument("--maximum-ms", type=int, default=3000)
    travel_direction.add_argument("--target-distance-m", type=float)
    travel_direction.add_argument("--sprint", action="store_true")
    travel_sprint = sub.add_parser("travel-sprint-chunk")
    travel_sprint.add_argument("direction", choices=["forward", "backward", "left", "right"])
    travel_sprint.add_argument("--maximum-ms", type=int, default=3000)
    travel_sprint.add_argument("--target-distance-m", type=float)
    arm = sub.add_parser("arm")
    arm.add_argument("--seconds", type=int, required=True)
    arm_bench = sub.add_parser("arm-bench")
    arm_bench.add_argument("--seconds", type=int, required=True)
    arm_camera = sub.add_parser("arm-camera")
    arm_camera.add_argument("--seconds", type=int, required=True)
    sub.add_parser("reset-stop")
    session_open = sub.add_parser("session-open")
    # A session lasts as long as the game it is bound to (--game-pid, else the
    # one gameplay process running); --seconds is accepted and ignored.
    session_open.add_argument("--seconds", type=int, default=1800,
                              help="ignored: the session lasts for the life of the game")
    session_open.add_argument("--game-pid", type=int)
    session_open.add_argument("--model-id")
    session_open.add_argument("--reasoning-effort")
    session_heartbeat = sub.add_parser("session-heartbeat")
    session_heartbeat.add_argument("--seconds", type=int, default=1800,
                                   help="ignored: nothing needs renewing")
    session_heartbeat.add_argument("--model-id")
    session_heartbeat.add_argument("--reasoning-effort")
    sub.add_parser("session-close")
    sub.add_parser("session-status")
    tap = sub.add_parser("tap")
    tap.add_argument("button")
    tap.add_argument("--milliseconds", type=int, default=500)
    open_map = sub.add_parser("open-map")
    open_map.add_argument("--milliseconds", type=int, default=500)
    map_selection = sub.add_parser("map-selection")
    map_selection.add_argument("direction", choices=["previous", "next"])
    map_selection.add_argument("--milliseconds", type=int, default=500)
    map_tab = sub.add_parser("map-tab")
    map_tab.add_argument("direction", choices=["previous", "next"])
    map_tab.add_argument("--milliseconds", type=int, default=500)
    map_layer = sub.add_parser("map-layer-toggle")
    map_layer.add_argument("--milliseconds", type=int, default=500)
    map_era = sub.add_parser("map-era-toggle")
    map_era.add_argument("--milliseconds", type=int, default=500)
    map_corresponding = sub.add_parser("map-view-corresponding")
    map_corresponding.add_argument("--milliseconds", type=int, default=500)
    map_pin = sub.add_parser("map-drop-pin")
    map_pin.add_argument("--milliseconds", type=int, default=500)
    sub.add_parser("map-telemetry")
    human_sampler = sub.add_parser("human-recording-sampler")
    human_sampler.add_argument("--interval-seconds", type=float, default=2.0)
    combat_tap = sub.add_parser("combat-tap")
    combat_tap.add_argument("--milliseconds", type=int, default=80)
    queens_blood = sub.add_parser("initiate-queens-blood-match")
    queens_blood.add_argument("--target-id")
    queens_blood.add_argument("--target-kind", default="npc")
    queens_blood.add_argument("--dialogue-confirmations", type=int, default=8)
    queens_blood.add_argument("--confirmation-milliseconds", type=int, default=500)
    queens_blood.add_argument("--begin-milliseconds", type=int, default=500)
    queens_blood.add_argument("--timeout-seconds", type=float, default=30.0)
    move = sub.add_parser("move")
    move.add_argument("x", type=float)
    move.add_argument("y", type=float)
    move.add_argument("--milliseconds", type=int, default=250)
    move_direction = sub.add_parser("move-direction")
    move_direction.add_argument("direction", choices=["forward", "backward", "left", "right"])
    move_direction.add_argument("--milliseconds", type=int, default=1000)
    look = sub.add_parser("look-camera")
    look.add_argument("dx", type=int)
    look.add_argument("dy", type=int)
    look.add_argument("--milliseconds", type=int, default=80)
    survey = sub.add_parser("survey-camera")
    survey.add_argument("--degrees", type=float, default=60.0)
    survey.add_argument("--milliseconds", type=int, default=80)
    route = sub.add_parser("plan-route")
    route.add_argument("--target-id")
    route.add_argument("--target-kind")
    route.add_argument("--map-scale-m-per-pixel", type=float)
    coordinator = sub.add_parser("navigate-next-segment")
    coordinator.add_argument("--target-id")
    coordinator.add_argument("--target-kind")
    coordinator.add_argument("--target-position", type=json.loads)
    coordinator.add_argument("--route-bearing-degrees", type=float)
    coordinator.add_argument("--route-source", default="unavailable")
    coordinator.add_argument("--route-segment-start", type=json.loads)
    coordinator.add_argument("--route-segment-end", type=json.loads)
    coordinator.add_argument("--maximum-ms", type=int, default=15000)
    coordinator.add_argument("--maximum-distance-m", type=float, default=40.0)
    coordinator.add_argument("--no-survey", action="store_true")
    coordinator.add_argument("--capture-screenshot", action="store_true")
    coordinator.add_argument("--visual-reviewed", action="store_true")
    coordinator.add_argument("--visual-review", type=json.loads)
    coordinator.add_argument("--movement-vector", type=json.loads)
    coordinator.add_argument("--sprint", action="store_true")
    stall = sub.add_parser("diagnose-stall")
    stall.add_argument("--direction", default="forward")
    stall.add_argument("--displacement-m", type=float)
    stall.add_argument("--vertical-change-m", type=float)
    calibrate = sub.add_parser("calibrate-camera-horizontal")
    calibrate.add_argument("delta", type=int)
    sub.add_parser("align-camera-to-detected-bench")
    sub.add_parser("stop")
    sub.add_parser("demo")
    forward_diag = sub.add_parser("diagnose-forward-pulses")
    forward_diag.add_argument("--extra-key-up-before-second", action="store_true")
    fixture = sub.add_parser("fixture-clone")
    fixture.add_argument("--source", required=True)
    fixture.add_argument("--fixture-root", required=True)
    fixture.add_argument("--fixture-id")
    run_start = sub.add_parser("run-start")
    run_start.add_argument("--executable", required=True)
    run_start.add_argument("--telemetry", required=True)
    run_start.add_argument("--runtime-dir", required=True)
    run_start.add_argument("--owner-id", default="ff7r-codex-agent")
    run_start.add_argument("--argument", action="append", default=[])
    run_wait = sub.add_parser("run-wait-telemetry")
    run_wait.add_argument("--executable", required=True)
    run_wait.add_argument("--telemetry", required=True)
    run_wait.add_argument("--runtime-dir", required=True)
    run_wait.add_argument("--owner-id", default="ff7r-codex-agent")
    run_wait.add_argument("--timeout-seconds", type=float, default=30.0)
    run_gameplay = sub.add_parser("run-wait-gameplay")
    run_gameplay.add_argument("--executable", required=True)
    run_gameplay.add_argument("--telemetry", required=True)
    run_gameplay.add_argument("--runtime-dir", required=True)
    run_gameplay.add_argument("--owner-id", default="ff7r-codex-agent")
    run_gameplay.add_argument("--timeout-seconds", type=float, default=60.0)
    run_release = sub.add_parser("run-release")
    run_release.add_argument("--executable", required=True)
    run_release.add_argument("--telemetry", required=True)
    run_release.add_argument("--runtime-dir", required=True)
    run_release.add_argument("--owner-id", default="ff7r-codex-agent")
    run_recover = sub.add_parser("run-recover-stale")
    run_recover.add_argument("--executable", required=True)
    run_recover.add_argument("--telemetry", required=True)
    run_recover.add_argument("--runtime-dir", required=True)
    run_recover.add_argument("--owner-id", default="ff7r-codex-agent")
    seal = sub.add_parser("evidence-seal")
    seal.add_argument("--evidence-root", required=True)
    seal.add_argument("--run-id", required=True)

    args = parser.parse_args()
    if args.command in {
        "arm", "arm-bench", "arm-camera", "reset-stop", "session-open",
        "session-heartbeat", "session-close", "session-status",
    }:
        try:
            emit(
                request_arm(args.seconds) if args.command == "arm" else
                request_arm_bench(args.seconds) if args.command == "arm-bench" else
                request_arm_camera(args.seconds) if args.command == "arm-camera" else
                request_reset_stop() if args.command == "reset-stop" else
                request_session_open(args.seconds, game_pid=args.game_pid if args.game_pid is not None else
                                     single_gameplay_process(WindowsGameEnvironment().game_process_ids()),
                                     model_id=args.model_id, reasoning_effort=args.reasoning_effort)
                if args.command == "session-open" else
                request_session_heartbeat(args.seconds, model_id=args.model_id, reasoning_effort=args.reasoning_effort)
                if args.command == "session-heartbeat" else
                request_session_close()
                if args.command == "session-close" else
                (SessionLease().status() or {"active": False, "reason": "no_session"})
            )
        except ArmError as exc:
            parser.error(str(exc))
        return

    if args.command == "fixture-clone":
        try:
            emit(FixtureBroker().clone(args.source, args.fixture_root, args.fixture_id).to_dict())
        except AutonomyError as exc:
            parser.error(str(exc))
        return

    if args.command == "capture-game-window":
        try:
            emit(create_service().capture_game_window())
        except Exception as exc:
            parser.error(str(exc))
        return

    if args.command == "close-game":
        try:
            request_session_close()
            emit(create_lifecycle_controller().close_game(args.timeout_seconds))
        except (AutonomyError, ArmError) as exc:
            parser.error(str(exc))
        return

    if args.command == "run-start":
        try:
            emit(LifecycleController(
                args.executable, args.telemetry, args.runtime_dir, args.owner_id
            ).start(args.argument))
        except AutonomyError as exc:
            parser.error(str(exc))
        return

    if args.command in {"run-wait-telemetry", "run-wait-gameplay", "run-release"}:
        try:
            controller = LifecycleController.attach(
                args.executable, args.telemetry, args.runtime_dir, args.owner_id
            )
            if args.command == "run-wait-telemetry":
                emit(controller.wait_for_fresh_telemetry(args.timeout_seconds))
            elif args.command == "run-wait-gameplay":
                emit(controller.wait_for_gameplay_ready(args.timeout_seconds))
            else:
                controller.release()
                emit({"released": True, "run_id": controller.run_id})
        except AutonomyError as exc:
            parser.error(str(exc))
        return

    if args.command == "run-recover-stale":
        try:
            emit(LifecycleController(
                args.executable, args.telemetry, args.runtime_dir, args.owner_id
            ).recover_stale())
        except AutonomyError as exc:
            parser.error(str(exc))
        return

    if args.command == "evidence-seal":
        try:
            bundle = EvidenceBundle.__new__(EvidenceBundle)
            bundle.root = Path(args.evidence_root).resolve() / args.run_id
            emit(bundle.seal())
        except (AutonomyError, OSError) as exc:
            parser.error(str(exc))
        return

    service = create_service()

    if args.command == "observe":
        emit(service.observe())
    elif args.command == "status":
        emit(service.status())
    elif args.command == "session-health":
        emit(service.session_health())
    elif args.command == "recover-session-control":
        emit(service.recover_session_control(args.milliseconds, args.timeout_seconds))
    elif args.command == "probe-forward-movement":
        emit(service.probe_forward_movement(args.milliseconds))
    elif args.command == "tap-vault":
        emit(service.tap_vault(args.milliseconds))
    elif args.command == "travel-forward-chunk":
        emit(service.travel_forward_chunk(args.maximum_ms, args.target_distance_m))
    elif args.command == "travel-direction-chunk":
        emit(service.travel_direction_chunk(
            args.direction, args.maximum_ms, args.target_distance_m, sprint=args.sprint
        ))
    elif args.command == "travel-sprint-chunk":
        emit(service.travel_sprint_chunk(args.direction, args.maximum_ms, args.target_distance_m))
    elif args.command == "tap":
        emit(service.tap(args.button, args.milliseconds))
    elif args.command == "open-map":
        emit(service.tap("map", args.milliseconds))
    elif args.command == "map-selection":
        emit(service.map_selection(args.direction, args.milliseconds))
    elif args.command == "map-tab":
        emit(service.map_tab(args.direction, args.milliseconds))
    elif args.command == "map-layer-toggle":
        emit(service.toggle_map_layer(args.milliseconds))
    elif args.command == "map-era-toggle":
        emit(service.toggle_map_era(args.milliseconds))
    elif args.command == "map-view-corresponding":
        emit(service.view_map_corresponding(args.milliseconds))
    elif args.command == "map-drop-pin":
        emit(service.drop_map_pin(args.milliseconds))
    elif args.command == "map-telemetry":
        emit(service.map_telemetry())
    elif args.command == "human-recording-sampler":
        emit(service.run_human_traversal_sampler(args.interval_seconds))
    elif args.command == "combat-tap":
        emit(service.combat_tap("attack", args.milliseconds))
    elif args.command == "initiate-queens-blood-match":
        emit(service.initiate_queens_blood_match(
            target_id=args.target_id,
            target_kind=args.target_kind,
            dialogue_confirmations=args.dialogue_confirmations,
            confirmation_milliseconds=args.confirmation_milliseconds,
            begin_milliseconds=args.begin_milliseconds,
            timeout_seconds=args.timeout_seconds,
        ))
    elif args.command == "move":
        emit(service.move(args.x, args.y, args.milliseconds))
    elif args.command == "move-direction":
        emit(service.move_direction(args.direction, args.milliseconds))
    elif args.command == "look-camera":
        emit(service.look_camera(args.dx, args.dy, args.milliseconds))
    elif args.command == "survey-camera":
        emit(service.survey_camera(args.degrees, args.milliseconds))
    elif args.command == "plan-route":
        emit(service.plan_navigation_route(args.target_id, args.target_kind, args.map_scale_m_per_pixel))
    elif args.command == "navigate-next-segment":
        emit(service.navigate_next_segment(
            target_id=args.target_id,
            target_kind=args.target_kind,
            target_position=args.target_position,
            route_bearing_degrees=args.route_bearing_degrees,
            route_source=args.route_source,
            route_segment_start=args.route_segment_start,
            route_segment_end=args.route_segment_end,
            maximum_ms=args.maximum_ms,
            maximum_distance_m=args.maximum_distance_m,
            survey=not args.no_survey,
            capture_screenshot=args.capture_screenshot,
            visual_reviewed=args.visual_reviewed,
            visual_review=args.visual_review,
            movement_vector=args.movement_vector,
            sprint=args.sprint,
        ))
    elif args.command == "diagnose-stall":
        emit(service.diagnose_navigation_stall(args.direction, args.displacement_m, args.vertical_change_m))
    elif args.command == "calibrate-camera-horizontal":
        emit(service.calibrate_camera_horizontal(args.delta))
    elif args.command == "align-camera-to-detected-bench":
        emit(service.align_camera_to_detected_bench())
    elif args.command == "stop":
        emit(service.stop())
    elif args.command == "demo":
        emit(service.approach_and_interact())
    elif args.command == "diagnose-forward-pulses":
        emit(service.diagnose_forward_pulses(
            extra_key_up_before_second=args.extra_key_up_before_second
        ))


if __name__ == "__main__":
    main()
