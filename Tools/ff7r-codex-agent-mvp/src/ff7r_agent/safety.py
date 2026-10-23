from __future__ import annotations

import ctypes
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, local
from typing import Callable, Iterator

from .runtime_ownership import owner_is_alive, process_absence_verified, process_identity_token
from .recording_driver import configured_recording_driver, validate_recording_driver


MAX_ARM_SECONDS = 120
MAX_BENCH_ARM_SECONDS = 30
MAX_CAMERA_ARM_SECONDS = 120
# A control session lasts as long as the game process it was opened for (see
# SessionLease), not a wall-clock lease. Callers still pass ``seconds`` to
# open and heartbeat; it is accepted, ignored and echoed as requested_seconds.
MAX_SESSION_LEASE_SECONDS = 1800
LIFETIME_GAME = "game_process"
GAME_EXECUTABLES = frozenset({"ff7rebirth_.exe", "ff7rebirth.exe"})
MUTEX_NAME = "Local\\FF7R-Codex-Agent-Safety-State"
_fallback_lock = Lock()


class ArmError(RuntimeError):
    pass


def default_runtime_dir() -> Path:
    base = os.getenv("LOCALAPPDATA")
    if not base:
        raise ArmError("LOCALAPPDATA is unavailable")
    return Path(base) / "ff7r-agent"


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _process_exists(process_id: object) -> bool:
    try:
        pid = int(process_id)
        if pid <= 0:
            return False
        return owner_is_alive(pid, None)
    except (OSError, TypeError, ValueError):
        return False


def _advisory_seconds(seconds: object) -> int | None:
    try:
        return int(seconds)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def single_gameplay_process(processes: dict[int, str]) -> int | None:
    """The one gameplay process in an inventory, or None when absent or ambiguous.

    A session opened without an explicit ``game_pid`` binds to this, so it
    still ends with the game; the launcher process is never the game.
    """
    gameplay = [int(pid) for pid, name in processes.items() if str(name).lower() == "ff7rebirth_.exe"]
    return gameplay[0] if len(gameplay) == 1 else None


def describe_control(
    session: dict[str, object] | None,
    last_close: dict[str, object] | None,
    *,
    stopped: bool,
    stopped_at: float | None = None,
    error: str | None = None,
) -> dict[str, object]:
    """Say plainly whether the harness will take input, and if not why not.

    ``state`` is ``open`` while a session authorises input, otherwise why it
    does not: ``closed`` (someone closed or stopped it), ``game_exited``,
    ``not_opened``, ``other_owner`` or ``unreadable``.  The stop broker
    latches after every action; with a session open that is the routine
    one-shot arm and is not reported, so ``latched`` means a stop that no
    session can lift.  Every refusal carries the same ``reason`` and
    ``since`` so a caller sees it once, where it is reported, instead of in
    each action's error.
    """
    def iso(moment: object) -> str | None:
        try:
            return datetime.fromtimestamp(float(moment), timezone.utc).isoformat()  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    if session and session.get("active"):
        return {"state": "open", "session_open": True, "latched": False, "reason": None,
                "since": iso(session.get("opened_at")), "client_id": session.get("client_id"),
                "game_pid": session.get("game_pid")}
    state, reason, since = "not_opened", "no control session has been opened", None
    session_state = session.get("state") if session else None
    if error:
        state, reason = "unreadable", error
    elif session_state == "GAME_EXITED":
        state, reason = "game_exited", "the game process this session was opened for has exited"
    elif session_state in ("EXPIRED", "OWNER_MISMATCH", "STALE_WINDOWS_SESSION"):
        state = "other_owner" if session_state == "OWNER_MISMATCH" else "closed"
        reason = {"EXPIRED": "the session's wall-clock lease expired",
                  "OWNER_MISMATCH": "the control session belongs to another owner",
                  "STALE_WINDOWS_SESSION": "the control session is from an earlier Windows session"}[session_state]
    elif last_close:
        state, reason = "closed", str(last_close.get("reason") or "closed")
        since = iso(last_close.get("closed_at"))
    elif stopped:
        since = iso(stopped_at)
    return {"state": state, "session_open": False, "latched": bool(stopped), "reason": reason,
            "since": since, "hint": "open_control_session to resume input"}


def _windows_session_marker() -> str:
    if os.name != "nt":
        return f"nonwindows:{os.getpid()}"
    kernel32 = ctypes.windll.kernel32
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    boot_epoch_minute = int(round((time.time() - kernel32.GetTickCount64() / 1000.0) / 60.0))
    session_id = ctypes.c_ulong()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
        raise ArmError("cannot determine the current Windows session")
    return f"windows:{session_id.value}:boot-minute:{boot_epoch_minute}"


@contextmanager
def _state_mutex() -> Iterator[None]:
    if os.name != "nt":
        with _fallback_lock:
            yield
        return
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ArmError("cannot create the local safety-state mutex")
    wait_result = kernel32.WaitForSingleObject(handle, 5000)
    if wait_result not in (0, 0x80):
        kernel32.CloseHandle(handle)
        raise ArmError("timed out acquiring the local safety-state mutex")
    try:
        yield
    finally:
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


