"""Opt-in, one-attempt recovery into an explicitly verified model working save.

Private receipts pin the account, assignment, driver and process creation
token. Public state contains only a recovery ID and phase; never file paths,
slot data, backend diagnostics or a narration event.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import time
from typing import Any
from uuid import uuid4

from .queen_blood_profile import _now, _read, _write
from .queen_blood_runs import process_identity, save_fingerprint
from .save_slot_save import file_proof


def receipt_path(service: Any) -> Path:
    runs = service._queen_blood_runs
    if runs is None:
        raise ValueError("recovery_tracking_unavailable")
    return runs.root / "working-save-recovery.json"


def publish(service: Any, recovery_id: str, phase: str) -> None:
    events = service._public_stream_event_dir
    if events is None:
        raise ValueError("recovery_stream_state_unavailable")
    _write(events.parent / "public-working-save-recovery.json",
           {"version": 1, "recoveryId": recovery_id, "phase": phase, "at": _now()})


def arm(service: Any) -> dict[str, Any]:
    status = service.get_queens_blood_run_status()
    loaded, assignment = status.get("loaded_save"), status.get("assignment")
    process = process_identity(service._environment)
    if (not status.get("run_loaded") or not status.get("loaded_save_verified")
            or not status.get("driver") or not loaded or not assignment or not process
            or loaded.get("process") != process):
        raise ValueError("recovery_requires_verified_loaded_model_run")
    target = save_fingerprint(loaded)
    if target["slot"] in {0, 14} or (target["slot"], target["steam_id"]) != (assignment["slot"], assignment["steam_id"]):
        raise ValueError("recovery_working_slot_mismatch")
    prior = _read(receipt_path(service))
    # A successful save can refresh the armed target without losing the
    # watcher's ID. A new process/run requires explicit re-arming.
    same = prior.get("phase") == "armed" and prior.get("process") == process and prior.get("driver") == status["driver"]
    recovery_id = prior["recovery_id"] if same else uuid4().hex
    record = {"version": 1, "recovery_id": recovery_id, "phase": "armed", "process": process,
              "driver": status["driver"], "assignment": assignment, "file": target,
              "file_proof": file_proof(Path(target["path"])), "at": _now()}
    # A save refresh can race the separate loss observer. It must not replace
    # that watcher's recovering/failed cover with a ready marker.
    if not same:
        publish(service, recovery_id, "ready")  # explicit arm of a verified run
    _write(receipt_path(service), record)
    return {"ok": True, "recovery_id": recovery_id, "process": process,
            "receipt_path": str(receipt_path(service)),
            "stream_state_directory": str(service._public_stream_event_dir.parent),
            "slot": target["slot"], "input_sent": False}


def disarm(service: Any) -> dict[str, Any]:
    path = receipt_path(service)
    record = _read(path)
    if record.get("version") == 1:
        _write(path, {**record, "phase": "disarmed", "at": _now()})
    # A deliberate exit must not restore gameplay or clear an unresolved BRB.
    return {"ok": True, "armed": False, "input_sent": False}


def process_lost(environment: Any, expected: dict[str, str]) -> bool:
    """Distinguish actual absence from unreadable enumeration and PID reuse."""
    probe = getattr(environment, "game_process_ids_verified", None)
    if not callable(probe):
        raise ValueError("recovery_process_probe_unavailable")
    ids = probe()
    if (not isinstance(ids, dict)
            or any(type(pid) is not int or pid <= 0 or not isinstance(name, str) or not name.strip()
                   for pid, name in ids.items())):
        raise ValueError("recovery_process_probe_invalid")
    pid = int(expected["pid"])
    if pid in ids:
        token = environment.game_process_identity_tokens([pid]).get(pid)
        if (str(ids[pid]).lower() != "ff7rebirth_.exe"
                or not token or str(token) != expected["token"]):
            raise ValueError("recovery_process_identity_changed_or_unknown")
        return False
    if any(str(name).lower() == "ff7rebirth_.exe" for name in ids.values()):
        raise ValueError("recovery_replacement_game_present")
    return True


def require_stream_cover(startup: Any, recovery_id: str) -> None:
    """Publication is not OBS success. Wait for the streamer's verified BRB ack."""
    clock = getattr(startup, "_monotonic", time.monotonic)
    sleep = getattr(startup, "_sleep", time.sleep)
    path = startup.service._public_stream_event_dir.parent / "working-save-recovery-cover.json"
    deadline = clock() + 15.0
    while clock() < deadline:
        value = _read(path)
        try:
            at = datetime.fromisoformat(value.get("at", "").replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - at).total_seconds()
        except (TypeError, ValueError):
            age = -1
        if (value.get("version") == 1 and value.get("recoveryId") == recovery_id
                and value.get("covered") is True and 0 <= age <= 10.0):
            return
        sleep(0.25)
    raise ValueError("recovery_stream_cover_unverified")


