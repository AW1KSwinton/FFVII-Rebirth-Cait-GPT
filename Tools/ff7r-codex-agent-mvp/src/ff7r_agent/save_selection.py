"""Reviewed Save-list linkage; no input and no hover/date authority."""
import re
from .save_slot_load import SaveSlotLoadError

CONTRACT = {"list_class": "EndVirtualScrollBox", "widget_name": "SaveData_ScrollBox",
            "owner_class": "MainMenu_SaveData_Test_C", "world_path": "/Temp/Untitled_1.MenuRenderingWorld", "user_index": 0}

def validate_contract(value):
    if not isinstance(value, dict) or value != CONTRACT or type(value.get("user_index")) is not int:
        raise SaveSlotLoadError("save_list_contract_invalid")
    return dict(value)

def complete_nodes(cell):
    nodes, walk = cell.get("nodes"), cell.get("node_walk")
    if (not isinstance(nodes, list) or not nodes or len(nodes) > 128
            or any(not isinstance(n, dict) for n in nodes) or not isinstance(walk, dict)
            or walk.get("node_limit") != 128 or walk.get("truncated") is not False
            or type(walk.get("visited")) is not int or walk["visited"] != len(nodes)):
        raise SaveSlotLoadError("save_widgets_truncated_or_invalid")
    return nodes

def selected_cell(response, contract):
    validate_contract(contract)
    discovery = response.get("save_list_discovery")
    if (not isinstance(discovery, dict) or discovery.get("source_supported") is not True
            or discovery.get("scan_truncated") is not False or discovery.get("records_truncated") is not False
            or discovery.get("scan_limit") != 2048 or discovery.get("record_limit") != 24
            or not isinstance(discovery.get("classes"), list)
            or any(not isinstance(c, dict) for c in discovery["classes"])
            or {c.get("class") for c in discovery["classes"] if c.get("source_supported") is True}
               != {"EndListBox", "EndVirtualScrollBox"}):
        raise SaveSlotLoadError("save_list_discovery_incomplete")
    lists, cells = response.get("save_lists"), response.get("save_cells")
    if not isinstance(lists, list) or len(lists) > 24 or not isinstance(cells, list) or len(cells) >= 24:
        raise SaveSlotLoadError("save_list_link_unverified")
    world = contract["world_path"]
    pattern = re.compile(r"EndVirtualScrollBox " + re.escape(world) + r":(MainMenu_SaveData_Test_C_[0-9]+)\.WidgetTree\.SaveData_ScrollBox\Z")
    active = [entry for entry in lists if isinstance(entry, dict) and pattern.fullmatch(str(entry.get("id", ""))) and entry.get("visible") is True]
    if len(active) != 1:
        raise SaveSlotLoadError("save_list_link_unverified")
    entry = active[0]
    instance = pattern.fullmatch(entry["id"]).group(1)
    owner_path = world + ":" + instance
    expected_owners = ["WidgetTree " + owner_path + ".WidgetTree", "MainMenu_SaveData_Test_C " + owner_path,
                       "World " + world, "Package /Temp/Untitled_1"]
    full = entry.get("selected_child_id")
    if (entry.get("class") != "Class /Script/EndGame.EndVirtualScrollBox"
            or entry.get("discovery_class") != "EndVirtualScrollBox" or entry.get("owners") != expected_owners
            or type(entry.get("user_index")) is not int or entry["user_index"] != 0
            or type(entry.get("selected_index")) is not int or entry["selected_index"] < 0
            or not isinstance(full, str) or not re.fullmatch(r"SaveData_Cell_C " + re.escape(world) + r":SaveData_Cell_C_[0-9]+", full)
            or entry.get("selected_save_cell_full_id") != full
            or entry.get("selected_save_cell_id") != full.split(" ", 1)[1]):
        raise SaveSlotLoadError("save_list_link_unverified")
    found = [c for c in cells if isinstance(c, dict) and c.get("full_id") == full]
    if len(found) != 1:
        raise SaveSlotLoadError("save_selected_cell_ambiguous")
    if found[0].get("id") != entry["selected_save_cell_id"]:
        raise SaveSlotLoadError("save_list_link_unverified")
    complete_nodes(found[0])
    return found[0], entry
