from __future__ import annotations

import os
from pathlib import Path

from .autonomy import LifecycleController
from .input import SimulationInput, WindowsHybridGamepadInput, WindowsKeyboardInput
from .navigation import KnownWorldStore
from .service import AgentService
from .startup import StartupCoordinator
from .safety import (
    ArmState, BenchArmState, CameraArmState, SessionLease, default_runtime_dir,
    request_session_close, request_session_heartbeat, request_session_open,
)
from .telemetry import JsonFileTelemetry, SimulationTelemetry


DEFAULT_GAME_EXECUTABLE = Path(r"D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64\ff7rebirth_.exe")
DEFAULT_TELEMETRY_FILE = Path(r"C:\CaitGPT\Telemetry\state.json")


def create_lifecycle_controller() -> LifecycleController:
    executable = Path(os.getenv("FF7R_GAME_EXECUTABLE", str(DEFAULT_GAME_EXECUTABLE)))
    telemetry = Path(os.getenv("FF7R_TELEMETRY_FILE", str(DEFAULT_TELEMETRY_FILE)))
    runtime_dir = Path(os.getenv("FF7R_AUTONOMY_RUNTIME_DIR", str(default_runtime_dir() / "lifecycle")))
    owner_id = os.getenv("FF7R_AUTONOMY_OWNER_ID", "ff7r-codex-agent")
    return LifecycleController(executable, telemetry, runtime_dir, owner_id)


def create_service(*, defer_validator_prewarm: bool = False) -> AgentService:
    telemetry_file = os.getenv("FF7R_TELEMETRY_FILE")
    input_name = os.getenv("FF7R_INPUT_BACKEND", "simulation").lower()
    runtime_dir = Path(
        os.getenv("FF7R_AUTONOMY_RUNTIME_DIR", str(default_runtime_dir()))
    )

    supplemental_value = os.getenv("FF7R_SUPPLEMENTAL_TELEMETRY_FILE")
    navigation_value = os.getenv("FF7R_NAVIGATION_TELEMETRY_FILE")
    navigation_route_value = os.getenv("FF7R_NAVIGATION_ROUTE_TELEMETRY_FILE")
    queen_blood_value = os.getenv("FF7R_QUEEN_BLOOD_TELEMETRY_FILE")
    map_ui_value = os.getenv("FF7R_MAP_UI_TELEMETRY_FILE")
    telemetry = (
        JsonFileTelemetry(
            telemetry_file,
            supplemental_path=supplemental_value,
            navigation_path=navigation_value,
            navigation_route_path=navigation_route_value,
            queen_blood_path=queen_blood_value,
            map_ui_path=map_ui_value,
        )
        if telemetry_file else SimulationTelemetry()
    )
    event_log_value = os.getenv("FF7R_WINDOWS_EVENT_LOG")
    event_log = (
        Path(event_log_value)
        if event_log_value
        else default_runtime_dir() / "windows-input-events.jsonl"
    )
    if input_name == "simulation":
        input_backend = SimulationInput()
    elif input_name == "windows":
        input_backend = WindowsKeyboardInput(event_log=event_log)
    elif input_name == "windows_gamepad":
        input_backend = WindowsHybridGamepadInput(event_log=event_log,
            background_only=os.getenv("FF7R_BACKGROUND_INPUT", "0").lower() in {"1", "true", "yes"})
    else:
        raise RuntimeError(
            f"unknown FF7R_INPUT_BACKEND: {input_name}; expected simulation, windows, or windows_gamepad"
        )

    max_age = float(os.getenv("FF7R_MAX_TELEMETRY_AGE", "2.0"))
    real_input = bool(getattr(input_backend, "is_real", False))
    arm_state = ArmState(runtime_dir=runtime_dir) if real_input else None
    bench_arm_state = BenchArmState(runtime_dir=runtime_dir) if real_input else None
    camera_arm_state = CameraArmState(runtime_dir=runtime_dir) if real_input else None
    session_lease = (
        SessionLease(
            runtime_dir=runtime_dir,
            owner_id=os.getenv("FF7R_AUTONOMY_OWNER_ID", "ff7r-codex-agent"),
        )
        if real_input else None
    )
    if session_lease is not None:
        session_status = session_lease.status()
        if session_status and session_status.get("active"):
            # A worker restarted under an open session (it outlives workers)
            # picks it up. The session authorises only this short re-arm;
            # every action still passes the normal live telemetry,
            # foreground, and process gates.
            arm_state.reset_stop()
            arm_state.arm(120)
            camera_arm_state.reset_stop()
            camera_arm_state.arm(120)
    action_log_value = os.getenv("FF7R_INPUT_LOG")
    action_log = (
        Path(action_log_value)
        if action_log_value
        else default_runtime_dir() / "input-actions.jsonl"
    )
    approach_trace_value = os.getenv("FF7R_APPROACH_TRACE_LOG")
    approach_trace_log = (
        Path(approach_trace_value)
        if approach_trace_value
        else default_runtime_dir() / "approach-settling.jsonl"
    )
    extended_settling_diagnostic = (
        os.getenv("FF7R_EXTENDED_SETTLING_DIAGNOSTIC", "1").lower()
        in {"1", "true", "yes", "on"}
    )
    known_world_value = os.getenv("FF7R_KNOWN_WORLD_FILE")
    known_world_store = KnownWorldStore(
        Path(known_world_value)
        if known_world_value
        else default_runtime_dir() / "known-world.json"
    )
    use_recorded_routes = (
        os.getenv("FF7R_USE_RECORDED_ROUTES", "0").lower()
        in {"1", "true", "yes", "on"}
    )
    screenshot_dir_value = os.getenv("FF7R_SCREENSHOT_DIR")
    screenshot_dir = (
        Path(screenshot_dir_value)
        if screenshot_dir_value
        else default_runtime_dir() / "screenshots"
    )
    benchmark_root_value = os.getenv("FF7R_BENCHMARK_DIR")
    benchmark_root = Path(benchmark_root_value) if benchmark_root_value else default_runtime_dir() / "benchmarks"
    human_recording_root_value = os.getenv("FF7R_HUMAN_RECORDING_DIR")
    human_recording_root = (
        Path(human_recording_root_value)
        if human_recording_root_value
        else default_runtime_dir() / "human-recordings"
    )
    service = AgentService(
        telemetry,
        input_backend,
        max_telemetry_age=max_age,
        arm_state=arm_state,
        bench_arm_state=bench_arm_state,
        camera_arm_state=camera_arm_state,
        session_lease=session_lease,
        action_log=action_log,
        public_stream_event_dir=(
            Path(os.environ["FF7R_PUBLIC_STREAM_EVENT_DIR"])
            if os.getenv("FF7R_PUBLIC_STREAM_EVENT_DIR") else None
        ),
        approach_trace_log=approach_trace_log,
        extended_settling_diagnostic=extended_settling_diagnostic,
        known_world_store=known_world_store,
        use_recorded_routes=use_recorded_routes,
        screenshot_dir=screenshot_dir,
        benchmark_root=benchmark_root,
        human_recording_root=human_recording_root,
        queen_blood_recording_root=Path(
            os.getenv("FF7R_QB_RECORDING_DIR", str(default_runtime_dir() / "qb-recordings"))
        ),
        queen_blood_opponents_file=Path(
            os.getenv("FF7R_QUEEN_BLOOD_OPPONENTS_FILE")
            or default_runtime_dir() / "queen-blood-opponents.json"
        ),
        queen_blood_geometry_file=Path(
            os.getenv("FF7R_QUEEN_BLOOD_GEOMETRY_FILE")
            or default_runtime_dir() / "queen-blood-board-geometry.json"
        ),
        queen_blood_profile_root=Path(
            os.getenv("FF7R_QUEEN_BLOOD_PROFILE_ROOT")
            or default_runtime_dir() / "queen-blood"
        ),
        queen_blood_background_input=(
            os.getenv("FF7R_QUEEN_BLOOD_BACKGROUND_INPUT", "1").lower() in {"1", "true", "yes", "on"}
        ),
        # A live MCP session must see three advancing, foreground gameplay
        # samples before real movement/camera/interaction is allowed.  This
        # is intentionally not a high-frequency scan; the service caches the
        # settled identity for the short normal action window.
        settled_gameplay_samples=3 if real_input else 1,
        # Longer than the Queen's Blood probe's worst publish interval, so a
        # settled cursor has been confirmed by at least one fresh sidecar.
        queen_blood_cursor_settle_seconds=1.5 if real_input else 0.0,
    )
    if not defer_validator_prewarm:
        start_service_validator_prewarm(service)
    if real_input:
        # Skip story cinemas while OBS streams (#158); FF7R_CUTSCENE_AUTOSKIP
        # can set always or off.
        service.set_cutscene_autoskip(os.getenv("FF7R_CUTSCENE_AUTOSKIP", "stream"))
        # Close tutorial tip panels the same way; FF7R_TIPS_AUTOCLOSE can set
        # always or off.
        service.set_tips_autoclose(os.getenv("FF7R_TIPS_AUTOCLOSE", "stream"))
    return service