class ArmState:
    """Atomic local safety broker shared by reconnecting MCP processes."""

    def __init__(
        self,
        runtime_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
        process_id: int | None = None,
        session_marker: Callable[[], str] = _windows_session_marker,
    ) -> None:
        del process_id  # retained for compatibility with older tests/callers
        self.runtime_dir = runtime_dir or default_runtime_dir()
        self.clock = clock
        self.session_marker = session_marker
        self.state_path = self.runtime_dir / "safety-state.json"

    def _closed_state(self) -> dict[str, object]:
        return {
            "version": 1,
            "session_marker": self.session_marker(),
            "armed_until": 0.0,
            "emergency_stop": True,
            "updated": self.clock(),
        }

    def _read_locked(self) -> dict[str, object]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (
                int(value["version"]) != 1
                or value["session_marker"] != self.session_marker()
            ):
                raise ValueError("different Windows session")
            return {
                "version": 1,
                "session_marker": str(value["session_marker"]),
                "armed_until": float(value["armed_until"]),
                "emergency_stop": bool(value["emergency_stop"]),
                "updated": float(value["updated"]),
            }
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            value = self._closed_state()
            _atomic_json(self.state_path, value)
            return value

    def _write_locked(self, value: dict[str, object]) -> None:
        value["updated"] = self.clock()
        _atomic_json(self.state_path, value)

    def snapshot(self) -> tuple[bool, float, bool]:
        with _state_mutex():
            value = self._read_locked()
            now = self.clock()
            stopped = bool(value["emergency_stop"])
            remaining = max(0.0, float(value["armed_until"]) - now)
            armed = not stopped and remaining > 0.0
            if remaining <= 0.0 and float(value["armed_until"]) != 0.0:
                value["armed_until"] = 0.0
                self._write_locked(value)
            return armed, remaining if armed else 0.0, stopped

    def status(self) -> tuple[bool, float]:
        armed, remaining, _ = self.snapshot()
        return armed, remaining

    def stop_info(self) -> tuple[bool, float]:
        """Whether the stop is latched and when this broker last changed."""
        with _state_mutex():
            value = self._read_locked()
            return bool(value["emergency_stop"]), float(value["updated"])

    def arm(self, seconds: int) -> dict[str, object]:
        seconds = int(seconds)
        if not 1 <= seconds <= MAX_ARM_SECONDS:
            raise ArmError(
                f"arming duration must be between 1 and {MAX_ARM_SECONDS} seconds"
            )
        with _state_mutex():
            value = self._read_locked()
            if bool(value["emergency_stop"]):
                raise ArmError("emergency stop is latched; run reset-stop first")
            value["armed_until"] = self.clock() + seconds
            self._write_locked(value)
        return {"ok": True, "requested_seconds": seconds, "broker": "local-session"}

    def clear(self) -> None:
        with _state_mutex():
            value = self._read_locked()
            value["armed_until"] = 0.0
            self._write_locked(value)

    def _latch_stop_locked(self) -> None:
        value = self._read_locked()
        value["armed_until"] = 0.0
        value["emergency_stop"] = True
        self._write_locked(value)

    def latch_stop(self) -> None:
        with _state_mutex():
            self._latch_stop_locked()

    def reset_stop(self) -> None:
        with _state_mutex():
            value = self._read_locked()
            value["armed_until"] = 0.0
            value["emergency_stop"] = False
            self._write_locked(value)


