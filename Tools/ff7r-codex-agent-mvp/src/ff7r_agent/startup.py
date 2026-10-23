"""Composable, bounded FFVII Rebirth startup and save-selection phases."""

from __future__ import annotations

import time
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, Callable

from .autonomy import (
    AutonomyError,
    DEFAULT_CLOSE_TIMEOUT_SECONDS,
    LifecycleController,
)
from .capture import BLANK_BACKGROUND_CAPTURE
from .safety import (
    ArmError,
    SessionLease,
    request_session_close,
    request_session_heartbeat,
    request_session_open,
)


class StartupError(RuntimeError):
    """A fail-closed startup phase refusal."""


class StartupPending(StartupError):
    """A safe, bounded handoff while Rebirth continues a slow transition."""

    def __init__(self, message: str, *, phase: str = "startup_pending") -> None:
        super().__init__(message)
        self.phase = phase


MENU_ACTIONS = (
    "confirm", "cancel", "up", "down", "left", "right",
    "sub_left", "sub_right", "menu", "map",
)

# From settled gameplay, the current in-game path is: wrap to the bottom with
# Up twice, open Save/Load, move to Load, open it, confirm the top save, then
# move the final No/Yes prompt to Yes.  Keep the final confirm separate so the
# safety-critical confirmation is explicit in the trace.
LOAD_LAST_SAVE_DEFAULT_MENU_ACTIONS = (
    "up", "up", "confirm", "down", "confirm", "confirm", "up",
)
LOAD_LAST_SAVE_CONFIRMATIONS = 1
LOAD_LAST_SAVE_MENU_SETTLE_SECONDS = 5.0
LOAD_LAST_SAVE_READINESS_TIMEOUT_SECONDS = 4.0
# After telemetry reports a loaded world, the screen must leave the black
# "Loading" screen (its widget, or under 5% of sampled pixels lit) within this time.
RENDER_WAIT_SECONDS = 60.0
RENDER_POLL_SECONDS = 6.0
LOADING_FRAME_MAX_NONBLACK = 0.05
# Directional menu navigation is deliberately shorter than the 500 ms
# confirm/menu hold. On this game/input path a long directional hold can
# auto-repeat and move past the intended Save/Load or Load entry.
LOAD_LAST_SAVE_CURSOR_TAP_MS = 150
LOAD_LAST_SAVE_CONFIRM_TAP_MS = 500
# The MCP runtime worker has a shorter request deadline than the public
# lifecycle timeouts.  Keep each internal wait below that boundary and return
# a resumable pending result instead of allowing the worker to be killed while
# Rebirth is still launching/loading.
STARTUP_LOCAL_WAIT_SECONDS = 5.0
# Startup/menu input uses the foreground keyboard/mouse path.  Focus recovery
# is therefore a required, bounded precondition rather than best effort.
FOREGROUND_FOCUS_RETRY_INTERVAL_SECONDS = 0.5
FOREGROUND_FOCUS_MAX_ATTEMPTS = 8
# The async startup job is not bound by the request deadline, so it retries a
# STARTUP_PENDING result itself. Live 2026-09-30, after a crash the relaunched
# game's window took longer than the focus budget to appear and the job ended
# pending 13 s in; one retry 10 s later reached gameplay.
STARTUP_PENDING_RETRY_SECONDS = 10.0
STARTUP_PENDING_MAX_RETRIES = 9
# New Game setup, live 2026-10-13: New Game sits above the preselected
# Continue; then come the bonus saved-data check, Camera Controls,
# Difficulty and, after Easy, Combat Style. Each setup screen is one
# Startmenu_00 widget whose Txt_Title names the screen and whose Txt_Help
# follows the highlighted option. The widget keeps its texts after a screen
# closes, so a screen is reached only when its own title replaces the last.
NEW_GAME_TITLE_PATH = "Startmenu_00.WidgetTree.Txt_Title"
NEW_GAME_HELP_PATH = "Startmenu_00.WidgetTree.Txt_Help"
NEW_GAME_CAMERA_TITLE = "$menu_title_2018_1208"
NEW_GAME_DIFFICULTY_TITLE = "$menu_title_2019_0305"
NEW_GAME_COMBAT_STYLE_TITLE = "$menu_options_0002_0035"
NEW_GAME_BONUS_TITLE = "$menu_title_savedata_bonus_check_title"
# Options in menu order, the first preselected, and the help text seen
# while each is highlighted, where it is known.
NEW_GAME_CAMERA_OPTIONS = ("default", "inverted_y", "inverted_x", "inverted_xy")
NEW_GAME_DIFFICULTIES = ("easy", "normal", "dynamic")
NEW_GAME_COMBAT_STYLES = ("active", "classic")
NEW_GAME_BONUS_CHOICES = ("check", "skip")
NEW_GAME_OPTION_HELP = {
    ("camera", "default"): "$menu_title_camera_help_normal_normal",
    ("difficulty", "easy"): "$help_message_0010_0010",
    ("combat_style", "active"): "$menu_options_0002_0036_help",
    ("combat_style", "classic"): "$menu_options_0002_0037_help",
    ("bonus_check", "check"): "$menu_title_savedata_bonus_check_execute_help",
}
# 150 ms taps moved one option per press on these screens; a held
# direction can auto-repeat.
NEW_GAME_MENU_TAP_MS = 150
NEW_GAME_CURSOR_GAP_SECONDS = 0.4
NEW_GAME_SCREEN_SETTLE_SECONDS = 1.0
NEW_GAME_SCREEN_TIMEOUT_SECONDS = 20.0
NEW_GAME_POLL_SECONDS = 0.5
# Check, then the "no eligible saved data" notice.
NEW_GAME_BONUS_MAX_CONFIRMS = 3
NEW_GAME_CUTSCENE_SKIP_ATTEMPTS = 2


