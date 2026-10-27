"""On-demand menu evidence for #410, reusing the field-menu probe from #406.

One menu-scoped request, with no polling thread or inputs. Full widget
identities survive this reader; they are not inventory-copy IDs or proof of
equipment ownership, slot topology, eligibility or an applied change.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from typing import Any

from .field_menu import _short, parse_field_menu
from .map_query import request_snapshot
from .navigation_values import MissionError
from .telemetry import TelemetryError

LOADOUT_MENU_FLAGS = ("loadoutmenu",)
# The same scoped read, owned by the Combat Settings screen (shortcuts, party).
SCREEN_FLAGS = {"equipment": LOADOUT_MENU_FLAGS, "battle_setting": ("battlesettingmenu",),
                "member_change": ("memberchangemenu",)}
SELECTION_LISTS = {
    "member": ".WidgetTree.MemberList.WidgetTree.SelectList",
    "equipment_category": ".WidgetTree.Equip_CategoryList.WidgetTree.EquipCategoryList",
    "equipment_inventory": ".WidgetTree.EquipList.WidgetTree.SelectList",
    "materia_inventory": ".WidgetTree.Materia_SelectList.WidgetTree.SelectList",
    "materia_weapon_slot": ".WidgetTree.Materia_List_All.WidgetTree.Materia_List1.WidgetTree.Materia_List_Box",
    "materia_armor_slot": ".WidgetTree.Materia_List_All.WidgetTree.Materia_List2.WidgetTree.Materia_List_Box",
}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "input_sent": False, "input_authorized": False,
            "loadout_available": False, "bindings_verified": False, "normalized_loadout": None}


def read_loadout_menu(service: Any, raw: bool = False, *, stage: str | None = None,
                      wait_seconds: float = 0, inspect_properties: bool = False,
                      include_choices: bool = False, screen: str = "equipment") -> dict[str, Any]:
    """Read once, or briefly await a named selector in an already open menu.

    ``screen`` scopes the read to Materia & Equipment (default), Combat
    Settings (``battle_setting``) or its Change Party Members window
    (``member_change``); named stages are equipment ones.

    The native cursor can be -1 while a menu animates. Waiting never opens a
    menu, retries a failed capture, or survives menu exit. These selector
    identities do not establish inventory-copy or equipped-item identities.
    """
    if type(raw) is not bool:
        raise ValueError("raw must be a boolean")
    if type(inspect_properties) is not bool:
        raise ValueError("inspect_properties must be a boolean")
    if type(include_choices) is not bool:
        raise ValueError("include_choices must be a boolean")
    if (inspect_properties or include_choices) and wait_seconds:
        raise ValueError("property/choice inspection must be a single capture, without waiting")
    if screen not in SCREEN_FLAGS:
        raise ValueError("screen must be equipment, battle_setting or member_change")
    if stage is not None and (screen != "equipment" or not isinstance(stage, str) or stage not in SELECTION_LISTS):
        raise ValueError("stage must name a supported loadout selector")
    if (type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds)
            or not 0 <= wait_seconds <= 3 or wait_seconds and stage is None):
        raise ValueError("wait_seconds must be 0 to 3 and requires a stage when nonzero")
    clock = getattr(service, "_monotonic", time.monotonic)
    deadline = clock() + wait_seconds
    attempts = 0
    owner = None
    while True:
        remaining = deadline - clock()
        # A capture has its own bounded response timeout. Do not begin another
        # when less than the probe's minimum 0.5-second timeout remains.
        capture_timeout = min(2.0, max(0.5, remaining)) if wait_seconds else 2.0
        result = _capture_loadout_menu(service, raw, timeout_seconds=capture_timeout,
                                       inspect_properties=inspect_properties, include_choices=include_choices,
                                       screen=screen)
        attempts += 1
        if stage is None:
            return result
        result["selection_wait"] = {"stage": stage, "attempts": attempts, "ready": False}
        if not result.get("ok"):
            return result
        current_owner = result["loadout_scope"]["owner_id"]
        if owner is not None and current_owner != owner:
            result.update(ok=False, reason="loadout_owner_changed")
            return result
        owner = current_owner
        discovery = result.get("list_discovery") or {}
        if any(discovery.get(key) is not False for key in ("scan_truncated", "records_truncated")):
            result.update(ok=False, reason="loadout_selection_incomplete")
            return result
        owner_path = owner.partition(" ")[2]
        expected_id = owner_path + SELECTION_LISTS[stage]
        selected = [row for row in result["selection_evidence"]
                    if str(row.get("list_id", "")).partition(" ")[2] == expected_id
                    and row["selection_identity_observed"]]
        if len(selected) > 1:
            result.update(ok=False, reason="loadout_selection_ambiguous")
            return result
        if selected:
            result["selection_wait"]["ready"] = True
            result["selection"] = selected[0]
            return result
        remaining = deadline - clock()
        if not wait_seconds or remaining <= 0.5 or attempts >= 6:
            result.update(ok=False, reason="loadout_selection_not_ready")
            return result
        service._sleeper(min(0.5, remaining - 0.5))


def _capture_loadout_menu(service: Any, raw: bool, *, timeout_seconds: float,
                          inspect_properties: bool = False, include_choices: bool = False,
                          screen: str = "equipment") -> dict[str, Any]:
    try:
        before = service.observe()
        if (before.get("source") in (None, "unknown", "simulation") or before.get("running") is not True
                or (before.get("telemetry") or {}).get("live") is not True):
            return _unavailable("loadout_telemetry_unavailable")
        if before.get("combat_state_observed") is not True or type(before.get("in_combat")) is not bool:
            return _unavailable("loadout_combat_state_unobserved")
        if before["in_combat"]:
            return _unavailable("loadout_field_menu_in_combat")
        if before.get("in_menu") is not True and before.get("paused") is not True:
            return _unavailable("loadout_menu_not_open")
        if type(before.get("source_sequence")) is not int:
            return _unavailable("loadout_source_sequence_unobserved")
        flags = SCREEN_FLAGS[screen]
        if inspect_properties:
            flags += ("loadoutinspect",)
        if include_choices:
            flags += ("loadoutchoices",)
        response, base = request_snapshot(service, heights=False, flags=flags,
                                          require_map=False, timeout_seconds=timeout_seconds)
    except (MissionError, TelemetryError) as error:
        return _unavailable(str(error))
    if response.get("texts_scope") != "menu":
        return _unavailable("loadout_menu_scope_unobserved")
    scope = response.get("loadout_scope")
    if not isinstance(scope, dict):
        scope = {}
    if (scope.get("observed") is not True or scope.get("active") is not True
            or scope.get("owner_still_attached") is not True or not scope.get("owner_id")):
        result = _unavailable("loadout_menu_scope_inactive")
        if raw:
            result["loadout_scope"] = scope
        return result
    if (type(response.get("source_sequence")) is not int
            or type(base.get("source_sequence")) is not int
            or response["source_sequence"] <= max(before["source_sequence"], base["source_sequence"])):
        return _unavailable("loadout_capture_not_advanced")
    try:
        stamp = datetime.fromisoformat(response["timestamp_utc"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 2.5:
            return _unavailable("loadout_capture_stale")
    except (KeyError, TypeError, ValueError, AttributeError):
        return _unavailable("loadout_capture_timestamp_invalid")
    # The raw records carry class, owners, full child ID and plain reflected
    # properties. field_menu._snapshot currently strips most of those fields.
    texts = [row for row in response.get("texts") or [] if isinstance(row, dict)]
    lists = [row for row in response.get("save_lists") or [] if isinstance(row, dict)]
    discovery = response.get("save_list_discovery")
    visible = []
    for row in lists:
        if row.get("visible") is not True:
            continue
        selected = row.get("selected_index")
        child = row.get("selected_child_id")
        visible.append({
            "list_id": row.get("id"), "class": row.get("class"), "owners": row.get("owners"),
            "user_index": row.get("user_index"), "selected_index": selected,
            "selected_child_id": child, "properties": row.get("properties"),
            "selected_nodes": row.get("selected_nodes"), "selected_node_walk": row.get("selected_node_walk"),
            "selected_texts": row.get("selected_texts"),
            "selected_property_types": row.get("selected_property_types"),
            "selected_properties": row.get("selected_properties"),
            "choices": row.get("choices"),
            "selection_identity_observed": row.get("user_index") == 0 and type(row.get("user_index")) is int
                and type(selected) is int and selected >= 0 and isinstance(child, str) and bool(child),
        })
    view = parse_field_menu(
        [(_short(str(row.get("path", ""))), str(row.get("text", ""))) for row in texts],
        [{**row, "owners": [str(owner).split(".")[-1] for owner in row.get("owners") or []]} for row in lists],
    )
    evidence = {"source": response.get("source"), "request_id": response.get("request_id"),
                "source_sequence": response.get("source_sequence"), "map_id": base.get("map_id"),
                "texts": texts, "lists": lists, "list_discovery": discovery}
    evidence["loadout_scope"] = scope
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {"ok": True, "input_sent": False, "input_authorized": False,
              "validation": "offline_candidate_live_bindings_pending", "evidence_sha256": digest,
              "capture": {k: evidence[k] for k in ("source", "request_id", "source_sequence", "map_id")},
              "scope": "unclassified_field_menu_evidence", "menu": view, "selection_evidence": visible,
              "list_discovery": discovery, "texts_at_cap": len(texts) >= 400,
              "loadout_scope": scope,
              "loadout_available": False, "bindings_verified": False, "normalized_loadout": None,
              "reason": "loadout_bindings_unverified"}
    if raw:
        result["evidence"] = evidence
    return result