def start_service_validator_prewarm(service: AgentService) -> None:
    """Start speculative work only after a worker can report its identity."""
    if bool(getattr(service.input, "is_real", False)):
        # Warm only the immutable standalone validator representation.  This
        # does not observe gameplay, create a movement lease, or authorize
        # input.  The route-time provider/binder still verifies every artifact,
        # fingerprint and validator-source identity before the warmed object
        # can be reused.  Warm-up failure is diagnostic only and leaves the
        # existing fail-closed cold path intact.
        try:
            from .validator_prewarm import prepare_default_validator_prewarm

            prepare_default_validator_prewarm(service)
        except Exception as exc:
            setattr(service, "_independent_validator_prewarm_status", {
                "state": "failed_to_start",
                "reason": type(exc).__name__,
                "detail": " ".join(str(exc).split())[:512],
            })



def create_startup_coordinator(
    lifecycle: LifecycleController | None = None,
    service: AgentService | None = None,
) -> StartupCoordinator:
    """Create the reusable launch/title/save startup coordinator."""
    runtime_dir = Path(
        os.getenv("FF7R_AUTONOMY_RUNTIME_DIR", str(default_runtime_dir()))
    )
    owner_id = os.getenv("FF7R_AUTONOMY_OWNER_ID", "ff7r-codex-agent")

    # Keep startup's control-session lease on the same configured runtime
    # directory as the service/lifecycle safety state.  This matters for
    # attached or reloadable workers whose default AppData directory may be
    # inaccessible, and does not bypass any lease ownership or expiry checks.
    def session_status() -> dict[str, object] | None:
        return SessionLease(runtime_dir=runtime_dir, owner_id=owner_id).status()

    def session_open(**values):
        return request_session_open(runtime_dir=runtime_dir, owner_id=owner_id, **values)

    def session_heartbeat(**values):
        return request_session_heartbeat(runtime_dir=runtime_dir, owner_id=owner_id, **values)

    def session_close(**values):
        return request_session_close(runtime_dir=runtime_dir, owner_id=owner_id, **values)

    return StartupCoordinator(
        lifecycle or create_lifecycle_controller(),
        service or create_service(),
        session_status=session_status,
        session_open=session_open,
        session_heartbeat=session_heartbeat,
        session_close=session_close,
    )
