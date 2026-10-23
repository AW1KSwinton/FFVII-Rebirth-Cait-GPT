"""Bounded Save-menu writes with ownership, UI and disk proofs (#215).

No save files are created or edited by this module: only the game's menu may
write them. The final confirmation is disabled until the Save-specific widget
names have been captured and supplied in FF7R_SAVE_WRITE_LAYOUT. Load-menu
dates, row order and a presumed default Yes button never authorize a write.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4
from .save_selection import selected_cell, complete_nodes, validate_contract

from .queen_blood_runs import model_key, process_identity, save_fingerprint
from .queen_blood_profile import _write
from .save_slot_load import (CHANGE_WAIT_SECONDS, MENU_OPEN_WAIT_SECONDS, SAVE_LOAD_ITEM, SaveFile,
                             SaveSlotLoadError, SaveSlotLoader, load_layout,
                             match_save_time)


DEFAULT_SAVE_WRITE_LAYOUT = Path(__file__).resolve().parents[2] / "config" / "save-write-layout.json"


@dataclass(frozen=True)
class SaveWriteLayout:
    slot_node: str
    empty_node: str | None
    empty_texts: tuple[str, ...]
    row_button_node: str
    prompt_slot_node: str
    yes_node: str
    no_node: str
    row_selection: dict | None = None
    # The text inside each button (live 2026-10-08: $menu_save_load_0002_0001
    # and _0002). When set, the selected button must carry its text.
    yes_text: str | None = None
    no_text: str | None = None

    @classmethod
    def configured(cls) -> "SaveWriteLayout":
        # The repository default was calibrated live on 2026-10-08 (probe
        # screenshot: Btn_00 is Yes, No is preselected; a test save to slot
        # 10 was written, verified and reloaded). FF7R_SAVE_WRITE_LAYOUT
        # overrides it for another game build or UI layout.
        path = os.getenv("FF7R_SAVE_WRITE_LAYOUT") or str(DEFAULT_SAVE_WRITE_LAYOUT)
        if not Path(path).is_file():
            raise SaveSlotLoadError("save_write_layout_unvalidated")
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            # The live Save prompt names no slot ("Save your progress to this
            # slot?"), so prompt_slot_node may be empty; the selected row is
            # then re-verified immediately before the row confirmation.
            names = ["slot_node", "row_button_node", "yes_node", "no_node"]
            if not isinstance(data, dict) or any(not isinstance(data.get(n), str) or not data[n].strip() for n in names):
                raise ValueError()
            if not isinstance(data.get("prompt_slot_node", ""), str):
                raise ValueError()
            texts = {key: data.get(key) for key in ("yes_text", "no_text")}
            if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in texts.values()):
                raise ValueError()
            empty, empty_node = data.get("empty_texts"), data.get("empty_node")
            # Occupied-only calibration does not invent an empty-row contract.
            # Allocation remains disabled until both fields are observed.
            if empty_node is None and empty in (None, []):
                empty = []
            elif (not isinstance(empty_node, str) or not empty_node.strip()
                    or not isinstance(empty, list) or not empty
                    or any(not isinstance(t, str) or not t.strip() for t in empty)):
                raise ValueError()
            if data["yes_node"] == data["no_node"]:
                raise ValueError()
            return cls(**{n: data[n] for n in names}, prompt_slot_node=data.get("prompt_slot_node", ""),
                       empty_node=empty_node, empty_texts=tuple(empty), **texts,
                       row_selection=validate_contract(data["row_selection"]) if data.get("row_selection") is not None else None)
        except (OSError, ValueError, TypeError) as exc:
            raise SaveSlotLoadError("save_write_layout_invalid") from exc


def file_proof(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SaveSlotLoadError("save_file_not_regular")
    before = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SaveSlotLoadError("save_file_changed_while_reading")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest}


def _one(nodes: list[dict], name: str) -> dict:
    found = [n for n in nodes if n.get("visible") is True and n.get("name") == name]
    if len(found) != 1:
        raise SaveSlotLoadError("save_widget_missing_or_ambiguous", node=name)
    return found[0]


SAVE_PROMPT_TEXTS = ("$menu_save_load_0002_0000", "Save your progress to this slot?")


def _object_path(node: dict) -> str:
    """The UObject path of a node's full id ("Class /Path:Outer.Inner")."""
    full = str(node.get("full_id") or "")
    return full.split(" ", 1)[1] if " " in full else full


def _within(node: dict, ancestor: dict) -> bool:
    if node is ancestor:
        return True
    path, root = _object_path(node), _object_path(ancestor)
    return bool(root) and (path == root or path.startswith(root + "."))


def _slot(text: Any) -> int:
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]{1,3}", text.strip()):
        raise SaveSlotLoadError("save_row_slot_unreadable")
    return int(text)


