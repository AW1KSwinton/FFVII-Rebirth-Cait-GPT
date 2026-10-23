"""Loaded-save receipts and explicit model runs, independent of save mtimes.

A receipt is produced only by a verified in-game load or Save-menu write. Process creation
tokens let it survive an MCP restart without surviving a game restart or PID
reuse. Model runs share a slot/profile across reasoning efforts. Assignments
never create, copy, overwrite or save game data. Safe menu saves retain the
model run's profile and earlier slot ownership when advancing its working slot.
"""
from __future__ import annotations

import json
import uuid

from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .public_stream_events import DRIVER_NAMES, MODEL_ID
from .queen_blood_profile import _now, _read, _write


def model_key(driver: dict[str, str]) -> str:
    family, model = driver.get("driver"), driver.get("model_id")
    if family not in DRIVER_NAMES or not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise ValueError("a verified driver and exact model ID are required")
    return f"{family}:{model.lower()}"


def process_identity(environment: Any) -> dict[str, str] | None:
    """Exactly one live game process, including its creation token."""
    ids = getattr(environment, "game_process_ids", None)
    tokens = getattr(environment, "game_process_identity_tokens", None)
    if not callable(ids) or not callable(tokens):
        return None
    processes = ids()
    # Steam keeps the launcher alive beside the real game executable. Bind
    # the loaded world to the latter; two actual game processes remain ambiguous.
    game_processes = {pid: name for pid, name in processes.items()
                      if str(name).lower() == "ff7rebirth_.exe"}
    if game_processes:
        processes = game_processes
    if len(processes) != 1:
        return None
    pid = next(iter(processes))
    token = tokens([pid]).get(pid)
    return {"pid": str(pid), "token": str(token)} if token else None