def recover(startup: Any, *, recovery_id: str) -> dict[str, Any]:
    service, lifecycle = startup.service, startup.lifecycle
    path, record = receipt_path(service), _read(receipt_path(service))
    result = {"ok": False, "recovery_id": recovery_id, "actions_replayed": False}
    if (record.get("version") != 1 or record.get("recovery_id") != recovery_id or record.get("phase") != "armed"):
        return {**result, "decision": "recovery_not_armed_or_already_attempted"}
    try:
        if not process_lost(service._environment, record["process"]):
            return {**result, "decision": "recovery_requires_observed_process_loss"}
        # Failures below keep BRB visible, including failed shutdown or reload.
        publish(service, recovery_id, "recovering")
        _write(path, {**record, "phase": "recovering", "at": _now()})
        with service._queen_blood_runs.mutation_lock():
            status = service.get_queens_blood_run_status()
            if status.get("driver") != record["driver"] or status.get("assignment") != record["assignment"]:
                raise ValueError("recovery_model_run_changed")
            if (save_fingerprint(record["file"]) != record["file"]
                    or file_proof(Path(record["file"]["path"])) != record["file_proof"]):
                raise ValueError("recovery_working_save_changed_since_arming")
            # Release input/session through the existing bounded lifecycle path.
            if service.stop().get("ok") is not True:
                raise ValueError("recovery_input_stop_unverified")
            require_stream_cover(startup, recovery_id)
            current = _read(path)
            status = service.get_queens_blood_run_status()
            if (current.get("recovery_id") != recovery_id or current.get("phase") != "recovering"
                    or status.get("driver") != record["driver"] or status.get("assignment") != record["assignment"]
                    or not process_lost(service._environment, record["process"])):
                raise ValueError("recovery_identity_changed_while_covering_stream")
            close_session = getattr(startup, "_session_close", None)
            if callable(close_session):
                result["control_session"] = close_session(reason="working_save_crash_recovery")
            result["close"] = lifecycle.close_game(timeout_seconds=30.0, force=False)
            if result["close"].get("ok") is not True:
                raise ValueError("recovery_close_unverified")
            if not process_lost(service._environment, record["process"]):
                raise ValueError("recovery_original_process_reappeared")
            if (save_fingerprint(record["file"]) != record["file"]
                    or file_proof(Path(record["file"]["path"])) != record["file_proof"]):
                raise ValueError("recovery_save_changed_during_close")
            # model_run pins the assignment, refuses account mismatch and
            # disables newest-file/Continue receipt shortcuts.
            result["startup"] = startup.start_and_load_save_slot(model_run=True)
            status = service.get_queens_blood_run_status()
            loaded = status.get("loaded_save") or {}
            current_process = process_identity(service._environment)
            if (result["startup"].get("ok") is not True or status.get("run_loaded") is not True
                    or status.get("loaded_save_verified") is not True or status.get("driver") != record["driver"]
                    or status.get("assignment") != record["assignment"]
                    or (loaded.get("slot"), loaded.get("steam_id"), loaded.get("path")) !=
                       (record["file"]["slot"], record["file"]["steam_id"], record["file"]["path"])
                    or not current_process or loaded.get("process") != current_process
                    or current_process == record["process"]
                    or file_proof(Path(record["file"]["path"])) != record["file_proof"]):
                raise ValueError("recovery_loaded_save_unverified")
            _write(path, {**record, "phase": "complete", "at": _now()})
            publish(service, recovery_id, "ready")
            return {**result, "ok": True, "state": "recovered", "slot": loaded["slot"]}
    except Exception as exc:
        _write(path, {**record, "phase": "failed", "decision": str(exc), "result": result, "at": _now()})
        publish(service, recovery_id, "failed")
        return {**result, "state": "recovery_failed", "decision": str(exc), "requires_review": True}