class SaveSlotWriter(SaveSlotLoader):
    def __init__(self, service: Any, *, write_layout: SaveWriteLayout | None = None) -> None:
        super().__init__(service)
        self.write_layout = write_layout
        self.committed = False
        self.last_save_evidence = {}
        self._save_frame = None

    def read(self, stage):
        screen = super().read(stage)
        if self.write_layout:
            response = screen.response
            request, sequence = response.get("request_id"), response.get("source_sequence")
            if (response.get("schema_version") != 1 or response.get("source") != "ue4ss_map_query"
                    or not isinstance(request, str) or not re.fullmatch(r"[0-9a-f]{32}", request)
                    or type(sequence) is not int or sequence < 0
                    or self._save_frame and (request == self._save_frame[0] or sequence <= self._save_frame[1])):
                raise SaveSlotLoadError("save_frame_stale_or_invalid")
            self._save_frame = (request, sequence)
        # Keep the last read-only widget proof for offline/live diagnosis,
        # excluding the exporter's large generic property reflection dumps.
        if "save_cells" in screen.response or "save_dialogs" in screen.response:
            self.last_save_evidence = {key: [
                {"id": item.get("id"), "nodes": [
                    {name: node.get(name) for name in ("name", "text", "visible", "keyboard_focus", "hovered")}
                    for node in (item.get("nodes") or [])[:128] if isinstance(node, dict)], "node_walk": item.get("node_walk")}
                for item in (screen.response.get(key) or [])[:24] if isinstance(item, dict)]
                for key in ("save_cells", "save_dialogs")}
        return screen

    def _driver_key(self) -> str | None:
        # A story save may run offline, where no driver is declared.
        return model_key(self.initial["driver"]) if self.initial.get("driver") else None

    def _guard_identity(self) -> None:
        status = self.service.get_queens_blood_run_status()
        if (status.get("driver") != self.initial["driver"] or status.get("assignment") != self.initial.get("assignment")
                or status.get("loaded_save") != self.initial["loaded_save"]
                or not status.get("loaded_save_verified")
                or process_identity(self.service._environment) != self.process):
            raise SaveSlotLoadError("save_identity_changed")
    def _guard_file_stats(self) -> None:
        # Cheap metadata fence after the final UI read; hashes were checked before it.
        proofs = {str(self.target_path): self.before, **self.protected}
        current = {str(p) for p in self.target_path.parent.glob("ff7rebirth[0-9][0-9][0-9].sav") if p != self.target_path and p.name != "ff7rebirth000.sav"}
        if current != set(self.protected):
            raise SaveSlotLoadError("another_manual_save_changed")
        for name, proof in proofs.items():
            path = Path(name)
            if path.is_symlink() or (proof is None) != (not path.exists()):
                raise SaveSlotLoadError("save_file_metadata_changed")
            if proof is not None:
                info = path.stat()
                if not path.is_file() or info.st_size != proof["size"] or info.st_mtime_ns != proof["mtime_ns"]:
                    raise SaveSlotLoadError("save_file_metadata_changed")

    def _guard_confirmation(self) -> None:
        # Full hashes are checked before the final UI snapshot. Fence the
        # process, owner and disk again after that snapshot, without another
        # potentially slow widget/hash walk before sending Confirm.
        self._guard_identity()
        owner = self.runs.owner(self.account, self.target)
        if owner is not None and owner != self._driver_key():
            raise SaveSlotLoadError("save_slot_owned_by_another_model")
        self._guard_file_stats()

    def _guard(self) -> None:
        self._guard_identity()
        owner = self.runs.owner(self.account, self.target)
        if owner is not None and owner != self._driver_key():
            raise SaveSlotLoadError("save_slot_owned_by_another_model")
        if file_proof(self.target_path) != self.before:
            raise SaveSlotLoadError("save_target_changed")
        self._check_protected_files()
        if getattr(self, "test_save", False):
            if owner is not None:
                raise SaveSlotLoadError("save_test_slot_owned_by_a_model")
        elif getattr(self, "story_save", False):
            if owner is not None:
                raise SaveSlotLoadError("save_story_slot_owned_by_a_model")
        elif self.before is not None and self.target != (self.initial.get("assignment") or {}).get("slot"):
            raise SaveSlotLoadError("occupied_slot_is_not_working_slot")

    def _protected_files(self) -> dict[str, Any]:
        return {str(path): file_proof(path) for path in self.target_path.parent.glob("ff7rebirth[0-9][0-9][0-9].sav")
                if path != self.target_path and path.name != "ff7rebirth000.sav"}

    def _check_protected_files(self) -> None:
        if self._protected_files() != self.protected:
            raise SaveSlotLoadError("another_manual_save_changed")

    def _nodes(self, item: dict) -> list[dict]:
        nodes = item.get("nodes")
        if isinstance(nodes, list) and len(nodes) >= 48:
            return complete_nodes(item)
        if not isinstance(nodes, list) or len(nodes) >= 48 or any(not isinstance(n, dict) for n in nodes):
            raise SaveSlotLoadError("save_widgets_truncated_or_invalid")
        return nodes

    def _row(self, screen) -> dict:
        if screen.text_cap_reached or screen.load_prompt_shown or "load_list" in screen.signals:
            raise SaveSlotLoadError("save_screen_ambiguous")
        if self.write_layout.row_selection:
            cell, selection = selected_cell(screen.response, self.write_layout.row_selection)
            if getattr(self, "_save_list_id", selection["id"]) != selection["id"]:
                raise SaveSlotLoadError("save_list_identity_changed")
            self._save_list_id = selection["id"]
            nodes = complete_nodes(cell)
            number = _slot(_one(nodes, self.write_layout.slot_node).get("text"))
            button = _one(nodes, self.write_layout.row_button_node)
            return {"slot": number, "nodes": nodes, "button": button, "selection": selection}
        cells = screen.response.get("save_cells")
        if not isinstance(cells, list) or not cells or len(cells) >= 24:
            raise SaveSlotLoadError("save_rows_missing_or_truncated")
        rows, slots = [], set()
        for cell in cells:
            nodes = self._nodes(cell)
            number = _slot(_one(nodes, self.write_layout.slot_node).get("text"))
            if number in slots:
                raise SaveSlotLoadError("save_row_slot_ambiguous")
            slots.add(number)
            button = _one(nodes, self.write_layout.row_button_node)
            if button.get("keyboard_focus") is True:
                rows.append({"slot": number, "nodes": nodes, "button": button})
        if len(rows) != 1:
            raise SaveSlotLoadError("save_selected_row_unverified")
        return rows[0]

    def _verify_target_row(self, screen) -> None:
        if not screen.save_list_open or screen.save_prompt_shown:
            raise SaveSlotLoadError("save_list_not_verified")
        row = self._row(screen)
        if row["slot"] != self.target:
            raise SaveSlotLoadError("save_selected_row_changed")
        empty_nodes = [n for n in row["nodes"] if n.get("name") == self.write_layout.empty_node and n.get("visible") is True]
        empty = len(empty_nodes) == 1 and empty_nodes[0].get("text") in self.write_layout.empty_texts
        if self.before is None:
            if self.write_layout.empty_node is None or not self.write_layout.empty_texts:
                raise SaveSlotLoadError("save_empty_row_contract_unvalidated")
            if not empty:
                raise SaveSlotLoadError("save_empty_row_unverified")
        else:
            if empty:
                raise SaveSlotLoadError("save_disk_ui_disagree")
            self._verify_row_time(row)

    def _verify_row_time(self, row: dict) -> None:
        nodes = row["nodes"]
        date = _one(nodes, "Txt_Date").get("text")
        clock = _one(nodes, "Txt_Time").get("text")
        stamp = self.target_path.stat().st_mtime
        saves = [SaveFile(self.target, str(self.target_path), stamp, self.account)]
        matches, basis = match_save_time(f"{date} {clock}", saves, self.layout.date_tolerance_seconds)
        if matches != [self.target] or basis != "date_time":
            raise SaveSlotLoadError("save_row_file_identity_unverified")

    def _open_save_list_from_main(self, main_screen):
        """R in the main menu opens the Save list directly (owner, live
        2026-10-08); otherwise go through Save / Load and its submenu."""
        self.press("save_shortcut", "open_save_list_shortcut")
        screen = self.wait_for(lambda s: s.save_list_open, MENU_OPEN_WAIT_SECONDS, "save_list")
        if screen is not None and screen.save_list_open:
            return screen
        if not self.in_menu():
            raise SaveSlotLoadError("save_menu_closed_unexpectedly")
        self._select_main_item(self.read("main_menu"), SAVE_LOAD_ITEM)
        return self._open_save_list()

    def _open_save_list(self):
        self.press("confirm", "open_save_load")
        # The submenu first shows "checking" while the game reads its saves
        # (live 2026-10-08); wait as long as the loader does.
        screen = self.wait_for(lambda s: s.save_load_choice is not None, MENU_OPEN_WAIT_SECONDS, "save_load")
        if screen is None or screen.save_load_choice is None:
            raise SaveSlotLoadError("save_load_choice_unreadable")
        if screen.save_load_choice == "load":
            self.press("up", "choose_save")
        screen = self.read("save_load")
        if screen.save_load_choice != "save":
            raise SaveSlotLoadError("save_choice_not_verified")
        self.press("confirm", "open_save_list")
        screen = self.wait_for(lambda s: s.save_list_open, MENU_OPEN_WAIT_SECONDS, "save_list")
        if screen is None or not screen.save_list_open:
            raise SaveSlotLoadError("save_list_not_verified")
        return screen

    def _highlight_save(self, screen):
        seen = set()
        # Step through observed rows; never predict the cursor from slot order.
        deadline = self.clock() + 60.0
        for _ in range(120):
            if self.clock() >= deadline:
                break
            self._guard()
            if not screen.save_list_open or screen.save_prompt_shown:
                raise SaveSlotLoadError("save_list_not_verified")
            number = self._row(screen)["slot"]
            if number == self.target:
                return screen
            if number in seen:
                break
            seen.add(number)
            self.press("down", "find_save_slot")
            screen = self.wait_for(lambda s: self._row(s)["slot"] != number, CHANGE_WAIT_SECONDS, "save_list")
            if screen is None:
                break
        raise SaveSlotLoadError("save_target_row_unavailable")

    def _dialog(self, screen) -> tuple[dict, dict]:
        dialogs = screen.response.get("save_dialogs")
        if (screen.text_cap_reached or not screen.save_prompt_shown or screen.load_prompt_shown or "load_list" in screen.signals or not isinstance(dialogs, list)
                or len(dialogs) != 1):
            raise SaveSlotLoadError("save_prompt_unverified")
        nodes = self._nodes(dialogs[0])
        if self.write_layout.prompt_slot_node:
            if _slot(_one(nodes, self.write_layout.prompt_slot_node).get("text")) != self.target:
                raise SaveSlotLoadError("save_prompt_target_changed")
        elif not any(n.get("visible") is True and n.get("text") in SAVE_PROMPT_TEXTS for n in nodes):
            # No slot in the prompt: it must at least be the Save question.
            raise SaveSlotLoadError("save_prompt_unverified")
        yes, no = _one(nodes, self.write_layout.yes_node), _one(nodes, self.write_layout.no_node)
        for button, text in ((yes, self.write_layout.yes_text), (no, self.write_layout.no_text)):
            if text and not any(n.get("visible") is True and n.get("text") == text and _within(n, button)
                                for n in nodes):
                raise SaveSlotLoadError("save_confirmation_button_text_changed")
        # Live 2026-10-08 the prompt's buttons never report keyboard focus;
        # the selected one's inner EndButton reports hovered instead. Exactly
        # one of the two buttons may own the focused/hovered node.
        marked = [n for n in nodes if n.get("visible") is True
                  and (n.get("keyboard_focus") is True or n.get("hovered") is True)]
        owners = {name for name, button in (("yes", yes), ("no", no))
                  for n in marked if _within(n, button)}
        if len(owners) != 1 or any(not (_within(n, yes) or _within(n, no)) for n in marked):
            raise SaveSlotLoadError("save_confirmation_selection_unverified")
        selected = owners.pop()
        # Callers read keyboard_focus as "selected" on the two button nodes.
        return ({**yes, "keyboard_focus": selected == "yes"},
                {**no, "keyboard_focus": selected == "no"})

    def run(self, slot: Any = None, execute: bool = False, *, calibrate_prompt: dict | None = None,
            test_save: bool = False, probe_prompt: bool = False, story_save: bool = False) -> dict[str, Any]:
        report = {"ok": False, "execute": execute, "saved": False, "presses": self.presses, "screens": self.screens}
        lease = None
        try:
            if (type(execute) is not bool or type(test_save) is not bool or type(probe_prompt) is not bool
                    or type(story_save) is not bool):
                raise SaveSlotLoadError("save_execute_must_be_boolean")
            if test_save and story_save:
                raise SaveSlotLoadError("save_test_and_story_are_exclusive")
            # An operator-designated test slot (owner, 2026-10-08: slot 10)
            # may be written for development only; it never becomes the
            # run's slot and never credits campaign wins.
            self.test_save = test_save
            if test_save:
                try:
                    test_slot = int(os.environ.get("FF7R_TEST_SAVE_SLOT", ""))
                except ValueError:
                    test_slot = None
                if test_slot is None or test_slot in {0, 14} or slot != test_slot:
                    raise SaveSlotLoadError("save_test_slot_not_designated")
                report["test_save"] = True
            # An operator-designated story slot (owner, 2026-10-20: slot 17,
            # the Story Playthrough) holds a story run outside any campaign:
            # like the test slot, it is never a run's slot and never credits
            # a win, but it may be written whatever the stream mode.
            self.story_save = story_save
            if story_save:
                try:
                    story_slot = int(os.environ.get("FF7R_STORY_SAVE_SLOT", ""))
                except ValueError:
                    story_slot = None
                if story_slot is None or story_slot in {0, 14} or slot != story_slot:
                    raise SaveSlotLoadError("save_story_slot_not_designated")
                report["story_save"] = True
            if probe_prompt and (not execute or calibrate_prompt is not None):
                raise SaveSlotLoadError("save_prompt_probe_requires_execute")
            self.layout = load_layout()
            if calibrate_prompt is not None:
                if (not isinstance(calibrate_prompt, dict)
                        or set(calibrate_prompt) not in ({"slot_node", "row_button_node"}, {"slot_node", "row_button_node", "row_selection"})
                        or any(not isinstance(calibrate_prompt.get(k), str) or not calibrate_prompt[k].strip() for k in ("slot_node", "row_button_node"))
                        or calibrate_prompt["slot_node"] == calibrate_prompt["row_button_node"]):
                    raise SaveSlotLoadError("save_calibration_row_contract_invalid")
                if "row_selection" in calibrate_prompt:
                    validate_contract(calibrate_prompt["row_selection"])
                # No prompt selectors are guessed or usable in this mode.
                self.write_layout = SaveWriteLayout(**calibrate_prompt, empty_node=None,
                    empty_texts=(), prompt_slot_node="", yes_node="", no_node="")
                report["calibration"] = True
            else:
                self.write_layout = self.write_layout or SaveWriteLayout.configured()
            self.runs = self.service._queen_blood_runs
            if self.runs is None:
                raise SaveSlotLoadError("save_ownership_tracking_unavailable")
            with self.runs.mutation_lock() as lease:
                self.initial = self.service.get_queens_blood_run_status()
                loaded = self.initial.get("loaded_save")
                assigned = self.initial.get("assignment")
                # Drivers are declared on live and rehearsal streams; a story
                # run (offline) needs only the verified loaded save.
                if ((not self.initial.get("driver") and not self.story_save)
                        or not loaded or not self.initial.get("loaded_save_verified")):
                    raise SaveSlotLoadError("save_requires_verified_loaded_slot_and_driver")
                if assigned and not self.initial.get("run_loaded"):
                    raise SaveSlotLoadError("save_model_run_not_loaded")
                self.process = process_identity(self.service._environment)
                if not self.process or loaded.get("process") != self.process:
                    raise SaveSlotLoadError("save_process_unverified")
                self.target = assigned["slot"] if slot is None and assigned else slot
                # The same range run assignment accepts: the game has more
                # than 15 manual slots (a live run was assigned slot 16, #262).
                if type(self.target) is not int or not 1 <= self.target <= 999 or self.target == 14:
                    raise SaveSlotLoadError("save_requires_manual_nonreserved_slot")
                # Only an explicitly loaded account may be written, never the newest account.
                save_fingerprint(loaded)
                self.account = loaded["steam_id"]
                directory = Path(loaded["path"]).parent
                if directory.is_symlink() or directory.resolve() != directory:
                    raise SaveSlotLoadError("save_account_path_unsafe")
                self.target_path = directory / f"ff7rebirth{self.target:03}.sav"
                self.before = file_proof(self.target_path)
                self.protected = self._protected_files()
                if calibrate_prompt is not None and (self.target != 15 or not assigned
                        or assigned.get("slot") != 15 or loaded.get("slot") != 15
                        or self.before is None
                        or self.runs.owner(self.account, 15) != model_key(self.initial["driver"])):
                    raise SaveSlotLoadError("save_calibration_requires_owned_loaded_slot_15")
                self._guard()
                report.update(slot=self.target, new_slot=self.before is None)
                self._focus()
                self._require_field()
                screen = self._highlight_save(self._open_save_list_from_main(self._open_main_menu()))
                self._guard()
                row_screen = self.read("save_list")
                self._verify_target_row(row_screen)
                if calibrate_prompt is not None:
                    report["row_contract_evidence"] = row_screen.response
                if not execute:
                    report.update(ok=True, state="dry_run_highlighted", closed=self.close_save_ui("save_dry_run_close"))
                    report["ok"] = report["closed"]
                    return report
                # Preserve a verified copy before the game may overwrite our slot.
                if self.before is not None:
                    backup = self.runs.root / "save-backups" / uuid4().hex / self.target_path.name
                    backup.parent.mkdir(parents=True)
                    backup.write_bytes(self.target_path.read_bytes())
                    if file_proof(backup)["sha256"] != self.before["sha256"]:
                        raise SaveSlotLoadError("save_backup_verification_failed")
                    report["backup"] = str(backup)
                self._guard()
                self._verify_target_row(self.read("before_save_prompt"))
                self._guard()
                # Row confirmation may itself write (for example an empty
                # slot with no overwrite prompt). Retain uncertainty from
                # the first potentially saving input, not only from Yes.
                lease["retain"] = True
                _write(self.runs.root / "save-operation-lock.json", {**lease, "phase": "opening_save_prompt",
                       "slot": self.target, "steam_id": self.account, "driver": self._driver_key(),
                       "before": self.before, "backup": report.get("backup"), "calibration": calibrate_prompt is not None,
                       "review_binding": {"initial": self.initial, "process": self.process, "protected": self.protected}})
                self._guard()
                final_row = self.read("final_save_row_after_file_guards")
                self._verify_target_row(final_row)
                if calibrate_prompt is not None:
                    report["final_row_contract_evidence"] = final_row.response
                    lock_path = self.runs.root / "save-operation-lock.json"
                    durable = json.loads(lock_path.read_text(encoding="utf-8"))
                    durable["final_row_contract"] = {"contract": calibrate_prompt, "response": final_row.response}
                    _write(lock_path, durable)
                self._guard_confirmation()
                self.committed = True
                self.press("confirm", "open_save_prompt")
                if calibrate_prompt is not None:
                    # This is the only potentially writing input in calibration.
                    # Capture before any subsequent input; never select/press Yes.
                    screen = self.read("save_calibration_after_row_confirm")
                    report["raw_save_evidence"] = {key: screen.response.get(key)
                        for key in ("save_cells", "save_dialogs")}
                    report["raw_save_menu"] = screen.response
                    if len(screen.response.get("save_dialogs") or []) == 1:
                        durable = json.loads(lock_path.read_text(encoding="utf-8"))
                        durable["observed_calibration_prompt"] = screen.response["save_dialogs"][0]
                        _write(lock_path, durable)
                    report["capture"] = self.service.capture_game_window()
                    if not isinstance(report["capture"], dict) or report["capture"].get("ok") is not True:
                        raise SaveSlotLoadError("save_calibration_capture_failed")
                    report.update(state="save_calibration_uncertain", requires_review=True)
                    if screen.response.get("save_dialogs") or screen.save_prompt_shown or screen.load_prompt_shown:
                        report.update(ok=True, state="save_calibration_prompt_observed")
                        return report
                if calibrate_prompt is None:
                    prompt = self.wait_for(lambda s: s.save_prompt_shown or s.load_prompt_shown,
                                           CHANGE_WAIT_SECONDS, "save_prompt")
                    yes, no = self._dialog(prompt) if prompt is not None else ({}, {})
                    if no.get("keyboard_focus") is True:
                        self.press("up", "select_verified_save_yes")
                    self._guard()
                    yes, no = self._dialog(self.read("before_save_confirm"))
                    if yes.get("keyboard_focus") is not True or no.get("keyboard_focus") is True:
                        raise SaveSlotLoadError("save_yes_not_verified")
                    self._guard()
                    if probe_prompt:
                        # Calibration: show Yes selected for a screenshot,
                        # then back out with Cancel; never press Yes.
                        report["probe_evidence"] = self.last_save_evidence
                        report["capture"] = self.service.capture_game_window()
                        self.press("cancel", "probe_cancel_save_prompt")
                        screen = self.wait_for(lambda s: s.save_list_open and not s.save_prompt_shown,
                                               CHANGE_WAIT_SECONDS, "save_list")
                        if screen is None:
                            raise SaveSlotLoadError("save_probe_cancel_unverified")
                        if file_proof(self.target_path) != self.before:
                            raise SaveSlotLoadError("save_probe_changed_the_save")
                        self._check_protected_files()
                        self.committed = False
                        lease["retain"] = False
                        report.update(ok=True, state="prompt_probed", requires_review=False,
                                      closed=self.close_save_ui("save_probe_close"))
                        return report
                    # Keep the retained lock through the separately verified Yes.
                    # No automatic retry, Esc, or assignment is safe until reviewed.
                    lease["retain"] = True
                    _write(self.runs.root / "save-operation-lock.json", {**lease, "phase": "confirming_save",
                           "slot": self.target, "steam_id": self.account, "driver": self._driver_key(),
                           "before": self.before, "backup": report.get("backup"), "calibration": calibrate_prompt is not None,
                       "review_binding": {"initial": self.initial, "process": self.process, "protected": self.protected}})
                    # The earlier Yes snapshot predates hashing and durable
                    # lock publication. Re-read it after both; a moved cursor,
                    # changed target or stale frame must never receive Confirm.
                    yes, no = self._dialog(self.read("final_save_prompt_after_file_guards"))
                    if yes.get("keyboard_focus") is not True or no.get("keyboard_focus") is True:
                        raise SaveSlotLoadError("save_yes_not_verified")
                    report["confirmation_evidence"] = self.last_save_evidence
                    self._guard_confirmation()
                    self.committed = True
                    self.press("confirm", "save_prompt:verified_yes")
                deadline, stable, proof = self.clock() + 30.0, 0, None
                while self.clock() < deadline:
                    if process_identity(self.service._environment) != self.process:
                        raise SaveSlotLoadError("game_exited_or_changed_during_save")
                    if calibrate_prompt is not None:
                        screen = self.read("save_calibration_observe_only")
                        report["raw_save_evidence"] = {key: screen.response.get(key)
                            for key in ("save_cells", "save_dialogs")}
                        if screen.response.get("save_dialogs") or screen.save_prompt_shown or screen.load_prompt_shown:
                            report["raw_save_menu"] = screen.response
                            report["capture"] = self.service.capture_game_window()
                            if not isinstance(report["capture"], dict) or report["capture"].get("ok") is not True:
                                raise SaveSlotLoadError("save_calibration_capture_failed")
                            report.update(ok=True, state="save_calibration_prompt_observed", requires_review=True)
                            return report
                    after = file_proof(self.target_path)
                    if (after and after["size"] > 0
                            and (self.before is None or after["sha256"] != self.before["sha256"])):
                        stable = stable + 1 if after == proof else 1
                        proof = after
                        screen = self.read("save_write_completion")
                        if stable >= 2 and screen.save_list_open and not screen.save_prompt_shown:
                            row = self._row(screen)
                            if row["slot"] != self.target:
                                raise SaveSlotLoadError("save_completion_row_changed")
                            self._verify_row_time(row)
                            self._check_protected_files()
                            if process_identity(self.service._environment) != self.process or file_proof(self.target_path) != proof:
                                raise SaveSlotLoadError("save_completion_identity_changed")
                            current = self.service.get_queens_blood_run_status()
                            if (not current.get("loaded_save_verified")
                                    or (self.initial.get("assignment") and not current.get("run_loaded"))
                                    or self.runs.owner(self.account, self.target) not in
                                       (None, self._driver_key())):
                                raise SaveSlotLoadError("save_identity_changed_after_write")
                            if current != self.initial:
                                # Public driver state contains dynamic heartbeats; compare identities only.
                                if any(current.get(k) != self.initial.get(k) for k in ("driver", "assignment", "loaded_save")):
                                    raise SaveSlotLoadError("save_identity_changed_after_write")
                            file = {"slot": self.target, "steam_id": self.account, "path": str(self.target_path)}
                            if self.test_save:
                                # A test slot never becomes the run's slot.
                                report["receipt"] = {"test_save": True, "slot": self.target}
                            elif self.story_save:
                                report["receipt"] = {"story_save": True, "slot": self.target}
                            else:
                                report["receipt"] = self.runs.record_verified_save(self.initial["driver"], file, self.process)
                            lease["retain"] = False
                            report.update(ok=True, saved=True, state="saved", file_proof=proof, requires_review=False)
                            try:
                                report["closed"] = False if calibrate_prompt is not None else self.close_save_ui("save_complete_close")
                            except Exception as exc:
                                report.update(closed=False, close_error=str(exc))
                            return report
                    else:
                        # Stability means consecutive reads. A missing,
                        # empty or reverted file breaks the previous proof.
                        stable, proof = 0, None
                    self.sleep(0.5)
                raise SaveSlotLoadError("save_write_unverified")
        except Exception as exc:
            report.update(ok=False, decision=exc.code if isinstance(exc, SaveSlotLoadError) else str(exc),
                          state="save_uncertain" if self.committed else "save_refused",
                          requires_review=bool((lease and lease["retain"]) or
                              (getattr(self, "runs", None) is not None and
                               (self.runs.root / "save-calibration-resolution-lock.json").exists())),
                          input_sent=bool(self.presses))
            report.update(save_evidence=self.last_save_evidence,
                          details=exc.details if isinstance(exc, SaveSlotLoadError) else {})
            # Leave unknown UI alone. Only normal dry-run/success closes menus.
            return report