def save_fingerprint(file: dict[str, Any]) -> dict[str, Any]:
    slot, steam_id = file.get("slot"), file.get("steam_id")
    path = Path(str(file.get("path") or ""))
    if (type(slot) is not int or not 0 <= slot <= 999 or not isinstance(steam_id, str)
            or not steam_id.isdecimal() or not path.is_absolute()
            or path.name != f"ff7rebirth{slot:03}.sav" or path.parent.name != steam_id):
        raise ValueError("the loaded save file identity is invalid")
    stat = path.stat()
    return {"slot": slot, "steam_id": steam_id, "path": str(path),
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def profile_id(driver: dict[str, str], save: dict[str, Any]) -> str:
    model_key(driver)
    return f"{driver['driver']}-{driver['model_id'].lower()}-{save['steam_id']}-slot{save['slot']:03}"


def assigned_profile_id(driver: dict[str, str], assigned: dict[str, Any]) -> str:
    """Save As advances the working slot while the same model run keeps its ledger."""
    anchor = assigned.get("profile_slot", assigned["slot"])
    if (type(anchor) is not int or anchor == 14 or not 1 <= anchor <= 999
            or anchor not in [assigned["slot"], *assigned.get("previous_slots", [])]):
        raise ValueError("the model run profile slot is invalid")
    return profile_id(driver, {**assigned, "slot": anchor})


class QueenBloodRuns:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.loaded_path = self.root / "loaded-save.json"
        self.runs_path = self.root / "model-runs.json"

    def invalidate(self, reason: str) -> None:
        _write(self.loaded_path, {"version": 1, "loaded": None, "reason": reason, "at": _now()})

    def begin_load(self, file: dict[str, Any], process: dict[str, str] | None) -> dict[str, Any]:
        self.invalidate("load_in_progress")
        return {"file": save_fingerprint(file), "process": process}

    def complete_load(self, candidate: dict[str, Any], process: dict[str, str] | None) -> dict[str, Any]:
        if not process or process != candidate.get("process"):
            return {"verified": False, "reason": "game_process_changed_during_load"}
        if save_fingerprint(candidate["file"]) != candidate["file"]:
            return {"verified": False, "reason": "save_file_changed_during_load"}
        loaded = {**candidate["file"], "process": process, "source": "verified_load_menu", "at": _now(),
                  "load_id": uuid.uuid4().hex}
        _write(self.loaded_path, {"version": 1, "loaded": loaded})
        return {"verified": True, **loaded}

    def loaded(self, process: dict[str, str] | None) -> dict[str, Any] | None:
        data = _read(self.loaded_path)
        loaded = data.get("loaded")
        if (data.get("version") != 1 or not isinstance(loaded, dict) or not process
                or loaded.get("process") != process or loaded.get("source") not in {"verified_load_menu", "verified_save_menu"}
                or type(loaded.get("slot")) is not int or not 0 <= loaded["slot"] <= 999
                or not isinstance(loaded.get("steam_id"), str) or not loaded["steam_id"].isdecimal()):
            return None
        # Later autosaves/manual saves change files but do not change which
        # world is loaded. Compare the file only during the load itself.
        return loaded

    def assignment(self, driver: dict[str, str]) -> dict[str, Any] | None:
        item = self._runs().get(model_key(driver))
        if not isinstance(item, dict):
            return None
        if (type(item.get("slot")) is not int or not 1 <= item["slot"] <= 999 or item["slot"] == 14
                or not isinstance(item.get("steam_id"), str) or not item["steam_id"].isdecimal()
                or item.get("profile_id") != assigned_profile_id(driver, item)):
            raise ValueError("the model run assignment is invalid")
        return item

    def _runs(self) -> dict[str, Any]:
        if not self.runs_path.exists():
            return {}
        try:
            data = json.loads(self.runs_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("runs"), dict):
                raise ValueError()
            slots = set()
            for key, item in data["runs"].items():
                if (not isinstance(item, dict) or model_key(item) != key
                        or type(item.get("slot")) is not int or not 1 <= item["slot"] <= 999 or item["slot"] == 14
                        or not isinstance(item.get("steam_id"), str) or not item["steam_id"].isdecimal()
                        or item.get("profile_id") != assigned_profile_id(item, item)):
                    raise ValueError()
                slot = (item["steam_id"], item["slot"])
                if slot in slots:
                    raise ValueError()
                slots.add(slot)
                previous = item.get("previous_slots", [])
                if not isinstance(previous, list):
                    raise ValueError()
                for number in previous:
                    if type(number) is not int or not 1 <= number <= 999 or number == 14:
                        raise ValueError()
                    owned = (item["steam_id"], number)
                    if owned in slots:
                        raise ValueError()
                    slots.add(owned)
            return data["runs"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError("the model run registry is invalid") from exc

    def assign(self, driver: dict[str, str], file: dict[str, Any]) -> dict[str, Any]:
        with self.mutation_lock():
            return self._assign(driver, file)

    @contextmanager
    def mutation_lock(self):
        """Serialize allocation and saving, including across worker restarts.

        A killed worker leaves the lock for evidence review. Never infer that a
        save did not happen from an expired timestamp or reclaim it automatically.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "save-operation-lock.json"
        resolution = self.root / "save-calibration-resolution-lock.json"
        if resolution.exists() or resolution.is_symlink():
            raise ValueError("save_calibration_resolution_requires_review_or_is_running")
        lease = {"version": 1, "token": uuid4().hex, "at": _now(), "retain": False}
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(lease, stream)
        except FileExistsError as exc:
            raise ValueError("save_operation_requires_review_or_is_running") from exc
        try:
            yield lease
        finally:
            if not lease["retain"]:
                stored = _read(path)
                if stored.get("token") == lease["token"]:
                    path.unlink()

    def _assign(self, driver: dict[str, str], file: dict[str, Any]) -> dict[str, Any]:
        save = save_fingerprint(file)
        if save["slot"] in {0, 14}:
            raise ValueError("model runs require an existing manual working slot; autosave and Fresh slot 14 are reserved")
        key = model_key(driver)
        existing = self.assignment(driver)
        if existing:
            if (existing["slot"], existing["steam_id"]) == (save["slot"], save["steam_id"]):
                return existing
            raise ValueError("this model already has a run; its assignment cannot be silently replaced")
        runs = self._runs()
        if self.owner(save["steam_id"], save["slot"]) is not None:
            raise ValueError("that save slot is already assigned to another model")
        record = {"driver": driver["driver"], "model_id": driver["model_id"],
                  "slot": save["slot"], "steam_id": save["steam_id"], "profile_id": profile_id(driver, save)}
        runs[key] = record
        _write(self.runs_path, {"version": 1, "runs": runs})
        return record

    def owner(self, steam_id: str, slot: int) -> str | None:
        for key, item in self._runs().items():
            if item["steam_id"] == steam_id and slot in [item["slot"], *item.get("previous_slots", [])]:
                return key
        return None

    def record_verified_save(self, driver: dict[str, str], file: dict[str, Any],
                             process: dict[str, str]) -> dict[str, Any]:
        """Caller holds mutation_lock and has verified the game's completed write.

        An explicitly selected empty slot becomes the working slot. Retain
        ownership of earlier slots so a later allocation cannot take them.
        """
        save, key = save_fingerprint(file), model_key(driver)
        if save["slot"] in {0, 14}:
            raise ValueError("reserved_save_slot")
        owner = self.owner(save["steam_id"], save["slot"])
        if owner is not None and owner != key:
            raise ValueError("save_slot_owned_by_another_model")
        runs = self._runs()
        previous = runs.get(key)
        if previous and previous["steam_id"] != save["steam_id"]:
            raise ValueError("save_account_changed")
        old_slots = previous.get("previous_slots", []) if previous else []
        if previous and previous["slot"] != save["slot"]:
            old_slots = [*old_slots, previous["slot"]]
        anchor = previous.get("profile_slot", previous["slot"]) if previous else save["slot"]
        record = {"driver": driver["driver"], "model_id": driver["model_id"], "profile_slot": anchor,
                  "slot": save["slot"], "steam_id": save["steam_id"],
                  "profile_id": profile_id(driver, {**save, "slot": anchor}),
                  "previous_slots": sorted(set(old_slots) - {save["slot"]})}
        _write(self.runs_path, {"version": 1, "runs": {**runs, key: record}})
        loaded = {**save, "process": process, "source": "verified_save_menu", "at": _now(),
                  "load_id": uuid.uuid4().hex}
        _write(self.loaded_path, {"version": 1, "loaded": loaded})
        return {"verified": True, "assignment": record, "loaded_save": loaded}

    def status(self, driver: dict[str, str] | None, process: dict[str, str] | None) -> dict[str, Any]:
        loaded = self.loaded(process)
        assigned = self.assignment(driver) if driver else None
        matches = bool(loaded and (not assigned or (assigned["slot"], assigned["steam_id"]) ==
                                   (loaded["slot"], loaded["steam_id"])))
        return {"ok": True, "driver": driver, "assignment": assigned,
                "loaded_save": loaded, "loaded_save_verified": loaded is not None,
                "profile_id": (assigned["profile_id"] if assigned else profile_id(driver, loaded)) if driver and matches else None,
                "run_loaded": bool(assigned and matches), "efforts_share_run": True}
