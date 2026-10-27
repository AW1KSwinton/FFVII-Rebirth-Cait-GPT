"""Positive field Main Menu evidence for bounded map-travel recovery.

The pause flag and the cached ``in_menu`` flag are never Cancel permission.
Use the established save-menu header/footer reader plus fresh root visibility.
All other screen owners, partial scans and unknown attachment flags stop input.
"""
from __future__ import annotations

import math
from typing import Any

from .conversation_state import field_blocker, has_field_telemetry
from .save_slot_load import SaveMenuScreen, TEXT_CAP


TITLE_SUFFIX = ".MainMenu_Base.WidgetTree.Txt_Title"
NATIVE_TITLE_SUFFIX = ".MainMenu_Top_Window.WidgetTree.TopContent2.WidgetTree.Txt_MainTitle"
MAIN_MENU_TITLES = frozenset({"$menu_mainmenu_0000_0000", "Main Menu"})
# Native 2026-10-23: these nonmodal field HUD roots stay attached behind the
# Main Menu. They never identify a menu and are accepted only in field context.
FIELD_HUD_ROOTS = frozenset({"Command_C", "NaviMap_End2_C"})
# These widgets may coexist with a cached Main Menu title or footer. Their
# text is a refusal even if the root census missed the owning screen.
OTHER_OWNER_PATHS = (
    ".BattleTips.", ".BattleTutorial.", ".MainMenu_Tutorial_",
    ".MainMenu_SaveLoad.", ".MainMenu_Card_", ".Shop.", ".Shop_",
    ".Com_Window", ".TalkMenu", ".CardGame", ".Pause_00.",
    ".Cinema_Skip", ".AreaMap.", ".WorldMap.",
)


def field_identity(base: dict[str, Any]) -> tuple[Any, ...]:
    return (base.get("source"), base.get("map_id"),
            (base.get("player") or {}).get("actor_id"))


def context_blocker(base: dict[str, Any]) -> str | None:
    blocker = field_blocker(base)
    if blocker:
        return blocker
    if base.get("running") is not True or (base.get("telemetry") or {}).get("live") is not True:
        return "telemetry_unavailable"
    if base.get("source") in (None, "simulation", "unknown"):
        return "live_source_unavailable"
    if not has_field_telemetry(base):
        return "field_telemetry_unavailable"
    position = base["player"]["position"]
    if not all(math.isfinite(position[axis]) for axis in ("x", "y", "z")):
        return "field_telemetry_unavailable"
    if base.get("in_combat") is not False or base.get("target") is not None:
        return "combat_or_target_active"
    if base.get("loading") is not False:
        return "loading_or_unknown"
    qb = base.get("queen_blood")
    # The ordinary field feed may omit the on-demand card probe. In that
    # case the root census below must still prove there is no card UI owner.
    if qb is not None and not isinstance(qb, dict):
        return "queen_blood_or_unknown"
    qb = qb or {}
    if any(qb.get(key) is not None and qb.get(key) is not False for key in
           ("active", "match_screen", "begin_match_available")):
        return "queen_blood_or_unknown"
    placement = qb.get("placement")
    if placement is not None and not isinstance(placement, dict):
        return "queen_blood_or_unknown"
    if (placement or {}).get("active") is True:
        return "queen_blood_placement_active"
    return None


def identify_main_menu(menu: dict[str, Any], roots: Any, base: dict[str, Any],
                       cinema: dict[str, Any]) -> dict[str, Any]:
    """Diagnostic proof; the normal input guard is still required per press."""
    result: dict[str, Any] = {"identified": False, "reason": "main_menu_unidentified"}
    blocker = context_blocker(base)
    if blocker:
        return {**result, "reason": blocker}
    if base.get("paused") is not True or type(base.get("in_menu")) is not bool:
        return {**result, "reason": "pause_not_observed"}
    if (cinema.get("observed") is not True or cinema.get("active") is not False
            or cinema.get("anim_cutscene") is not False or cinema.get("playing") != []):
        return {**result, "reason": "cutscene_or_unknown"}
    screen = SaveMenuScreen(menu, [])
    title = [(path, text) for path, text in screen.texts
             if path.endswith((TITLE_SUFFIX, NATIVE_TITLE_SUFFIX)) and text in MAIN_MENU_TITLES]
    result["main_menu_text_seen"] = bool(title)
    if (len(screen.texts) >= TEXT_CAP or len(title) != 1 or screen.main_menu_item is None
            or screen.signals or screen.save_load_help
            or any(token in path for path, _ in screen.texts for token in OTHER_OWNER_PATHS)):
        return result
    if (not isinstance(roots, dict) or roots.get("observed") is not True
            or roots.get("configured_complete") is not True or roots.get("truncated") is not False):
        return {**result, "reason": "ui_roots_incomplete"}
    widgets = roots.get("widgets")
    if not isinstance(widgets, list) or any(not isinstance(w, dict) for w in widgets):
        return {**result, "reason": "ui_roots_invalid"}
    menus = []
    for widget in widgets:
        if widget.get("configured_root") is True:
            details = widget.get("details")
            attached = details.get("in_viewport_ex") if isinstance(details, dict) else None
            if type(attached) is not bool:
                return {**result, "reason": "ui_owner_unknown"}
            if attached:
                if (widget.get("class") != "MainMenu_Base_C" or widget.get("is_visible") is not True
                        or not isinstance(widget.get("path"), str) or not widget["path"]):
                    return {**result, "reason": "other_ui_owner", "owner": widget.get("class")}
                menus.append(widget)
        # A generic child can be visible under a different owning screen
        # while IsInViewport is false. Without extended attachment evidence,
        # never infer that this unknown widget is harmless menu decoration.
        else:
            details = widget.get("details")
            attached = details.get("in_viewport_ex") if isinstance(details, dict) else None
            if widget.get("in_viewport") is False and attached is False:
                continue
            if (attached is True and widget.get("in_viewport") is False
                    and widget.get("class") in FIELD_HUD_ROOTS):
                continue
            # The native title/footer can be managed as sibling widgets of
            # MainMenu_Base. Bind those two known components to their actual
            # text owner; their class name or a retained text alone is no proof.
            root_path = str(widget.get("path") or "").split(" ", 1)[-1]
            suffix = (NATIVE_TITLE_SUFFIX if widget.get("class") == "MainMenu_Top_Window_C"
                      else ".Foot.WidgetTree.Txt_Help" if widget.get("class") in {"Foot_C", "MainMenu_Foot_C"} else None)
            matching = [item for item in menu.get("texts") or [] if isinstance(item, dict)
                        and item.get("rendered") is True and suffix
                        and str(item.get("path") or "").endswith(suffix)
                        and str(item.get("full_id") or "").split(" ", 1)[-1].startswith(root_path + ".WidgetTree.")]
            if (attached is True and root_path and len(matching) == 1
                    and (suffix != NATIVE_TITLE_SUFFIX or matching[0].get("text") in MAIN_MENU_TITLES)):
                continue
            if widget.get("in_viewport") is not False or widget.get("is_visible") is not False:
                return {**result, "reason": "ui_owner_unknown", "owner": widget.get("class")}
    if len(menus) != 1:
        return {**result, "reason": "main_menu_absent_or_ambiguous"}
    return {**result, "identified": True, "reason": "field_main_menu_identified",
            "menu_path": menus[0].get("path"), "title_path": title[0][0],
            "selected_item": screen.main_menu_item}