class SaveCalibrationResolver(SaveSlotWriter):
    """Reviewed cancellation only; retained legacy locks are never adopted."""
    def run(self, *, expected_token: str, expected_sha256: str, execute: bool = False, cancel_review: dict | None = None) -> dict:
        report = {"ok": False, "saved": False, "requires_review": True, "presses": self.presses}
        try:
            if type(execute) is not bool or not isinstance(expected_token, str) or not re.fullmatch(r"[0-9a-f]{32}", expected_token):
                raise SaveSlotLoadError("save_resolution_contract_invalid")
            if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
                raise SaveSlotLoadError("save_resolution_contract_invalid")
            expected_sha256 = expected_sha256.lower()
            self.runs = self.service._queen_blood_runs
            path = self.runs.root / "save-operation-lock.json"
            if path.is_symlink() or not path.is_file():
                raise SaveSlotLoadError("save_resolution_lock_invalid")
            original = path.read_bytes()
            lock = json.loads(original)
            def exact_lock():
                if path.is_symlink() or not path.is_file() or path.read_bytes() != original:
                    raise SaveSlotLoadError("save_resolution_lock_changed")
            if (hashlib.sha256(original).hexdigest() != expected_sha256 or lock.get("token") != expected_token
                    or lock.get("version") != 1 or lock.get("retain") is not True
                    or lock.get("calibration") is not True or lock.get("slot") != 15
                    or lock.get("phase") != "opening_save_prompt" or not isinstance(lock.get("review_binding"), dict)):
                raise SaveSlotLoadError("save_resolution_lock_unreviewable")
            binding = lock["review_binding"]
            self.initial, self.process, self.protected = binding["initial"], binding["process"], binding["protected"]
            self.target, self.account, self.before = 15, lock["steam_id"], lock["before"]
            loaded, assigned = self.initial["loaded_save"], self.initial["assignment"]
            if (not self.initial.get("run_loaded") or not self.initial.get("loaded_save_verified")
                    or loaded.get("slot") != 15 or assigned.get("slot") != 15
                    or loaded.get("steam_id") != self.account or assigned.get("steam_id") != self.account
                    or self.runs.owner(self.account, 15) != model_key(self.initial["driver"])
                    or lock.get("driver") != model_key(self.initial["driver"])):
                raise SaveSlotLoadError("save_resolution_requires_owned_loaded_15")
            self.target_path = Path(loaded["path"])
            if self.target_path.name != "ff7rebirth015.sav" or self.target_path.is_symlink():
                raise SaveSlotLoadError("save_resolution_path_invalid")
            backup = Path(lock["backup"])
            if file_proof(backup)["sha256"] != self.before["sha256"]:
                raise SaveSlotLoadError("save_resolution_backup_changed")
            self.layout = load_layout()
            review_raws, review_paths = {}, {}
            if cancel_review is not None:
                from .save_cancel_review import load_review, verify_prompt
                contract, self._save_list_id, prompt_id, original_cell_id, review_raws, review_paths = load_review(cancel_review, lock)
                self.write_layout = SaveWriteLayout(**contract, empty_node=None, empty_texts=(), prompt_slot_node="",yes_node="",no_node="")
                report["proof_limit"] = ("reviewed_source_final_read_attestation; original raw row is initial highlight, not final frame" if review_raws else "durable final raw row and observed prompt")
            else:
                self.write_layout = SaveWriteLayout.configured()
            claim_raw = None
            def exact_claim():
                if claim_raw is not None and (claim.is_symlink() or not claim.is_file() or claim.read_bytes() != claim_raw):
                    raise SaveSlotLoadError("save_resolution_claim_changed")
            def guard():
                exact_lock()
                exact_claim()
                for key, review_path in review_paths.items():
                    if review_path.is_symlink() or review_path.read_bytes() != review_raws[key]:
                        raise SaveSlotLoadError("save_cancel_review_changed")
                if file_proof(backup)["sha256"] != self.before["sha256"]:
                    raise SaveSlotLoadError("save_resolution_backup_changed")
                self._guard()
                if not self.service.get_queens_blood_run_status().get("run_loaded"):
                    raise SaveSlotLoadError("save_resolution_run_not_loaded")
            claim = self.runs.root / "save-calibration-resolution-lock.json"
            if claim.exists():
                raise SaveSlotLoadError("save_resolution_requires_review_or_is_running")
            guard()
            screen = self.read("resolution_prompt")
            if cancel_review is not None:
                verify_prompt(screen, prompt_id)
            else:
                yes, no = self._dialog(screen)
            report.update(state="save_resolution_preflight", expected_token=expected_token,
                          expected_sha256=expected_sha256, evidence=self.last_save_evidence)
            if not execute:
                report["ok"] = True
                return report
            claim_raw = (json.dumps({"token": expected_token, "lock_sha256": expected_sha256,
                                     "resolution_id": uuid4().hex}) + "\n").encode("utf-8")
            with claim.open("xb") as stream:
                stream.write(claim_raw)
            self._focus()
            guard()
            if cancel_review is not None:
                guard()
                verify_prompt(self.read("resolution_final_cancel_prompt"), prompt_id)
                self._guard_identity()
                self._guard_file_stats()
                exact_lock();exact_claim()
                self.press("cancel", "resolution_single_held_cancel", milliseconds=500)
                # No retry and no menu cleanup before the same selected15 reappears.
                after_cancel = self.wait_for(lambda s: not s.response.get("save_dialogs") and not s.save_prompt_shown and s.save_list_open,
                                             CHANGE_WAIT_SECONDS, "resolution_after_held_cancel")
                if after_cancel is None or after_cancel.response.get("save_dialogs") or after_cancel.save_prompt_shown or not after_cancel.save_list_open:
                    raise SaveSlotLoadError("save_resolution_prompt_not_cancelled")
                self._verify_target_row(after_cancel)
                restored_cell, _ = selected_cell(after_cancel.response, contract["row_selection"])
                if restored_cell["full_id"] != original_cell_id:
                    raise SaveSlotLoadError("save_cancel_original_cell_changed")
            else:
                # A single directional choice may move focus, but only freshly
                # observed No authorizes Confirm. Never confirm Yes or retry.
                screen = self.read("resolution_fresh_prompt")
                yes, no = self._dialog(screen)
                if no.get("keyboard_focus") is not True:
                    guard()
                    self.press("down", "resolution_select_no")
                guard()
                yes, no = self._dialog(self.read("resolution_verify_no"))
                if no.get("keyboard_focus") is not True or yes.get("keyboard_focus") is True:
                    raise SaveSlotLoadError("save_resolution_no_not_focused")
                guard()
                # Disk/identity proof can take time. No is freshly observed after
                # that work; no further lengthy proof may stale this UI decision.
                yes, no = self._dialog(self.read("resolution_final_no"))
                if no.get("keyboard_focus") is not True or yes.get("keyboard_focus") is True:
                    raise SaveSlotLoadError("save_resolution_no_not_focused")
                exact_lock()
                exact_claim()
                self.press("confirm", "resolution_confirm_verified_no")
            # Close only known save UI after the verified cancellation. Never
            # confirm again, and never close an unknown active dialog.
            for _ in range(6):
                guard()
                screen = self.read("resolution_close_verify")
                if screen.response.get("save_dialogs") or screen.save_prompt_shown or screen.load_prompt_shown:
                    raise SaveSlotLoadError("save_resolution_prompt_not_cancelled")
                if not self._save_ui_visible(screen) and not self.in_menu():
                    break
                if not self._save_ui_visible(screen):
                    raise SaveSlotLoadError("save_resolution_unknown_menu")
                guard()
                screen = self.read("resolution_fresh_close_verify")
                if (screen.response.get("save_dialogs") or screen.save_prompt_shown or screen.load_prompt_shown
                        or not self._save_ui_visible(screen)):
                    raise SaveSlotLoadError("save_resolution_close_ui_changed")
                exact_lock()
                exact_claim()
                self.press("menu", "resolution_close_known_menu")
            else:
                raise SaveSlotLoadError("save_resolution_ui_not_closed")
            for _ in range(2):
                guard()
                screen = self.read("resolution_stable_field")
                observation = self.service.observe()
                health = self.service.session_health()
                if (self._save_ui_visible(screen) or screen.response.get("save_dialogs")
                        or observation.get("in_menu") is not False
                        or (observation.get("telemetry") or {}).get("live") is not True
                        or health.get("loaded_gameplay") is not True or health.get("in_menu") is not False
                        or health.get("paused") or health.get("in_combat")):
                    raise SaveSlotLoadError("save_resolution_ui_not_closed")
                self.sleep(0.5)
            guard()
            archive = self.runs.root / "save-operation-reviews" / (expected_token + ".json")
            archive.parent.mkdir(exist_ok=True)
            with archive.open("x", encoding="utf-8") as stream:
                json.dump({"lock": lock, "lock_sha256": expected_sha256,
                           "lock_raw_base64": base64.b64encode(original).decode("ascii"),
                           "claim_raw_base64": base64.b64encode(claim_raw).decode("ascii"),
                           "cancel_review_artifacts": {key: {"sha256": hashlib.sha256(raw).hexdigest(), "raw_base64":base64.b64encode(raw).decode("ascii")} for key,raw in review_raws.items()},
                           "cancel_review_config": cancel_review,"proof_limit": report.get("proof_limit"),
                           "resolution": "verified_cancelled_unchanged", "screens": self.screens, "presses": self.presses}, stream)
            archived = archive.read_bytes()
            proof = json.loads(archived)
            if (base64.b64decode(proof["lock_raw_base64"]) != original
                    or base64.b64decode(proof["claim_raw_base64"]) != claim_raw
                    or proof["lock_sha256"] != expected_sha256):
                raise SaveSlotLoadError("save_resolution_archive_unverified")
            for key, raw in review_raws.items():
                entry = proof["cancel_review_artifacts"][key]
                if base64.b64decode(entry["raw_base64"]) != raw or entry["sha256"] != hashlib.sha256(raw).hexdigest():
                    raise SaveSlotLoadError("save_resolution_archive_unverified")
            guard()
            if archive.is_symlink() or archive.read_bytes() != archived:
                raise SaveSlotLoadError("save_resolution_archive_changed")
            path.unlink()
            claim.unlink()
            report.update(ok=True, state="save_calibration_cancelled", requires_review=False, archive=str(archive))
        except Exception as exc:
            report.update(state="save_resolution_refused", decision=exc.code if isinstance(exc, SaveSlotLoadError) else str(exc))
        return report