class StartupCoordinator:
    """Coordinate lifecycle, visual evidence, prompts, and readiness.

    Phase 1 launches or attaches to Rebirth and dismisses only the title
    prompt.  Phase 2 is separate so Continue, another save, and New Game can
    share the same process without coupling launch to a save choice.
    """

    def __init__(
        self,
        lifecycle: LifecycleController,
        service: Any,
        *,
        session_status: Callable[[], dict[str, Any] | None] | None = None,
        session_open: Callable[..., dict[str, Any]] = request_session_open,
        session_heartbeat: Callable[..., dict[str, Any]] = request_session_heartbeat,
        session_close: Callable[..., dict[str, Any]] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.lifecycle = lifecycle
        self.service = service
        self._session_status = session_status or (lambda: SessionLease().status())
        self._startup_session_binding = ContextVar("startup_session_binding", default=None)
        self._session_open = session_open
        self._session_heartbeat = session_heartbeat
        self._session_close = session_close or (
            lambda **_kwargs: {"closed": False, "reason": "session_close_not_configured"}
        )
        self._sleep = sleeper
        self._monotonic = monotonic

    @staticmethod
    def _pid(context: dict[str, Any] | None) -> int | None:
        value = context.get("pid") if context else None
        return int(value) if value is not None else None

    @staticmethod
    def _bounded_wait(timeout_seconds: float) -> float:
        return min(
            STARTUP_LOCAL_WAIT_SECONDS,
            max(0.1, float(timeout_seconds)),
        )

    @staticmethod
    def _is_owned_launch(mode: str | None, context: dict[str, Any] | None) -> bool:
        """Keep launch grace when a reconnect re-attaches to our own run.

        ``LifecycleController.ensure_started`` reports ``attached`` whenever
        the process already exists, including after a startup call launched
        it but failed before the session record was written.  Treating that
        case as an external attachment caused the wrapper to fail-fast on a
        slow/temporarily headless load.  The durable context marker is kept
        when the lifecycle controller reconnects to that same run.
        """
        if mode == "launched":
            return True
        return bool(
            mode == "attached"
            and context
            and context.get("attachment") == "existing_controller_run"
            and context.get("owner_id")
        )

    def _verify_bound_model_run(self) -> None:
        binding = self._startup_session_binding.get()
        if binding and binding.get("run"):
            current = self.service.get_queens_blood_run_status()
            if any(current.get(key) != binding["run"][key] for key in ("driver", "assignment")):
                raise StartupError("MODEL_RUN_CHANGED_DURING_STARTUP")

    def _open_startup_session(self, game_pid: int | None, seconds: int) -> dict[str, Any]:
        binding = self._startup_session_binding.get()
        self._verify_bound_model_run()
        values = {"seconds": seconds, "game_pid": game_pid}
        if binding:
            values.update(model_id=binding["recording"]["modelId"],
                          reasoning_effort=binding["recording"]["effort"], client_id=binding["client_id"])
        result = self._session_open(**values)
        self._remember_temporary_session(result)
        if binding and result.get("recording_driver") != binding["recording"]:
            raise StartupError("CONTROL_SESSION_ATTRIBUTION_UNVERIFIED")
        self._verify_bound_model_run()
        return result

    def _remember_temporary_session(self, session: dict[str, Any]) -> None:
        binding = self._startup_session_binding.get()
        if (binding and binding.get("run") and not binding["explicit_client"]
                and session.get("active")
                and session.get("client_id") in (None, binding["client_id"])):
            # Includes a clientless title-launch lease attributed by startup.
            # Explicit callers and other clients retain their own leases.
            binding["temporary_session"] = deepcopy(session)

    def _release_temporary_session(self, binding: dict[str, Any]) -> dict[str, Any]:
        try:
            released = self._session_close(
                reason="model_startup_finished", expected_session=binding["temporary_session"])
            return {**released, "ok": bool(released.get("closed")) or released.get("reason") == "no_session"}
        except Exception as exc:
            return {"ok": False, "closed": False, "reason": str(exc)}

    def _ensure_session(self, game_pid: int | None, seconds: int) -> dict[str, Any]:
        self._verify_bound_model_run()
        try:
            current = self._session_status()
        except ArmError as exc:
            raise StartupError(f"CONTROL_SESSION_STATUS_FAILED: {exc}") from exc
        if current and current.get("active"):
            binding = self._startup_session_binding.get()
            if binding and current.get("owner_matches") is not True:
                raise StartupError("CONTROL_SESSION_OWNER_MISMATCH")
            current_pid = current.get("game_pid")
            if game_pid is not None and current_pid is None and current.get("owner_matches") is True:
                # Cold startup opens before the game exists. Once its PID is
                # known, replace only our unbound lease with a process-bound
                # one so the session can actually end when this game exits.
                allowed_clients = (None, binding["client_id"]) if binding else (None,)
                if (current.get("client_id") not in allowed_clients
                        or (binding and binding.get("explicit_client")
                            and current.get("client_id") != binding["client_id"])):
                    raise StartupError("CONTROL_SESSION_CLIENT_MISMATCH")
                if (binding and current.get("recording_driver") is not None
                        and current.get("recording_driver") != binding["recording"]):
                    raise StartupError("CONTROL_SESSION_RECORDING_DRIVER_MISMATCH")
                try:
                    released = self._session_close(reason="startup_bind_launched_game", expected_session=current)
                    if not released.get("closed"):
                        raise StartupError("CONTROL_SESSION_CHANGED_BEFORE_GAME_BINDING")
                    return self._open_startup_session(game_pid, seconds)
                except (ArmError, TypeError) as exc:
                    raise StartupError(f"CONTROL_SESSION_GAME_BINDING_FAILED: {exc}") from exc
            if game_pid is not None and current_pid is not None and int(current_pid) != game_pid:
                # A prior launch may have died while its control record was
                # still OPEN. Rebind only when the recorded PID is proven
                # absent; an actually running different game remains a hard
                # owner mismatch.
                environment = getattr(self.lifecycle, "environment", None)
                processes_probe = getattr(environment, "game_process_ids_verified", None) or getattr(environment, "game_process_ids", None)
                try:
                    if not callable(processes_probe):
                        raise StartupError("CONTROL_SESSION_PROCESS_PROBE_UNAVAILABLE")
                    processes = processes_probe()
                    if not isinstance(processes, dict):
                        raise StartupError("CONTROL_SESSION_PROCESS_PROBE_INVALID")
                except Exception as exc:
                    raise StartupError("CONTROL_SESSION_PROCESS_PROBE_FAILED") from exc
                if int(current_pid) not in processes and current.get("owner_matches", False):
                    try:
                        self._session_close(reason="startup_rebind_exited_game")
                        current = None
                    except (ArmError, TypeError) as exc:
                        raise StartupError(
                            f"CONTROL_SESSION_GAME_MISMATCH: cleanup failed: {exc}"
                        ) from exc
                else:
                    raise StartupError("CONTROL_SESSION_GAME_MISMATCH: session belongs to another game PID")
            if current is None:
                try:
                    return self._open_startup_session(game_pid, seconds)
                except (ArmError, TypeError) as exc:
                    raise StartupError(f"CONTROL_SESSION_OPEN_FAILED: {exc}") from exc
            try:
                values = {"seconds": seconds}
                binding = self._startup_session_binding.get()
                if binding:
                    # Title startup can open our lease before OBS is recording
                    # and the driver can be declared. Attribute that undeclared
                    # lease through the heartbeat; never replace another driver.
                    recording_driver = current.get("recording_driver")
                    if recording_driver is not None and recording_driver != binding["recording"]:
                        raise StartupError("CONTROL_SESSION_RECORDING_DRIVER_MISMATCH")
                    if binding.get("explicit_client") and current.get("client_id") != binding["client_id"]:
                        raise StartupError("CONTROL_SESSION_CLIENT_MISMATCH")
                    values.update(model_id=binding["recording"]["modelId"], reasoning_effort=binding["recording"]["effort"])
                result = self._session_heartbeat(**values)
                self._remember_temporary_session(result)
                if binding and result.get("recording_driver") != binding["recording"]:
                    raise StartupError("CONTROL_SESSION_ATTRIBUTION_UNVERIFIED")
                self._verify_bound_model_run()
                return result
            except (ArmError, TypeError) as exc:
                raise StartupError(f"CONTROL_SESSION_RENEW_FAILED: {exc}") from exc
        if current and self._startup_session_binding.get() and current.get("owner_matches") is not True:
            dead_stale_session = (
                current.get("state") == "STALE_WINDOWS_SESSION"
                and current.get("recorded_controller_alive") is False
                and current.get("recorded_game_alive") is False
                and type(current.get("controller_pid")) is int and current["controller_pid"] > 0
                and type(current.get("game_pid")) is int and current["game_pid"] > 0
            )
            if not dead_stale_session:
                raise StartupError("CONTROL_SESSION_OWNER_MISMATCH")
            # Delegate to SessionLease.open, which rechecks both processes
            # under its mutex and preserves the old record in an archive.
            # Do not close or rewrite a historical lease from this layer.
        if current and current.get("state") in {"EXPIRED", "CLOSED", "GAME_EXITED", "OWNER_MISMATCH"}:
            if current.get("state") in {"EXPIRED", "CLOSED", "GAME_EXITED"} and current.get("owner_matches", True):
                try:
                    self._session_close(reason="startup_recover_prior_session")
                    current = None
                except (ArmError, TypeError) as exc:
                    raise StartupError(
                        f"CONTROL_SESSION_ARCHIVE_REQUIRED: {exc}"
                    ) from exc
            else:
                raise StartupError(
                    "CONTROL_SESSION_ARCHIVE_REQUIRED: archive the prior control session before reopening"
                )
        try:
            return self._open_startup_session(game_pid, seconds)
        except (ArmError, TypeError) as exc:
            raise StartupError(f"CONTROL_SESSION_OPEN_FAILED: {exc}") from exc

    def _wait_interactive(
        self,
        timeout_seconds: float,
        *,
        allow_initial_process_missing: bool = False,
        fail_fast_headless: bool = True,
        allow_visual_startup_boundary: bool = False,
    ) -> dict[str, Any]:
        """Wait for live telemetry and a reachable input route without input."""
        started = self._monotonic()
        timeout_seconds = self._bounded_wait(timeout_seconds)
        deadline = started + max(1.0, timeout_seconds)
        latest: dict[str, Any] | None = None
        process_grace = min(20.0, max(3.0, float(timeout_seconds) * 0.45))
        next_visual_probe = started
        next_focus_attempt = started
        focus_attempts = 0
        while self._monotonic() < deadline:
            latest = self.service.session_health()
            process_exists = latest.get("process_exists")
            if process_exists is False:
                if allow_initial_process_missing and self._monotonic() - started < process_grace:
                    self._sleep(0.1)
                    continue
                raise StartupError("STARTUP_FAILED: owned game process exited or never appeared")
            # Treat an absent/unknown input route as unsafe too. Menu input
            # and visual startup evidence need either foreground keyboard
            # input or the explicitly enabled background gamepad route.
            if process_exists is True and latest.get("input_reachable", latest.get("foreground_window")) is not True:
                focus = getattr(self.lifecycle, "focus_game_window", None)
                now = self._monotonic()
                if callable(focus) and (
                    focus_attempts < FOREGROUND_FOCUS_MAX_ATTEMPTS
                    and now >= next_focus_attempt
                ):
                    try:
                        recovery = focus()
                    except Exception as exc:
                        recovery = {"ok": False, "decision": str(exc)}
                    focus_attempts += 1
                    latest.setdefault("foreground_recovery", []).append(recovery)
                    next_focus_attempt = now + FOREGROUND_FOCUS_RETRY_INTERVAL_SECONDS
                if not callable(focus):
                    latest["foreground_recovery"] = [{
                        "ok": False,
                        "state": "foreground_recovery_unavailable",
                    }]
                    latest["foreground_recovery_attempts"] = 0
                    latest["foreground_recovery_exhausted"] = True
                    raise StartupPending(
                        "STARTUP_PENDING: verified foreground window is required, "
                        "but foreground recovery is unavailable; "
                        f"last_health={latest}",
                        phase="startup_loading",
                    )
                if (
                    focus_attempts >= FOREGROUND_FOCUS_MAX_ATTEMPTS
                    and now >= next_focus_attempt
                ):
                    latest["foreground_recovery_attempts"] = focus_attempts
                    latest["foreground_recovery_exhausted"] = True
                    raise StartupPending(
                        "STARTUP_PENDING: verified foreground window was not established "
                        f"after {focus_attempts} bounded recovery attempts; "
                        f"last_health={latest}",
                        phase="startup_loading",
                    )
            if (
                latest.get("telemetry_live")
                and process_exists is not False
                and latest.get("input_reachable", latest.get("foreground_window")) is True
            ):
                return latest
            if (
                allow_visual_startup_boundary
                and process_exists is True
                and latest.get("input_reachable", latest.get("foreground_window")) is True
                and not latest.get("telemetry_live")
                and self._monotonic() >= next_visual_probe
            ):
                try:
                    visual = dict(self.service.capture_game_window())
                except Exception:
                    visual = {}
                frame = visual.get("startup_frame")
                if isinstance(frame, dict) and frame.get("label") in {"title_prompt", "main_menu"}:
                    latest["startup_frame"] = frame
                    latest["startup_visual_capture"] = visual
                    return latest
                next_visual_probe = self._monotonic() + 0.5
            self._sleep(0.1)
        if (
            isinstance(latest, dict)
            and latest.get("process_exists") is True
            and latest.get("input_reachable", latest.get("foreground_window")) is not True
        ):
            latest["foreground_recovery_attempts"] = focus_attempts
            latest["foreground_recovery_exhausted"] = (
                focus_attempts >= FOREGROUND_FOCUS_MAX_ATTEMPTS
            )
            raise StartupPending(
                "STARTUP_PENDING: verified foreground window was not established "
                f"after {focus_attempts} bounded recovery attempts; "
                f"last_health={latest}",
                phase="startup_loading",
            )
        raise StartupPending(
            "STARTUP_PENDING: no usable game input route or fresh "
            f"telemetry yet; last_health={latest}",
            phase="startup_loading",
        )

    def _capture(self, label: str) -> dict[str, Any]:
        try:
            result = dict(self.service.capture_game_window())
        except Exception as exc:
            raise StartupError(f"STARTUP_CAPTURE_FAILED:{label}: {exc}") from exc
        result["startup_phase_label"] = label
        return result

    def _wait_for_startup_boundary(self, timeout_seconds: float) -> dict[str, Any]:
        """Wait for a positively classified title or main-menu frame."""
        timeout_seconds = self._bounded_wait(timeout_seconds)
        deadline = self._monotonic() + max(1.0, float(timeout_seconds))
        latest: dict[str, Any] | None = None
        while self._monotonic() < deadline:
            self._wait_interactive(
                min(2.0, max(1.0, deadline - self._monotonic())),
                fail_fast_headless=False,
                allow_visual_startup_boundary=True,
            )
            latest = self._capture("startup-boundary-probe")
            frame = latest.get("startup_frame")
            if isinstance(frame, dict) and frame.get("label") in {"title_prompt", "main_menu"}:
                return latest
            # An all-black/unknown frame is expected during a slow shader or
            # save-load transition.  Keep waiting, but never press a key based
            # on telemetry alone.
            self._sleep(0.5)
        raise StartupPending(
            "STARTUP_PENDING: no positively classified title or main-menu frame "
            f"yet; last_capture={latest}",
            phase="startup_loading",
        )

    def _wait_for_main_menu(self, timeout_seconds: float) -> dict[str, Any]:
        """Wait for a conservative visual main-menu classification."""
        timeout_seconds = self._bounded_wait(timeout_seconds)
        deadline = self._monotonic() + max(1.0, float(timeout_seconds))
        latest: dict[str, Any] | None = None
        while self._monotonic() < deadline:
            health = self._wait_interactive(
                min(2.0, max(1.0, deadline - self._monotonic())),
                fail_fast_headless=False,
                allow_visual_startup_boundary=True,
            )
            latest = self._capture("main-menu-probe")
            frame = latest.get("startup_frame")
            if isinstance(frame, dict) and frame.get("label") == "main_menu":
                latest["main_menu_health"] = health
                return latest
            self._sleep(0.5)
        raise StartupPending(
            "STARTUP_PENDING: title prompt was dismissed but the main menu was "
            f"not visually confirmed yet; last_capture={latest}",
            phase="startup_loading",
        )

    def _wait_for_in_game_menu(self, timeout_seconds: float) -> dict[str, Any]:
        """Wait for telemetry to prove that the active-game menu is open."""
        deadline = self._monotonic() + max(1.0, float(timeout_seconds))
        latest: dict[str, Any] | None = None
        while self._monotonic() < deadline:
            latest = self.service.session_health()
            if latest.get("process_exists") is False:
                raise StartupError("LOAD_LAST_SAVE_FAILED: game process exited")
            if latest.get("foreground_window") is False:
                raise StartupError("LOAD_LAST_SAVE_FAILED: game is not foreground")
            if (
                latest.get("telemetry_live")
                and (latest.get("paused") or latest.get("in_menu"))
            ):
                return latest
            self._sleep(0.1)
        raise StartupError(
            "LOAD_LAST_SAVE_MENU_TIMEOUT: in-game menu was not observed; "
            f"last_health={latest}"
        )

    def _wait_fresh(self, timeout_seconds: float) -> dict[str, Any]:
        try:
            return self.lifecycle.wait_for_fresh_telemetry(
                self._bounded_wait(timeout_seconds)
            )
        except AutonomyError as exc:
            raise StartupPending(
                f"STARTUP_PENDING: fresh advancing telemetry is not available: {exc}",
                phase="telemetry_pending",
            ) from exc

    def _wait_ready(self, timeout_seconds: float) -> dict[str, Any]:
        try:
            return self.lifecycle.wait_for_gameplay_ready(
                self._bounded_wait(timeout_seconds)
            )
        except AutonomyError as exc:
            raise StartupPending(
                f"GAMEPLAY_LOADING: settled gameplay is not ready yet: {exc}",
                phase="gameplay_loading",
            ) from exc

    def _wait_ready_resumable(self, timeout_seconds: float) -> dict[str, Any]:
        """Wait in short worker-safe slices for a background startup job.

        The ordinary MCP request path deliberately caps internal waits so a
        slow save load cannot wedge the bridge. A background startup job has
        its own response channel, so it may continue across several bounded
        readiness slices while the caller polls status. Each slice remains
        short, and the total wait remains explicitly bounded.
        """
        deadline = self._monotonic() + max(1.0, float(timeout_seconds))
        last_error = "gameplay readiness was not observed"
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise StartupPending(
                    "GAMEPLAY_LOADING: settled gameplay did not become ready "
                    f"within {float(timeout_seconds):.1f}s; last_error={last_error}",
                    phase="gameplay_loading",
                )
            try:
                return self.lifecycle.wait_for_gameplay_ready(
                    min(STARTUP_LOCAL_WAIT_SECONDS, remaining)
                )
            except AutonomyError as exc:
                last_error = str(exc)

    def resume_gameplay_readiness(
        self,
        *,
        readiness_timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        """Complete a previously confirmed Continue selection.

        This method is intentionally input-free. It is called only after the
        Continue/Resume confirmations have already been sent and the durable
        startup phase is gameplay_loading. It can therefore be run by the
        asynchronous startup worker without replaying menu input.
        """
        try:
            lifecycle = self.lifecycle.ensure_started(allow_launch=False)
            context = dict(self.lifecycle.context or lifecycle.get("context") or {})
            if (context.get("startup_phase") or "") != "gameplay_loading":
                return {
                    "ok": False,
                    "phase": "startup_not_resumable",
                    "decision": (
                        "STARTUP_RESUME_REQUIRES_GAMEPLAY_LOADING: "
                        f"actual_phase={context.get('startup_phase')!r}"
                    ),
                    "input_replayed": False,
                }
            session = self._session_status()
            if session is not None and session.get("active") is not True:
                return {
                    "ok": False,
                    "phase": "startup_not_resumable",
                    "decision": "STARTUP_RESUME_REQUIRES_ACTIVE_CONTROL_SESSION",
                    "input_replayed": False,
                    "control_session": session,
                }
            ready = self._wait_ready_resumable(readiness_timeout_seconds)
            settled = None
            settle = getattr(self.service, "mark_gameplay_settled", None)
            if callable(settle):
                settled = settle(ready.get("run_binding", {}).get("stable_samples"))
            gamepad_reconnect = self._reconnect_gamepad_after_gameplay()
            if gamepad_reconnect is not None and not gamepad_reconnect.get("ok", False):
                cleanup = self._cleanup_after_failure(close_session=True)
                return {
                    "ok": False,
                    "phase": "startup_failed",
                    "decision": gamepad_reconnect.get(
                        "decision", "GAMEPAD_RECONNECT_FAILED"
                    ),
                    "ready_telemetry": ready,
                    "settled_gameplay": settled,
                    "gamepad_reconnect": gamepad_reconnect,
                    "control_session": session,
                    "input_replayed": False,
                    "cleanup": cleanup,
                }
            self.lifecycle.mark_startup_phase("gameplay_ready")
            return {
                "ok": True,
                "phase": "gameplay_ready",
                "mode": "continue_previous_save",
                "ready_telemetry": ready,
                "settled_gameplay": settled,
                "gamepad_reconnect": gamepad_reconnect,
                "control_session": session,
                "input_replayed": False,
            }
        except StartupPending as exc:
            return {
                "ok": False,
                "pending": True,
                "phase": exc.phase,
                "decision": str(exc),
                "input_replayed": False,
                "retry": "poll_readiness_or_resume_same_phase_without_replaying_input",
            }
        except (AutonomyError, OSError, ValueError, RuntimeError) as exc:
            return {
                "ok": False,
                "phase": "startup_failed",
                "decision": f"STARTUP_RESUME_FAILED: {exc}",
                "input_replayed": False,
            }

    def _tap_confirm(self) -> dict[str, Any]:
        try:
            cooldown = getattr(self.service, "_wait_for_coordinator_cooldown", None)
            if callable(cooldown):
                cooldown()
            # Defaults are intentionally not reselected: Enter confirms the
            # already-selected Continue/Yes choices in the known path.
            startup_tap = getattr(self.service, "startup_tap", None)
            if callable(startup_tap):
                try:
                    return startup_tap("confirm", 500)
                except Exception:
                    # Once telemetry is live, ordinary menu input remains the
                    # correct path for save prompts that are not classified as
                    # title/main-menu frames.
                    pass
            return self.service.tap("confirm", 500)
        except Exception as exc:
            raise StartupError(f"STARTUP_CONFIRM_FAILED: {exc}") from exc

    def _reconnect_gamepad_after_gameplay(self) -> dict[str, Any] | None:
        """Refresh an optional virtual controller after the save world settles."""
        reconnect = getattr(self.service, "reconnect_gamepad", None)
        if not callable(reconnect):
            return None
        try:
            result = reconnect()
            return result if isinstance(result, dict) else {"ok": True, "result": result}
        except Exception as exc:
            return {"ok": False, "decision": f"GAMEPAD_RECONNECT_FAILED: {exc}"}

    def _cleanup_after_failure(
        self,
        *,
        close_game: bool = False,
        close_session: bool = True,
    ) -> dict[str, Any]:
        """Make a failed startup retry-safe without touching external runs."""
        cleanup: dict[str, Any] = {}
        if close_session:
            try:
                cleanup["control_session"] = self._session_close(
                    reason="startup_failure_cleanup"
                )
            except Exception as exc:
                cleanup["control_session"] = {
                    "closed": False,
                    "decision": f"CONTROL_SESSION_CLEANUP_FAILED: {exc}",
                }
        if close_game:
            close = getattr(self.lifecycle, "close_game", None)
            if callable(close):
                try:
                    cleanup["game"] = close(timeout_seconds=8.0)
                except Exception as exc:
                    cleanup["game"] = {
                        "ok": False,
                        "decision": f"GAME_CLEANUP_FAILED: {exc}",
                    }
            else:
                cleanup["game"] = {
                    "ok": False,
                    "decision": "GAME_CLEANUP_UNAVAILABLE",
                }
        cleanup["retryable"] = bool(
            cleanup.get("game", {}).get("ok")
            if isinstance(cleanup.get("game"), dict)
            else not close_game
        )
        return cleanup

    def start_game_to_main_menu(
        self,
        *,
        session_seconds: int = 1800,
        launch_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
        dismiss_attached_title_prompt: bool = True,
    ) -> dict[str, Any]:
        """Phase 1: launch/attach and stop at the title-cleared main menu."""
        owned_launch = False
        mode: str | None = None
        session_acquired = False
        session: dict[str, Any] | None = None
        try:
            lifecycle = self.lifecycle.ensure_started(allow_launch=True)
            context = dict(self.lifecycle.context or lifecycle.get("context") or {})
            mode = lifecycle.get("mode")
            owned_launch = self._is_owned_launch(mode, context)
            session = self._ensure_session(self._pid(context), session_seconds)
            session_acquired = True
            health = self._wait_interactive(
                interactive_timeout_seconds,
                allow_initial_process_missing=owned_launch,
                fail_fast_headless=not owned_launch,
                allow_visual_startup_boundary=True,
            )
            # A title prompt can be visible before UE4SS publishes its first
            # snapshot.  Do not block on telemetry before using the bounded
            # visual-startup path; after the prompt is dismissed, the normal
            # fresh-telemetry wait resumes below.
            fresh = None
            if not isinstance(health.get("startup_visual_capture"), dict):
                fresh = self._wait_fresh(launch_timeout_seconds)
            # A live spatial snapshot can precede the point at which the save
            # world accepts gameplay input.  Let the service's bounded,
            # low-rate settle gate prove readiness before taking the shortcut.
            if health.get("loaded_gameplay") and not health.get("gameplay_ready"):
                settle = getattr(self.service, "wait_for_settled_gameplay", None)
                if callable(settle):
                    settle(launch_timeout_seconds)
                    health = self.service.session_health()
            if health.get("gameplay_ready"):
                self.lifecycle.mark_startup_phase("gameplay_ready")
                return {
                    "ok": True,
                    "phase": "gameplay_ready",
                    "mode": mode,
                    "already_ready": True,
                    "fresh_telemetry": fresh,
                    "health": health,
                    "control_session": session,
                }
            # The interactive wait may already have captured the first
            # positively classified startup frame while telemetry was stale.
            # Reuse that exact evidence so a title prompt is not consumed by
            # the probe and accidentally treated as an already-open menu.
            initial_visual = health.get("startup_visual_capture")
            if isinstance(initial_visual, dict):
                before = dict(initial_visual)
                before["startup_phase_label"] = "startup-boundary-probe"
            else:
                before = self._wait_for_startup_boundary(interactive_timeout_seconds)
            boundary = before.get("startup_frame")
            if isinstance(boundary, dict) and boundary.get("label") == "main_menu":
                # A visual menu frame is useful evidence, but it is not enough
                # to authorize the next phase.  On a slow cold start the old
                # telemetry file can still contain a stale sequence while the
                # menu is already rendered.  Require a fresh advancing sample
                # before reporting the phase as ready.
                if fresh is None:
                    fresh = self._wait_fresh(launch_timeout_seconds)
                self.lifecycle.mark_startup_phase("main_menu")
                return {
                    "ok": True,
                    "phase": "main_menu",
                    "mode": mode,
                    "title_prompt_dismissed": False,
                    "already_at_main_menu": True,
                    "fresh_telemetry": fresh,
                    "capture": before,
                    "control_session": session,
                }
            if mode == "attached" and not dismiss_attached_title_prompt:
                cleanup = self._cleanup_after_failure(close_session=True)
                return {
                    "ok": False,
                    "phase": "visual_confirmation_required",
                    "decision": "TITLE_PROMPT_VISUAL_CONFIRMATION_REQUIRED",
                    "mode": mode,
                    "fresh_telemetry": fresh,
                    "health": health,
                    "capture": before,
                    "control_session": session,
                    "cleanup": cleanup,
                }
            title_confirm = self._tap_confirm()
            after_fresh = self._wait_fresh(launch_timeout_seconds)
            after = self._wait_for_main_menu(interactive_timeout_seconds)
            after_health = after.get("main_menu_health") or self.service.session_health()
            self.lifecycle.mark_startup_phase("main_menu")
            return {
                "ok": True,
                "phase": "main_menu",
                "mode": mode,
                "title_prompt_dismissed": True,
                "fresh_telemetry": fresh,
                "title_confirm": title_confirm,
                "after_confirm_telemetry": after_fresh,
                "after_confirm_health": after_health,
                "capture_before": before,
                "capture_after": after,
                "control_session": session,
            }
        except StartupPending as exc:
            return {
                "ok": False,
                "pending": True,
                "phase": exc.phase,
                "decision": str(exc),
                "mode": mode,
                "control_session": session,
                "retry": "resume_the_same_phase_without_replaying_input",
            }
        except StartupError as exc:
            return {
                "ok": False,
                "phase": "startup_failed",
                "decision": str(exc),
                "cleanup": self._cleanup_after_failure(
                    close_game=owned_launch,
                    close_session=session_acquired,
                ),
            }
        except (AutonomyError, OSError, ValueError) as exc:
            return {
                "ok": False,
                "phase": "startup_failed",
                "decision": str(exc),
                "cleanup": self._cleanup_after_failure(
                    close_game=owned_launch,
                    close_session=session_acquired,
                ),
            }

    def continue_from_last_save(
        self,
        *,
        session_seconds: int = 1800,
        selection_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        """Phase 2 default path: Continue, then Resume/Yes, then gameplay."""
        return self.select_game_start(
            "continue_previous_save",
            session_seconds=session_seconds,
            selection_timeout_seconds=selection_timeout_seconds,
            interactive_timeout_seconds=interactive_timeout_seconds,
            expect_gameplay_ready=True,
            confirmation_presses=2,
        )

    def load_last_save(
        self,
        *,
        menu_actions: list[str] | None = None,
        session_seconds: int = 1800,
        selection_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
        confirmation_presses: int = LOAD_LAST_SAVE_CONFIRMATIONS,
    ) -> dict[str, Any]:
        """Load the latest save from active gameplay without restarting Rebirth.

        The default path opens the in-game menu, waits for the menu state and
        a short render/input settling period, then follows the explicit
        Save/Load -> Load -> latest-save -> Yes path.  A caller may supply a
        different bounded cursor path when the game opens with another item
        selected.  The operation never launches or closes the game.
        """
        actions = list(
            LOAD_LAST_SAVE_DEFAULT_MENU_ACTIONS
            if menu_actions is None else menu_actions
        )
        invalid = [action for action in actions if action not in MENU_ACTIONS]
        if invalid:
            return {
                "ok": False,
                "phase": "load_failed",
                "decision": f"INVALID_MENU_ACTIONS: {invalid}",
                "mode": "load_last_save",
            }
        if not 1 <= int(confirmation_presses) <= 4:
            return {
                "ok": False,
                "phase": "load_failed",
                "decision": "INVALID_CONFIRMATION_PRESS_COUNT",
                "mode": "load_last_save",
            }

        session_acquired = False
        session: dict[str, Any] | None = None
        actions_sent: list[dict[str, Any]] = []
        confirmations: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        try:
            lifecycle = self.lifecycle.ensure_started(allow_launch=False)
            context = dict(self.lifecycle.context or lifecycle.get("context") or {})
            session = self._ensure_session(self._pid(context), session_seconds)
            session_acquired = True
            health = self._wait_interactive(interactive_timeout_seconds)
            if health.get("loaded_gameplay") and not health.get("gameplay_settled"):
                settle = getattr(self.service, "wait_for_settled_gameplay", None)
                if callable(settle):
                    try:
                        settle(interactive_timeout_seconds)
                        health = self.service.session_health()
                    except (AutonomyError, OSError, RuntimeError, ValueError) as exc:
                        cleanup = self._cleanup_after_failure(close_session=True)
                        return {
                            "ok": False,
                            "phase": "load_failed",
                            "decision": f"LOAD_GAMEPLAY_SETTLE_FAILED: {exc}",
                            "mode": "load_last_save",
                            "health": health,
                            "control_session": session,
                            "cleanup": cleanup,
                        }
            if not health.get("gameplay_ready"):
                cleanup = self._cleanup_after_failure(close_session=True)
                return {
                    "ok": False,
                    "phase": "load_failed",
                    "decision": (
                        "LOAD_REQUIRES_GAMEPLAY: active settled gameplay is required "
                        "before opening the in-game menu"
                    ),
                    "mode": "load_last_save",
                    "health": health,
                    "control_session": session,
                    "cleanup": cleanup,
                }

            self.lifecycle.mark_startup_phase("selection_in_progress")
            cooldown = getattr(self.service, "_wait_for_coordinator_cooldown", None)
            if callable(cooldown):
                cooldown()
            menu_open = self.service.tap("menu", 500)
            menu_health = self._wait_for_in_game_menu(interactive_timeout_seconds)
            # Telemetry proves that the menu is open; this additional bounded
            # pause prevents cursor inputs racing the first rendered frame on
            # slow machines.
            self._sleep(LOAD_LAST_SAVE_MENU_SETTLE_SECONDS)
            self._wait_fresh(selection_timeout_seconds)

            actions_sent: list[dict[str, Any]] = []
            for action in actions:
                # The service repeats this guard, but waiting for the menu
                # state here avoids racing the first menu frame after Options.
                self._wait_for_in_game_menu(selection_timeout_seconds)
                milliseconds = (
                    LOAD_LAST_SAVE_CONFIRM_TAP_MS
                    if action == "confirm"
                    else LOAD_LAST_SAVE_CURSOR_TAP_MS
                )
                cooldown = getattr(self.service, "_wait_for_coordinator_cooldown", None)
                if callable(cooldown):
                    cooldown()
                actions_sent.append(self.service.tap(action, milliseconds))
                self._wait_fresh(selection_timeout_seconds)

            confirmations: list[dict[str, Any]] = []
            for index in range(int(confirmation_presses)):
                self._wait_for_in_game_menu(selection_timeout_seconds)
                if index == 0:
                    invalidate = getattr(self.service, "invalidate_loaded_save", None)
                    if callable(invalidate):
                        invalidate("unverified_load_last_save")
                cooldown = getattr(self.service, "_wait_for_coordinator_cooldown", None)
                if callable(cooldown):
                    cooldown()
                confirmations.append(self.service.tap("confirm", 500))
                if index + 1 < int(confirmation_presses):
                    self._wait_fresh(selection_timeout_seconds)

            # Save loading can take longer than the MCP host's bounded
            # request window on this machine.  Wait briefly for the common
            # fast path, but return a structured pending phase rather than
            # allowing the worker request to be killed while the game is
            # still loading.  The caller can then use the normal
            # wait_for_gameplay_ready operation without replaying input.
            try:
                ready = self._wait_ready(
                    min(
                        LOAD_LAST_SAVE_READINESS_TIMEOUT_SECONDS,
                        max(1.0, float(selection_timeout_seconds)),
                    )
                )
            except StartupError as exc:
                self.lifecycle.mark_startup_phase("gameplay_loading")
                return {
                    "ok": False,
                    "phase": "gameplay_loading",
                    "decision": "LOAD_INPUT_ACCEPTED_GAMEPLAY_READINESS_PENDING",
                    "readiness_pending": str(exc),
                    "mode": "load_last_save",
                    "menu_open": menu_open,
                    "menu_health": menu_health,
                    "actions": actions_sent,
                    "confirmations": confirmations,
                    "control_session": session,
                }
            settled = None
            settle = getattr(self.service, "mark_gameplay_settled", None)
            if callable(settle):
                settled = settle(ready.get("run_binding", {}).get("stable_samples"))
            gamepad_reconnect = self._reconnect_gamepad_after_gameplay()
            if gamepad_reconnect is not None and not gamepad_reconnect.get("ok", False):
                cleanup = self._cleanup_after_failure(close_session=True)
                return {
                    "ok": False,
                    "phase": "load_failed",
                    "decision": gamepad_reconnect.get(
                        "decision", "GAMEPAD_RECONNECT_FAILED"
                    ),
                    "mode": "load_last_save",
                    "menu_open": menu_open,
                    "menu_health": menu_health,
                    "actions": actions_sent,
                    "confirmations": confirmations,
                    "ready_telemetry": ready,
                    "settled_gameplay": settled,
                    "gamepad_reconnect": gamepad_reconnect,
                    "control_session": session,
                    "cleanup": cleanup,
                }
            self.lifecycle.mark_startup_phase("gameplay_ready")
            return {
                "ok": True,
                "phase": "gameplay_ready",
                "mode": "load_last_save",
                "menu_open": menu_open,
                "menu_health": menu_health,
                "actions": actions_sent,
                "confirmations": confirmations,
                "ready_telemetry": ready,
                "settled_gameplay": settled,
                "gamepad_reconnect": gamepad_reconnect,
                "control_session": session,
            }
        except StartupPending as exc:
            return {
                "ok": False,
                "pending": True,
                "phase": exc.phase,
                "decision": str(exc),
                "mode": "load_last_save",
                "actions": actions_sent,
                "confirmations": confirmations,
                "control_session": session,
                "retry": (
                    "call_wait_for_gameplay_ready_without_replaying_input"
                    if confirmations
                    else "resume_the_same_phase_without_replaying_input"
                ),
            }
        except StartupError as exc:
            return {
                "ok": False,
                "phase": "load_failed",
                "decision": str(exc),
                "mode": "load_last_save",
                "cleanup": self._cleanup_after_failure(close_session=session_acquired),
            }
        except (AutonomyError, OSError, ValueError, RuntimeError) as exc:
            return {
                "ok": False,
                "phase": "load_failed",
                "decision": str(exc),
                "mode": "load_last_save",
                "cleanup": self._cleanup_after_failure(close_session=session_acquired),
            }

    def start_and_continue_from_last_save(
        self,
        *,
        session_seconds: int = 1800,
        launch_timeout_seconds: float = 60.0,
        selection_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        """Compose phase 1 startup with the default Continue path."""
        phase_one = self.start_game_to_main_menu(
            session_seconds=session_seconds,
            launch_timeout_seconds=launch_timeout_seconds,
            interactive_timeout_seconds=interactive_timeout_seconds,
        )
        if not phase_one.get("ok"):
            return {
                "ok": False,
                "phase": phase_one.get("phase", "startup_failed"),
                "decision": phase_one.get("decision", "STARTUP_PHASE_ONE_FAILED"),
                "start": phase_one,
            }
        if phase_one.get("phase") == "gameplay_ready":
            return {
                "ok": True,
                "phase": "gameplay_ready",
                "already_ready": True,
                "start": phase_one,
            }
        phase_two = self.continue_from_last_save(
            session_seconds=session_seconds,
            selection_timeout_seconds=selection_timeout_seconds,
            interactive_timeout_seconds=interactive_timeout_seconds,
        )
        return {
            "ok": bool(phase_two.get("ok")),
            "phase": phase_two.get("phase", "selection_failed"),
            "decision": phase_two.get("decision"),
            "start": phase_one,
            "continue": phase_two,
        }

    def start_and_continue_from_last_save_resumable(
        self,
        *,
        session_seconds: int = 1800,
        launch_timeout_seconds: float = 60.0,
        selection_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
        readiness_timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        """Run startup and continue, then finish slow gameplay readiness.

        The menu/input portion is the existing composed wrapper. If it reaches
        gameplay_loading after sending all confirmations, the input-free
        readiness continuation runs in the same worker invocation. This makes
        the complete operation safe to poll without replaying Continue/Resume
        input or relying on a long synchronous bridge request.
        """
        def attempt() -> dict[str, Any]:
            return self.start_and_continue_from_last_save(
                session_seconds=session_seconds,
                launch_timeout_seconds=launch_timeout_seconds,
                selection_timeout_seconds=selection_timeout_seconds,
                interactive_timeout_seconds=interactive_timeout_seconds,
            )

        result = attempt()
        retries = 0
        while (
            not result.get("ok")
            and result.get("phase") == "startup_loading"
            and str(result.get("decision") or "").startswith("STARTUP_PENDING")
            and retries < STARTUP_PENDING_MAX_RETRIES
        ):
            retries += 1
            self._sleep(STARTUP_PENDING_RETRY_SECONDS)
            result = attempt()
        result["startup_pending_retries"] = retries
        if not result.get("ok") and result.get("phase") == "gameplay_loading":
            continuation = self.resume_gameplay_readiness(
                readiness_timeout_seconds=readiness_timeout_seconds,
            )
            result["readiness_resume"] = continuation
            result["ok"] = bool(continuation.get("ok"))
            result["phase"] = continuation.get("phase", result.get("phase"))
            result["decision"] = continuation.get("decision", result.get("decision"))
        if result.get("ok"):
            render = self._await_rendered_world()
            result["render_check"] = render
            if not render.get("ok"):
                result.update(
                    ok=False,
                    phase="render_stuck_loading",
                    decision=(
                        "STARTUP_RENDER_STUCK_LOADING: telemetry reports loaded gameplay "
                        "but the screen still shows the loading screen; restart the game"
                    ),
                )
        return result

    def _await_rendered_world(self, timeout_seconds: float = RENDER_WAIT_SECONDS) -> dict[str, Any]:
        """Wait until the screen shows more than the black loading screen.

        Live 2026-09-28 the game stayed on its black "Loading" screen while
        telemetry (and the music) reported a loaded, settled world, so a
        telemetry-only readiness check passed. The frame classifier names
        that screen ("loading": black apart from the blue Loading widget in
        the bottom-right corner). A capture failure is not treated as a stuck
        load.
        """
        capture = getattr(self.service, "capture_game_window", None)
        if not callable(capture):
            return {"ok": True, "checked": False}
        deadline = self._monotonic() + timeout_seconds
        while True:
            try:
                visual = dict(capture())
            except Exception as exc:
                # A background PrintWindow returns no colour at all while the
                # loading screen is up (live 2026-10-14, a fast travel and a
                # save load), so keep waiting on that. If it outlasts the
                # wait, the render is unverified rather than stuck.
                if BLANK_BACKGROUND_CAPTURE in str(exc) and self._monotonic() < deadline:
                    self._sleep(RENDER_POLL_SECONDS)
                    continue
                return {"ok": True, "checked": False, "error": str(exc)}
            frame = visual.get("startup_frame") if isinstance(visual.get("startup_frame"), dict) else {}
            metrics = frame.get("metrics") if isinstance(frame.get("metrics"), dict) else {}
            nonblack = metrics.get("whole_nonblack")
            label = frame.get("render_classifier_label", frame.get("label"))
            evidence = {
                "whole_nonblack": nonblack,
                "render_label": label,
                "screenshot": visual.get("image_path", visual.get("path")),
            }
            if label != "loading" and (
                    not isinstance(nonblack, (int, float)) or nonblack > LOADING_FRAME_MAX_NONBLACK):
                return {"ok": True, "checked": True, **evidence}
            if self._monotonic() >= deadline:
                return {"ok": False, "checked": True, **evidence}
            self._sleep(RENDER_POLL_SECONDS)

    def start_and_load_save_slot(self, *, model_id: str | None = None,
                                 reasoning_effort: str | None = None,
                                 client_id: str | None = None, **arguments) -> dict[str, Any]:
        """Load with exact explicit attribution or a verified registered declaration.

        Registered model startup/recovery binds the complete immutable declaration
        and assignment through every new/renewed session. An owned undeclared lease
        can acquire attribution; foreign or conflicting leases are refused.
        Implicit startup leases are released on exit so the next gameplay client
        can open its own lease. Explicit caller leases remain caller-managed.
        Generic legacy loads are unchanged.
        """
        from .recording_driver import validate_recording_driver
        bound = None
        try:
            if arguments.get("model_run"):
                run = self.service.get_queens_blood_run_status()
                if not run.get("driver") or not run.get("assignment"):
                    return {"ok": False, "phase": "load_failed", "decision": "MODEL_RUN_UNASSIGNED",
                            "input_sent": False, "started": False}
                driver = run["driver"]
                recording = validate_recording_driver(driver.get("model_id"), driver.get("effort"))
                if recording is None or not isinstance(driver.get("run_id"), str) or not driver["run_id"]:
                    raise StartupError("MODEL_RUN_RECORDING_DRIVER_UNVERIFIED")
                explicit = validate_recording_driver(model_id, reasoning_effort)
                if explicit is not None and explicit != recording:
                    raise StartupError("MODEL_RUN_RECORDING_DRIVER_MISMATCH")
                bound = {"recording": recording, "run": deepcopy({key: run[key] for key in ("driver", "assignment")}),
                         "client_id": client_id or "model-startup:" + driver["run_id"], "explicit_client": client_id is not None}
            elif model_id is not None or reasoning_effort is not None:
                bound = {"recording": validate_recording_driver(model_id, reasoning_effort), "run": None,
                         "client_id": client_id, "explicit_client": client_id is not None}
            elif client_id is not None:
                raise StartupError("CONTROL_SESSION_EXPLICIT_DRIVER_REQUIRED")
        except (ValueError, StartupError) as exc:
            return {"ok": False, "phase": "load_failed", "decision": str(exc), "input_sent": False, "started": False}
        token = self._startup_session_binding.set(bound)
        result = None
        try:
            result = self._start_and_load_save_slot(**arguments)
        finally:
            try:
                if bound and bound.get("temporary_session"):
                    released = self._release_temporary_session(bound)
                    if result is not None:
                        result["control_session_release"] = released
                        if not released["ok"] and result.get("ok"):
                            result.update(ok=False, phase="load_failed",
                                          decision="MODEL_RUN_CONTROL_SESSION_RELEASE_FAILED")
            finally:
                self._startup_session_binding.reset(token)
        return result

    def _start_and_load_save_slot(
        self,
        *,
        slot: Any = None,
        model_run: bool = False,
        execute: bool = False,
        skip_if_latest: bool = False,
        session_seconds: int = 1800,
        launch_timeout_seconds: float = 60.0,
        selection_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
        readiness_timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        """Start Rebirth and load the requested slot directly from its title.

        An attached field world uses the existing field loader. At the title,
        Continue is only a fallback when the direct path cannot be verified
        and has safely returned to the menu before confirming a load. Both
        paths bind the same slot/account/fingerprint receipt. Resolve the
        slot before launch; dry runs only highlight it and close the list.
        """
        from .save_slot_load import (
            SaveSlotLoadError,
            aliases_path_for,
            load_title_save_slot,
            resolve_save_slot,
        )

        run = None
        if model_run:
            run = self.service.get_queens_blood_run_status()
            if not run.get("driver") or not run.get("assignment"):
                return {"ok": False, "phase": "load_failed", "decision": "MODEL_RUN_UNASSIGNED",
                        "input_sent": False, "started": False}
            slot = run["assignment"]["slot"]
            execute, skip_if_latest = True, False

        try:
            target = resolve_save_slot(slot, aliases_path=aliases_path_for(self.service),
                                       steam_id=run["assignment"]["steam_id"] if run else None)
            if run and target["file"]["steam_id"] != run["assignment"]["steam_id"]:
                return {"ok": False, "phase": "load_failed", "decision": "MODEL_RUN_ACCOUNT_MISMATCH",
                        "input_sent": False, "started": False}
        except SaveSlotLoadError as exc:
            return {
                "ok": False,
                "phase": "load_failed",
                "decision": exc.code,
                "details": exc.details,
                "mode": "start_and_load_save_slot",
                "started": False,
            }
        def start_title():
            return self.start_game_to_main_menu(
                session_seconds=session_seconds, launch_timeout_seconds=launch_timeout_seconds,
                interactive_timeout_seconds=interactive_timeout_seconds)

        start = start_title()
        retries = 0
        while (not start.get("ok") and start.get("phase") == "startup_loading"
               and str(start.get("decision") or "").startswith("STARTUP_PENDING")
               and retries < STARTUP_PENDING_MAX_RETRIES):
            retries += 1
            self._sleep(STARTUP_PENDING_RETRY_SECONDS)
            start = start_title()
        start["startup_pending_retries"] = retries
        result: dict[str, Any] = {
            "mode": "start_and_load_save_slot",
            "slot": target["slot"],
            "target": target,
            "start": start,
        }
        if not start.get("ok"):
            result.update(
                ok=False,
                phase=start.get("phase", "startup_failed"),
                decision=start.get("decision", "STARTUP_FAILED"),
            )
            return result
        if run:
            current_run = self.service.get_queens_blood_run_status()
            previous_driver, current_driver = run["driver"], current_run.get("driver") or {}
            if (current_run.get("assignment") != run["assignment"]
                    or current_driver != previous_driver):
                result.update(ok=False, phase="load_failed", decision="MODEL_RUN_CHANGED_DURING_STARTUP")
                return result
        loaded = None
        if start.get("phase") == "main_menu":
            def before_title_input():
                self._verify_bound_model_run()
            loaded = load_title_save_slot(
                self.service, slot=target["slot"], execute=bool(execute),
                steam_id=target["file"]["steam_id"], before_input=before_title_input)
            result["title_load"] = loaded
            result["load_path"] = "title"
            if not loaded.get("ok") and loaded.get("fallback_allowed"):
                # A missing/old exporter can refuse without sending input.
                # If input was sent, the loader must prove it backed out.
                # Also require the existing visual main-menu boundary before
                # the legacy Continue path; never guess from a blank feed.
                self._verify_bound_model_run()
                capture = self._capture("title-load-fallback")
                result["fallback_capture"] = capture
                if (capture.get("startup_frame") or {}).get("label") != "main_menu":
                    result.update(ok=False, phase="load_failed", decision="TITLE_LOAD_FALLBACK_UNVERIFIED", load=loaded)
                    return result
                fallback = self.start_and_continue_from_last_save_resumable(
                    session_seconds=session_seconds, launch_timeout_seconds=launch_timeout_seconds,
                    selection_timeout_seconds=selection_timeout_seconds,
                    interactive_timeout_seconds=interactive_timeout_seconds,
                    readiness_timeout_seconds=readiness_timeout_seconds)
                result["fallback_start"] = fallback
                if not fallback.get("ok"):
                    result.update(ok=False, phase=fallback.get("phase", "startup_failed"),
                                  decision=fallback.get("decision", "STARTUP_FAILED"), load=loaded)
                    return result
                loaded = None
                result["load_path"] = "field_fallback"
        elif start.get("phase") == "gameplay_ready":
            result["load_path"] = "field"
            render = self._await_rendered_world()
            result["render_before_load"] = render
            if not render.get("ok"):
                result.update(ok=False, phase="render_stuck_loading", decision="STARTUP_RENDER_STUCK_LOADING")
                return result
        else:
            result.update(ok=False, phase="load_failed", decision="SAVE_LOAD_STARTUP_PHASE_UNVERIFIED")
            return result

        loaded_status = getattr(self.service, "get_queens_blood_run_status", None)
        receipt = loaded_status().get("loaded_save") if callable(loaded_status) else None
        if (loaded is None and result["load_path"] == "field" and execute and skip_if_latest and target.get("latest") and receipt
                and (receipt.get("slot"), receipt.get("steam_id")) == (target["slot"], target["file"]["steam_id"])):
            result.update(
                ok=True,
                phase="gameplay_ready",
                decision="VERIFIED_REQUESTED_SAVE_ALREADY_LOADED",
                load_skipped=True,
            )
            return result
        load = getattr(self.service, "load_save_slot", None)
        if loaded is None and not callable(load):
            result.update(ok=False, phase="load_failed", decision="LOAD_SAVE_SLOT_UNAVAILABLE")
            return result
        # Continue can report ready before the world does: live 2026-09-28
        # the load then refused with load_requires_field_gameplay. Wait for
        # the settled-world gate first; the load re-checks it anyway.
        settle = getattr(self.service, "wait_for_settled_gameplay", None)
        if loaded is None and callable(settle):
            try:
                result["settle"] = settle(timeout_seconds=readiness_timeout_seconds)
            except Exception as exc:
                result["settle"] = {"ok": False, "decision": str(exc)}
        if run:
            current_run = self.service.get_queens_blood_run_status()
            current_driver = current_run.get("driver") or {}
            if (current_run.get("assignment") != run["assignment"]
                    or current_driver != run["driver"]):
                result.update(ok=False, phase="load_failed", decision="MODEL_RUN_CHANGED_DURING_STARTUP")
                return result
        if loaded is None:
            try:
                loaded = load(slot=target["slot"], execute=bool(execute), steam_id=target["file"]["steam_id"])
            except Exception as exc:
                loaded = {"ok": False, "phase": "load_failed", "decision": f"LOAD_SAVE_SLOT_FAILED: {exc}"}
        def verified_model_load():
            if not run:
                return True
            current = self.service.get_queens_blood_run_status()
            return (current.get("run_loaded") and current.get("assignment") == run["assignment"]
                    and current.get("driver") == run["driver"])

        if loaded.get("ok") and not verified_model_load():
            loaded = {**loaded, "ok": False, "phase": "load_failed", "decision": "MODEL_RUN_LOAD_UNVERIFIED"}
        if execute and loaded.get("ok"):
            render = self._await_rendered_world()
            result["render_check"] = render
            if not render.get("ok"):
                loaded = {**loaded, "ok": False, "phase": "render_stuck_loading",
                          "decision": "STARTUP_RENDER_STUCK_LOADING"}
            elif not verified_model_load():
                loaded = {**loaded, "ok": False, "phase": "load_failed", "decision": "MODEL_RUN_LOAD_UNVERIFIED"}
            else:
                reconnect = self._reconnect_gamepad_after_gameplay()
                result["gamepad_reconnect"] = reconnect
                if reconnect is not None and not reconnect.get("ok"):
                    loaded = {**loaded, "ok": False, "phase": "load_failed",
                              "decision": reconnect.get("decision", "GAMEPAD_RECONNECT_FAILED")}
                else:
                    self.lifecycle.mark_startup_phase("gameplay_ready")
        result["load"] = loaded
        result.update(
            ok=bool(loaded.get("ok")),
            phase=loaded.get("phase", "load_failed"),
            decision=loaded.get("decision"),
        )
        return result

    def prepare_benchmark_attempt(
        self,
        *,
        session_seconds: int = 1800,
        launch_timeout_seconds: float = 60.0,
        selection_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
        readiness_timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        '''Prepare one benchmark attempt without authorizing movement.

        Repeated attempts normally leave an already settled gameplay world in
        place. Attach to that world after the same process, foreground,
        telemetry, and control-session checks used by the other startup
        phases. A durable gameplay_loading phase is resumed without replaying
        Continue/Resume input. All other non-ready states delegate to the
        existing resumable startup composition.
        '''
        def decorate(
            result: dict[str, Any],
            *,
            mode: str,
            reason: str,
        ) -> dict[str, Any]:
            decorated = dict(result)
            decorated['mode'] = mode
            decorated['reason'] = reason
            decorated['movement_authorized'] = False
            decorated.setdefault('input_replayed', False)
            return decorated

        def cold_start(reason: str) -> dict[str, Any]:
            try:
                result = self.start_and_continue_from_last_save_resumable(
                    session_seconds=session_seconds,
                    launch_timeout_seconds=launch_timeout_seconds,
                    selection_timeout_seconds=selection_timeout_seconds,
                    interactive_timeout_seconds=interactive_timeout_seconds,
                    readiness_timeout_seconds=readiness_timeout_seconds,
                )
            except (AutonomyError, OSError, ValueError, RuntimeError) as exc:
                result = {
                    'ok': False,
                    'phase': 'startup_failed',
                    'decision': str(exc),
                }
            return decorate(
                result,
                mode='cold_resumable_startup',
                reason=reason,
            )

        def warm_readiness_snapshot() -> dict[str, Any]:
            checker = getattr(self.service, 'benchmark_readiness_snapshot', None)
            if not callable(checker):
                raise StartupError(
                    'BENCHMARK_READINESS_UNAVAILABLE: warm proof checker is not configured'
                )
            try:
                snapshot = checker()
            except Exception as exc:
                raise StartupError(
                    f'BENCHMARK_READINESS_CHECK_FAILED: {exc}'
                ) from exc
            if not isinstance(snapshot, dict):
                raise StartupError(
                    'BENCHMARK_READINESS_CHECK_FAILED: invalid readiness snapshot'
                )
            return snapshot

        try:
            try:
                lifecycle = self.lifecycle.ensure_started(allow_launch=False)
            except (AutonomyError, OSError, ValueError, RuntimeError):
                return cold_start('warm_attach_not_available')

            context = dict(self.lifecycle.context or lifecycle.get('context') or {})
            session = self._ensure_session(self._pid(context), session_seconds)
            if (context.get('startup_phase') or '') == 'gameplay_loading':
                resumed = self.resume_gameplay_readiness(
                    readiness_timeout_seconds=readiness_timeout_seconds,
                )
                resumed.setdefault('control_session', session)
                return decorate(
                    resumed,
                    mode='resumable_startup',
                    reason='durable_gameplay_loading',
                )

            health = self._wait_interactive(interactive_timeout_seconds)
            gameplay_ready = bool(
                health.get('gameplay_ready') is True
                and health.get('loaded_gameplay', True) is not False
                and health.get('paused') is not True
                and health.get('in_menu') is not True
            )
            if gameplay_ready:
                benchmark_readiness = warm_readiness_snapshot()
                proof_valid = bool(
                    benchmark_readiness.get('ok') is True
                    and benchmark_readiness.get('settled_proof_valid') is True
                    and benchmark_readiness.get('settling_required') is False
                    and benchmark_readiness.get('movement_authorized') is False
                    and benchmark_readiness.get('navigation_admitted') is False
                    and benchmark_readiness.get('navigation_admission_required') is True
                    and benchmark_readiness.get('fresh_advancing_state_required') is True
                )
                if not proof_valid:
                    return decorate(
                        {
                            'ok': False,
                            'phase': 'benchmark_readiness_required',
                            'decision': 'BENCHMARK_READINESS_PROOF_REQUIRED',
                            'health': health,
                            'benchmark_readiness': benchmark_readiness,
                            'control_session': session,
                        },
                        mode='warm_attach',
                        reason='warm_readiness_not_proven',
                    )
                self.lifecycle.mark_startup_phase('gameplay_ready')
                return decorate(
                    {
                        'ok': True,
                        'phase': 'gameplay_ready',
                        'already_ready': True,
                        'health': health,
                        'benchmark_readiness': benchmark_readiness,
                        'control_session': session,
                    },
                    mode='warm_attach',
                    reason='already_running_settled_gameplay',
                )
            return cold_start('warm_attach_not_ready')
        except StartupPending:
            return cold_start('warm_attach_not_ready')
        except (StartupError, AutonomyError, OSError, ValueError, RuntimeError) as exc:
            return decorate(
                {
                    'ok': False,
                    'phase': 'startup_failed',
                    'decision': str(exc),
                },
                mode='warm_attach',
                reason='warm_attach_refused',
            )

    def restart_and_continue_from_last_save(
        self,
        *,
        session_seconds: int = 1800,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
        launch_timeout_seconds: float = 60.0,
        selection_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        """Close the owned game, then relaunch and continue from its last save.

        A failed or ambiguous close is terminal for this operation: the
        startup wrapper is not called and no second game can be launched.
        """
        control_session: dict[str, Any] | None = None
        try:
            control_session = self._session_close(reason="restart_before_close")
        except Exception as exc:
            return {
                "ok": False,
                "phase": "restart_failed",
                "decision": f"CONTROL_SESSION_CLOSE_FAILED: {exc}",
                "control_session": {
                    "closed": False,
                    "decision": str(exc),
                },
            }

        try:
            close = self.lifecycle.close_game(timeout_seconds=close_timeout_seconds)
        except (AutonomyError, OSError, ValueError, RuntimeError) as exc:
            return {
                "ok": False,
                "phase": "restart_failed",
                "decision": f"GAME_CLOSE_FAILED: {exc}",
                "control_session": control_session,
                "close": {"ok": False, "decision": str(exc)},
            }
        if not isinstance(close, dict) or not close.get("ok"):
            return {
                "ok": False,
                "phase": "restart_failed",
                "decision": (
                    close.get("decision") or close.get("state")
                    if isinstance(close, dict)
                    else "GAME_CLOSE_FAILED: invalid close result"
                ),
                "control_session": control_session,
                "close": close,
            }

        try:
            start = self.start_and_continue_from_last_save(
                session_seconds=session_seconds,
                launch_timeout_seconds=launch_timeout_seconds,
                selection_timeout_seconds=selection_timeout_seconds,
                interactive_timeout_seconds=interactive_timeout_seconds,
            )
        except (AutonomyError, OSError, ValueError, RuntimeError) as exc:
            cleanup = self._cleanup_after_failure(
                close_game=True,
                close_session=True,
            )
            start = {
                "ok": False,
                "phase": "startup_failed",
                "decision": f"RESTART_STARTUP_FAILED: {exc}",
                "cleanup": cleanup,
            }
        return {
            "ok": bool(start.get("ok")),
            "phase": start.get("phase", "startup_failed"),
            "decision": start.get("decision"),
            "control_session": control_session,
            "close": close,
            "start": start,
        }

    def select_game_start(
        self,
        mode: str,
        *,
        menu_actions: list[str] | None = None,
        session_seconds: int = 1800,
        selection_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
        expect_gameplay_ready: bool | None = None,
        confirmation_presses: int | None = None,
        menu_action_milliseconds: int = 500,
    ) -> dict[str, Any]:
        """Phase 2: choose Continue, another save, or New Game."""
        mode = str(mode).strip().lower()
        if mode not in {"continue_previous_save", "load_save", "new_game"}:
            return {"ok": False, "phase": "selection_failed", "decision": "INVALID_GAME_START_MODE"}
        if expect_gameplay_ready is None:
            expect_gameplay_ready = mode == "continue_previous_save"
        if confirmation_presses is None:
            confirmation_presses = {"continue_previous_save": 2, "load_save": 2, "new_game": 1}[mode]
        if not 1 <= int(confirmation_presses) <= 4:
            return {"ok": False, "phase": "selection_failed", "decision": "INVALID_CONFIRMATION_PRESS_COUNT"}
        actions = list(menu_actions or [])
        invalid = [action for action in actions if action not in MENU_ACTIONS]
        if invalid:
            return {"ok": False, "phase": "selection_failed", "decision": f"INVALID_MENU_ACTIONS: {invalid}"}
        session_acquired = False
        session: dict[str, Any] | None = None
        actions_sent: list[dict[str, Any]] = []
        confirmations: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        try:
            lifecycle = self.lifecycle.ensure_started(allow_launch=False)
            context = dict(self.lifecycle.context or lifecycle.get("context") or {})
            session = self._ensure_session(self._pid(context), session_seconds)
            session_acquired = True
            health = self._wait_interactive(interactive_timeout_seconds)
            if health.get("gameplay_ready"):
                cleanup = self._cleanup_after_failure(close_session=True)
                return {
                    "ok": False,
                    "phase": "selection_failed",
                    "decision": "SELECTION_REQUIRES_MAIN_MENU: gameplay is already active",
                    "mode": mode,
                    "health": health,
                    "control_session": session,
                    "cleanup": cleanup,
                }
            fresh = self._wait_fresh(selection_timeout_seconds)
            if (context.get("startup_phase") or "") not in {"main_menu", "post_selection_prompt"}:
                cleanup = self._cleanup_after_failure(close_session=True)
                return {
                    "ok": False,
                    "phase": "selection_failed",
                    "decision": "STARTUP_PHASE_REQUIRED: call start_game_to_main_menu first",
                    "mode": mode,
                    "health": health,
                    "fresh_telemetry": fresh,
                    "control_session": session,
                    "cleanup": cleanup,
                }
            self.lifecycle.mark_startup_phase("selection_in_progress")
            evidence = [self._capture("before-game-selection")]
            for index, action in enumerate(actions, start=1):
                actions_sent.append(self.service.tap(action, int(menu_action_milliseconds)))
                self._wait_fresh(selection_timeout_seconds)
                evidence.append(self._capture(f"after-menu-action-{index}-{action}"))
            for index in range(int(confirmation_presses)):
                if index == 0:
                    invalidate = getattr(self.service, "invalidate_loaded_save", None)
                    if callable(invalidate):
                        invalidate(f"title_selection:{mode}")
                confirmations.append(self._tap_confirm())
                self._wait_fresh(selection_timeout_seconds)
                evidence.append(self._capture(f"after-selection-confirm-{index + 1}-{mode}"))
            if expect_gameplay_ready:
                ready = self._wait_ready(selection_timeout_seconds)
                settled = None
                settle = getattr(self.service, "mark_gameplay_settled", None)
                if callable(settle):
                    settled = settle(ready.get("run_binding", {}).get("stable_samples"))
                gamepad_reconnect = self._reconnect_gamepad_after_gameplay()
                if gamepad_reconnect is not None and not gamepad_reconnect.get("ok", False):
                    cleanup = self._cleanup_after_failure(close_session=True)
                    return {
                        "ok": False,
                        "phase": "selection_failed",
                        "decision": gamepad_reconnect.get(
                            "decision", "GAMEPAD_RECONNECT_FAILED"
                        ),
                        "mode": mode,
                        "ready_telemetry": ready,
                        "settled_gameplay": settled,
                        "evidence": evidence,
                        "gamepad_reconnect": gamepad_reconnect,
                        "control_session": session,
                        "cleanup": cleanup,
                    }
                self.lifecycle.mark_startup_phase("gameplay_ready")
                return {
                    "ok": True,
                    "phase": "gameplay_ready",
                    "mode": mode,
                    "actions": actions_sent,
                    "confirmations": confirmations,
                    "ready_telemetry": ready,
                    "settled_gameplay": settled,
                    "gamepad_reconnect": gamepad_reconnect,
                    "evidence": evidence,
                    "control_session": session,
                }
            self.lifecycle.mark_startup_phase("post_selection_prompt")
            return {
                "ok": True,
                "phase": "post_selection_prompt",
                "mode": mode,
                "actions": actions_sent,
                "confirmations": confirmations,
                "evidence": evidence,
                "control_session": session,
                "gameplay_ready_requested": bool(expect_gameplay_ready),
            }
        except StartupPending as exc:
            all_confirmations_sent = len(confirmations) >= int(confirmation_presses)
            if expect_gameplay_ready and all_confirmations_sent:
                try:
                    self.lifecycle.mark_startup_phase("gameplay_loading")
                except Exception:
                    pass
            return {
                "ok": False,
                "pending": True,
                "phase": exc.phase,
                "decision": str(exc),
                "mode": mode,
                "actions": actions_sent,
                "confirmations": confirmations,
                "evidence": evidence,
                "control_session": session,
                "retry": (
                    "call_wait_for_gameplay_ready_without_replaying_input"
                    if all_confirmations_sent and expect_gameplay_ready
                    else "fresh_telemetry_required_before_next_confirmation"
                    if confirmations
                    else "resume_the_same_phase_without_replaying_input"
                ),
            }
        except StartupError as exc:
            return {
                "ok": False,
                "phase": "selection_failed",
                "decision": str(exc),
                "mode": mode,
                "cleanup": self._cleanup_after_failure(close_session=session_acquired),
            }
        except (AutonomyError, OSError, ValueError) as exc:
            return {
                "ok": False,
                "phase": "selection_failed",
                "decision": str(exc),
                "mode": mode,
                "cleanup": self._cleanup_after_failure(close_session=session_acquired),
            }

    @staticmethod
    def _new_game_settings(difficulty: Any, combat_style: Any, camera: Any,
                           bonus_check: Any) -> dict[str, str]:
        """Read "easy", "classic", "classic (easy)" or "classic_easy" style
        settings into one choice per setup screen; ValueError when unknown."""
        words = str(difficulty or "").casefold()
        for mark in "()_-,/":
            words = words.replace(mark, " ")
        tokens = words.split()
        difficulties = [word for word in tokens if word in NEW_GAME_DIFFICULTIES]
        styles = [word for word in tokens if word in NEW_GAME_COMBAT_STYLES]
        unknown = [word for word in tokens if word not in NEW_GAME_DIFFICULTIES + NEW_GAME_COMBAT_STYLES]
        if unknown or len(difficulties) > 1 or len(styles) > 1:
            raise ValueError(f"NEW_GAME_DIFFICULTY_UNKNOWN: {difficulty!r}; difficulty is one of "
                             f"{', '.join(NEW_GAME_DIFFICULTIES)}, optionally with a combat style")
        if combat_style is not None:
            style = str(combat_style).strip().casefold()
            if style not in NEW_GAME_COMBAT_STYLES:
                raise ValueError(f"NEW_GAME_COMBAT_STYLE_UNKNOWN: {combat_style!r}")
            if styles and styles[0] != style:
                raise ValueError("NEW_GAME_COMBAT_STYLE_CONFLICT: difficulty names "
                                 f"{styles[0]!r} but combat_style is {style!r}")
            styles = [style]
        # Classic is offered as a style of Easy, so "classic" alone means Easy.
        chosen = {"difficulty": difficulties[0] if difficulties else "easy",
                  "combat_style": styles[0] if styles else "active"}
        if chosen["combat_style"] == "classic" and chosen["difficulty"] != "easy":
            raise ValueError("NEW_GAME_CLASSIC_REQUIRES_EASY: Combat Style is chosen after Easy")
        chosen["camera"] = str(camera or "default").strip().casefold()
        if chosen["camera"] not in NEW_GAME_CAMERA_OPTIONS:
            raise ValueError(f"NEW_GAME_CAMERA_UNKNOWN: {camera!r}")
        chosen["bonus_check"] = str(bonus_check or "check").strip().casefold()
        if chosen["bonus_check"] not in NEW_GAME_BONUS_CHOICES:
            raise ValueError(f"NEW_GAME_BONUS_CHECK_UNKNOWN: {bonus_check!r}")
        return chosen

    def _new_game_screen(self) -> dict[str, Any]:
        """The setup screen's title and highlighted-option help keys; no input."""
        read = getattr(self.service, "read_screen_texts", None)
        if not callable(read):
            raise StartupError("NEW_GAME_SCREEN_TEXTS_UNAVAILABLE")
        try:
            texts = read("Startmenu_00").get("texts") or []
        except Exception as exc:
            # Scene transitions can drop a read; the caller polls again.
            return {"title": None, "help": None, "error": str(exc)}

        def value(path: str) -> str | None:
            found = [item.get("text") for item in texts
                     if isinstance(item, dict) and str(item.get("path", "")).endswith(path)]
            return found[-1] if found else None

        return {"title": value(NEW_GAME_TITLE_PATH), "help": value(NEW_GAME_HELP_PATH)}

    def _wait_new_game_screen(self, titles: set[str], *, leaving: str | None,
                              timeout_seconds: float = NEW_GAME_SCREEN_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Poll until one of ``titles`` replaces ``leaving``; ``reached`` says whether it did."""
        deadline = self._monotonic() + timeout_seconds
        screen: dict[str, Any] = {}
        for _ in range(int(timeout_seconds / NEW_GAME_POLL_SECONDS) + 1):
            screen = self._new_game_screen()
            if screen.get("title") in titles and screen.get("title") != leaving:
                self._sleep(NEW_GAME_SCREEN_SETTLE_SECONDS)
                return {**self._new_game_screen(), "reached": True}
            if self._monotonic() >= deadline:
                break
            self._sleep(NEW_GAME_POLL_SECONDS)
        return {**screen, "reached": False}

    def _choose_new_game_option(self, name: str, title: str, options: tuple[str, ...],
                                choice: str) -> dict[str, Any]:
        """Move from the observed current option to ``choice`` and
        confirm it once the help text shows the cursor is on it."""
        before = self._new_game_screen()
        if before.get("title") != title:
            raise StartupError(f"NEW_GAME_SCREEN_CHANGED:{name}: title={before.get('title')!r}")
        start_index = next((index for index, option in enumerate(options)
                            if NEW_GAME_OPTION_HELP.get((name, option)) is not None
                            and before.get("help") == NEW_GAME_OPTION_HELP[(name, option)]), None)
        if start_index is None:
            raise StartupError(f"NEW_GAME_CURSOR_UNKNOWN:{name}: help={before.get('help')!r}")
        target_index = options.index(choice)
        direction = "down" if target_index > start_index else "up"
        presses = []
        cursor_steps = []
        previous = before
        for _ in range(abs(target_index - start_index)):
            presses.append(self.service.tap(direction, NEW_GAME_MENU_TAP_MS))
            self._sleep(NEW_GAME_CURSOR_GAP_SECONDS)
            current = self._new_game_screen()
            if (current.get("title") != title or not current.get("help")
                    or current.get("help") == previous.get("help")):
                raise StartupError(f"NEW_GAME_CURSOR_UNVERIFIED:{name}={choice}: "
                                   f"title={current.get('title')!r} help={current.get('help')!r}")
            cursor_steps.append({"direction": direction, "before_help": previous.get("help"),
                                 "after_help": current.get("help")})
            previous = current
        after = self._new_game_screen()
        expected = NEW_GAME_OPTION_HELP.get((name, choice))
        moved = target_index == start_index or after.get("help") != before.get("help")
        if after.get("title") != title or (expected and after.get("help") != expected) or not moved:
            raise StartupError(f"NEW_GAME_CURSOR_UNVERIFIED:{name}={choice}: title={after.get('title')!r} "
                               f"help={after.get('help')!r}")
        evidence = self._capture(f"new-game-{name}-{choice}")
        presses.append(self.service.tap("confirm", NEW_GAME_MENU_TAP_MS))
        return {"screen": name, "choice": choice, "help": after.get("help"),
                "presses": presses, "cursor_steps": cursor_steps, "evidence": evidence}

    def _pass_new_game_bonus(self, choice: str, leaving: str | None) -> dict[str, Any]:
        """Check (or Skip) bonus saved data and close its notice until
        Camera Controls shows; bounded, and never past that screen."""
        presses: list[dict[str, Any]] = []
        screens: list[dict[str, Any]] = []
        before = self._new_game_screen()
        if (before.get("title") != NEW_GAME_BONUS_TITLE
                or before.get("help") != NEW_GAME_OPTION_HELP[("bonus_check", "check")]):
            raise StartupError("NEW_GAME_BONUS_SCREEN_UNVERIFIED")
        if choice == "skip":
            presses.append(self.service.tap("down", NEW_GAME_MENU_TAP_MS))
            self._sleep(NEW_GAME_CURSOR_GAP_SECONDS)
            after = self._new_game_screen()
            if (after.get("title") != NEW_GAME_BONUS_TITLE or not after.get("help")
                    or after.get("help") == before.get("help")):
                raise StartupError("NEW_GAME_CURSOR_UNVERIFIED:bonus_check=skip")
        for _ in range(NEW_GAME_BONUS_MAX_CONFIRMS):
            screens.append(self._new_game_screen())
            if screens[-1].get("title") == NEW_GAME_CAMERA_TITLE:
                return {"choice": choice, "presses": presses, "screens": screens,
                        "camera": {**screens[-1], "reached": True}}
            frame = self._capture(f"new-game-bonus-{len(screens)}").get("startup_frame")
            if (isinstance(frame, dict) and frame.get("label") == "main_menu"
                    and screens[-1].get("title") != NEW_GAME_BONUS_TITLE):
                raise StartupError("NEW_GAME_NOT_SELECTED: the main menu is still showing")
            presses.append(self.service.tap("confirm", NEW_GAME_MENU_TAP_MS))
            camera = self._wait_new_game_screen({NEW_GAME_CAMERA_TITLE}, leaving=leaving,
                                                timeout_seconds=NEW_GAME_SCREEN_TIMEOUT_SECONDS / 4)
            if camera["reached"]:
                return {"choice": choice, "presses": presses, "screens": screens, "camera": camera}
        raise StartupError(f"NEW_GAME_BONUS_NOT_PASSED: Camera Controls did not follow; screens={screens}")

    def _await_new_game_gameplay(self, timeout_seconds: float, skip_cutscenes: bool) -> dict[str, Any]:
        """Skip the opening cinemas and wait for settled gameplay with none playing."""
        deadline = self._monotonic() + max(1.0, float(timeout_seconds))
        state_of = getattr(self.service, "cutscene_state", None)
        skip = getattr(self.service, "skip_cutscene", None)
        skips: list[dict[str, Any]] = []
        attempts: dict[str, int] = {}
        last_error = "gameplay readiness was not observed"

        def cutscene() -> dict[str, Any]:
            return state_of() if callable(state_of) else {"observed": False, "active": None}

        while self._monotonic() < deadline:
            state = cutscene()
            if state.get("active"):
                scenes = ",".join(sorted(str(item.get("sequence")) for item in state.get("scenes") or []))
                if skip_cutscenes and callable(skip) and attempts.get(scenes, 0) < NEW_GAME_CUTSCENE_SKIP_ATTEMPTS:
                    attempts[scenes] = attempts.get(scenes, 0) + 1
                    skipped = skip()
                    skips.append(skipped)
                    if skipped.get("reason") == "not_skippable":
                        attempts[scenes] = NEW_GAME_CUTSCENE_SKIP_ATTEMPTS
                else:
                    self._sleep(NEW_GAME_POLL_SECONDS * 2)
                continue
            try:
                ready = self.lifecycle.wait_for_gameplay_ready(
                    min(STARTUP_LOCAL_WAIT_SECONDS, max(0.1, deadline - self._monotonic())))
            except AutonomyError as exc:
                last_error = str(exc)
                continue
            # In-engine scenes leave the field telemetry live: readiness
            # counts only once no cinema is playing either.
            after = cutscene()
            if not after.get("active"):
                return {"ready_telemetry": ready, "cutscene": after, "skips": skips}
        raise StartupPending(
            f"GAMEPLAY_LOADING: the new game did not reach gameplay within {float(timeout_seconds):.0f}s; "
            f"last_error={last_error}; skips={skips}",
            phase="gameplay_loading",
        )

    def start_new_game(self, *, model_id: str | None = None,
                       reasoning_effort: str | None = None,
                       client_id: str | None = None, **arguments: Any) -> dict[str, Any]:
        """Start a fresh game with optional explicit recording attribution."""
        from .recording_driver import validate_recording_driver
        try:
            recording = validate_recording_driver(model_id, reasoning_effort)
            if client_id is not None and recording is None:
                raise ValueError("CONTROL_SESSION_EXPLICIT_DRIVER_REQUIRED")
        except ValueError as exc:
            return {"ok": False, "phase": "new_game_failed", "decision": str(exc),
                    "mode": "new_game", "input_sent": False}
        binding = ({"recording": recording, "run": None, "client_id": client_id,
                    "explicit_client": client_id is not None} if recording else None)
        token = self._startup_session_binding.set(binding)
        try:
            return self._start_new_game(**arguments)
        finally:
            self._startup_session_binding.reset(token)

    def _start_new_game(
        self,
        *,
        difficulty: str = "easy",
        combat_style: str | None = None,
        camera: str = "default",
        bonus_check: str = "check",
        skip_cutscenes: bool = True,
        session_seconds: int = 1800,
        launch_timeout_seconds: float = 60.0,
        interactive_timeout_seconds: float = 45.0,
        readiness_timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Launch or attach at the title, start a New Game with the given
        settings, skip the opening cinemas and stop in settled gameplay.

        ``difficulty`` is easy, normal or dynamic and may carry the combat
        style, as in "classic (easy)" or "classic_easy"; ``combat_style`` is
        active or classic (Classic is a style of Easy). Every setup screen
        is identified by its title text, and every choice by the highlighted
        option's help text, before it is confirmed. A game already in
        gameplay is refused: close it first.
        """
        try:
            settings = self._new_game_settings(difficulty, combat_style, camera, bonus_check)
        except ValueError as exc:
            return {"ok": False, "phase": "new_game_failed", "decision": str(exc),
                    "mode": "new_game", "input_sent": False}
        result: dict[str, Any] = {"mode": "new_game", "settings": settings}

        def start_title():
            return self.start_game_to_main_menu(
                session_seconds=session_seconds, launch_timeout_seconds=launch_timeout_seconds,
                interactive_timeout_seconds=interactive_timeout_seconds)

        start = start_title()
        retries = 0
        while (not start.get("ok") and start.get("phase") == "startup_loading"
               and str(start.get("decision") or "").startswith("STARTUP_PENDING")
               and retries < STARTUP_PENDING_MAX_RETRIES):
            retries += 1
            self._sleep(STARTUP_PENDING_RETRY_SECONDS)
            start = start_title()
        start["startup_pending_retries"] = retries
        result["start"] = start
        if not start.get("ok"):
            return {**result, "ok": False, "phase": start.get("phase", "startup_failed"),
                    "decision": start.get("decision", "STARTUP_FAILED")}
        if start.get("phase") != "main_menu":
            return {**result, "ok": False, "phase": "new_game_failed",
                    "decision": f"NEW_GAME_REQUIRES_MAIN_MENU: startup reached {start.get('phase')!r}; "
                                "close the running game first"}
        leaving = self._new_game_screen().get("title")
        selection = self.select_game_start(
            "new_game", menu_actions=["up"], session_seconds=session_seconds,
            interactive_timeout_seconds=interactive_timeout_seconds, expect_gameplay_ready=False,
            confirmation_presses=1, menu_action_milliseconds=NEW_GAME_MENU_TAP_MS)
        result["selection"] = selection
        if not selection.get("ok"):
            return {**result, "ok": False, "phase": selection.get("phase", "selection_failed"),
                    "decision": selection.get("decision", "NEW_GAME_SELECTION_FAILED")}
        try:
            self.lifecycle.mark_startup_phase("new_game_setup")
            camera_screen = self._wait_new_game_screen({NEW_GAME_CAMERA_TITLE}, leaving=leaving,
                                                       timeout_seconds=NEW_GAME_SCREEN_SETTLE_SECONDS * 3)
            if not camera_screen["reached"]:
                result["bonus"] = self._pass_new_game_bonus(settings["bonus_check"], leaving)
            screens = [self._choose_new_game_option("camera", NEW_GAME_CAMERA_TITLE,
                                                    NEW_GAME_CAMERA_OPTIONS, settings["camera"])]
            if not self._wait_new_game_screen({NEW_GAME_DIFFICULTY_TITLE},
                                              leaving=NEW_GAME_CAMERA_TITLE)["reached"]:
                raise StartupError("NEW_GAME_DIFFICULTY_SCREEN_NOT_SEEN")
            screens.append(self._choose_new_game_option("difficulty", NEW_GAME_DIFFICULTY_TITLE,
                                                        NEW_GAME_DIFFICULTIES, settings["difficulty"]))
            if settings["difficulty"] == "easy":
                if not self._wait_new_game_screen({NEW_GAME_COMBAT_STYLE_TITLE},
                                                  leaving=NEW_GAME_DIFFICULTY_TITLE)["reached"]:
                    raise StartupError("NEW_GAME_COMBAT_STYLE_SCREEN_NOT_SEEN")
                screens.append(self._choose_new_game_option("combat_style", NEW_GAME_COMBAT_STYLE_TITLE,
                                                            NEW_GAME_COMBAT_STYLES, settings["combat_style"]))
            result["screens"] = screens
            self.lifecycle.mark_startup_phase("gameplay_loading")
            arrival = self._await_new_game_gameplay(readiness_timeout_seconds, skip_cutscenes)
            result.update(arrival)
            settle = getattr(self.service, "mark_gameplay_settled", None)
            if callable(settle):
                result["settled_gameplay"] = settle(
                    arrival["ready_telemetry"].get("run_binding", {}).get("stable_samples"))
            reconnect = self._reconnect_gamepad_after_gameplay()
            result["gamepad_reconnect"] = reconnect
            if reconnect is not None and not reconnect.get("ok", False):
                raise StartupError(reconnect.get("decision", "GAMEPAD_RECONNECT_FAILED"))
            self.lifecycle.mark_startup_phase("gameplay_ready")
            return {**result, "ok": True, "phase": "gameplay_ready"}
        except StartupPending as exc:
            return {**result, "ok": False, "pending": True, "phase": exc.phase, "decision": str(exc)}
        except (StartupError, AutonomyError, OSError, ValueError) as exc:
            return {**result, "ok": False, "phase": "new_game_failed", "decision": str(exc),
                    "cleanup": self._cleanup_after_failure(close_session=True)}
