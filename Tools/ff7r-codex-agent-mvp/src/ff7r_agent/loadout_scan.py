"""Bounded cursor inspection of a virtualized loadout inventory.

Widget identities can be recycled: each row is evidence at a logical menu
index, not a persistent owned-copy ID. Only Up/Down are ever sent. The caller
uses field_menu_async; this job leaves the menu and control session open.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable

from .loadout_menu import read_loadout_menu


SCAN_STAGES = frozenset({"member", "equipment_inventory", "materia_inventory"})


class ScanStopped(RuntimeError):
    pass


def scan_loadout_choices(service: Any, stage: str = "materia_inventory", execute: bool = False,
                         max_choices: int = 64, timeout_seconds: float = 90, *,
                         read: Callable[[], dict[str, Any]] | None = None,
                         clock: Callable[[], float] | None = None,
                         sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Inspect each menu index, stopping on exit, interruption or changed data.

    Requires the named list already open. A plan sends nothing; execution
    navigates to index zero then through the list once, without wrapping.
    """
    if type(execute) is not bool:
        raise ValueError("execute must be a boolean")
    if not isinstance(stage, str) or stage not in SCAN_STAGES:
        raise ValueError("stage must be member, equipment_inventory or materia_inventory")
    if type(max_choices) is not int or not 1 <= max_choices <= 64:
        raise ValueError("max_choices must be an integer from 1 to 64")
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 1 <= timeout_seconds <= 120):
        raise ValueError("timeout_seconds must be 1 to 120")
    clock = clock or getattr(service, "_monotonic", time.monotonic)
    sleep = sleep or getattr(service, "_sleeper", time.sleep)
    deadline = clock() + timeout_seconds
    read = read or (lambda: read_loadout_menu(service, raw=True, stage=stage,
                                              wait_seconds=min(3, max(0, deadline - clock()))))
    rows: dict[int, dict[str, Any]] = {}
    presses: list[str] = []
    context: dict[str, Any] = {}
    sequence = -1
    count = None
    accepted_inputs = 0
    input_uncertain = False

    def capture() -> dict[str, Any]:
        nonlocal sequence, count
        if deadline - clock() < 0.5:
            raise ScanStopped("loadout_scan_expired")
        value = read()
        if value.get("ok") is not True:
            raise ScanStopped(str(value.get("reason") or "loadout_capture_failed"))
        selection = value.get("selection")
        if not isinstance(selection, dict) or selection.get("selection_identity_observed") is not True:
            raise ScanStopped("loadout_selection_unobserved")
        owner = (value.get("loadout_scope") or {}).get("owner_id")
        capture_context = value.get("capture") or {}
        identity = {"owner_id": owner, "list_id": selection.get("list_id"),
                    "map_id": capture_context.get("map_id"), "source": capture_context.get("source")}
        if not all(isinstance(v, str) and v for v in identity.values()):
            raise ScanStopped("loadout_scan_identity_unobserved")
        if context and identity != context:
            raise ScanStopped("loadout_scan_context_changed")
        context.update(identity)
        current_sequence = capture_context.get("source_sequence")
        if type(current_sequence) is not int or current_sequence <= sequence:
            raise ScanStopped("loadout_scan_capture_not_advanced")
        sequence = current_sequence
        raw_count = (selection.get("properties") or {}).get("ItemCount")
        if not isinstance(raw_count, str) or not raw_count.isascii() or not raw_count.isdigit():
            raise ScanStopped("loadout_choice_count_unobserved")
        observed_count = int(raw_count)
        if not 1 <= observed_count <= max_choices:
            raise ScanStopped("loadout_choice_budget")
        if count is not None and observed_count != count:
            raise ScanStopped("loadout_choice_count_changed")
        count = observed_count
        index = selection.get("selected_index")
        if type(index) is not int or not 0 <= index < count or not selection.get("selected_child_id"):
            raise ScanStopped("loadout_cursor_invalid")
        return value

    def row(value: dict[str, Any]) -> dict[str, Any]:
        selected = value["selection"]
        info = []
        owner_path = context["owner_id"].partition(" ")[2]
        info_prefix = owner_path + ".WidgetTree.EquipInfo.WidgetTree."
        for text in (value.get("evidence") or {}).get("texts") or []:
            path = str(text.get("full_id", "")).partition(" ")[2]
            if text.get("rendered") is True and path.startswith(info_prefix):
                suffix = path[len(info_prefix):]
                if suffix in ("Txt_Title", "Materia_Ap.WidgetTree.Txt_ApInfo", "Materia_Level.WidgetTree.Txt_Lv"):
                    info.append({"field": suffix, "text": text.get("text")})
        return {"index": selected["selected_index"], "widget_id_at_capture": selected["selected_child_id"],
                "texts": [dict(t) for t in selected.get("selected_texts") or [] if t.get("rendered") is True],
                "info": info, "source_sequence": value["capture"]["source_sequence"]}

    def remember(value: dict[str, Any]) -> None:
        item = row(value)
        # Text widgets have no copy identity. Compare their field/text values,
        # since virtualized rows may use another widget when revisited.
        def content(item: dict[str, Any]) -> tuple[Any, ...]:
            return tuple(sorted((str(t.get("path", "")).split(".WidgetTree.", 1)[-1], str(t.get("text", "")))
                                for t in item["texts"]))
        previous = rows.get(item["index"])
        if previous is not None and content(previous) != content(item):
            raise ScanStopped("loadout_choices_changed")
        if not item["texts"]:
            raise ScanStopped("loadout_choice_text_unobserved")
        rows[item["index"]] = item

    def move(value: dict[str, Any], direction: str, wanted: int) -> dict[str, Any]:
        nonlocal accepted_inputs, input_uncertain
        if clock() >= deadline:
            raise ScanStopped("loadout_scan_expired")
        previous = value["selection"]["selected_index"]
        # Once an input is attempted its outcome is never replayed here.
        presses.append(direction)
        try:
            receipt = service.tap(direction, 120)
        except Exception:
            input_uncertain = True
            raise
        if not isinstance(receipt, dict) or receipt.get("ok") is not True:
            input_uncertain = not isinstance(receipt, dict) or receipt.get("ok") is not False
            context["input_refusal"] = receipt
            raise ScanStopped("loadout_input_unverified" if input_uncertain else "loadout_input_refused")
        accepted_inputs += 1
        settle = min(deadline, clock() + 3)
        for _ in range(6):
            value = capture()
            index = value["selection"]["selected_index"]
            if index == wanted:
                remember(value)
                return value
            if index != previous:
                raise ScanStopped("loadout_cursor_unexpected")
            if clock() + 0.5 >= settle:
                break
            sleep(0.5)
        raise ScanStopped("loadout_cursor_stalled")

    reason = "loadout_choices_scanned"
    complete = False
    start_index = None
    try:
        screen = capture()
        start_index = screen["selection"]["selected_index"]
        if not execute:
            return {"ok": True, "execute": False, "input_sent": False, "complete": False,
                    "stage": stage, "context": context, "item_count": count,
                    "cursor_moves_at_most": start_index + count - 1,
                    "reason": "loadout_scan_plan", "persistent_copy_ids": False}
        remember(screen)
        index = start_index
        while index > 0:
            index -= 1
            screen = move(screen, "up", index)
        while index < count - 1:
            index += 1
            screen = move(screen, "down", index)
        complete = len(rows) == count
    except ScanStopped as error:
        reason = str(error)
    except Exception as error:
        # Preserve partial evidence; a tap exception may still have landed.
        reason = "loadout_scan_interrupted"
        context["error_type"] = type(error).__name__
        context["error"] = str(error)[:512]
    return {"ok": complete, "execute": execute,
            "input_sent": True if accepted_inputs else None if input_uncertain else False,
            "input_attempted": bool(presses), "input_outcome_unknown": input_uncertain,
            "accepted_inputs": accepted_inputs, "stage": stage,
            "complete": complete, "reason": reason, "context": context, "item_count": count,
            "start_index": start_index, "choices": [rows[i] for i in sorted(rows)],
            "presses": presses, "persistent_copy_ids": False, "loadout_changed": False}