class SessionLease:
    """Owner-bound control session that lasts for the life of the game.

    This is deliberately separate from ``ArmState``.  The regular arm remains
    one-shot and fail-closed; a live session only authorises the service to
    reset and rearm it for the next bounded action.

    The session is bound to the game process it was opened for (PID plus the
    process creation token, so a reused PID is not the same game) instead of
    a wall clock: it stays open with no heartbeat until the game exits or
    someone closes or stops it, and it is never silently reclaimed once
    closed.  The 30-minute lease it replaces expired mid-play, and the
    harness then refused input with the stop latched and nobody noticed.
    The time limit's job, keeping a forgotten session from typing into the
    owner's other windows, is done by the per-action preconditions: the game
    must be running and in the foreground, and the one-shot arm still lapses
    after every action.

    A record written before this (no ``lifetime``) keeps its wall-clock
    expiry until it is heartbeated, which adopts it into the game's life.
    """

    VERSION = 1

    def __init__(
        self,
        runtime_dir: Path | None = None,
        owner_id: str | None = None,
        clock: Callable[[], float] = time.time,
        session_marker: Callable[[], str] = _windows_session_marker,
        process_id: int | None = None,
        process_alive: Callable[[int, str | None], bool] | None = None,
        process_identity: Callable[[int], str | None] | None = None,
        process_absent: Callable[[int], bool] | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir or default_runtime_dir()
        self.owner_id = str(owner_id or os.getenv("FF7R_AUTONOMY_OWNER_ID", "ff7r-codex-agent"))
        self.clock = clock
        self.session_marker = session_marker
        self.process_id = int(process_id or os.getpid())
        # Injectable so the game's life can be faked; the defaults are looked
        # up per call and use the same handle-safe probes as the rest of the
        # harness.
        self._process_alive = process_alive
        self._process_identity = process_identity
        self._process_absent = process_absent
        self.state_path = self.runtime_dir / "control-session.json"
        self.last_close_path = self.runtime_dir / "control-last-close.json"

    def _alive(self, pid: int, token: str | None) -> bool:
        return bool((self._process_alive or owner_is_alive)(pid, token))

    def _identity(self, pid: int) -> str | None:
        return (self._process_identity or process_identity_token)(pid)

    def _ended(self, value: dict[str, object]) -> str | None:
        """Why an OPEN record no longer authorises input, or None while it does."""
        if value.get("lifetime") == LIFETIME_GAME:
            pid = value.get("game_pid")
            if value.get("game_bound") and pid is not None and not self._alive(
                int(pid), value.get("game_identity")  # type: ignore[arg-type]
            ):
                return "GAME_EXITED"
            return None
        return "EXPIRED" if float(value.get("expires_at") or 0.0) <= self.clock() else None

    def _bind_game(self, value: dict[str, object], game_pid: int | None) -> None:
        """Record the game process this session lives and dies with.

        Bound only to a process seen alive now, so a PID that never existed
        (a dry run, a title screen before launch) is recorded but not
        enforced.  The input gates still need a running game for every action.
        """
        pid = int(game_pid) if game_pid is not None else None
        identity = self._identity(pid) if pid is not None else None
        value.update({
            "lifetime": LIFETIME_GAME,
            "expires_at": None,
            "game_pid": pid,
            "game_identity": identity,
            "game_bound": bool(pid is not None and (identity is not None or self._alive(pid, None))),
        })

    def _read(self) -> dict[str, object] | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArmError(f"control session is unreadable: {self.state_path}") from exc
        if not isinstance(value, dict) or int(value.get("version", 0)) != self.VERSION:
            raise ArmError("control session has an unsupported format")
        if value.get("session_marker") != self.session_marker():
            raise ArmError("control session belongs to a different Windows session")
        return value

    def _read_unbound(self) -> dict[str, object] | None:
        """Read a lease record before applying the current boot/session fence."""
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArmError(f"control session is unreadable: {self.state_path}") from exc
        if not isinstance(value, dict) or int(value.get("version", 0)) != self.VERSION:
            raise ArmError("control session has an unsupported format")
        return value

    def status(self) -> dict[str, object] | None:
        with _state_mutex():
            value = self._read_unbound()
            if value is None:
                return None
            if value.get("session_marker") != self.session_marker():
                result = dict(value)
                result.update({
                    "active": False,
                    "owner_matches": False,
                    "seconds_remaining": 0.0,
                    "state": "STALE_WINDOWS_SESSION",
                    "stale_session": True,
                    "recorded_controller_alive": _process_exists(value.get("controller_pid")),
                    "recorded_game_alive": _process_exists(value.get("game_pid")),
                })
                return result
            owner_matches = value.get("owner_id") == self.owner_id
            ended = self._ended(value) if value.get("state") == "OPEN" else None
            active = owner_matches and value.get("state") == "OPEN" and ended is None
            result = dict(value)
            game_lifetime = value.get("lifetime") == LIFETIME_GAME
            result.update({
                "active": active,
                "owner_matches": owner_matches,
                "lifetime": LIFETIME_GAME if game_lifetime else "wall_clock",
                # None: the session has no clock. A pre-game-lifetime record
                # still reports the time left on its old lease.
                "seconds_remaining": None if game_lifetime else (
                    max(0.0, float(value.get("expires_at") or 0.0) - self.clock()) if active else 0.0),
            })
            if not owner_matches:
                result["state"] = "OWNER_MISMATCH"
            if owner_matches and not active and value.get("state") == "OPEN":
                result["state"] = ended or "EXPIRED"
            return result

    def last_close(self) -> dict[str, object] | None:
        """How and when the most recent session ended, for status reports."""
        try:
            value = json.loads(self.last_close_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (not isinstance(value, dict) or value.get("version") != 1
                or value.get("session_marker") != self.session_marker()):
            return None
        return value

    # Routes and approaches close and reopen the lease without a declaration
    # within seconds; a reopen that soon after a declared lease keeps its
    # declaration, so their clips are not recorded as unverified. A later
    # session must declare again.
    DRIVER_CARRY_SECONDS = 120.0

    def _remember_driver(self, driver: dict[str, str]) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.runtime_dir / "recording-driver.json", {
            "version": 1, "session_marker": self.session_marker(), "driver": driver, "at": self.clock()})

    def _remembered_driver(self) -> dict[str, str] | None:
        try:
            value = json.loads((self.runtime_dir / "recording-driver.json").read_text(encoding="utf-8"))
            if (value.get("version") != 1 or value.get("session_marker") != self.session_marker()
                    or not 0.0 <= self.clock() - float(value.get("at")) <= self.DRIVER_CARRY_SECONDS):
                return None
            driver = value.get("driver") or {}
            return validate_recording_driver(driver.get("modelId"), driver.get("effort"))
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def open(self, seconds: int = MAX_SESSION_LEASE_SECONDS, game_pid: int | None = None, client_id: str | None = None,
             model_id: str | None = None, reasoning_effort: str | None = None) -> dict[str, object]:
        try:
            driver = configured_recording_driver(model_id, reasoning_effort)
        except ValueError as exc:
            raise ArmError(str(exc)) from exc
        with _state_mutex():
            current = self._read_unbound()
            if current is not None:
                if current.get("session_marker") != self.session_marker():
                    if _process_exists(current.get("game_pid")) or _process_exists(current.get("controller_pid")):
                        raise ArmError("a prior Windows-session control process is still alive")
                    archive = self.runtime_dir / (
                        f"control-session.stale-{int(self.clock())}-{os.getpid()}.json"
                    )
                    if archive.exists():
                        raise ArmError("refusing to overwrite a prior stale-session archive")
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    os.replace(self.state_path, archive)
                    current = None

            if current is not None:
                ended = self._ended(current) if current.get("state") == "OPEN" else None
                if current.get("state") == "OPEN" and ended is None:
                    raise ArmError("an active control session already exists")
                if ended == "GAME_EXITED" and current.get("owner_id") == self.owner_id:
                    # The game it was for has gone, so nothing is left for the
                    # record to protect: archive it and open the new session.
                    self._archive(current, "game_exited")
                else:
                    raise ArmError("a closed or expired control session must be explicitly archived")
            now = self.clock()
            value: dict[str, object] = {
                "version": self.VERSION,
                "session_marker": self.session_marker(),
                "owner_id": self.owner_id,
                "controller_pid": self.process_id,
                "client_id": client_id,
                "recording_driver": driver if driver is not None else self._remembered_driver(),
                "opened_at": now,
                "heartbeat_at": now,
                # Advisory, kept for the audit trail: the session has no clock.
                "requested_seconds": _advisory_seconds(seconds),
                "state": "OPEN",
            }
            self._bind_game(value, game_pid)
            _atomic_json(self.state_path, value)
            if value["recording_driver"] is not None:
                self._remember_driver(value["recording_driver"])
            result = dict(value)
            result["active"] = True
            result["seconds_remaining"] = None
            return result

    def heartbeat(self, seconds: int = MAX_SESSION_LEASE_SECONDS,
                  model_id: str | None = None, reasoning_effort: str | None = None) -> dict[str, object]:
        """Re-declare the recording attribution; ``seconds`` is accepted and ignored.

        Nothing needs renewing any more.  This is still how a client changes
        its model or effort mid-session, and how a session written before
        game-lifetime sessions is adopted into the game's life.
        """
        del seconds
        try:
            driver = validate_recording_driver(model_id, reasoning_effort)
        except ValueError as exc:
            raise ArmError(str(exc)) from exc
        with _state_mutex():
            value = self._read()
            active = bool(
                value is not None
                and value.get("state") == "OPEN"
                and self._ended(value) is None
            )
            if value is None or not active:
                raise ArmError("control session is not active")
            if value.get("owner_id") != self.owner_id:
                raise ArmError("control session owner differs")
            value["heartbeat_at"] = self.clock()
            if value.get("lifetime") != LIFETIME_GAME:
                self._bind_game(value, value.get("game_pid"))  # type: ignore[arg-type]
            if driver is not None:
                value["recording_driver"] = driver
            _atomic_json(self.state_path, value)
            if isinstance(value.get("recording_driver"), dict):
                self._remember_driver(value["recording_driver"])
            result = dict(value)
            result["active"] = True
            result["seconds_remaining"] = None
            return result

    def consume_entity_interaction(self, client_id: str, game_pid: int, target_id: str,
                                   request_key: str | None = None) -> None:
        """Persist at-most-once admission before input, including worker restarts.

        Without a ``request_key`` the whole session admits one interaction.
        A game-lifetime session outlives any one mission, so a mission names
        its request (the observation it acts on): that request is admitted
        once, and a later mission's fresh request is a different one.
        """
        with _state_mutex():
            value = self._read()
            if (not client_id or not target_id or value is None
                or value.get("owner_id") != self.owner_id
                or value.get("client_id") != client_id or value.get("game_pid") != game_pid
                or value.get("state") != "OPEN" or self._ended(value) is not None):
                raise ArmError("entity_interaction_session_mismatch")
            keys = [str(key) for key in value.get("entity_interactions") or []]
            if (request_key is None and value.get("entity_interaction") is not None) or (
                    request_key is not None and str(request_key) in keys):
                raise ArmError("entity_interaction_already_consumed")
            value["entity_interaction"] = {"target_id": target_id, "consumed_at": self.clock(),
                                           "request_key": request_key}
            if request_key is not None:
                value["entity_interactions"] = (keys + [str(request_key)])[-32:]
            _atomic_json(self.state_path, value)

    def _archive(self, value: dict[str, object], reason: str) -> dict[str, object]:
        """Archive a record and remove it, leaving a note of how it ended."""
        value["state"] = "CLOSED"
        value["closed_at"] = self.clock()
        if isinstance(value.get("recording_driver"), dict):
            self._remember_driver(value["recording_driver"])
        value["close_reason"] = str(reason)
        archive = self.runtime_dir / (
            f"control-session.closed-{int(self.clock())}-{os.getpid()}-{uuid.uuid4().hex}.json"
        )
        if archive.exists():
            raise ArmError("refusing to overwrite a prior control-session archive")
        _atomic_json(archive, value)
        _atomic_json(self.last_close_path, {
            "version": 1, "session_marker": self.session_marker(), "reason": str(reason),
            "closed_at": value["closed_at"], "client_id": value.get("client_id"),
            "game_pid": value.get("game_pid"), "owner_id": value.get("owner_id"),
        })
        self.state_path.unlink(missing_ok=True)
        return {"closed": True, "archive_path": str(archive), "reason": str(reason)}

    def close(self, reason: str = "explicit_close", *,
              expected_session: dict[str, object] | None = None,
              expected_client_id: str | None = None) -> dict[str, object]:
        with _state_mutex():
            value = self._read()
            if value is None:
                return {"closed": False, "reason": "no_session"}
            if value.get("owner_id") != self.owner_id:
                raise ArmError("control session owner differs")
            # Check the acquired lease under the same lock as its removal.
            # A replacement lease or changed attribution must never be closed
            # by delayed startup cleanup.
            if expected_session is not None and any(
                value.get(key) != expected_session.get(key)
                for key in ("session_marker", "owner_id", "controller_pid", "opened_at",
                            "game_pid", "client_id", "recording_driver")
            ):
                return {"closed": False, "reason": "control_session_changed"}
            # A task that only borrowed the session names itself, and does not
            # get to close what another client opened.
            if expected_client_id is not None and value.get("client_id") != expected_client_id:
                return {"closed": False, "reason": "session_owned_by_another_client",
                        "client_id": value.get("client_id")}
            return self._archive(value, reason)

    def recover_legacy_exited_game(
        self, expected_session: dict[str, object], verify_no_game: Callable[[], None],
    ) -> dict[str, object]:
        """Recover only this owner's unchanged legacy lease for a proven dead PID.

        The caller proves lifecycle ownership and neutral input first. The
        fresh strict game inventory and PID probe run again under the safety
        mutex; status/expiry alone never authorise reclaiming a peer.
        """
        with _state_mutex():
            value = self._read()
            if (value is None or value.get("owner_id") != self.owner_id
                    or value.get("state") != "OPEN"
                    or value.get("lifetime") not in (None, "wall_clock")
                    or type(value.get("game_pid")) is not int or value["game_pid"] <= 0):
                raise ArmError("legacy_control_session_recovery_unverified")
            # Compare the complete record, including removed fields. Status
            # adds computed fields and normalises a legacy lifetime to wall_clock.
            if not isinstance(expected_session, dict):
                raise ArmError("control_session_changed")
            expected = {key: item for key, item in expected_session.items()
                        if key not in {"active", "owner_matches", "seconds_remaining", "lifetime"}}
            recorded = {key: item for key, item in value.items() if key != "lifetime"}
            if recorded != expected:
                raise ArmError("control_session_changed")
            verify_no_game()
            try:
                absent = (self._process_absent or process_absence_verified)(value["game_pid"])
            except Exception as exc:
                raise ArmError("legacy_game_pid_live_or_unreadable") from exc
            if absent is not True:
                raise ArmError("legacy_game_pid_live_or_unreadable")
            raw = self.state_path.read_bytes()
            if json.loads(raw) != value:
                raise ArmError("control_session_changed")
            raw_archive = self.runtime_dir / (
                f"control-session.raw-exited-game-{int(self.clock())}-{uuid.uuid4().hex}.json"
            )
            # Retain the original bytes separately from the normal CLOSED audit.
            with raw_archive.open("xb") as stream:
                stream.write(raw)
            result = self._archive(value, "legacy_game_exited")
            # Fence the close and latches together so delayed recovery cannot
            # latch a replacement client's newly opened session.
            for broker in (ArmState, BenchArmState, CameraArmState):
                broker(runtime_dir=self.runtime_dir)._latch_stop_locked()
            result["safety_state"] = "latched"
            result["raw_archive_path"] = str(raw_archive)
            return result

    def abort(self, reason: str = "emergency_stop") -> dict[str, object]:
        """Close the lease after an explicit emergency stop."""
        return self.close(reason)


def request_session_open(
    seconds: int = MAX_SESSION_LEASE_SECONDS,
    runtime_dir: Path | None = None,
    owner_id: str | None = None,
    game_pid: int | None = None,
    client_id: str | None = None,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, object]:
    """Open the control session for the game's life; ``seconds`` is ignored."""
    lease = SessionLease(runtime_dir=runtime_dir, owner_id=owner_id)
    result = lease.open(seconds, game_pid, client_id, model_id, reasoning_effort)
    broker = ArmState(runtime_dir=runtime_dir)
    camera = CameraArmState(runtime_dir=runtime_dir)
    broker.reset_stop()
    camera.reset_stop()
    broker.arm(MAX_ARM_SECONDS)
    camera.arm(MAX_CAMERA_ARM_SECONDS)
    result["armed_for_next_action_seconds"] = MAX_ARM_SECONDS
    result["camera_armed_for_next_action_seconds"] = MAX_CAMERA_ARM_SECONDS
    return result


def request_session_heartbeat(
    seconds: int = MAX_SESSION_LEASE_SECONDS,
    runtime_dir: Path | None = None,
    owner_id: str | None = None,
    model_id: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, object]:
    """Re-declare attribution and re-arm; ``seconds`` is ignored (nothing to renew)."""
    lease = SessionLease(runtime_dir=runtime_dir, owner_id=owner_id)
    result = lease.heartbeat(seconds, model_id, reasoning_effort)
    broker = ArmState(runtime_dir=runtime_dir)
    camera = CameraArmState(runtime_dir=runtime_dir)
    broker.reset_stop()
    camera.reset_stop()
    broker.arm(MAX_ARM_SECONDS)
    camera.arm(MAX_CAMERA_ARM_SECONDS)
    result["armed_for_next_action_seconds"] = MAX_ARM_SECONDS
    result["camera_armed_for_next_action_seconds"] = MAX_CAMERA_ARM_SECONDS
    return result


def request_session_close(
    runtime_dir: Path | None = None,
    owner_id: str | None = None,
    reason: str = "explicit_close",
    expected_session: dict[str, object] | None = None,
    client_id: str | None = None,
) -> dict[str, object]:
    """Close the session and latch every safety broker.

    An explicit close is a stop.  A caller that only borrowed the session
    passes its ``client_id`` (or the ``expected_session`` it acquired): if
    that is not the session on record nothing is closed, and nothing is
    latched, so a task cannot take the control session down with it.
    """
    lease = SessionLease(runtime_dir=runtime_dir, owner_id=owner_id)
    result = lease.close(reason, expected_session=expected_session, expected_client_id=client_id)
    if (expected_session is not None or client_id is not None) and not result.get("closed"):
        return result
    ArmState(runtime_dir=runtime_dir).latch_stop()
    BenchArmState(runtime_dir=runtime_dir).latch_stop()
    CameraArmState(runtime_dir=runtime_dir).latch_stop()
    result["safety_state"] = "latched"
    return result


class BenchArmState(ArmState):
    """Separate, one-use local capability broker for bench interaction."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.state_path = self.runtime_dir / "bench-safety-state.json"

    def arm(self, seconds: int) -> dict[str, object]:
        seconds = int(seconds)
        if not 1 <= seconds <= MAX_BENCH_ARM_SECONDS:
            raise ArmError(
                f"bench arming duration must be between 1 and {MAX_BENCH_ARM_SECONDS} seconds"
            )
        result = super().arm(seconds)
        result["capability"] = "bench-interaction"
        return result


class CameraArmState(ArmState):
    """Separate local capability broker for calibrated camera input."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.state_path = self.runtime_dir / "camera-safety-state.json"

    def arm(self, seconds: int) -> dict[str, object]:
        seconds = int(seconds)
        if not 1 <= seconds <= MAX_CAMERA_ARM_SECONDS:
            raise ArmError(
                f"camera arming duration must be between 1 and {MAX_CAMERA_ARM_SECONDS} seconds"
            )
        result = super().arm(seconds)
        result["capability"] = "camera-calibration"
        return result


def request_arm(seconds: int, runtime_dir: Path | None = None) -> dict[str, object]:
    return ArmState(runtime_dir=runtime_dir).arm(seconds)


def request_reset_stop(runtime_dir: Path | None = None) -> dict[str, object]:
    broker = ArmState(runtime_dir=runtime_dir)
    broker.reset_stop()
    BenchArmState(runtime_dir=runtime_dir).reset_stop()
    CameraArmState(runtime_dir=runtime_dir).reset_stop()
    return {"ok": True, "reset": True, "broker": "local-session"}


def request_arm_bench(seconds: int, runtime_dir: Path | None = None) -> dict[str, object]:
    return BenchArmState(runtime_dir=runtime_dir).arm(seconds)


def request_arm_camera(seconds: int, runtime_dir: Path | None = None) -> dict[str, object]:
    return CameraArmState(runtime_dir=runtime_dir).arm(seconds)


_observation_process_snapshot = local()


@contextmanager
def game_process_snapshot():
    """Share one process enumeration only within one synchronous observation.

    Thread-local nesting is safe; leaving the outer scope always discards it.
    Foreground, leases and executable identity are still checked independently.
    """
    if getattr(_observation_process_snapshot, "values", None) is not None:
        yield
        return
    _observation_process_snapshot.values = {}
    try:
        yield
    finally:
        del _observation_process_snapshot.values


class WindowsGameEnvironment:
    """Win32 process checks plus bounded graceful game-window control."""

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    def _game_processes(self) -> dict[int, str]:
        values = getattr(_observation_process_snapshot, "values", None)
        if values is None:
            return self._scan_game_processes()
        key = type(self)
        if key not in values:
            values[key] = self._scan_game_processes()
        return dict(values[key])

    def _scan_game_processes(self, *, strict: bool = False) -> dict[int, str]:
        if os.name != "nt":
            if strict:
                raise SafetyError("verified_game_process_probe_requires_windows")
            return {}
        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if snapshot == self.INVALID_HANDLE_VALUE:
            if strict:
                raise SafetyError("game_process_snapshot_failed")
            return {}
        result: dict[int, str] = {}
        entry = self.PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile.lower()
                if name in GAME_EXECUTABLES:
                    result[int(entry.th32ProcessID)] = name
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            if strict and kernel32.GetLastError() != 18:  # ERROR_NO_MORE_FILES
                raise SafetyError("game_process_enumeration_incomplete")
        finally:
            kernel32.CloseHandle(snapshot)
        return result

    def game_process_exists(self) -> bool:
        return bool(self._game_processes())

    def game_process_ids(self) -> dict[int, str]:
        """Return the currently observed allow-listed game PIDs and names."""
        return self._game_processes()

    def game_process_ids_verified(self) -> dict[int, str]:
        """Fresh inventory that cannot turn a failed probe into a crash proof."""
        return self._scan_game_processes(strict=True)

    def game_process_identity_tokens(
        self, pids: list[int] | tuple[int, ...] | set[int]
    ) -> dict[int, str]:
        """Return creation tokens for currently observed game PIDs.

        A missing token is intentionally omitted. Callers that may terminate a
        process must treat that as an identity-verification failure rather than
        guessing that a reused or inaccessible PID is still the same process.
        """
        observed = self._game_processes()
        tokens: dict[int, str] = {}
        for raw_pid in pids:
            pid = int(raw_pid)
            if pid not in observed:
                continue
            token = process_identity_token(pid)
            if token is not None:
                tokens[pid] = str(token)
        return tokens

    def game_window_handles(self) -> list[dict[str, int]]:
        """Return top-level windows belonging to the allow-listed game PIDs.

        ``GetProcess().MainWindowHandle`` is not reliable for this DX12 game:
        the visible Rebirth window can be discoverable through the foreground
        window API while the process property still reports handle zero. Keep
        the discovery inside the same Win32 session as input/capture and put
        the foreground window first.
        """
        if os.name != "nt":
            return []
        process_ids = self._game_processes()
        if not process_ids:
            return []
        user32 = ctypes.windll.user32
        handle_type = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = [
            handle_type, ctypes.POINTER(ctypes.c_ulong)
        ]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetForegroundWindow.restype = handle_type
        user32.IsWindow.argtypes = [handle_type]
        user32.IsWindow.restype = ctypes.c_bool
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, handle_type, ctypes.c_void_p)
        user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
        user32.EnumWindows.restype = ctypes.c_bool

        windows: list[dict[str, int]] = []
        seen: set[int] = set()

        def add_window(raw_handle: object) -> None:
            hwnd = int(getattr(raw_handle, "value", raw_handle) or 0)
            if not hwnd or hwnd in seen or not user32.IsWindow(handle_type(hwnd)):
                return
            process_id = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(
                handle_type(hwnd), ctypes.byref(process_id)
            )
            pid = int(process_id.value)
            if pid not in process_ids:
                return
            seen.add(hwnd)
            windows.append({"hwnd": hwnd, "pid": pid})

        add_window(user32.GetForegroundWindow())

        @callback_type
        def collect_window(raw_handle: object, _lparam: object) -> bool:
            add_window(raw_handle)
            return True

        user32.EnumWindows(collect_window, None)
        return windows

    def game_render_window_handles(self) -> list[dict[str, int]]:
        """Visible Unreal render windows; exclude IME and other helper windows."""
        if os.name != "nt":
            return []
        user32 = ctypes.windll.user32
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        result = []
        for window in self.game_window_handles():
            hwnd = ctypes.c_void_p(window["hwnd"])
            name = ctypes.create_unicode_buffer(256)
            if (user32.IsWindowVisible(hwnd) and user32.GetClassNameW(hwnd, name, 256)
                    and name.value == "UnrealWindow"):
                result.append(window)
        return result

    def request_graceful_close(
        self,
        target_pids: set[int] | list[int] | tuple[int, ...] | None = None,
    ) -> dict[str, object]:
        """Post WM_CLOSE only to verified allow-listed game windows.

        ``target_pids`` is supplied by the lifecycle controller when a durable
        run context exists.  Filtering both the process snapshot and the
        window list prevents a close request for one run from touching an
        unrelated Rebirth process.  This remains graceful-only; termination
        is a separate lifecycle-owned operation.
        """
        observed_processes = self._game_processes()
        targets = None if target_pids is None else {int(pid) for pid in target_pids}
        processes = {
            pid: name
            for pid, name in observed_processes.items()
            if targets is None or pid in targets
        }
        if not processes:
            return {
                "requested": False,
                "state": "already_closed",
                "processes": {},
                "windows": [],
                "target_pids": sorted(targets) if targets is not None else None,
            }
        windows = [
            window for window in self.game_window_handles()
            if targets is None or int(window["pid"]) in targets
        ]
        if os.name != "nt":
            return {
                "requested": False,
                "state": "manual_end_task_required",
                "reason": "graceful_close_requires_windows",
                "processes": processes,
                "windows": windows,
                "target_pids": sorted(targets) if targets is not None else None,
            }
        user32 = ctypes.windll.user32
        handle_type = ctypes.c_void_p
        user32.PostMessageW.argtypes = [
            handle_type, ctypes.c_uint, handle_type, handle_type
        ]
        user32.PostMessageW.restype = ctypes.c_bool
        posted: list[int] = []
        posted_pids: set[int] = set()
        for window in windows:
            pid = int(window["pid"])
            if pid in posted_pids:
                continue
            if user32.PostMessageW(
                handle_type(int(window["hwnd"])),
                0x0010,  # WM_CLOSE
                handle_type(0),
                handle_type(0),
            ):
                posted.append(int(window["hwnd"]))
                posted_pids.add(pid)
        return {
            "requested": bool(posted),
            "state": "close_requested" if posted else "manual_end_task_required",
            "reason": None if posted else "game_window_not_found",
            "processes": processes,
            "windows": windows,
            "posted_windows": posted,
            "target_pids": sorted(targets) if targets is not None else None,
            "posted_pids": sorted(posted_pids),
        }

    def foreground_window_handle(self) -> int | None:
        """The current foreground window, or None off Windows."""
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        value = user32.GetForegroundWindow()
        return int(value) if value else None

    def restore_foreground_window(self, hwnd: int) -> dict[str, object]:
        """Give the foreground back to a window the harness borrowed it from.

        Best-effort and non-invasive: a closed window or a denied activation
        is reported, never retried, and minimised windows are left as they are.
        """
        if os.name != "nt" or not hwnd:
            return {"ok": False, "state": "unsupported"}
        user32 = ctypes.windll.user32
        handle_type = ctypes.c_void_p
        user32.IsWindow.argtypes = [handle_type]
        user32.IsWindow.restype = ctypes.c_bool
        if not user32.IsWindow(handle_type(hwnd)):
            return {"ok": False, "state": "window_closed"}
        user32.SetForegroundWindow.argtypes = [handle_type]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        if not user32.SetForegroundWindow(handle_type(hwnd)):
            user32.GetForegroundWindow.argtypes = []
            user32.GetForegroundWindow.restype = handle_type
            user32.GetWindowThreadProcessId.argtypes = [handle_type, ctypes.POINTER(ctypes.c_uint32)]
            user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
            user32.AttachThreadInput.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool]
            user32.AttachThreadInput.restype = ctypes.c_bool
            current = ctypes.windll.kernel32.GetCurrentThreadId()
            owner = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            attached = bool(owner and owner != current and user32.AttachThreadInput(current, owner, True))
            try:
                user32.SetForegroundWindow(handle_type(hwnd))
            finally:
                if attached:
                    user32.AttachThreadInput(current, owner, False)
        return {"ok": self.foreground_window_handle() == int(hwnd), "state": "restored"}

    def focus_game_window(self, pid: int | None = None, *, wake_display: bool = False) -> dict[str, object]:
        """Bring an allow-listed Rebirth window to the foreground.

        Window discovery and input both stay inside this Win32 desktop session.
        A caller may provide the durably bound gameplay PID; otherwise the
        gameplay process is preferred over the launcher.  This is deliberately
        best-effort: Windows may deny foreground activation when another app
        owns the foreground lock, so the caller must still re-check the result.
        """
        if os.name != "nt":
            return {"ok": False, "state": "unsupported_platform", "foreground": False}
        windows = self.game_render_window_handles() if wake_display else self.game_window_handles()
        if pid is not None:
            candidates = [window for window in windows if int(window["pid"]) == int(pid)]
        else:
            candidates = [
                window for window in windows
                if self._game_processes().get(int(window["pid"])) == "ff7rebirth_.exe"
            ] or windows
        if not candidates:
            return {
                "ok": False,
                "state": "game_window_not_found",
                "foreground": self.game_is_foreground(),
            }
        if wake_display and len(candidates) != 1:
            return {"ok": False, "state": "ambiguous_game_render_window", "foreground": False}
        display_wake_ok = None
        if wake_display:
            # A one-shot display idle reset; no keys, persistent power-plan
            # changes or attempts to unlock the Windows session.
            wake = ctypes.windll.kernel32.SetThreadExecutionState
            wake.argtypes = [ctypes.c_uint32]
            wake.restype = ctypes.c_uint32
            display_wake_ok = bool(wake(0x00000002))  # ES_DISPLAY_REQUIRED
        user32 = ctypes.windll.user32
        handle_type = ctypes.c_void_p
        if len(candidates) == 1:
            window = candidates[0]
        else:
            user32.GetWindowTextLengthW.argtypes = [handle_type]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowTextW.argtypes = [handle_type, ctypes.c_wchar_p, ctypes.c_int]
            user32.GetWindowTextW.restype = ctypes.c_int

            def title(hwnd: int) -> str:
                length = max(0, int(user32.GetWindowTextLengthW(handle_type(hwnd))))
                if length == 0:
                    return ""
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(handle_type(hwnd), buffer, length + 1)
                return buffer.value.strip().casefold()

            # Rebirth creates a small process-owned helper window alongside
            # the actual game surface.  The helper may be enumerated first and
            # can become foreground when the process is launched, but it
            # cannot show the game or receive the intended input.  Prefer the
            # exact visible game title while retaining the allow-listed
            # fallback.
            window = max(
                candidates,
                key=lambda item: title(int(item["hwnd"])) == "final fantasy vii rebirth",
            )
        hwnd = int(window["hwnd"])
        user32.ShowWindow.argtypes = [handle_type, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        user32.BringWindowToTop.argtypes = [handle_type]
        user32.BringWindowToTop.restype = ctypes.c_bool
        user32.SetForegroundWindow.argtypes = [handle_type]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        restored = bool(user32.ShowWindow(handle_type(hwnd), 9))  # SW_RESTORE
        brought_to_top = bool(user32.BringWindowToTop(handle_type(hwnd)))
        requested = bool(user32.SetForegroundWindow(handle_type(hwnd)))
        foreground = self.game_is_foreground()
        attached_thread_ids: list[int] = []
        attach_failures: list[int] = []
        detach_failures: list[int] = []
        if not foreground:
            # A normal SetForegroundWindow request can be denied by the
            # foreground-lock rules when this worker is not the active UI
            # thread. Temporarily sharing the input queue with the current
            # foreground and target window threads gives Windows the required
            # activation context. Every attachment is detached before the
            # result is reported; a failed detach is fail-closed even when
            # the target happens to become foreground.
            user32.GetForegroundWindow.argtypes = []
            user32.GetForegroundWindow.restype = handle_type
            user32.GetWindowThreadProcessId.argtypes = [
                handle_type, ctypes.POINTER(ctypes.c_uint32)
            ]
            user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
            user32.AttachThreadInput.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool
            ]
            user32.AttachThreadInput.restype = ctypes.c_bool
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentThreadId.argtypes = []
            kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
            current_thread_id = int(kernel32.GetCurrentThreadId())
            target_thread_id = int(
                user32.GetWindowThreadProcessId(handle_type(hwnd), None)
            )
            foreground_handle = user32.GetForegroundWindow()
            foreground_hwnd = int(getattr(foreground_handle, "value", foreground_handle) or 0)
            foreground_thread_id = (
                int(user32.GetWindowThreadProcessId(handle_type(foreground_hwnd), None))
                if foreground_hwnd
                else 0
            )
            for thread_id in (foreground_thread_id, target_thread_id):
                if (
                    not thread_id
                    or thread_id == current_thread_id
                    or thread_id in attached_thread_ids
                ):
                    continue
                if bool(
                    user32.AttachThreadInput(
                        ctypes.c_uint32(current_thread_id),
                        ctypes.c_uint32(thread_id),
                        ctypes.c_bool(True),
                    )
                ):
                    attached_thread_ids.append(thread_id)
                else:
                    attach_failures.append(thread_id)
            requested = bool(user32.SetForegroundWindow(handle_type(hwnd))) or requested
            for thread_id in reversed(attached_thread_ids):
                if not bool(
                    user32.AttachThreadInput(
                        ctypes.c_uint32(current_thread_id),
                        ctypes.c_uint32(thread_id),
                        ctypes.c_bool(False),
                    )
                ):
                    detach_failures.append(thread_id)
            foreground = self.game_is_foreground()
        ok = foreground and not attach_failures and not detach_failures
        return {
            "ok": ok,
            "state": "foreground" if ok else (
                "foreground_requested" if foreground else "foreground_denied"
            ),
            "hwnd": hwnd,
            "pid": int(window["pid"]),
            "restored": restored,
            "brought_to_top": brought_to_top,
            "requested": requested,
            "foreground": foreground,
            "attached_thread_ids": attached_thread_ids,
            "attach_failures": attach_failures,
            "detach_failures": detach_failures,
            **({"display_wake_requested": True, "display_wake_ok": display_wake_ok} if wake_display else {}),
        }

    def terminate_game_processes(
        self,
        pids: list[int] | tuple[int, ...] | set[int],
        expected_identities: dict[int | str, dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """Terminate only currently observed, allow-listed Rebirth PIDs.

        This is the last-resort orphan cleanup used by a controller that has
        already proven ownership of the exact run context.  It is not exposed
        as a general process-kill primitive and silently ignores PIDs that no
        longer resolve to one of the two Rebirth executables.
        """
        requested = sorted({int(pid) for pid in pids if int(pid) > 0})
        if os.name != "nt":
            return {
                "ok": False,
                "state": "unsupported_platform",
                "requested_pids": requested,
                "terminated_pids": [],
            }
        observed = self._game_processes()
        candidates = [pid for pid in requested if pid in observed]
        expected = expected_identities or {}
        user32 = ctypes.windll.kernel32
        user32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        user32.OpenProcess.restype = ctypes.c_void_p
        user32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.TerminateProcess.restype = ctypes.c_bool
        user32.CloseHandle.argtypes = [ctypes.c_void_p]
        user32.CloseHandle.restype = ctypes.c_bool
        terminated: list[int] = []
        failures: list[dict[str, object]] = []
        # Stop the gameplay child before the launcher so the launcher cannot
        # immediately respawn it while the close path is finishing.
        candidates.sort(key=lambda candidate: observed[candidate] == "ff7rebirth.exe")
        for candidate in candidates:
            expected_identity = expected.get(candidate) or expected.get(str(candidate))
            if expected:
                if not isinstance(expected_identity, dict):
                    failures.append({"pid": candidate, "reason": "expected_identity_missing"})
                    continue
                expected_name = str(expected_identity.get("name") or "").lower()
                if expected_name and observed[candidate].lower() != expected_name:
                    failures.append({"pid": candidate, "reason": "process_name_mismatch"})
                    continue
                expected_token = str(expected_identity.get("creation_token") or "")
                actual_token = process_identity_token(candidate)
                if not expected_token or actual_token is None:
                    failures.append({"pid": candidate, "reason": "process_identity_unavailable"})
                    continue
                if str(actual_token) != expected_token:
                    failures.append({"pid": candidate, "reason": "process_identity_mismatch"})
                    continue
            handle = user32.OpenProcess(
                self.PROCESS_TERMINATE | self.PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                candidate,
            )
            if not handle:
                failures.append({"pid": candidate, "reason": "open_process_failed"})
                continue
            try:
                if user32.TerminateProcess(handle, 1):
                    terminated.append(candidate)
                else:
                    failures.append({"pid": candidate, "reason": "terminate_process_failed"})
            finally:
                user32.CloseHandle(handle)
        return {
            "ok": not failures,
            "state": "termination_requested" if terminated else "no_matching_processes",
            "requested_pids": requested,
            "terminated_pids": terminated,
            "failures": failures,
        }

    def game_is_foreground(self) -> bool:
        processes = self._game_processes()
        if not processes or os.name != "nt":
            return False
        handle = ctypes.windll.user32.GetForegroundWindow()
        if not handle:
            return False
        process_id = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        return int(process_id.value) in processes
