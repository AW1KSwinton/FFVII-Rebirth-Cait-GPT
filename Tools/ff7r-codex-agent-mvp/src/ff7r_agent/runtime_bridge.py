from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .controller_policy import (
    LiveControllerPolicyError,
    proven_live_controller_policy_evidence,
    require_proven_live_controller_policy,
)
from .gamepad_broker import BrokerProcess
from .gameplay_narration import AgentSpeechPublisher
from .runtime_identity import collect_identity
from .runtime_profile import (
    GitTimeoutError,
    profile_contract_from_environment,
    validate_current_checkout,
    verify_runtime_identity,
)
from .runtime_ownership import collect_runtime_ownership_inventory, ownership_conflicts, process_identity_token
from .subprocess_options import no_console_window_kwargs

# Validating the checkout runs several git commands, 150-320 ms each on this
# OneDrive-hosted repository (0.45-1.25 s each under game load, 2026-10-05),
# and it ran before every worker call (about 0.5 s per game action). A
# successful validation is reused, re-checked in the background once older
# than the TTL, and at once invalidated when HEAD, the index or the current
# ref changes on disk.
CHECKOUT_VALIDATION_TTL_SECONDS = 10.0
_checkout_validation_cache: dict[str, Any] = {}
_checkout_validation_lock = threading.Lock()


def _checkout_state_stamp(contract: dict[str, Any]) -> tuple[Any, ...]:
    root = Path(str(contract.get("repository_root") or ""))
    git_dir = root / ".git"
    if git_dir.is_file():  # a worktree: .git points at the real directory
        try:
            pointer = git_dir.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                git_dir = (root / pointer.split(":", 1)[1].strip()).resolve()
        except OSError:
            return (None,)
    stamp: list[Any] = []
    head = git_dir / "HEAD"
    for candidate in (head, git_dir / "index"):
        try:
            stat = candidate.stat()
            stamp.append((candidate.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamp.append((candidate.name, None))
    try:
        reference = head.read_text(encoding="utf-8").strip()
        stamp.append(reference)
        if reference.startswith("ref:"):
            ref_path = reference.split(":", 1)[1].strip()
            for base in (git_dir, git_dir.parent.parent if git_dir.parent.name == "worktrees" else git_dir):
                ref_file = base / ref_path
                if ref_file.exists():
                    stamp.append((ref_path, ref_file.stat().st_mtime_ns))
                    break
    except OSError:
        stamp.append(None)
    return tuple(stamp)


_checkout_validation_refreshing: set[str] = set()


def _cached_checkout_validation(contract: dict[str, Any]) -> dict[str, Any]:
    """The checkout's validation, without making a request wait on Git.

    A changed HEAD, index or ref is validated before the request (a rebind
    needs the truth). With none changed, an expired result is still used and
    re-checked for uncommitted edits in the background: live 2026-10-05 the
    five Git calls took 3-6 s on this OneDrive checkout, so one request in
    every ten seconds stalled that long. A failed background check is raised
    by the next request, which is how a stale result would have failed. A
    timed-out Git read proves nothing either way: in the background it keeps
    the previous result for the next re-check, and before a request it is
    tried once more (live, a background timeout refused a menu press).
    """
    key = json.dumps(contract, sort_keys=True, default=str)
    stamp = _checkout_state_stamp(contract)
    now = time.monotonic()
    with _checkout_validation_lock:
        cached = _checkout_validation_cache.get(key)
        if cached and cached["stamp"] == stamp:
            failure = cached.get("failure")
            if failure is not None:
                del _checkout_validation_cache[key]
                raise failure
            if now - cached["at"] >= CHECKOUT_VALIDATION_TTL_SECONDS and key not in _checkout_validation_refreshing:
                _checkout_validation_refreshing.add(key)
                threading.Thread(target=_refresh_checkout_validation, args=(key, contract, stamp),
                                 name="checkout-validation", daemon=True).start()
            return cached["result"]
    try:
        result = validate_current_checkout(contract)
    except GitTimeoutError:
        result = validate_current_checkout(contract)
    with _checkout_validation_lock:
        _checkout_validation_cache[key] = {"stamp": stamp, "at": now, "result": result}
    return result


def _refresh_checkout_validation(key: str, contract: dict[str, Any], stamp: tuple[Any, ...]) -> None:
    started = time.monotonic()
    entry: dict[str, Any] | None
    try:
        entry = {"stamp": stamp, "at": started, "result": validate_current_checkout(contract)}
    except GitTimeoutError:
        entry = None  # keep the previous result; the next request re-checks
    except Exception as exc:
        entry = {"stamp": stamp, "at": started, "failure": exc}
    with _checkout_validation_lock:
        if entry is not None:
            _checkout_validation_cache[key] = entry
        _checkout_validation_refreshing.discard(key)


class RuntimeBridgeError(RuntimeError):
    pass


class RuntimeInfrastructureError(RuntimeBridgeError):
    """A bounded bridge readiness/recovery failure safe for MCP responses."""

    def __init__(
        self,
        error_code: str,
        *,
        operation: str,
        phase: str,
        message: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.operation = operation
        self.phase = phase
        self.message = message
        self.diagnostics = dict(diagnostics or {})

    def as_response(self) -> dict[str, Any]:
        result = {
            "ok": False,
            "error_type": type(self).__name__,
            "error_code": self.error_code,
            "operation": self.operation,
            "phase": self.phase,
            "error": self.message,
        }
        if self.diagnostics:
            result["diagnostics"] = dict(self.diagnostics)
        return result


_LOGGER = logging.getLogger(__name__)


# Live placements can finish their input before the post-move observation replies.
# Leave five seconds below the MCP host's approximately 30-second wrapper deadline
# for fail-closed worker termination and broker neutralisation.
DEFAULT_RUNTIME_REQUEST_TIMEOUT_SECONDS = 25.0
RESULT_ARTIFACT_SCHEMA = "ff7r-runtime-result-v1"

# Native navigation capture is deliberately one-shot and can take several
# minutes on the target machine.  Keep the ordinary request path short and
# expose only this bounded, explicitly named route operation through the
# asynchronous bridge path below.
ASYNC_ROUTE_START_OPERATION = "start_live_route_adapter"
ASYNC_ROUTE_STATUS_OPERATION = "get_live_route_adapter_status"
ASYNC_ROUTE_WORK_OPERATION = "run_live_route_adapter"
ASYNC_CAPTURE_START_OPERATION = "start_topology_capture"
ASYNC_CAPTURE_STATUS_OPERATION = "get_topology_capture_status"
ASYNC_CAPTURE_WORK_OPERATION = "capture_topology_cache"
ASYNC_STARTUP_START_OPERATION = "start_and_continue_from_last_save_async"
ASYNC_STARTUP_STATUS_OPERATION = "get_start_and_continue_from_last_save_status"
ASYNC_STARTUP_WORK_OPERATION = "run_start_and_continue_from_last_save"
ASYNC_QUEENS_BLOOD_START_OPERATION = "initiate_queens_blood_match_async"
ASYNC_QUEENS_BLOOD_STATUS_OPERATION = "get_initiate_queens_blood_match_status"
ASYNC_QUEENS_BLOOD_WORK_OPERATION = "run_initiate_queens_blood_match"
ASYNC_SETUP_START_OPERATION = "setup_queens_blood_match_async"
ASYNC_SETUP_STATUS_OPERATION = "get_setup_queens_blood_match_status"
ASYNC_SETUP_WORK_OPERATION = "run_setup_queens_blood_match"
ASYNC_SETUP_ALLOWED_OPERATIONS = frozenset({ASYNC_SETUP_STATUS_OPERATION, "stop_all_input"})
ASYNC_CONVERSATION_START_OPERATION = "finish_conversation_async"
ASYNC_CONVERSATION_STATUS_OPERATION = "get_finish_conversation_status"
ASYNC_CONVERSATION_WORK_OPERATION = "run_finish_conversation"
ASYNC_CLOSE_START_OPERATION = "close_game_async"
ASYNC_CLOSE_STATUS_OPERATION = "get_close_game_status"
ASYNC_CLOSE_WORK_OPERATION = "close_game"
# Marking cards waits for a settled cursor and fresh marker telemetry per
# card, which legitimately exceeds the ordinary runtime request deadline.
ASYNC_MULLIGAN_START_OPERATION = "mulligan_cards_async"
ASYNC_MULLIGAN_STATUS_OPERATION = "get_mulligan_cards_status"
ASYNC_MULLIGAN_WORK_OPERATION = "mulligan_cards"
# A shop purchase reads the shop's texts around every press.
ASYNC_SHOP_BUY_START_OPERATION = "buy_shop_item_async"
ASYNC_SHOP_BUY_STATUS_OPERATION = "get_buy_shop_item_status"
ASYNC_SHOP_BUY_WORK_OPERATION = "buy_shop_item"
# Casting a spell or using an item from the field (party) menu.
ASYNC_FIELD_MENU_START_OPERATION = "field_menu_async"
ASYNC_FIELD_MENU_STATUS_OPERATION = "get_field_menu_status"
ASYNC_FIELD_MENU_WORK_OPERATION = "run_field_menu"
# A whole Queen's Blood match runs for minutes.
ASYNC_MATCH_START_OPERATION = "play_queens_blood_match_async"
ASYNC_MATCH_STATUS_OPERATION = "get_play_queens_blood_match_status"
ASYNC_MATCH_WORK_OPERATION = "play_queens_blood_match"
# Walking the deck editor's owned-card grid reads one card per cursor step.
ASYNC_SCAN_START_OPERATION = "scan_queens_blood_collection_async"
ASYNC_SCAN_STATUS_OPERATION = "get_scan_queens_blood_collection_status"
ASYNC_SCAN_WORK_OPERATION = "scan_queens_blood_collection"
# Editing a deck reads the deck, walks the editor and re-reads the deck.
ASYNC_DECK_START_OPERATION = "edit_queens_blood_deck_async"
ASYNC_DECK_STATUS_OPERATION = "get_edit_queens_blood_deck_status"
ASYNC_DECK_WORK_OPERATION = "edit_queens_blood_deck"
# Fast travel by map spot cycles the map and waits out the loading screen.
ASYNC_TRAVEL_START_OPERATION = "travel_to_map_spot_async"
ASYNC_TRAVEL_STATUS_OPERATION = "get_travel_to_map_spot_status"
ASYNC_TRAVEL_WORK_OPERATION = "travel_to_map_spot"
# Entering Chadley's combat simulator waits out his greeting (up to ~20 s)
# and the scenes before the simulator's screen.
ASYNC_SIMULATOR_START_OPERATION = "enter_combat_simulator_async"
ASYNC_SIMULATOR_STATUS_OPERATION = "get_enter_combat_simulator_status"
ASYNC_SIMULATOR_WORK_OPERATION = "enter_combat_simulator"
# Loading a save slot walks the main menu and waits out the load.
ASYNC_LOAD_SLOT_START_OPERATION = "load_save_slot_async"
ASYNC_LOAD_SLOT_STATUS_OPERATION = "get_load_save_slot_status"
ASYNC_LOAD_SLOT_WORK_OPERATION = "load_save_slot"
# Startup (Continue) followed by an in-game load of the requested slot.
ASYNC_SLOT_STARTUP_START_OPERATION = "start_and_load_save_slot_async"
ASYNC_SLOT_STARTUP_STATUS_OPERATION = "get_start_and_load_save_slot_status"
ASYNC_SLOT_STARTUP_WORK_OPERATION = "run_start_and_load_save_slot"
# A New Game: title, setup screens, opening cinemas, first control.
ASYNC_NEW_GAME_START_OPERATION = "start_new_game_async"
ASYNC_NEW_GAME_STATUS_OPERATION = "get_start_new_game_status"
ASYNC_NEW_GAME_WORK_OPERATION = "run_start_new_game"
ASYNC_SAVE_START_OPERATION = "save_game_progress_async"
ASYNC_SAVE_STATUS_OPERATION = "get_save_game_progress_status"
ASYNC_SAVE_WORK_OPERATION = "save_game_progress"
ASYNC_RECOVERY_START_OPERATION = "recover_working_save_async"
ASYNC_RECOVERY_STATUS_OPERATION = "get_recover_working_save_status"
ASYNC_RECOVERY_WORK_OPERATION = "run_recover_working_save"
# Explore mode: walk to a point picked from an explore_view, as one job inside
# the worker (no control-session close, no stop latch on exit).
ASYNC_EXPLORE_START_OPERATION = "explore_goto_async"
ASYNC_EXPLORE_STATUS_OPERATION = "get_explore_goto_status"
ASYNC_EXPLORE_WORK_OPERATION = "run_explore_goto"
ASYNC_EXPLORE_ALLOWED_OPERATIONS = frozenset({ASYNC_EXPLORE_STATUS_OPERATION, "stop_all_input"})
ASYNC_SAVE_ALLOWED_OPERATIONS = frozenset({ASYNC_SAVE_STATUS_OPERATION, "stop_all_input"})
ASYNC_RECOVERY_ALLOWED_OPERATIONS = frozenset({ASYNC_RECOVERY_STATUS_OPERATION, "stop_all_input"})
INTERNAL_ACTIVE_ASYNC_JOB_OPERATION = "__get_active_async_job__"
QUEEN_BLOOD_PREPARATION_OPERATIONS = frozenset({
    "get_queens_blood_preparation_context", "prepare_queens_blood_deck",
})
ASYNC_ROUTE_ALLOWED_OPERATIONS = frozenset({
    ASYNC_ROUTE_STATUS_OPERATION,
    "get_queens_blood_preparation_context",
    "prepare_queens_blood_deck",
    "stop_all_input",
})
ASYNC_CAPTURE_ALLOWED_OPERATIONS = frozenset({
    ASYNC_CAPTURE_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_STARTUP_ALLOWED_OPERATIONS = frozenset({
    ASYNC_STARTUP_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_QUEENS_BLOOD_ALLOWED_OPERATIONS = frozenset({
    ASYNC_QUEENS_BLOOD_STATUS_OPERATION,
    "get_queens_blood_preparation_context",
    "prepare_queens_blood_deck",
    "stop_all_input",
})
ASYNC_CONVERSATION_ALLOWED_OPERATIONS = frozenset({
    ASYNC_CONVERSATION_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_CLOSE_ALLOWED_OPERATIONS = frozenset({
    ASYNC_CLOSE_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_MULLIGAN_ALLOWED_OPERATIONS = frozenset({
    ASYNC_MULLIGAN_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_SHOP_BUY_ALLOWED_OPERATIONS = frozenset({
    ASYNC_SHOP_BUY_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_FIELD_MENU_ALLOWED_OPERATIONS = frozenset({
    ASYNC_FIELD_MENU_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_MATCH_ALLOWED_OPERATIONS = frozenset({
    ASYNC_MATCH_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_SCAN_ALLOWED_OPERATIONS = frozenset({
    ASYNC_SCAN_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_DECK_ALLOWED_OPERATIONS = frozenset({
    ASYNC_DECK_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_TRAVEL_ALLOWED_OPERATIONS = frozenset({
    ASYNC_TRAVEL_STATUS_OPERATION,
    "get_queens_blood_preparation_context",
    "prepare_queens_blood_deck",
    "stop_all_input",
})
ASYNC_SIMULATOR_ALLOWED_OPERATIONS = frozenset({
    ASYNC_SIMULATOR_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_LOAD_SLOT_ALLOWED_OPERATIONS = frozenset({
    ASYNC_LOAD_SLOT_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_SLOT_STARTUP_ALLOWED_OPERATIONS = frozenset({
    ASYNC_SLOT_STARTUP_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_NEW_GAME_ALLOWED_OPERATIONS = frozenset({
    ASYNC_NEW_GAME_STATUS_OPERATION,
    "stop_all_input",
})
ASYNC_STATUS_OPERATIONS = frozenset({
    ASYNC_SETUP_STATUS_OPERATION,
    ASYNC_ROUTE_STATUS_OPERATION,
    ASYNC_CAPTURE_STATUS_OPERATION,
    ASYNC_STARTUP_STATUS_OPERATION,
    ASYNC_QUEENS_BLOOD_STATUS_OPERATION,
    ASYNC_CONVERSATION_STATUS_OPERATION,
    ASYNC_CLOSE_STATUS_OPERATION,
    ASYNC_MULLIGAN_STATUS_OPERATION,
    ASYNC_SHOP_BUY_STATUS_OPERATION,
    ASYNC_FIELD_MENU_STATUS_OPERATION,
    ASYNC_MATCH_STATUS_OPERATION,
    ASYNC_SCAN_STATUS_OPERATION,
    ASYNC_DECK_STATUS_OPERATION,
    ASYNC_TRAVEL_STATUS_OPERATION,
    ASYNC_SIMULATOR_STATUS_OPERATION,
    ASYNC_LOAD_SLOT_STATUS_OPERATION,
    ASYNC_SLOT_STARTUP_STATUS_OPERATION,
    ASYNC_NEW_GAME_STATUS_OPERATION,
    ASYNC_SAVE_STATUS_OPERATION,
    ASYNC_RECOVERY_STATUS_OPERATION,
    ASYNC_EXPLORE_STATUS_OPERATION,
})

# These operations belong to the stable bridge/control plane. They must not
# be sent to runtime_commands.invoke_runtime, where they would be reported as
# unsupported worker operations before being retried by a caller.
BRIDGE_OPERATIONS = frozenset({
    "get_agent_runtime_status",
    "get_runtime_identity",
    "get_bootstrap_verification",
    "restart_agent_runtime",
    "say_on_stream",
})
_READINESS_EXEMPT_OPERATIONS = BRIDGE_OPERATIONS | {
    "__shutdown__",
    "stop_all_input",
}


def _bridge_identity() -> dict[str, Any]:
    """Describe the actual host transport, including the HTTP singleton."""
    return {
        "name": os.getenv("FF7R_MCP_BRIDGE_NAME", "ff7r-agent-mcp"),
        "entrypoint": os.getenv(
            "FF7R_MCP_BRIDGE_ENTRYPOINT",
            "ff7r_agent.mcp_runtime_server:main",
        ),
        "transport": os.getenv("FF7R_MCP_TRANSPORT", "stdio"),
        "pid": os.getpid(),
        "process_creation_token": process_identity_token(os.getpid()),
    }


class _ReboundBridgeProcess:
    """RPC client for a fresh source-bound runtime bridge subprocess."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        identity: dict[str, Any],
        *,
        request_timeout_seconds: float,
    ) -> None:
        self._process = process
        self._identity = dict(identity)
        self._request_timeout_seconds = request_timeout_seconds
        self._lock = threading.Lock()
        self._request_id = 0

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    @classmethod
    def start(
        cls,
        *,
        current: dict[str, Any],
        contract: dict[str, Any],
        broker: BrokerProcess | None,
        startup_timeout_seconds: float,
        request_timeout_seconds: float,
    ) -> "_ReboundBridgeProcess":
        child_environment = os.environ.copy()
        if contract.get("deployment_profile") == "FEATURE_VALIDATION":
            child_environment["FF7R_RUNTIME_EXPECTED_COMMIT"] = str(current["git_commit_sha"])
        package_root = str(current["package_import_root"])
        existing_pythonpath = child_environment.get("PYTHONPATH")
        child_environment["PYTHONPATH"] = (
            package_root
            if not existing_pythonpath
            else package_root + os.pathsep + existing_pythonpath
        )
        child_environment["FF7R_MCP_BRIDGE_NAME"] = "ff7r-agent-runtime-child"
        child_environment["FF7R_MCP_BRIDGE_ENTRYPOINT"] = "ff7r_agent.runtime_bridge_process:main"
        # The parent owns request-scoped recovery. A child must never create
        # another bridge child recursively during the same handoff.
        child_environment["FF7R_RUNTIME_REBIND_DISABLED"] = "1"
        if broker is not None:
            child_environment["FF7R_RUNTIME_SHARED_BROKER_PORT"] = str(broker.port)
            child_environment["FF7R_RUNTIME_SHARED_BROKER_PID"] = str(broker.process.pid)
            child_environment["FF7R_GAMEPAD_BROKER_PORT"] = str(broker.port)
        process = subprocess.Popen(
            [sys.executable, "-m", "ff7r_agent.runtime_bridge_process"],
            cwd=str(current["repository_root"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
            env=child_environment,
            **no_console_window_kwargs(),
        )
        if process.stdin is None or process.stdout is None:
            cls._terminate(process)
            raise RuntimeBridgeError("rebound bridge process pipes were not created")
        try:
            ready_line = RuntimeSupervisor._readline_with_timeout(
                process.stdout,
                startup_timeout_seconds,
                "rebound bridge process startup timed out",
            )
            ready = json.loads(ready_line)
            if ready.get("ok") is not True or ready.get("event") != "bridge_ready":
                raise RuntimeBridgeError(f"rebound bridge process did not become ready: {ready}")
            identity = ready.get("identity")
            if not isinstance(identity, dict):
                raise RuntimeBridgeError("rebound bridge process returned no identity")
        except Exception:
            cls._terminate(process)
            raise
        return cls(
            process,
            identity,
            request_timeout_seconds=request_timeout_seconds,
        )

    @property
    def identity(self) -> dict[str, Any]:
        return dict(self._identity)

    def is_alive(self) -> bool:
        return self._process.poll() is None

    def active_async_job(self) -> dict[str, Any] | None:
        result = self.call(INTERNAL_ACTIVE_ASYNC_JOB_OPERATION, {})
        active_job = result.get("active_job")
        return dict(active_job) if isinstance(active_job, dict) else None

    def call(self, operation: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if not self.is_alive() or self._process.stdin is None or self._process.stdout is None:
                raise RuntimeBridgeError("rebound bridge process is not running")
            self._request_id += 1
            request_id = self._request_id
            message = {
                "id": request_id,
                "operation": operation,
                "arguments": dict(arguments or {}),
            }
            try:
                self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
                self._process.stdin.flush()
                line = RuntimeSupervisor._readline_with_timeout(
                    self._process.stdout,
                    self._request_timeout_seconds,
                    "rebound bridge process request timed out",
                )
            except RuntimeBridgeError:
                raise
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeBridgeError("rebound bridge process transport failed") from exc
            if not line:
                raise RuntimeBridgeError("rebound bridge process exited before replying")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeBridgeError("rebound bridge process returned invalid JSON") from exc
            if response.get("id") != request_id or not isinstance(response.get("result"), dict):
                raise RuntimeBridgeError("rebound bridge process response was invalid")
            return response["result"]

    def close(self) -> None:
        with self._lock:
            if self.is_alive() and self._process.stdin is not None and self._process.stdout is not None:
                try:
                    self._request_id += 1
                    request_id = self._request_id
                    self._process.stdin.write(json.dumps({
                        "id": request_id,
                        "operation": "__bridge_shutdown__",
                        "arguments": {},
                    }, separators=(",", ":")) + "\n")
                    self._process.stdin.flush()
                    RuntimeSupervisor._readline_with_timeout(
                        self._process.stdout,
                        min(self._request_timeout_seconds, 2.0),
                        "rebound bridge process shutdown timed out",
                    )
                except Exception:
                    pass
            self._terminate(self._process)


class RuntimeSupervisor:
    """Own a restartable worker and process-level bridge handoff behind stable transport.

    The worker imports AgentService and navigation policy. Restarting it picks
    up edited source without restarting MCP. When the Windows gamepad backend
    is active, a separate broker owns the virtual X360 device across worker
    generations so a code reload does not remove the controller from Rebirth.
    """

    def __init__(
        self,
        *,
        # Live 2026-09-28 with the game, OBS and Windows audiodg saturating
        # the CPU, every worker start and in-place bridge rebind timed out at
        # 5 s ("runtime worker could not become ready").
        # Live 2026-10-05 a worker took 15-25 s to start with the game
        # running (5-6 s of imports alone); 20 s failed the first request.
        startup_timeout_seconds: float = 60.0,
        request_timeout_seconds: float = DEFAULT_RUNTIME_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._speech = AgentSpeechPublisher(
            Path(os.environ["FF7R_PUBLIC_STREAM_EVENT_DIR"])
            if os.getenv("FF7R_PUBLIC_STREAM_EVENT_DIR") else None
        )
        # The public-call lock already serializes requests; this explicit
        # lock documents and protects the source rebind boundary if lifecycle
        # code invokes it directly in the future.
        self._rebind_lock = threading.Lock()
        try:
            self._bridge_source_identity = collect_identity(
                process_role="mcp-bridge",
                module_path=__file__,
            )
        except Exception as exc:
            # Do not make construction itself mutate or repair anything. A
            # missing binding is handled fail-closed by the first live call.
            self._bridge_source_identity = {
                "process_role": "mcp-bridge",
                "binding_error": f"{type(exc).__name__}: {exc}",
            }
        self._bridge_process: _ReboundBridgeProcess | None = None
        self._active_bridge_identity: dict[str, Any] | None = None
        self._startup_timeout_seconds = float(startup_timeout_seconds)
        if not 0.5 <= self._startup_timeout_seconds <= 120.0:
            raise ValueError("startup timeout must be between 0.5 and 120 seconds")
        self._request_timeout_seconds = float(request_timeout_seconds)
        if not 0.5 <= self._request_timeout_seconds <= 120.0:
            raise ValueError("request timeout must be between 0.5 and 120 seconds")
        self._worker: subprocess.Popen[str] | None = None
        self._broker: BrokerProcess | None = None
        self._generation = 0
        self._request_id = 0
        self._started_at: float | None = None
        self._last_restart_at: float | None = None
        self._last_startup_duration_seconds: float | None = None
        self._worker_identity: dict[str, Any] | None = None
        self._closing = False
        self._closed = False
        self._async_result_lock = threading.Lock()
        self._async_request: dict[str, Any] | None = None

    def _ensure_broker(self) -> None:
        if self._closing or self._closed:
            raise RuntimeBridgeError("runtime supervisor is closed")
        if os.getenv("FF7R_INPUT_BACKEND", "simulation").lower() != "windows_gamepad":
            return
        if self._broker is not None and self._broker.process.poll() is None:
            return
        shared_port = os.getenv("FF7R_RUNTIME_SHARED_BROKER_PORT")
        if shared_port:
            try:
                shared_pid = int(os.getenv("FF7R_RUNTIME_SHARED_BROKER_PID", "0"))
                self._broker = BrokerProcess.attach(
                    port=int(shared_port),
                    process_id=shared_pid,
                )
                if self._broker.process.poll() is not None:
                    raise RuntimeBridgeError("shared gamepad broker is not running")
            except Exception as exc:
                self._broker = None
                raise RuntimeBridgeError("shared gamepad broker could not be attached") from exc
            return
        self._broker = BrokerProcess.start()

    def _worker_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._broker is not None:
            env["FF7R_GAMEPAD_BROKER_PORT"] = str(self._broker.port)
        return env

    @staticmethod
    def _readline_with_timeout(
        stream: Any,
        timeout_seconds: float,
        timeout_message: str = "runtime worker startup timed out",
    ) -> str:
        value: list[str] = []
        failure: list[BaseException] = []

        def read() -> None:
            try:
                value.append(stream.readline())
            except BaseException as exc:  # transport thread must report to caller
                failure.append(exc)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout_seconds)
        if reader.is_alive():
            raise RuntimeBridgeError(timeout_message)
        if failure:
            raise RuntimeBridgeError("runtime worker readiness read failed") from failure[0]
        return value[0] if value else ""

    def _start_worker_locked(self) -> dict[str, Any]:
        self._ensure_broker()
        startup_started = time.monotonic()
        worker = subprocess.Popen(
            [sys.executable, "-m", "ff7r_agent.runtime_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=self._worker_environment(),
            **no_console_window_kwargs(),
        )
        if worker.stdin is None or worker.stdout is None:
            worker.terminate()
            raise RuntimeBridgeError("runtime worker pipes were not created")
        try:
            ready = self._readline_with_timeout(worker.stdout, self._startup_timeout_seconds)
        except Exception:
            worker.terminate()
            raise
        if not ready:
            code = worker.poll()
            raise RuntimeBridgeError(f"runtime worker exited during startup (code={code})")
        try:
            payload = json.loads(ready)
        except json.JSONDecodeError as exc:
            worker.terminate()
            raise RuntimeBridgeError(f"invalid runtime readiness payload: {ready!r}") from exc
        if payload.get("ok") is not True or payload.get("event") != "runtime_ready":
            worker.terminate()
            raise RuntimeBridgeError(f"runtime worker did not become ready: {payload}")
        self._worker = worker
        self._generation += 1
        self._started_at = time.time()
        self._last_startup_duration_seconds = max(
            0.0, time.monotonic() - startup_started,
        )
        ready_identity = payload.get("identity")
        self._worker_identity = dict(ready_identity) if isinstance(ready_identity, dict) else None
        contract = profile_contract_from_environment()
        if contract is not None:
            verification = verify_runtime_identity(self._worker_identity or {}, contract=contract)
            if not verification.get("ok"):
                try:
                    worker.terminate()
                    worker.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        worker.kill()
                    except OSError:
                        pass
                self._worker = None
                self._worker_identity = None
                raise RuntimeBridgeError(
                    "runtime worker source binding verification failed: "
                    + json.dumps(verification, sort_keys=True)
                )
        if self._worker_identity is not None:
            self._worker_identity["worker_generation"] = self._generation
            self._worker_identity["worker_pid"] = worker.pid
        return self.status_locked()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.poll() is None:
            return
        self._worker = None
        self._start_worker_locked()

    @staticmethod
    def _source_binding_summary(identity: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(identity, dict):
            return {}
        return {
            key: identity.get(key)
            for key in (
                "deployment_profile",
                "repository_root",
                "source_root",
                "package_import_root",
                "expected_feature_ref",
                "expected_feature_commit",
                "expected_branch",
                "git_branch",
                "git_commit_sha",
                "source_commit_sha",
                "git_dirty",
                "git_dirty_paths",
            )
            if key in identity
        }

    @staticmethod
    def _same_source_path(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return False
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))

    def _source_binding_mismatches(
        self,
        bound: dict[str, Any],
        current: dict[str, Any],
        contract: dict[str, Any],
    ) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        if bound.get("binding_error"):
            mismatches.append({"field": "bridge_source_binding", "actual": bound.get("binding_error")})
        for field in ("git_commit_sha", "git_branch", "git_dirty"):
            if bound.get(field) != current.get(field):
                mismatches.append({
                    "field": f"bridge.{field}",
                    "expected": current.get(field),
                    "actual": bound.get(field),
                })
        for field in ("repository_root", "source_root", "package_import_root"):
            if not self._same_source_path(bound.get(field), current.get(field)):
                mismatches.append({
                    "field": f"bridge.{field}",
                    "expected": current.get(field),
                    "actual": bound.get(field),
                })
        binding_verification = verify_runtime_identity(bound, contract=contract)
        if not binding_verification.get("ok"):
            mismatches.append({
                "field": "bridge_source_binding_contract",
                "mismatches": binding_verification.get("mismatches", []),
            })
        if contract.get("deployment_profile") == "FEATURE_VALIDATION":
            expected_commit = contract.get("expected_feature_commit")
            if expected_commit != current.get("git_commit_sha"):
                mismatches.append({
                    "field": "profile.expected_feature_commit",
                    "expected": current.get("git_commit_sha"),
                    "actual": expected_commit,
                })
        return mismatches

    def _raise_readiness_error(
        self,
        *,
        operation: str,
        phase: str,
        message: str,
        diagnostics: dict[str, Any] | None = None,
        error_code: str = "RUNTIME_BRIDGE_NOT_READY",
    ) -> None:
        raise RuntimeInfrastructureError(
            error_code,
            operation=operation,
            phase=phase,
            message=message,
            diagnostics=diagnostics,
        )

    def _start_rebound_bridge_locked(
        self,
        current: dict[str, Any],
        contract: dict[str, Any],
    ) -> _ReboundBridgeProcess:
        # The parent owns the stable broker before any child is started.  This
        # prevents a child from becoming the broker owner when the first live
        # call arrives before the parent has created one, which would make a
        # later generation unable to bind the singleton broker.
        self._ensure_broker()
        return _ReboundBridgeProcess.start(
            current=current,
            contract=contract,
            broker=self._broker,
            startup_timeout_seconds=self._startup_timeout_seconds,
            request_timeout_seconds=self._request_timeout_seconds,
        )

    def _active_async_job_for_rebind_locked(self) -> dict[str, Any] | None:
        if self._bridge_process is not None:
            if not self._bridge_process.is_alive():
                return None
            return self._bridge_process.active_async_job()
        return self._active_async_job_locked()

    def _rebind_to_current_checkout_locked(
        self,
        operation: str,
        current: dict[str, Any],
        contract: dict[str, Any],
        mismatches: list[dict[str, Any]],
    ) -> None:
        previous_identity = (
            self._active_bridge_identity
            if self._bridge_process is not None
            else self._bridge_source_identity
        )
        previous = self._source_binding_summary(previous_identity)
        current_revision = str(current.get("git_commit_sha") or "")
        target_contract = dict(contract)
        if contract.get("deployment_profile") == "FEATURE_VALIDATION":
            target_contract["expected_feature_commit"] = current_revision
        with self._rebind_lock:
            try:
                active_job = self._active_async_job_for_rebind_locked()
            except Exception as exc:
                self._raise_readiness_error(
                    operation=operation,
                    phase="active_job_check",
                    message="could not determine whether the stale bridge has an active asynchronous job",
                    error_code="REBIND_DEFERRED_ACTIVE_JOB",
                    diagnostics={
                        "rebind_attempted": False,
                        "cause": f"{type(exc).__name__}: {exc}",
                        "previous_binding": previous,
                        "expected_source": self._source_binding_summary(current),
                        "source_binding_mismatches": mismatches,
                    },
                )
            if active_job is not None:
                self._raise_readiness_error(
                    operation=operation,
                    phase="rebind_deferred",
                    message="automatic bridge rebind deferred while an asynchronous live operation is running",
                    error_code="REBIND_DEFERRED_ACTIVE_JOB",
                    diagnostics={
                        "rebind_attempted": False,
                        "active_job": active_job,
                        "previous_binding": previous,
                        "expected_source": self._source_binding_summary(current),
                        "source_binding_mismatches": mismatches,
                    },
                )
            replacement: _ReboundBridgeProcess | None = None
            try:
                _LOGGER.info(
                    "runtime bridge source binding is stale; starting a fresh bridge process from %s to %s",
                    previous.get("git_commit_sha"),
                    current_revision,
                )
                # The parent environment and its historical binding remain
                # untouched. The child gets a private contract for the already
                # validated checkout and must prove its loaded module paths.
                replacement = self._start_rebound_bridge_locked(current, target_contract)
                rebound = replacement.identity
                verification = verify_runtime_identity(
                    rebound,
                    contract=target_contract,
                    worker_identity=rebound.get("worker_identity"),
                )
                if not verification.get("ok"):
                    raise RuntimeBridgeError(
                        "fresh bridge source verification failed: "
                        + json.dumps(verification, sort_keys=True)
                    )
                if rebound.get("git_commit_sha") != current_revision:
                    raise RuntimeBridgeError(
                        "fresh bridge process source revision did not match current checkout"
                    )

                # The replacement has proved readiness before the old local
                # worker is stopped. This keeps a failed handoff from changing
                # the parent's environment or source-binding record.
                old_process = self._bridge_process
                self._shutdown_worker_locked()
                self._bridge_process = replacement
                self._active_bridge_identity = rebound
                self._worker = None
                self._worker_identity = None
                replacement = None
                if old_process is not None:
                    old_process.close()
                _LOGGER.info("runtime bridge process rebind completed at %s", current_revision)
            except Exception as exc:
                if replacement is not None:
                    try:
                        replacement.close()
                    except Exception:
                        _LOGGER.exception("runtime bridge cleanup after failed process rebind also failed")
                _LOGGER.exception("runtime bridge process rebind failed at %s", current_revision)
                self._raise_readiness_error(
                    operation=operation,
                    phase="rebind",
                    message="automatic bridge process rebind failed; live operation was not sent",
                    diagnostics={
                        "rebind_attempted": True,
                        "previous_binding": previous,
                        "expected_source": self._source_binding_summary(current),
                        "source_binding_mismatches": mismatches,
                        "cause": f"{type(exc).__name__}: {exc}",
                    },
                )

    def _ensure_live_runtime_ready_locked(self, operation: str) -> None:
        if operation in _READINESS_EXEMPT_OPERATIONS:
            return
        try:
            contract = profile_contract_from_environment()
        except Exception as exc:
            self._raise_readiness_error(
                operation=operation,
                phase="contract",
                message="runtime source-binding contract could not be loaded",
                diagnostics={"cause": f"{type(exc).__name__}: {exc}"},
            )
        if contract is None:
            if self._bridge_process is not None:
                return
            # Legacy/simulation runtimes have no explicit source contract. The
            # normal worker lifecycle remains unchanged for those callers.
            try:
                self._ensure_worker_locked()
            except Exception as exc:
                self._raise_readiness_error(
                    operation=operation,
                    phase="worker_start",
                    message="runtime worker could not become ready",
                    diagnostics={"cause": f"{type(exc).__name__}: {exc}"},
                )
            return
        try:
            current = _cached_checkout_validation(contract)
        except Exception as exc:
            self._raise_readiness_error(
                operation=operation,
                phase="checkout",
                message="current runtime checkout failed source-binding validation",
                diagnostics={"cause": f"{type(exc).__name__}: {exc}"},
            )
        bound_identity = (
            self._active_bridge_identity
            if self._bridge_process is not None
            else self._bridge_source_identity
        )
        comparison_contract = dict(contract)
        if (
            self._bridge_process is not None
            and contract.get("deployment_profile") == "FEATURE_VALIDATION"
        ):
            comparison_contract["expected_feature_commit"] = current.get("git_commit_sha")
        mismatches = self._source_binding_mismatches(
            bound_identity,
            current,
            comparison_contract,
        )
        if self._bridge_process is not None and not self._bridge_process.is_alive():
            mismatches.append({
                "field": "bridge_process_alive",
                "expected": True,
                "actual": False,
            })
        if self._worker_identity is not None:
            worker_verification = verify_runtime_identity(
                self._worker_identity,
                contract=comparison_contract,
            )
            if not worker_verification.get("ok"):
                mismatches.append({
                    "field": "worker_source_binding",
                    "mismatches": worker_verification.get("mismatches", []),
                })
        if mismatches and operation in ASYNC_STATUS_OPERATIONS:
            # Status is read-only and must remain owned by the runtime that
            # created the job, even after it reaches a terminal state.  The
            # next non-status operation will perform the source rebind.
            if self._bridge_process is None or self._bridge_process.is_alive():
                return
        if mismatches and os.getenv("FF7R_RUNTIME_REBIND_DISABLED") == "1":
            self._raise_readiness_error(
                operation=operation,
                phase="child_binding",
                message="active bridge child is stale; parent rebind is required",
                diagnostics={
                    "rebind_attempted": False,
                    "expected_source": self._source_binding_summary(current),
                    "source_binding_mismatches": mismatches,
                },
            )
        if mismatches:
            self._rebind_to_current_checkout_locked(
                operation,
                current,
                contract,
                mismatches,
            )
            return
        if self._bridge_process is not None:
            return
        try:
            self._ensure_worker_locked()
        except Exception as exc:
            self._raise_readiness_error(
                operation=operation,
                phase="worker_start",
                message="runtime worker could not become ready",
                diagnostics={
                    "expected_source": self._source_binding_summary(current),
                    "cause": f"{type(exc).__name__}: {exc}",
                },
            )

    def _terminate_worker_locked(self) -> None:
        """Terminate a wedged worker without retrying its in-flight command."""
        worker = self._worker
        if worker is None:
            return
        with self._async_result_lock:
            request = self._async_request
            if request is not None and request.get("state") == "running":
                label = {
                    ASYNC_CAPTURE_WORK_OPERATION: "topology capture",
                    ASYNC_STARTUP_WORK_OPERATION: "startup",
                    ASYNC_QUEENS_BLOOD_WORK_OPERATION: "Queen's Blood initiation",
                    ASYNC_CONVERSATION_WORK_OPERATION: "conversation recovery",
                    ASYNC_CLOSE_WORK_OPERATION: "game close",
                    ASYNC_MULLIGAN_WORK_OPERATION: "Queen's Blood mulligan",
                    ASYNC_SHOP_BUY_WORK_OPERATION: "shop purchase",
                    ASYNC_FIELD_MENU_WORK_OPERATION: "field menu",
                    ASYNC_MATCH_WORK_OPERATION: "Queen's Blood match",
                    ASYNC_SCAN_WORK_OPERATION: "Queen's Blood collection scan",
                    ASYNC_DECK_WORK_OPERATION: "Queen's Blood deck edit",
                    ASYNC_TRAVEL_WORK_OPERATION: "map fast travel",
                    ASYNC_SIMULATOR_WORK_OPERATION: "combat simulator entry",
                    ASYNC_LOAD_SLOT_WORK_OPERATION: "save slot load",
                    ASYNC_SLOT_STARTUP_WORK_OPERATION: "startup with save slot load",
                    ASYNC_NEW_GAME_WORK_OPERATION: "new game startup",
                    ASYNC_SAVE_WORK_OPERATION: "game save",
                    ASYNC_RECOVERY_WORK_OPERATION: "working save recovery",
                    ASYNC_EXPLORE_WORK_OPERATION: "explore walk",
                }.get(request.get("operation"), "route")
                request["state"] = "cancelled"
                request["error"] = (
                    "runtime worker terminated while asynchronous "
                    f"{label} request was active"
                )
        try:
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    try:
                        worker.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            self._worker = None
            self._worker_identity = None
            # A worker can be killed while it is holding a movement command.
            # Centre the persistent broker without disconnecting the device.
            if self._broker is not None:
                try:
                    self._broker.neutralise()
                except Exception:
                    # The broker watchdog remains the final neutralisation
                    # boundary if the immediate best-effort call fails.
                    pass

    @staticmethod
    def _artifact_descriptor(artifact: Any) -> dict[str, Any] | None:
        if not isinstance(artifact, dict):
            return None
        schema = artifact.get("schema")
        path = artifact.get("path")
        digest = artifact.get("sha256")
        size = artifact.get("bytes")
        if (
            schema != RESULT_ARTIFACT_SCHEMA
            or not isinstance(path, str)
            or not path
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest.lower())
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            return None
        descriptor = {
            "schema": schema,
            "path": path,
            "sha256": digest.lower(),
            "bytes": size,
        }
        for key in ("operation", "response_id", "worker_pid"):
            if key in artifact:
                descriptor[key] = artifact[key]
        return descriptor

    @classmethod
    def _validate_response_artifact(cls, artifact: Any) -> dict[str, Any]:
        descriptor = cls._artifact_descriptor(artifact)
        if descriptor is None:
            raise RuntimeBridgeError("retained_result_artifact_metadata_invalid")
        path = Path(descriptor["path"])
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RuntimeBridgeError("retained_result_artifact_unavailable") from exc
        if len(raw) != descriptor["bytes"]:
            raise RuntimeBridgeError("retained_result_artifact_size_mismatch")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != descriptor["sha256"]:
            raise RuntimeBridgeError("retained_result_artifact_sha256_mismatch")
        return descriptor

    @classmethod
    def _load_response_artifact(cls, artifact: Any) -> dict[str, Any]:
        descriptor = cls._validate_response_artifact(artifact)
        try:
            value = json.loads(Path(descriptor["path"]).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeBridgeError("retained_result_artifact_json_invalid") from exc
        if not isinstance(value, dict):
            raise RuntimeBridgeError("retained_result_artifact_response_invalid")
        return value

    @staticmethod
    def _compact_async_response_summary(response: dict[str, Any]) -> dict[str, Any]:
        """Return bounded terminal facts without loading an artifact payload."""
        result = response.get("result")
        if not isinstance(result, dict):
            return {"response_ok": response.get("ok") is True, "evidence_retained": True}
        artifact = RuntimeSupervisor._artifact_descriptor(result.get("artifact"))
        if artifact is not None:
            return {
                "response_ok": response.get("ok") is True,
                "evidence_retained": True,
                "evidence_artifact": artifact,
            }
        summary: dict[str, Any] = {
            "response_ok": response.get("ok") is True,
            "evidence_retained": True,
        }
        scalar_keys = (
            "ok", "state", "reason", "reason_code", "phase", "decision",
            "distance_to_final_destination_m", "movement_cycles", "pulse_count",
            "cycles", "route_distance_m", "observed_displacement_m",
        )
        for key in scalar_keys:
            if key not in result:
                continue
            value = result[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                summary[key] = value
        for key in ("route_metrics", "controller_policy", "navigation_time_budget"):
            value = result.get(key)
            if isinstance(value, dict):
                summary[key] = value
        timeline = result.get("timeline")
        if isinstance(timeline, list):
            counts: dict[str, int] = {}
            for item in timeline:
                if isinstance(item, dict) and isinstance(item.get("event"), str):
                    event = item["event"]
                    counts[event] = counts.get(event, 0) + 1
            summary["timeline_event_count"] = len(timeline)
            summary["timeline_event_counts"] = dict(sorted(counts.items()))
            summary["last_events"] = [
                {"event": item.get("event"), "reason": item.get("reason"), "sequence": item.get("sequence")}
                for item in timeline[-8:]
                if isinstance(item, dict)
            ]
        return summary

    def _async_status_locked(
        self,
        job_id: str | None = None,
        *,
        status_operation: str | None = None,
        include_response: bool = True,
    ) -> dict[str, Any]:
        with self._async_result_lock:
            request = dict(self._async_request) if self._async_request is not None else None
        if request is None:
            label = (
                "startup"
                if status_operation == ASYNC_STARTUP_STATUS_OPERATION
                else "topology capture"
                if status_operation == ASYNC_CAPTURE_STATUS_OPERATION
                else "Queen's Blood initiation"
                if status_operation == ASYNC_QUEENS_BLOOD_STATUS_OPERATION
                else "conversation recovery"
                if status_operation == ASYNC_CONVERSATION_STATUS_OPERATION
                else "game close"
                if status_operation == ASYNC_CLOSE_STATUS_OPERATION
                else "Queen's Blood mulligan"
                if status_operation == ASYNC_MULLIGAN_STATUS_OPERATION
                else "shop purchase"
                if status_operation == ASYNC_SHOP_BUY_STATUS_OPERATION
                else "field menu"
                if status_operation == ASYNC_FIELD_MENU_STATUS_OPERATION
                else "Queen's Blood match"
                if status_operation == ASYNC_MATCH_STATUS_OPERATION
                else "Queen's Blood collection scan"
                if status_operation == ASYNC_SCAN_STATUS_OPERATION
                else "Queen's Blood match setup"
                if status_operation == ASYNC_SETUP_STATUS_OPERATION
                else "Queen's Blood deck edit"
                if status_operation == ASYNC_DECK_STATUS_OPERATION
                else "map fast travel"
                if status_operation == ASYNC_TRAVEL_STATUS_OPERATION
                else "combat simulator entry"
                if status_operation == ASYNC_SIMULATOR_STATUS_OPERATION
                else "save slot load"
                if status_operation == ASYNC_LOAD_SLOT_STATUS_OPERATION
                else "startup with save slot load"
                if status_operation == ASYNC_SLOT_STARTUP_STATUS_OPERATION
                else "new game startup"
                if status_operation == ASYNC_NEW_GAME_STATUS_OPERATION
                else "game save"
                if status_operation == ASYNC_SAVE_STATUS_OPERATION
                else "working save recovery"
                if status_operation == ASYNC_RECOVERY_STATUS_OPERATION
                else "explore walk"
                if status_operation == ASYNC_EXPLORE_STATUS_OPERATION
                else "route"
            )
            return {"ok": False, "error": f"no asynchronous {label} request exists"}
        if job_id is not None and str(job_id) != request.get("job_id"):
            label = (
                "startup"
                if request.get("operation") == ASYNC_STARTUP_WORK_OPERATION
                else "topology capture"
                if request.get("operation") == ASYNC_CAPTURE_WORK_OPERATION
                else "Queen's Blood initiation"
                if request.get("operation") == ASYNC_QUEENS_BLOOD_WORK_OPERATION
                else "conversation recovery"
                if request.get("operation") == ASYNC_CONVERSATION_WORK_OPERATION
                else "game close"
                if request.get("operation") == ASYNC_CLOSE_WORK_OPERATION
                else "Queen's Blood mulligan"
                if request.get("operation") == ASYNC_MULLIGAN_WORK_OPERATION
                else "shop purchase"
                if request.get("operation") == ASYNC_SHOP_BUY_WORK_OPERATION
                else "field menu"
                if request.get("operation") == ASYNC_FIELD_MENU_WORK_OPERATION
                else "Queen's Blood match"
                if request.get("operation") == ASYNC_MATCH_WORK_OPERATION
                else "Queen's Blood collection scan"
                if request.get("operation") == ASYNC_SCAN_WORK_OPERATION
                else "Queen's Blood match setup"
                if request.get("operation") == ASYNC_SETUP_WORK_OPERATION
                else "Queen's Blood deck edit"
                if request.get("operation") == ASYNC_DECK_WORK_OPERATION
                else "map fast travel"
                if request.get("operation") == ASYNC_TRAVEL_WORK_OPERATION
                else "combat simulator entry"
                if request.get("operation") == ASYNC_SIMULATOR_WORK_OPERATION
                else "save slot load"
                if request.get("operation") == ASYNC_LOAD_SLOT_WORK_OPERATION
                else "startup with save slot load"
                if request.get("operation") == ASYNC_SLOT_STARTUP_WORK_OPERATION
                else "new game startup"
                if request.get("operation") == ASYNC_NEW_GAME_WORK_OPERATION
                else "game save"
                if request.get("operation") == ASYNC_SAVE_WORK_OPERATION
                else "working save recovery"
                if request.get("operation") == ASYNC_RECOVERY_WORK_OPERATION
                else "explore walk"
                if request.get("operation") == ASYNC_EXPLORE_WORK_OPERATION
                else "route"
            )
            return {
                "ok": False,
                "error": f"asynchronous {label} job not found",
                "job_id": str(job_id),
            }
        result = {
            "ok": True,
            "job_id": request.get("job_id"),
            "operation": request.get("operation", ASYNC_ROUTE_WORK_OPERATION),
            "state": request.get("state"),
            "started_at": request.get("started_at"),
            "worker_generation": request.get("worker_generation"),
            "worker_pid": request.get("worker_pid"),
        }
        if request.get("state") == "completed":
            artifact = request.get("response_artifact")
            if include_response:
                try:
                    result["response"] = (
                        self._load_response_artifact(artifact)
                        if artifact is not None
                        else request.get("response")
                    )
                except RuntimeBridgeError as exc:
                    result["ok"] = False
                    result["error"] = str(exc)
                    result["response_available"] = False
            else:
                # Terminal detection never reads or re-serializes the full
                # forensic route payload. The hash-bound artifact remains
                # retained for an explicit full retrieval.
                result["response_available"] = (
                    artifact is not None or request.get("response") is not None
                )
                if request.get("response_summary") is not None:
                    result["response_summary"] = request["response_summary"]
        elif request.get("error"):
            result["error"] = request.get("error")
        if request.get("progress") is not None:
            result["progress"] = dict(request["progress"])
        if request.get("controller_policy") is not None:
            result["controller_policy"] = dict(request["controller_policy"])
        if request.get("operation") == ASYNC_CAPTURE_WORK_OPERATION:
            status_path = request.get("capture_status_path")
            if status_path:
                try:
                    value = json.loads(Path(status_path).read_text(encoding="utf-8"))
                    if isinstance(value, dict) and (
                        request.get("capture_request_id") is None
                        or value.get("request_id") == request.get("capture_request_id")
                    ):
                        result["capture_status"] = value
                except (OSError, json.JSONDecodeError):
                    result["capture_status"] = None
        return result

    def _async_route_request_locked(self) -> dict[str, Any] | None:
        return self._active_async_job_locked()

    def _active_async_job_locked(self) -> dict[str, Any] | None:
        with self._async_result_lock:
            request = self._async_request
            if request is None or request.get("state") != "running":
                return None
            return dict(request)

    def active_async_job(self) -> dict[str, Any] | None:
        """Return the running async job without triggering source readiness checks."""

        with self._lock:
            return self._active_async_job_locked()

    @staticmethod
    def _async_route_error(operation: str, job_id: str, work_operation: str = ASYNC_ROUTE_WORK_OPERATION) -> str:
        allowed_set = {
            ASYNC_CAPTURE_WORK_OPERATION: ASYNC_CAPTURE_ALLOWED_OPERATIONS,
            ASYNC_STARTUP_WORK_OPERATION: ASYNC_STARTUP_ALLOWED_OPERATIONS,
            ASYNC_QUEENS_BLOOD_WORK_OPERATION: ASYNC_QUEENS_BLOOD_ALLOWED_OPERATIONS,
            ASYNC_CONVERSATION_WORK_OPERATION: ASYNC_CONVERSATION_ALLOWED_OPERATIONS,
            ASYNC_CLOSE_WORK_OPERATION: ASYNC_CLOSE_ALLOWED_OPERATIONS,
            ASYNC_MULLIGAN_WORK_OPERATION: ASYNC_MULLIGAN_ALLOWED_OPERATIONS,
            ASYNC_SHOP_BUY_WORK_OPERATION: ASYNC_SHOP_BUY_ALLOWED_OPERATIONS,
            ASYNC_FIELD_MENU_WORK_OPERATION: ASYNC_FIELD_MENU_ALLOWED_OPERATIONS,
            ASYNC_MATCH_WORK_OPERATION: ASYNC_MATCH_ALLOWED_OPERATIONS,
            ASYNC_SCAN_WORK_OPERATION: ASYNC_SCAN_ALLOWED_OPERATIONS,
            ASYNC_DECK_WORK_OPERATION: ASYNC_DECK_ALLOWED_OPERATIONS,
            ASYNC_SETUP_WORK_OPERATION: ASYNC_SETUP_ALLOWED_OPERATIONS,
            ASYNC_TRAVEL_WORK_OPERATION: ASYNC_TRAVEL_ALLOWED_OPERATIONS,
            ASYNC_SIMULATOR_WORK_OPERATION: ASYNC_SIMULATOR_ALLOWED_OPERATIONS,
            ASYNC_LOAD_SLOT_WORK_OPERATION: ASYNC_LOAD_SLOT_ALLOWED_OPERATIONS,
            ASYNC_SLOT_STARTUP_WORK_OPERATION: ASYNC_SLOT_STARTUP_ALLOWED_OPERATIONS,
            ASYNC_NEW_GAME_WORK_OPERATION: ASYNC_NEW_GAME_ALLOWED_OPERATIONS,
            ASYNC_SAVE_WORK_OPERATION: ASYNC_SAVE_ALLOWED_OPERATIONS,
            ASYNC_RECOVERY_WORK_OPERATION: ASYNC_RECOVERY_ALLOWED_OPERATIONS,
            ASYNC_EXPLORE_WORK_OPERATION: ASYNC_EXPLORE_ALLOWED_OPERATIONS,
        }.get(work_operation, ASYNC_ROUTE_ALLOWED_OPERATIONS)
        allowed = ", ".join(sorted(allowed_set | {"say_on_stream"}))
        prefix = {
            ASYNC_CAPTURE_WORK_OPERATION: "async_topology_capture_in_progress",
            ASYNC_STARTUP_WORK_OPERATION: "async_startup_in_progress",
            ASYNC_QUEENS_BLOOD_WORK_OPERATION: "async_queens_blood_in_progress",
            ASYNC_CONVERSATION_WORK_OPERATION: "async_conversation_recovery_in_progress",
            ASYNC_CLOSE_WORK_OPERATION: "async_close_in_progress",
            ASYNC_MULLIGAN_WORK_OPERATION: "async_mulligan_in_progress",
            ASYNC_SHOP_BUY_WORK_OPERATION: "async_shop_buy_in_progress",
            ASYNC_FIELD_MENU_WORK_OPERATION: "async_field_menu_in_progress",
            ASYNC_MATCH_WORK_OPERATION: "async_match_in_progress",
            ASYNC_SCAN_WORK_OPERATION: "async_scan_in_progress",
            ASYNC_DECK_WORK_OPERATION: "async_deck_in_progress",
            ASYNC_SETUP_WORK_OPERATION: "async_match_setup_in_progress",
            ASYNC_TRAVEL_WORK_OPERATION: "async_travel_in_progress",
            ASYNC_SIMULATOR_WORK_OPERATION: "async_simulator_in_progress",
            ASYNC_LOAD_SLOT_WORK_OPERATION: "async_load_slot_in_progress",
            ASYNC_SLOT_STARTUP_WORK_OPERATION: "async_slot_startup_in_progress",
            ASYNC_NEW_GAME_WORK_OPERATION: "async_new_game_in_progress",
            ASYNC_SAVE_WORK_OPERATION: "async_save_in_progress",
            ASYNC_RECOVERY_WORK_OPERATION: "async_recovery_in_progress",
            ASYNC_EXPLORE_WORK_OPERATION: "async_explore_in_progress",
        }.get(work_operation, "async_route_in_progress")
        return (
            f"{prefix}: operation "
            f"{operation!r} is unavailable while job {job_id!r} is running; "
            f"allowed operations are {allowed}"
        )

    @staticmethod
    def _async_job_allowed_operations(request: dict[str, Any]) -> list[str]:
        allowed = {
            ASYNC_ROUTE_WORK_OPERATION: ASYNC_ROUTE_ALLOWED_OPERATIONS,
            ASYNC_CAPTURE_WORK_OPERATION: ASYNC_CAPTURE_ALLOWED_OPERATIONS,
            ASYNC_STARTUP_WORK_OPERATION: ASYNC_STARTUP_ALLOWED_OPERATIONS,
            ASYNC_QUEENS_BLOOD_WORK_OPERATION: ASYNC_QUEENS_BLOOD_ALLOWED_OPERATIONS,
            ASYNC_CONVERSATION_WORK_OPERATION: ASYNC_CONVERSATION_ALLOWED_OPERATIONS,
            ASYNC_CLOSE_WORK_OPERATION: ASYNC_CLOSE_ALLOWED_OPERATIONS,
            ASYNC_MULLIGAN_WORK_OPERATION: ASYNC_MULLIGAN_ALLOWED_OPERATIONS,
            ASYNC_SHOP_BUY_WORK_OPERATION: ASYNC_SHOP_BUY_ALLOWED_OPERATIONS,
            ASYNC_FIELD_MENU_WORK_OPERATION: ASYNC_FIELD_MENU_ALLOWED_OPERATIONS,
            ASYNC_MATCH_WORK_OPERATION: ASYNC_MATCH_ALLOWED_OPERATIONS,
            ASYNC_SCAN_WORK_OPERATION: ASYNC_SCAN_ALLOWED_OPERATIONS,
            ASYNC_DECK_WORK_OPERATION: ASYNC_DECK_ALLOWED_OPERATIONS,
            ASYNC_SETUP_WORK_OPERATION: ASYNC_SETUP_ALLOWED_OPERATIONS,
            ASYNC_TRAVEL_WORK_OPERATION: ASYNC_TRAVEL_ALLOWED_OPERATIONS,
            ASYNC_SIMULATOR_WORK_OPERATION: ASYNC_SIMULATOR_ALLOWED_OPERATIONS,
            ASYNC_LOAD_SLOT_WORK_OPERATION: ASYNC_LOAD_SLOT_ALLOWED_OPERATIONS,
            ASYNC_SLOT_STARTUP_WORK_OPERATION: ASYNC_SLOT_STARTUP_ALLOWED_OPERATIONS,
            ASYNC_NEW_GAME_WORK_OPERATION: ASYNC_NEW_GAME_ALLOWED_OPERATIONS,
            ASYNC_SAVE_WORK_OPERATION: ASYNC_SAVE_ALLOWED_OPERATIONS,
            ASYNC_RECOVERY_WORK_OPERATION: ASYNC_RECOVERY_ALLOWED_OPERATIONS,
            ASYNC_EXPLORE_WORK_OPERATION: ASYNC_EXPLORE_ALLOWED_OPERATIONS,
        }.get(request.get("operation"), frozenset({"stop_all_input"}))
        return sorted(allowed | {"say_on_stream"})

    def _async_route_rejection_locked(
        self,
        operation: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        error_name = {
            ASYNC_CAPTURE_WORK_OPERATION: "async_topology_capture_in_progress",
            ASYNC_STARTUP_WORK_OPERATION: "async_startup_in_progress",
            ASYNC_QUEENS_BLOOD_WORK_OPERATION: "async_queens_blood_in_progress",
            ASYNC_CONVERSATION_WORK_OPERATION: "async_conversation_recovery_in_progress",
            ASYNC_CLOSE_WORK_OPERATION: "async_close_in_progress",
            ASYNC_MULLIGAN_WORK_OPERATION: "async_mulligan_in_progress",
            ASYNC_SHOP_BUY_WORK_OPERATION: "async_shop_buy_in_progress",
            ASYNC_FIELD_MENU_WORK_OPERATION: "async_field_menu_in_progress",
            ASYNC_MATCH_WORK_OPERATION: "async_match_in_progress",
            ASYNC_SCAN_WORK_OPERATION: "async_scan_in_progress",
            ASYNC_DECK_WORK_OPERATION: "async_deck_in_progress",
            ASYNC_SETUP_WORK_OPERATION: "async_match_setup_in_progress",
            ASYNC_TRAVEL_WORK_OPERATION: "async_travel_in_progress",
            ASYNC_SIMULATOR_WORK_OPERATION: "async_simulator_in_progress",
            ASYNC_LOAD_SLOT_WORK_OPERATION: "async_load_slot_in_progress",
            ASYNC_SLOT_STARTUP_WORK_OPERATION: "async_slot_startup_in_progress",
            ASYNC_NEW_GAME_WORK_OPERATION: "async_new_game_in_progress",
            ASYNC_SAVE_WORK_OPERATION: "async_save_in_progress",
            ASYNC_RECOVERY_WORK_OPERATION: "async_recovery_in_progress",
            ASYNC_EXPLORE_WORK_OPERATION: "async_explore_in_progress",
        }.get(request.get("operation"), "async_route_in_progress")
        return {
            "ok": False,
            "error": error_name,
            "error_type": "RuntimeBridgeError",
            "operation": operation,
            "job_id": request.get("job_id"),
            "allowed_operations": self._async_job_allowed_operations(request),
        }

    def _finish_async_request(
        self,
        job_id: str,
        worker: subprocess.Popen[str],
        request_id: int,
    ) -> None:
        response: dict[str, Any] | None = None
        error: str | None = None
        try:
            if worker.stdout is None:
                raise RuntimeBridgeError("runtime worker asynchronous response pipe was not created")
            while True:
                line = worker.stdout.readline()
                if not line:
                    raise RuntimeBridgeError("runtime worker exited before asynchronous response")
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise RuntimeBridgeError("runtime worker asynchronous response was not an object")
                if response.get("id") != request_id:
                    raise RuntimeBridgeError("runtime worker asynchronous response id mismatch")
                if response.get("event") != "match_setup_progress":
                    break
                from .queen_blood_setup import STAGES
                progress = response.get("progress")
                if (not isinstance(progress, dict) or progress.get("stage") not in STAGES
                        or not isinstance(progress.get("completed_stages"), list)
                        or len(progress["completed_stages"]) > 20
                        or any(stage not in STAGES for stage in progress["completed_stages"])
                        or type(progress.get("elapsed_seconds")) not in (int, float)
                        or not 0 <= progress["elapsed_seconds"] <= 3600):
                    raise RuntimeBridgeError("invalid match setup progress")
                with self._async_result_lock:
                    request = self._async_request
                    if request is None or request.get("job_id") != job_id or request.get("state") != "running":
                        return
                    if request.get("operation") != ASYNC_SETUP_WORK_OPERATION:
                        raise RuntimeBridgeError("unexpected match setup progress")
                    request["progress"] = {key: progress[key] for key in ("stage", "completed_stages", "elapsed_seconds")}
        except Exception as exc:
            error = str(exc)
        if error is not None:
            # A malformed/foreign response means the worker's response stream
            # can no longer be trusted. Fail the job and terminate that exact
            # worker before any later request can reuse its pipes.
            with self._lock:
                with self._async_result_lock:
                    request = self._async_request
                    if request is None or request.get("job_id") != job_id:
                        return
                    if request.get("state") == "cancelled":
                        return
                    request["state"] = "failed"
                    request["error"] = error
                if self._worker is worker:
                    self._terminate_worker_locked()
            return
        with self._async_result_lock:
            request = self._async_request
            if request is None or request.get("job_id") != job_id:
                return
            if request.get("state") == "cancelled":
                return
            request["state"] = "completed" if error is None else "failed"
            if response is not None:
                artifact = None
                response_result = response.get("result")
                if isinstance(response_result, dict):
                    artifact = self._artifact_descriptor(response_result.get("artifact"))
                if artifact is not None:
                    expected_operation = request.get("operation")
                    expected_response_id = request_id
                    # On Windows, the venv launcher can leave Popen's PID as
                    # a shim/parent while runtime code executes in its child
                    # Python process. Artifacts are written by that executing
                    # process (os.getpid()), which is attested in readiness
                    # identity. Keep Popen's PID for supervision/status, but
                    # bind artifact provenance to the attested runtime PID.
                    worker_identity_pid = None
                    if isinstance(self._worker_identity, dict):
                        candidate_pid = self._worker_identity.get("pid")
                        if isinstance(candidate_pid, int) and not isinstance(candidate_pid, bool):
                            worker_identity_pid = candidate_pid
                    expected_worker_pid = worker_identity_pid or worker.pid
                    provenance_ok = (
                        artifact.get("operation") == expected_operation
                        and artifact.get("response_id") == expected_response_id
                        and artifact.get("worker_pid") == expected_worker_pid
                    )
                    if not provenance_ok:
                        request["state"] = "failed"
                        request["error"] = "retained_result_artifact_provenance_mismatch"
                    else:
                        try:
                            self._validate_response_artifact(artifact)
                        except RuntimeBridgeError as exc:
                            request["state"] = "failed"
                            request["error"] = str(exc)
                        else:
                            request["response_artifact"] = artifact
                            request["response"] = None
                            request["response_summary"] = self._compact_async_response_summary(response)
                else:
                    request["response"] = response
                    request["response_summary"] = self._compact_async_response_summary(response)
            if error is not None:
                request["error"] = error

    def _start_async_job_locked(
        self,
        arguments: dict[str, Any],
        *,
        start_operation: str,
        work_operation: str,
        job_prefix: str,
    ) -> dict[str, Any]:
        arguments = dict(arguments)
        if work_operation == ASYNC_SETUP_WORK_OPERATION and any(str(key).startswith("__") for key in arguments):
            return {"ok": False, "error": "match_setup_context_is_supervisor_owned"}
        controller_policy = None
        if work_operation == ASYNC_ROUTE_WORK_OPERATION and arguments.get("dry_run") is False:
            try:
                require_proven_live_controller_policy(arguments)
                controller_policy = proven_live_controller_policy_evidence(arguments)
            except LiveControllerPolicyError as exc:
                # Reject before worker creation/write.  This is the live
                # controller configuration boundary, not a worker preflight
                # failure, so no topology or game work has started.
                return {
                    "ok": False,
                    "error": exc.code,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "operation": start_operation,
                    "controller_policy": None,
                    "missing_or_invalid_fields": list(exc.fields),
                }
        if work_operation == ASYNC_CAPTURE_WORK_OPERATION:
            arguments.setdefault("capture_request_id", str(uuid.uuid4()))
        with self._async_result_lock:
            current = self._async_request
            if current is not None and current.get("state") == "running":
                return self._async_route_rejection_locked(
                    start_operation,
                    dict(current),
                )
        self._ensure_live_runtime_ready_locked(work_operation)
        if self._bridge_process is not None:
            return self._bridge_process.call(start_operation, arguments)
        self._ensure_worker_locked()
        if work_operation == ASYNC_SETUP_WORK_OPERATION:
            bootstrap = self._bootstrap_verification_locked()
            if bootstrap.get("ok") is not True:
                return {"ok": False, "error": "match_setup_bootstrap_failed", "bootstrap": bootstrap}
            arguments["__setup_context"] = {"identity": self.identity_locked(), "bootstrap": bootstrap,
                                            "status": self.status_locked()}
        worker = self._worker
        assert worker is not None and worker.stdin is not None
        self._request_id += 1
        request_id = self._request_id
        job_id = f"{job_prefix}-{request_id}"
        message = {
            "id": request_id,
            "operation": work_operation,
            "arguments": dict(arguments),
        }
        status = self.status_locked()
        with self._async_result_lock:
            self._async_request = {
                "job_id": job_id,
                "state": "running",
                "operation": work_operation,
                "started_at": time.time(),
                "worker_generation": status.get("generation"),
                "worker_pid": worker.pid,
            }
            if controller_policy is not None:
                self._async_request["controller_policy"] = dict(controller_policy)
            if work_operation == ASYNC_CAPTURE_WORK_OPERATION:
                self._async_request["capture_status_path"] = arguments.get("status_path")
                self._async_request["capture_request_id"] = arguments.get("capture_request_id")
        try:
            worker.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            worker.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            with self._async_result_lock:
                if self._async_request is not None and self._async_request.get("job_id") == job_id:
                    self._async_request["state"] = "failed"
                    self._async_request["error"] = str(exc)
                raise RuntimeBridgeError(
                    "runtime worker transport failed; asynchronous job was not started"
                ) from exc
        threading.Thread(
            target=self._finish_async_request,
            args=(job_id, worker, request_id),
            name="ff7r-async-response",
            daemon=True,
        ).start()
        return self._async_status_locked(job_id)

    def _start_async_route_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_ROUTE_START_OPERATION,
            work_operation=ASYNC_ROUTE_WORK_OPERATION,
            job_prefix="route",
        )

    def _start_async_capture_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_CAPTURE_START_OPERATION,
            work_operation=ASYNC_CAPTURE_WORK_OPERATION,
            job_prefix="capture",
        )

    def _start_async_startup_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_STARTUP_START_OPERATION,
            work_operation=ASYNC_STARTUP_WORK_OPERATION,
            job_prefix="startup",
        )

    def _start_async_queens_blood_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_QUEENS_BLOOD_START_OPERATION,
            work_operation=ASYNC_QUEENS_BLOOD_WORK_OPERATION,
            job_prefix="queens-blood",
        )

    def _start_async_conversation_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_CONVERSATION_START_OPERATION,
            work_operation=ASYNC_CONVERSATION_WORK_OPERATION,
            job_prefix="conversation",
        )

    def _start_async_match_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_MATCH_START_OPERATION,
            work_operation=ASYNC_MATCH_WORK_OPERATION,
            job_prefix="match",
        )

    def _start_async_deck_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_DECK_START_OPERATION,
            work_operation=ASYNC_DECK_WORK_OPERATION,
            job_prefix="deck",
        )

    def _start_async_travel_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_TRAVEL_START_OPERATION,
            work_operation=ASYNC_TRAVEL_WORK_OPERATION,
            job_prefix="travel",
        )

    def _start_async_simulator_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_SIMULATOR_START_OPERATION,
            work_operation=ASYNC_SIMULATOR_WORK_OPERATION,
            job_prefix="simulator",
        )

    def _start_async_load_slot_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_LOAD_SLOT_START_OPERATION,
            work_operation=ASYNC_LOAD_SLOT_WORK_OPERATION,
            job_prefix="load-slot",
        )

    def _start_async_slot_startup_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_SLOT_STARTUP_START_OPERATION,
            work_operation=ASYNC_SLOT_STARTUP_WORK_OPERATION,
            job_prefix="slot-startup",
        )

    def _start_async_new_game_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_NEW_GAME_START_OPERATION,
            work_operation=ASYNC_NEW_GAME_WORK_OPERATION,
            job_prefix="new-game",
        )

    def _start_async_scan_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_SCAN_START_OPERATION,
            work_operation=ASYNC_SCAN_WORK_OPERATION,
            job_prefix="scan",
        )

    def _start_async_mulligan_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_MULLIGAN_START_OPERATION,
            work_operation=ASYNC_MULLIGAN_WORK_OPERATION,
            job_prefix="mulligan",
        )

    def _start_async_shop_buy_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_SHOP_BUY_START_OPERATION,
            work_operation=ASYNC_SHOP_BUY_WORK_OPERATION,
            job_prefix="shop-buy",
        )

    def _start_async_field_menu_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_FIELD_MENU_START_OPERATION,
            work_operation=ASYNC_FIELD_MENU_WORK_OPERATION,
            job_prefix="field-menu",
        )

    def _start_async_close_locked(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._start_async_job_locked(
            arguments,
            start_operation=ASYNC_CLOSE_START_OPERATION,
            work_operation=ASYNC_CLOSE_WORK_OPERATION,
            job_prefix="close",
        )

    def _request_locked(self, operation: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_live_runtime_ready_locked(operation)
        if self._bridge_process is not None:
            return self._bridge_process.call(operation, arguments)
        active = self._async_route_request_locked()
        if active is not None:
            raise RuntimeBridgeError(
                self._async_route_error(
                    operation,
                    str(active.get("job_id")),
                    str(active.get("operation") or ASYNC_ROUTE_WORK_OPERATION),
                )
            )
        self._ensure_worker_locked()
        worker = self._worker
        assert worker is not None and worker.stdin is not None and worker.stdout is not None
        self._request_id += 1
        request_id = self._request_id
        message = {
            "id": request_id,
            "operation": operation,
            "arguments": dict(arguments or {}),
        }
        try:
            worker.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            worker.stdin.flush()
            line = self._readline_with_timeout(
                worker.stdout,
                self._request_timeout_seconds,
                "runtime worker request timed out",
            )
        except RuntimeBridgeError as exc:
            if str(exc) == "runtime worker request timed out":
                self._terminate_worker_locked()
                raise RuntimeBridgeError(
                    "runtime worker request timed out; worker terminated; "
                    "request was not retried"
                ) from exc
            raise
        except (BrokenPipeError, OSError) as exc:
            # Never retry a game-affecting request automatically: the worker
            # may have executed input before its reply channel failed.
            raise RuntimeBridgeError("runtime worker transport failed; request was not retried") from exc
        if not line:
            raise RuntimeBridgeError(
                "runtime worker exited before replying; request was not retried to avoid duplicate input"
            )
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeBridgeError("runtime worker returned invalid JSON; request was not retried") from exc
        if response.get("id") != request_id:
            raise RuntimeBridgeError("runtime worker response id mismatch")
        return response

    def _prepare_queens_blood_deck_locked(self, arguments: dict[str, Any], *,
                                        context_only: bool = False) -> dict[str, Any]:
        # Validate this supervisor's code, without starting/rebinding an input
        # worker. Source changes during an active job must fail closed.
        contract = profile_contract_from_environment()
        operation = "get_queens_blood_preparation_context" if context_only else "prepare_queens_blood_deck"
        if contract is not None:
            current = _cached_checkout_validation(contract)
            mismatches = self._source_binding_mismatches(self._bridge_source_identity, current, contract)
            if mismatches:
                self._raise_readiness_error(operation=operation, phase="checkout",
                    message="deck preparation supervisor source binding is stale; no worker was started",
                    diagnostics={"source_binding_mismatches": mismatches})
        from .queen_blood_preparation import prepare_runtime_snapshot
        from .safety import WindowsGameEnvironment, default_runtime_dir
        root = Path(os.getenv("FF7R_QUEEN_BLOOD_PROFILE_ROOT") or default_runtime_dir() / "queen-blood")
        events = os.getenv("FF7R_PUBLIC_STREAM_EVENT_DIR")
        try:
            candidate = prepare_runtime_snapshot(root=root, state_dir=Path(events).parent if events else None,
                environment=WindowsGameEnvironment(), arguments=arguments, context_only=context_only)
            return {"ok": True, "result": candidate}
        except (ValueError, OSError, TypeError) as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__,
                    "operation": operation, "input_sent": False}

    def call(self, operation: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        # Keep control-plane operations on the stable bridge. In particular,
        if operation == "talk_to_npc_async":
            operation = ASYNC_CONVERSATION_START_OPERATION
            arguments = {"fast_forward_dialogue": True, **dict(arguments or {}), "_start_talk": True}
        elif operation == "get_talk_to_npc_status":
            operation = ASYNC_CONVERSATION_STATUS_OPERATION
        if operation == "resolve_save_calibration_async":
            values = dict(arguments or {})
            execute = values.pop("execute", False)
            operation = ASYNC_SAVE_START_OPERATION
            arguments = {"execute": execute, "resolve_calibration": values}
        elif operation == "get_resolve_save_calibration_status":
            operation = ASYNC_SAVE_STATUS_OPERATION
        if operation == "start_model_run_async":
            if any(key in (arguments or {}) for key in ("slot", "execute", "skip_if_latest", "model_run")):
                raise RuntimeBridgeError("model-run startup uses only its registered slot")
            operation = ASYNC_SLOT_STARTUP_START_OPERATION
            arguments = {**(arguments or {}), "model_run": True}
        elif operation == "get_start_model_run_status":
            operation = ASYNC_SLOT_STARTUP_STATUS_OPERATION
        # do not let identity/status requests take a speculative worker trip
        # through runtime_commands before reaching their actual owner.
        if operation in BRIDGE_OPERATIONS:
            if operation == "get_agent_runtime_status":
                return self.status()
            if operation == "get_runtime_identity":
                return self.identity()
            if operation == "get_bootstrap_verification":
                return self.bootstrap_verification()
            if operation == "restart_agent_runtime":
                return self.restart()
        with self._lock:
            if self._closing or self._closed:
                raise RuntimeBridgeError("runtime supervisor is closed")
            if operation == "say_on_stream":
                # Voice is independent of the input worker, even during a
                # route/load/menu job or a source rebind. Never send another
                # worker request, start a broker or interrupt the running job.
                return {"ok": True, "result": self._speech.say(**dict(arguments or {}))}
            if self._bridge_process is not None:
                if operation in QUEEN_BLOOD_PREPARATION_OPERATIONS:
                    # The child owns any active job and validates its own code.
                    # Never rebind/start workers just to read a deck snapshot.
                    return self._bridge_process.call(operation, arguments)
                self._ensure_live_runtime_ready_locked(operation)
                if self._bridge_process is not None:
                    return self._bridge_process.call(operation, arguments)
            if operation in ASYNC_STATUS_OPERATIONS:
                status_arguments = arguments or {}
                include_response = status_arguments.get("include_response", True)
                if type(include_response) is not bool:
                    raise RuntimeBridgeError("include_response must be a boolean")
                return self._async_status_locked(
                    status_arguments.get("job_id"),
                    status_operation=operation,
                    include_response=include_response,
                )
            if operation == "stop_all_input":
                with self._async_result_lock:
                    active = self._async_request is not None and self._async_request.get("state") == "running"
                    job_id = self._async_request.get("job_id") if active else None
                if active:
                    # The route worker may be inside a long native capture and
                    # cannot consume a second stdin request. Terminating that
                    # exact worker invokes the broker neutralisation boundary;
                    # a fresh worker then records the explicit stop operation.
                    self._terminate_worker_locked()
                    result = self._request_locked("stop_all_input", {})
                    if isinstance(result.get("result"), dict):
                        cancelled_key = (
                            "async_capture_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_CAPTURE_WORK_OPERATION
                            else "async_startup_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_STARTUP_WORK_OPERATION
                            else "async_queens_blood_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_QUEENS_BLOOD_WORK_OPERATION
                            else "async_conversation_recovery_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_CONVERSATION_WORK_OPERATION
                            else "async_close_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_CLOSE_WORK_OPERATION
                            else "async_mulligan_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_MULLIGAN_WORK_OPERATION
                            else "async_shop_buy_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_SHOP_BUY_WORK_OPERATION
                            else "async_field_menu_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_FIELD_MENU_WORK_OPERATION
                            else "async_match_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_MATCH_WORK_OPERATION
                            else "async_scan_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_SCAN_WORK_OPERATION
                            else "async_match_setup_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_SETUP_WORK_OPERATION
                            else "async_deck_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_DECK_WORK_OPERATION
                            else "async_travel_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_TRAVEL_WORK_OPERATION
                            else "async_simulator_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_SIMULATOR_WORK_OPERATION
                            else "async_load_slot_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_LOAD_SLOT_WORK_OPERATION
                            else "async_slot_startup_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_SLOT_STARTUP_WORK_OPERATION
                            else "async_new_game_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_NEW_GAME_WORK_OPERATION
                            else "async_save_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_SAVE_WORK_OPERATION
                            else "async_recovery_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_RECOVERY_WORK_OPERATION
                            else "async_explore_cancelled"
                            if self._async_request is not None
                            and self._async_request.get("operation") == ASYNC_EXPLORE_WORK_OPERATION
                            else "async_route_cancelled"
                        )
                        result["result"].update({
                            cancelled_key: True,
                            "cancelled_job_id": job_id,
                        })
                    return result
            active_request = self._async_route_request_locked()
            if operation in QUEEN_BLOOD_PREPARATION_OPERATIONS:
                if active_request is not None and active_request.get("operation") not in {
                        ASYNC_ROUTE_WORK_OPERATION, ASYNC_TRAVEL_WORK_OPERATION, ASYNC_QUEENS_BLOOD_WORK_OPERATION}:
                    return self._async_route_rejection_locked(operation, active_request)
                return self._prepare_queens_blood_deck_locked(dict(arguments or {}),
                    context_only=operation == "get_queens_blood_preparation_context")
            if active_request is not None:
                return self._async_route_rejection_locked(operation, active_request)
            if operation == ASYNC_STARTUP_WORK_OPERATION:
                raise RuntimeBridgeError(
                    "internal asynchronous startup operation must be started through "
                    f"{ASYNC_STARTUP_START_OPERATION!r}"
                )
            if operation == ASYNC_QUEENS_BLOOD_WORK_OPERATION:
                raise RuntimeBridgeError(
                    "internal asynchronous Queen's Blood operation must be started through "
                    f"{ASYNC_QUEENS_BLOOD_START_OPERATION!r}"
                )
            if operation == ASYNC_CONVERSATION_WORK_OPERATION:
                raise RuntimeBridgeError(
                    "internal asynchronous conversation recovery must be started through "
                    f"{ASYNC_CONVERSATION_START_OPERATION!r}"
                )
            if operation == ASYNC_ROUTE_START_OPERATION:
                return self._start_async_route_locked(dict(arguments or {}))
            if operation == ASYNC_CAPTURE_START_OPERATION:
                return self._start_async_capture_locked(dict(arguments or {}))
            if operation == ASYNC_STARTUP_START_OPERATION:
                return self._start_async_startup_locked(dict(arguments or {}))
            if operation == ASYNC_QUEENS_BLOOD_START_OPERATION:
                return self._start_async_queens_blood_locked(dict(arguments or {}))
            if operation == ASYNC_CONVERSATION_START_OPERATION:
                return self._start_async_conversation_locked(dict(arguments or {}))
            if operation == ASYNC_CLOSE_START_OPERATION:
                return self._start_async_close_locked(dict(arguments or {}))
            if operation == ASYNC_MULLIGAN_START_OPERATION:
                return self._start_async_mulligan_locked(dict(arguments or {}))
            if operation == ASYNC_SHOP_BUY_START_OPERATION:
                return self._start_async_shop_buy_locked(dict(arguments or {}))
            if operation == ASYNC_FIELD_MENU_START_OPERATION:
                return self._start_async_field_menu_locked(dict(arguments or {}))
            if operation == ASYNC_MATCH_START_OPERATION:
                return self._start_async_match_locked(dict(arguments or {}))
            if operation == ASYNC_SCAN_START_OPERATION:
                return self._start_async_scan_locked(dict(arguments or {}))
            if operation == ASYNC_SETUP_START_OPERATION:
                return self._start_async_job_locked(dict(arguments or {}), start_operation=operation,
                    work_operation=ASYNC_SETUP_WORK_OPERATION, job_prefix="match-setup")
            if operation == ASYNC_SETUP_WORK_OPERATION:
                return {"ok": False, "error": "match_setup_requires_async",
                        "use_operation": ASYNC_SETUP_START_OPERATION, "status_operation": ASYNC_SETUP_STATUS_OPERATION}
            if operation == ASYNC_DECK_START_OPERATION:
                return self._start_async_deck_locked(dict(arguments or {}))
            if operation == ASYNC_TRAVEL_START_OPERATION:
                return self._start_async_travel_locked(dict(arguments or {}))
            if operation == ASYNC_SIMULATOR_START_OPERATION:
                return self._start_async_simulator_locked(dict(arguments or {}))
            if operation == ASYNC_LOAD_SLOT_START_OPERATION:
                return self._start_async_load_slot_locked(dict(arguments or {}))
            if operation == ASYNC_SLOT_STARTUP_START_OPERATION:
                return self._start_async_slot_startup_locked(dict(arguments or {}))
            if operation == ASYNC_NEW_GAME_START_OPERATION:
                return self._start_async_new_game_locked(dict(arguments or {}))
            if operation in {ASYNC_SAVE_START_OPERATION, ASYNC_RECOVERY_START_OPERATION}:
                saving = operation == ASYNC_SAVE_START_OPERATION
                return self._start_async_job_locked(
                    dict(arguments or {}), start_operation=operation,
                    work_operation=ASYNC_SAVE_WORK_OPERATION if saving else ASYNC_RECOVERY_WORK_OPERATION,
                    job_prefix="save" if saving else "recovery")
            if operation == ASYNC_EXPLORE_START_OPERATION:
                return self._start_async_job_locked(
                    dict(arguments or {}), start_operation=operation,
                    work_operation=ASYNC_EXPLORE_WORK_OPERATION, job_prefix="explore")
            if operation == ASYNC_EXPLORE_WORK_OPERATION:
                return {"ok": False, "error": "explore_goto_requires_async",
                        "use_operation": ASYNC_EXPLORE_START_OPERATION,
                        "status_operation": ASYNC_EXPLORE_STATUS_OPERATION}
            if operation in {ASYNC_SAVE_WORK_OPERATION, ASYNC_RECOVERY_WORK_OPERATION}:
                saving = operation == ASYNC_SAVE_WORK_OPERATION
                return {"ok": False, "error": operation + "_requires_async",
                        "use_operation": ASYNC_SAVE_START_OPERATION if saving else ASYNC_RECOVERY_START_OPERATION,
                        "status_operation": ASYNC_SAVE_STATUS_OPERATION if saving else ASYNC_RECOVERY_STATUS_OPERATION}
            if operation == ASYNC_LOAD_SLOT_WORK_OPERATION:
                return {
                    "ok": False,
                    "error": "load_save_slot_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_LOAD_SLOT_START_OPERATION,
                    "status_operation": ASYNC_LOAD_SLOT_STATUS_OPERATION,
                    "message": (
                        "loading a save slot walks the main menu and waits for the "
                        "load; start it asynchronously and poll status"
                    ),
                }
            if operation in {ASYNC_NEW_GAME_WORK_OPERATION, "start_new_game"}:
                return {
                    "ok": False,
                    "error": "start_new_game_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_NEW_GAME_START_OPERATION,
                    "status_operation": ASYNC_NEW_GAME_STATUS_OPERATION,
                    "message": (
                        "a new game walks the setup screens and the opening "
                        "cinemas; start it asynchronously and poll status"
                    ),
                }
            if operation in {ASYNC_SLOT_STARTUP_WORK_OPERATION, "start_and_load_save_slot"}:
                return {
                    "ok": False,
                    "error": "start_and_load_save_slot_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_SLOT_STARTUP_START_OPERATION,
                    "status_operation": ASYNC_SLOT_STARTUP_STATUS_OPERATION,
                    "message": (
                        "startup followed by a save slot load takes minutes; start it "
                        "asynchronously and poll status"
                    ),
                }
            if operation == ASYNC_TRAVEL_WORK_OPERATION:
                return {
                    "ok": False,
                    "error": "travel_to_map_spot_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_TRAVEL_START_OPERATION,
                    "status_operation": ASYNC_TRAVEL_STATUS_OPERATION,
                    "message": "fast travel cycles the map and waits for the load; start it asynchronously and poll status",
                }
            if operation == ASYNC_SIMULATOR_WORK_OPERATION and (arguments or {}).get("execute"):
                return {
                    "ok": False,
                    "error": "enter_combat_simulator_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_SIMULATOR_START_OPERATION,
                    "status_operation": ASYNC_SIMULATOR_STATUS_OPERATION,
                    "message": ("entering the simulator waits out Chadley's greeting and its scenes; "
                                "start it asynchronously and poll status"),
                }
            if operation == ASYNC_DECK_WORK_OPERATION:
                return {
                    "ok": False,
                    "error": "edit_queens_blood_deck_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_DECK_START_OPERATION,
                    "status_operation": ASYNC_DECK_STATUS_OPERATION,
                    "message": "a deck edit reads the deck and walks the editor; start it asynchronously and poll status",
                }
            if operation == ASYNC_SCAN_WORK_OPERATION:
                return {
                    "ok": False,
                    "error": "scan_queens_blood_collection_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_SCAN_START_OPERATION,
                    "status_operation": ASYNC_SCAN_STATUS_OPERATION,
                    "message": "the collection scan reads one card per cursor step; start it asynchronously and poll status",
                }
            if operation in {ASYNC_MATCH_WORK_OPERATION, "play_queens_blood"}:
                return {
                    "ok": False,
                    "error": "play_queens_blood_match_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_MATCH_START_OPERATION,
                    "status_operation": ASYNC_MATCH_STATUS_OPERATION,
                    "message": (
                        "a Queen's Blood match runs for minutes; start it "
                        "asynchronously and poll status"
                    ),
                }
            if operation in (ASYNC_FIELD_MENU_WORK_OPERATION, "cast_field_spell", "use_field_item"):
                # Live 2026-10-21 an Ether outran the request deadline and the
                # worker was killed with the menu open (the Ether was used).
                return {
                    "ok": False,
                    "error": "field_menu_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "message": "cast spells and use items with field_menu_async and poll get_field_menu_status",
                    "use_operation": ASYNC_FIELD_MENU_START_OPERATION,
                    "status_operation": ASYNC_FIELD_MENU_STATUS_OPERATION,
                }
            if operation == ASYNC_SHOP_BUY_WORK_OPERATION and (arguments or {}).get("execute"):
                # Live 2026-09-30 a purchase outran the request deadline and
                # the worker was killed mid-purchase (the pack was bought).
                return {
                    "ok": False,
                    "error": "buy_shop_item_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_SHOP_BUY_START_OPERATION,
                    "status_operation": ASYNC_SHOP_BUY_STATUS_OPERATION,
                    "message": (
                        "a purchase reads the shop around every press and can exceed the "
                        "ordinary runtime request deadline; start it asynchronously and poll status"
                    ),
                }
            if operation == ASYNC_MULLIGAN_WORK_OPERATION:
                return {
                    "ok": False,
                    "error": "mulligan_cards_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_MULLIGAN_START_OPERATION,
                    "status_operation": ASYNC_MULLIGAN_STATUS_OPERATION,
                    "message": (
                        "marking cards waits for a settled cursor and fresh marker "
                        "telemetry per card and can exceed the ordinary runtime "
                        "request deadline; start it asynchronously and poll status"
                    ),
                }
            if operation == ASYNC_CLOSE_WORK_OPERATION:
                return {
                    "ok": False,
                    "error": "close_game_requires_async",
                    "error_type": "RuntimeBridgeError",
                    "operation": operation,
                    "use_operation": ASYNC_CLOSE_START_OPERATION,
                    "status_operation": ASYNC_CLOSE_STATUS_OPERATION,
                    "message": (
                        "managed game close can legitimately exceed the ordinary "
                        "runtime request deadline; start it asynchronously and poll status"
                    ),
                }
            return self._request_locked(operation, arguments)

    def _shutdown_worker_locked(self) -> None:
        worker = self._worker
        if worker is None:
            return
        with self._async_result_lock:
            async_running = (
                self._async_request is not None
                and self._async_request.get("state") == "running"
            )
        if async_running:
            self._terminate_worker_locked()
            return
        if worker.poll() is None:
            try:
                self._request_locked("__shutdown__", {})
            except Exception:
                pass
            try:
                worker.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                worker.terminate()
                try:
                    worker.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    worker.kill()
        self._worker = None
        self._worker_identity = None

    def _restart_locked(self) -> dict[str, Any]:
        """Restart the worker while the supervisor lock is already held."""
        if self._closing or self._closed:
            raise RuntimeBridgeError("runtime supervisor is closed")
        active = self._async_route_request_locked()
        if active is not None:
            self._terminate_worker_locked()
            label = {
                ASYNC_CAPTURE_WORK_OPERATION: "topology capture",
                ASYNC_STARTUP_WORK_OPERATION: "startup",
                ASYNC_CLOSE_WORK_OPERATION: "game close",
                ASYNC_MULLIGAN_WORK_OPERATION: "Queen's Blood mulligan",
                ASYNC_SHOP_BUY_WORK_OPERATION: "shop purchase",
                ASYNC_FIELD_MENU_WORK_OPERATION: "field menu",
                ASYNC_MATCH_WORK_OPERATION: "Queen's Blood match",
                ASYNC_SCAN_WORK_OPERATION: "Queen's Blood collection scan",
                ASYNC_DECK_WORK_OPERATION: "Queen's Blood deck edit",
                ASYNC_TRAVEL_WORK_OPERATION: "map fast travel",
                ASYNC_SIMULATOR_WORK_OPERATION: "combat simulator entry",
                ASYNC_LOAD_SLOT_WORK_OPERATION: "save slot load",
                ASYNC_SLOT_STARTUP_WORK_OPERATION: "startup with save slot load",
                ASYNC_NEW_GAME_WORK_OPERATION: "new game startup",
                ASYNC_SAVE_WORK_OPERATION: "game save",
                ASYNC_RECOVERY_WORK_OPERATION: "working save recovery",
                ASYNC_EXPLORE_WORK_OPERATION: "explore walk",
            }.get(active.get("operation"), "route")
            prefix = {
                ASYNC_CAPTURE_WORK_OPERATION: "async_topology_capture_in_progress",
                ASYNC_STARTUP_WORK_OPERATION: "async_startup_in_progress",
                ASYNC_CLOSE_WORK_OPERATION: "async_close_in_progress",
                ASYNC_MULLIGAN_WORK_OPERATION: "async_mulligan_in_progress",
                ASYNC_SHOP_BUY_WORK_OPERATION: "async_shop_buy_in_progress",
                ASYNC_FIELD_MENU_WORK_OPERATION: "async_field_menu_in_progress",
                ASYNC_MATCH_WORK_OPERATION: "async_match_in_progress",
                ASYNC_SCAN_WORK_OPERATION: "async_scan_in_progress",
                ASYNC_DECK_WORK_OPERATION: "async_deck_in_progress",
                ASYNC_SETUP_WORK_OPERATION: "async_match_setup_in_progress",
                ASYNC_TRAVEL_WORK_OPERATION: "async_travel_in_progress",
                ASYNC_SIMULATOR_WORK_OPERATION: "async_simulator_in_progress",
                ASYNC_LOAD_SLOT_WORK_OPERATION: "async_load_slot_in_progress",
                ASYNC_SLOT_STARTUP_WORK_OPERATION: "async_slot_startup_in_progress",
                ASYNC_NEW_GAME_WORK_OPERATION: "async_new_game_in_progress",
                ASYNC_SAVE_WORK_OPERATION: "async_save_in_progress",
                ASYNC_RECOVERY_WORK_OPERATION: "async_recovery_in_progress",
                ASYNC_EXPLORE_WORK_OPERATION: "async_explore_in_progress",
            }.get(active.get("operation"), "async_route_in_progress")
            raise RuntimeBridgeError(
                prefix + ": restart refused; the active " + label + " "
                f"{active.get('job_id')!r} was cancelled and input was neutralized; "
                "call stop_all_input to establish a clean worker"
            )
        self._shutdown_worker_locked()
        self._last_restart_at = time.time()
        return self._start_worker_locked()

    def restart(self) -> dict[str, Any]:
        """Restart policy/service code while retaining the stable MCP/broker."""
        with self._lock:
            if self._bridge_process is not None:
                return self._bridge_process.call("restart_agent_runtime", {})
            return self._restart_locked()

    def status_locked(self) -> dict[str, Any]:
        worker = self._worker
        broker_status: dict[str, Any] | None = None
        if self._broker is not None:
            try:
                broker_status = {
                    **self._broker.status(),
                    "pid": self._broker.process.pid,
                    "port": self._broker.port,
                }
            except Exception as exc:
                broker_status = {
                    "ok": False,
                    "error": str(exc),
                    "pid": self._broker.process.pid,
                    "port": self._broker.port,
                    "process_alive": self._broker.process.poll() is None,
                    "listener_alive": False,
                    "device_active": False,
                    "active": False,
                }
        return {
            "ok": True,
            "generation": self._generation,
            "worker_pid": worker.pid if worker is not None and worker.poll() is None else None,
            "worker_running": bool(worker is not None and worker.poll() is None),
            "started_at": self._started_at,
            "last_restart_at": self._last_restart_at,
            "last_startup_duration_seconds": self._last_startup_duration_seconds,
            "gamepad_broker": broker_status,
            "source_binding": self._source_binding_summary(self._bridge_source_identity),
            "active_async_request": self._async_route_request_locked(),
        }

    def _compose_delegated_identity_locked(self, active: dict[str, Any]) -> dict[str, Any]:
        identity = dict(self._bridge_source_identity)
        child_bridge_identity = active.get("bridge_identity")
        identity.update({
            "process_role": "mcp-bridge",
            "pid": os.getpid(),
            "bridge_identity": _bridge_identity(),
            # This is the transport process's loaded binding. It is kept at
            # the revision captured at construction and is never rewritten
            # from current on-disk Git metadata.
            "bridge_source_binding": self._source_binding_summary(
                self._bridge_source_identity
            ),
            "active_runtime_identity": dict(active),
            "active_bridge_source_binding": self._source_binding_summary(active),
            "active_bridge_process_identity": {
                "pid": active.get("pid"),
                "process_creation_token": (
                    child_bridge_identity.get("process_creation_token")
                    if isinstance(child_bridge_identity, dict)
                    else None
                ),
            },
            "worker_identity": active.get("worker_identity"),
            "worker_pid": active.get("worker_pid"),
            "gamepad_broker_identity": active.get("gamepad_broker_identity"),
            "bridge_status": active.get("bridge_status"),
            "runtime_profile_verification": active.get(
                "runtime_profile_verification"
            ),
            "transport_profile_verification": verify_runtime_identity(
                identity,
                contract=profile_contract_from_environment(),
            ),
        })
        return identity
    def identity_locked(self) -> dict[str, Any]:
        status = self.status_locked()
        if self._worker_identity is not None:
            # The worker captured this identity from its actual imported
            # modules during startup. Reuse it here instead of repeating Git
            # and module discovery on every stable-bridge request. Apart from
            # being cheaper, this keeps the identity surface bounded even when
            # a Git/OneDrive operation is temporarily slow.
            identity = dict(self._worker_identity)
            module_paths = dict(identity.get("module_paths") or {})
            bridge_module_path = str(Path(__file__).resolve())
            module_paths["ff7r_agent.runtime_bridge"] = bridge_module_path
            module_paths.setdefault(
                "ff7r_agent.mcp_http_server",
                str((Path(__file__).resolve().parent / "mcp_http_server.py")),
            )
            module_paths.setdefault(
                "ff7r_agent.mcp_runtime_server",
                str((Path(__file__).resolve().parent / "mcp_runtime_server.py")),
            )
            identity.update(
                {
                    "process_role": "mcp-bridge",
                    "pid": os.getpid(),
                    "loaded_module_path": bridge_module_path,
                    "module_paths": module_paths,
                    "worker_generation": status.get("generation"),
                    "worker_pid": status.get("worker_pid"),
                    "bridge_identity": _bridge_identity(),
                    "bridge_source_binding": self._source_binding_summary(self._bridge_source_identity),
                    "gamepad_broker_identity": status.get("gamepad_broker"),
                    "worker_identity": dict(self._worker_identity),
                    "bridge_status": status,
                }
            )
            identity["runtime_profile_verification"] = verify_runtime_identity(
                identity,
                contract=profile_contract_from_environment(),
                worker_identity=self._worker_identity,
            )
            return identity
        identity = collect_identity(
            process_role="mcp-bridge",
            module_path=__file__,
            worker_generation=status.get("generation"),
            worker_pid=status.get("worker_pid"),
            bridge_identity=_bridge_identity(),
            broker_identity=status.get("gamepad_broker"),
        )
        # collect_identity reads disk, so anchor source fields back to the
        # binding captured when this transport process loaded its modules.
        for key in (
            "deployment_profile",
            "repository_root",
            "source_root",
            "package_import_root",
            "expected_feature_ref",
            "expected_feature_commit",
            "expected_branch",
            "git_branch",
            "git_commit",
            "git_commit_sha",
            "source_commit_sha",
            "git_dirty",
            "git_dirty_paths",
        ):
            if key in self._bridge_source_identity:
                identity[key] = self._bridge_source_identity[key]
        identity["worker_identity"] = self._worker_identity
        identity["bridge_status"] = status
        identity["bridge_source_binding"] = self._source_binding_summary(self._bridge_source_identity)
        identity["runtime_profile_verification"] = verify_runtime_identity(
            identity,
            contract=profile_contract_from_environment(),
            worker_identity=self._worker_identity,
        )
        return identity

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._closing or self._closed:
                return {"ok": False, "worker_running": False, "error": "runtime supervisor is closed"}
            if self._bridge_process is not None:
                try:
                    status = self._bridge_process.call("get_agent_runtime_status", {})
                except Exception as exc:
                    return {"ok": False, "worker_running": False, "error": str(exc)}
                status["transport_source_binding"] = self._source_binding_summary(self._bridge_source_identity)
                status["active_source_binding"] = self._source_binding_summary(self._active_bridge_identity)
                return status
            if self._worker is None or self._worker.poll() is not None:
                try:
                    self._start_worker_locked()
                except Exception as exc:
                    return {"ok": False, "worker_running": False, "error": str(exc)}
            return self.status_locked()

    def identity(self) -> dict[str, Any]:
        with self._lock:
            if self._closing or self._closed:
                return {"ok": False, "worker_running": False, "error": "runtime supervisor is closed"}
            if self._bridge_process is not None:
                try:
                    active = self._bridge_process.call("get_runtime_identity", {})
                except Exception as exc:
                    return {"ok": False, "worker_running": False, "error": str(exc)}
                return self._compose_delegated_identity_locked(active)
            if self._worker is None or self._worker.poll() is not None:
                try:
                    self._start_worker_locked()
                except Exception as exc:
                    return {
                        "ok": False,
                        "worker_running": False,
                        "error": str(exc),
                    }
            return self.identity_locked()

    def bootstrap_verification(self) -> dict[str, Any]:
        """Run the read-only task bootstrap checks through one stable call."""
        with self._lock:
            return self._bootstrap_verification_locked()

    def _bootstrap_verification_locked(self) -> dict[str, Any]:
        if self._closing or self._closed:
            raise RuntimeBridgeError("runtime supervisor is closed")
        if self._bridge_process is not None:
            return self._bridge_process.call("get_bootstrap_verification", {})
        # Do not turn an already-running stale broker into a second broker
        # merely because bootstrap needed a worker.  The broker mutex is
        # authoritative; this read-only preflight supplies diagnostics.
        if (
            self._broker is None
            and os.getenv("FF7R_INPUT_BACKEND", "simulation").lower() == "windows_gamepad"
        ):
            ownership = collect_runtime_ownership_inventory()
            conflicts = ownership_conflicts(ownership, None)
            if conflicts:
                return {
                    "ok": False,
                    "error": "RUNTIME_OWNERSHIP_CONFLICT",
                    "runtime_ownership": {"inventory": ownership, "conflicts": conflicts},
                    "read_only": True,
                    "input_sent": False,
                    "lifecycle_mutated": False,
                }
        self._ensure_worker_locked()
        checks: dict[str, dict[str, Any]] = {}
        for operation in (
            "get_lifecycle_status",
            "get_session_health",
            "observe_game",
        ):
            checks[operation] = self._request_locked(operation, {})
        identity = self.identity_locked()
        ownership = collect_runtime_ownership_inventory()
        conflicts = ownership_conflicts(ownership, identity)
        return {
            "ok": all(result.get("ok") is True for result in checks.values()) and not conflicts,
            "runtime_identity": identity,
            "checks": checks,
            "runtime_ownership": {"inventory": ownership, "conflicts": conflicts, "ready_eligible": not conflicts},
            "read_only": True,
            "input_sent": False,
            "lifecycle_mutated": False,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            # Set this before waiting on the worker so requests arriving while
            # shutdown is in progress cannot start a new worker/broker after
            # the final neutralisation step.
            self._closing = True
            bridge_process = self._bridge_process
            self._bridge_process = None
            self._active_bridge_identity = None
            if bridge_process is not None:
                bridge_process.close()
            self._shutdown_worker_locked()
            if self._broker is not None:
                self._broker.close()
                self._broker = None
            self._closed = True
