from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[2]
PROBE_LUA = ROOT / "ff7r-telemetry-exporter" / "QueenBlood" / "Scripts" / "main.lua"
CATEGORY_LUA = ROOT / "ff7r-telemetry-exporter" / "QueenBlood" / "Scripts" / "card_catalogue.lua"


def probe_source() -> str:
    """The probe as written, with its section timing unwrapped:
    ``timed("name", name, args)`` reads as ``name(args)``."""
    source = PROBE_LUA.read_text(encoding="utf-8")
    source = re.sub(r'timed\("(\w+)", \1\)', r"\1()", source)
    return re.sub(r'timed\("(\w+)", \1, ', r"\1(", source)


def test_probe_publishes_the_canonical_harness_and_initiation_contract() -> None:
    source = probe_source()

    assert "schema_version = 1" in source
    assert "base_sequence = sequence" in source
    assert "begin_match_available = begin_match_available" in source
    assert "match_screen = active" in source
    assert "StaticFindObject(path)" in source
    assert "local function first_ui_object(classes)" in source
    assert "FindAllOf(class_name)" in source
    assert "ui, ui_class = first_ui_object(UI_CLASSES)" in source
    assert '"CardGame_Test_CardBackSmall_C"' in source
    assert '"CardGame_CardFrame_C"' in source
    assert '"CardGame_Card_Full_C"' in source
    assert '"EndCardGameMenu"' in source
    assert '"EndCardGameConfirmWindow"' in source
    assert '"CardGame_ConfirmWindow_C"' in source
    assert 'Do not fall back to FindAllOf("UserWidget")' in source
    assert "local function is_live_card_ui_instance(object)" in source
    assert "local function is_live_card_hand_root(object)" in source
    assert 'if class_name ~= "cardgame_top_c" and class_name ~= "cardgame_test_c" then' in source
    assert 'if #roots >= 2 or not is_live_card_hand_root(object) then return end' in source
    assert 'if is_live_card_hand_root(object) then\n                        append_root(object)' in source
    # Full UObject-array scans are cached; absent guessed classes are gone.
    assert "local HAND_SCAN_REFRESH_SECONDS = 10" in source
    assert "local roots_fresh = #cached_hand_roots > 0" in source
    assert "if all_valid then" in source
    ui_classes = source[source.index("local UI_CLASSES = {"):source.index("local UI_PATHS")]
    assert '"EndCardGameUI",' not in ui_classes
    assert '"CardGameUI",' not in ui_classes
    assert '"CardGame_Top_C",' in ui_classes
    assert 'for _, root_class in ipairs({"CardGame_Top_C", "CardGame_Test_C"})' in source
    assert "SpecifiedColor" not in source
    assert "widget_brush_state" not in source
    assert "widget_parent_visual_chain" not in source
    assert "entry.color_and_opacity_channels" not in source
    assert "local function debug_live_card_image_identity(known_root, allow_card_name_identity)" in source
    assert "debug_live_card_image_identity(ui, true)" in source
    assert "local identity_hand = encode_live_identity_hand()" in source
    assert "mulligan_banner_visible" not in source
    assert "visible_mulligan_message" not in source
    assert "widget_tree.FindWidget" not in source
    assert "GetAllWidgets" not in source
    assert "ForEachWidget" not in source
    assert 'lowered:find("blueprintgeneratedclass", 1, true)' in source
    assert 'lowered:find("/cardgame_top.cardgame_top_c:widgettree.", 1, true)' in source
    assert 'lowered:find("/cardgame_test.cardgame_test_c:widgettree.", 1, true)' in source
    assert 'lowered:find("endcardgamemenu /", 1, true)' in source
    assert 'lowered:find("cardgame_confirmwindow_c /", 1, true)' in source
    assert 'if is_live_card_ui_instance(object) then' in source
    assert 'write_debug(GAME_DEBUG_PATH, "ui_discovery", ui_class or "")' in source
    assert "local function reflected_array_items(value, maximum)" in source
    assert "value:GetArrayNum()" in source
    assert "value:ForEach" in source
    assert "local zero_based = nil" in source
    assert "normalized_index = normalized_index - 1" in source
    assert 'write_debug(GAME_DEBUG_PATH, "manager_hand_array_type", type(raw_objects))' in source
    assert 'write_debug(GAME_DEBUG_PATH, "manager_hand_array_count", #objects)' in source
    assert "local function encode_manager_hand(manager)" in source
    assert "local function card_identity_from_asset_path(value)" in source
    assert "CardFull_0N.Billboard_Thumbnail.ImagePath.AssetPathName" in source
    assert "card_unique_id = catalogue_entry" in source
    assert "card_name_id = catalogue_entry" in source
    assert "cost = catalogue_entry" in source
    assert "power = catalogue_entry" in source
    assert "effect_id = catalogue_entry" in source
    assert "effect_text_id = catalogue_entry" in source
    assert "effect_influence = catalogue_entry" in source
    assert "influence_square_count = catalogue_entry" in source
    assert "influence_offsets = catalogue_entry" in source
    assert "effect_influence_square_count = catalogue_entry" in source
    assert "effect_influence_offsets = catalogue_entry" in source
    assert "effect_target = catalogue_entry" in source
    assert "effect_type = catalogue_entry" in source
    assert "effect_int_parameters = catalogue_entry" in source
    assert "effect_text = catalogue_entry" in source
    assert "effect_text_source = catalogue_entry" in source
    assert "effect_type_name = catalogue_entry" in source
    assert "effect_target_name = catalogue_entry" in source
    assert "effect_text_label_references = catalogue_entry" in source
    assert "effect_heading_types = catalogue_entry" in source
    assert "effect_parameter_references = catalogue_entry" in source
    assert "effect_reference_counts = catalogue_entry" in source
    assert "effect_filter_tags = catalogue_entry" in source
    assert "effect_reference_count0 = catalogue_entry" in source
    assert "effect_card_definitions = effect_card_definitions(catalogue_entry)" in source
    assert 'name_source = resolved_name' in source
    assert '"CardGameCardInfo.NameID->Text/US/Resident_TxtRes"' in source
    assert '"hand_actor.CardIndex(slot_only)"' in source
    assert '"bPlayerTurnVisible"' in source
    assert '"bEnemyTurnVisible"' in source
    assert 'phase_field = "bPlayerTurnVisible/bEnemyTurnVisible"' in source
    assert 'local card_catalogue = {}' in source
    assert 'pcall(require, "card_catalogue")' in source
    assert '"EndTextResourceAPI"' not in source
    assert "\nregister_card_title_assignment_hook()\n" not in source
    assert "\nregister_card_image_assignment_hooks()\n" not in source
    assert '"CardHandActors", "PlayerCardHandActors"' in source
    assert '"CardIndex"' in source
    assert '"CardHandActors.CardIndex"' in source
    assert '"bCursorVisible"' in source
    assert '"manager.CardHandActors[].bCursorVisible"' in source
    assert 'local MULLIGAN_PANEL_NAME = "Pnl_Mulligan"' in source
    assert "read_mulligan_marker(visual_widget, widget_path)" in source
    assert "bSelectedForMulligan" not in source
    assert "read_boolean_field" not in source
    assert 'visual_widget:GetPropertyValue("Billboard_Thumbnail")' in source
    assert 'card_image_identity_resolved_direct' in source
    assert 'local direct_identity_complete = true' in source
    assert 'not manager_hand_seen and not direct_mulligan_marker' in source
    assert 'local LIVE_BILLBOARD_FALLBACK_ENABLED = false' in source
    assert 'local function live_identity_count()' in source
    assert 'card_identity_count = live_identity_count()' in source
    assert 'local visual_widget = live_full_widgets[slot]' in source
    assert 'visual_widget = child' in source
    assert 'if visual_widget ~= nil then' in source
    assert 'return widget.Visibility' in source
    assert 'local method = widget.GetVisibility' not in source
    assert "local function live_mulligan_marker_observed()" in source
    # The UI root is only looked up (and used) before a match is under way.
    assert 'and manager_match_in_progress(manager) == true' in source
    assert 'if ui ~= nil and not in_match then' in source
    assert 'identity_hand = encode_live_identity_hand()' in source
    assert 'hand = identity_hand' in source
    assert 'MULLIGAN_BANNER' not in source
    assert 'cursor_field = cursor_field' in source
    assert '"CurrentPhase"' not in source
    assert '"LegalMoves"' not in source


def test_talk_menu_lookup_does_not_pin_a_hidden_old_dialogue() -> None:
    source = probe_source()
    lookup = source[source.index("local function find_talk_menu()"):
                    source.index("local function widget_layout")]
    assert "if cached ~= nil and demand.talk_menu_shown(cached) == true then return cached end" in lookup
    assert 'FindAllOf("TalkMenu_00_C")' in lookup
    assert "if demand.talk_menu_shown(menu) == true then" in lookup
    assert "talk_menu_cache.menu = refs.path(menu)" in lookup
    assert "return cached" in lookup  # still throttles expensive global lookups


def test_visible_deck_top_fallback_is_bounded_and_guide_gated() -> None:
    source = probe_source()
    lookup = source[
        source.index("local function deck_screen_top(cached_only)"):
        source.index("local function register_deck_hooks()")
    ]
    assert 'FindAllOf("MainMenu_Card_TopNPC_C")' not in lookup  # class list drives the primary path
    assert 'FindAllOf("MainMenu_Card_Top_C")' in lookup
    assert 'card_ui_widget_shown(top) and deck_screen_guide_shown(top)' in lookup
    assert 'guide:find("Change Deck", 1, true)' in lookup
    assert 'guide:find("Jump to Card", 1, true)' in lookup
    assert 'guide:find("Craft Prebuilt Deck", 1, true)' in lookup
    cursor = source[source.index("local function scoped_snapshot("):source.index("demand.last_ticket = nil")]
    assert 'state = demand.encode_deck_select_snapshot(true)' in cursor
    assert 'state = demand.encode_deck_select_snapshot(false)' in cursor
    assert cursor.index('state = demand.encode_deck_select_snapshot(false)') < cursor.index('state = idle_snapshot(true)')


def test_ui_cache_requires_visible_widget_ancestry() -> None:
    source = probe_source()

    visibility = source[
        source.index("local function card_ui_widget_shown"):
        source.index("local function is_live_card_ui_instance")
    ]
    assert "widget:IsVisible()" in visibility
    assert "widget:GetRenderOpacity()" in visibility
    assert "widget:GetParent()" in visibility
    assert "object:IsInViewport()" in source
    assert "object:GetOuter()" in source

    lookup = source[
        source.index("local function first_ui_object(classes)"):
        source.index("local function find_manager()")
    ]
    assert lookup.index("cached_ui_object = nil") < lookup.index("if now - last_ui_lookup_at < UI_LOOKUP_PERIOD_SECONDS")
    assert "if valid(cached_ui_object) then return cached_ui_object" not in lookup


def test_probe_debug_helper_is_declared_before_reflection_reads_and_appends() -> None:
    source = probe_source()

    assert source.index("local function write_debug") < source.index("local function read_field")
    assert 'io.open(path, "ab")' in source
    assert "LoopAsync(SAMPLE_PERIOD_MS" in source
    assert "return false" in source


def test_probe_exports_bounded_per_slot_identity_and_mulligan_diagnostics() -> None:
    source = probe_source()

    assert "slot_diagnostics = {}" in source
    assert "live_card_probe.slot_diagnostics[slot] = slot_probe" in source
    assert "slot_probe.hand_index = slot - 1" in source
    assert "slot_probe.card_name_widget_found = valid(name_widget)" in source
    assert "slot_probe.card_name_text_read = card_name_text ~= nil" in source
    assert "slot_probe.card_name_identity_resolved = name_identity ~= nil" in source
    assert "slot_probe.asset_path_name_type" in source
    assert "slot_probe.asset_path_name_text = path_asset_text" in source
    assert "slot_probe.sub_path_string_text = path_sub_text" in source
    assert "slot_probe.mulligan_selected = marker" in source
    assert "slot_probe.mulligan_selection_source = marker_source" in source
    assert "slot_probe.widget_path = widget_path" in source
    assert "live_card_probe.deck_card_paths = deck_cards" in source
    assert "card_identity_from_asset_path(path_asset_text)" in source
    assert "card_name_text:sub(1, 128)" in source
    assert "path_asset_text:sub(1, 192)" in source


def test_mulligan_marker_resolves_only_the_authored_panel_for_one_card() -> None:
    source = probe_source()
    start = source.index("local function resolve_mulligan_panel")
    end = source.index("\nlocal function call_method", start)
    lookup = source[start:end]

    # Cooked CardGame_Card_Full: Img_Mulligan_Icon/Base are Pnl_Mulligan's
    # only slots, and every accepted candidate must carry the panel's FName.
    assert 'local MULLIGAN_PANEL_CHILD_FIELDS = {"Img_Mulligan_Icon", "Img_Mulligan_Base"}' in source
    assert "return child.Slot" in lookup
    assert "return panel_slot.Parent" in lookup
    assert "return card_widget.WidgetTree" in lookup
    assert 'StaticFindObject(tree_path .. "." .. MULLIGAN_PANEL_NAME)' in lookup
    assert 'StaticFindObject(card_path .. ".WidgetTree." .. MULLIGAN_PANEL_NAME)' in lookup
    assert "MULLIGAN_PANEL_RETRY_SECONDS" in lookup
    assert "GetWidgetFromName" not in lookup
    assert "FindWidget" not in lookup
    assert "FindAllOf" not in lookup
    assert "RootWidget" not in lookup

    accept = source[source.index("local function accept_mulligan_panel"):start]
    assert "if name ~= MULLIGAN_PANEL_NAME then" in accept

    start = source.index("local function read_mulligan_marker")
    end = source.index("\nlocal function live_card_full_widgets", start)
    marker = source[start:end]
    assert "local token = visibility_token(panel)" in marker
    assert "local visible = mulligan_panel_marker(token)" in marker
    assert "if visible == nil then return nil, nil, visuals end" in marker
    assert "IsVisible" not in marker
    assert "RenderOpacity" not in marker
    assert "SpecifiedColor" not in marker
    # Only the two animation-keyed values decode; the deal-time default (4)
    # and every other ESlateVisibility value stay unknown.
    assert "local MULLIGAN_PANEL_VISIBILITY = {[0] = true, [1] = false}" in source
    assert 'lowered:find("visible", 1, true)' not in source


def test_live_card_identity_maps_the_whole_cardfull_pool() -> None:
    source = probe_source()
    start = source.index("scan_live_card_full_widgets = function")
    end = source.index("\nlocal function first_ui_object", start)
    scan = source[start:end]

    # CardFull_01..15 is one pool; hand actors name their widget by CardIndex.
    assert "local CARD_WIDGET_POOL_SIZE = 15" in source
    assert "if slot >= 1 and slot <= CARD_WIDGET_POOL_SIZE then return slot - 1 end" in source
    assert 'widget_names[slot] = "CardFull_" .. tostring(raw_slot)' in scan
    assert "widget_priorities" not in scan
    assert "local identity = live_card_identity_by_slot[card_index]" in source
    assert "if card_index ~= nil and card_index >= 0 then" in source


def test_live_identity_hand_exports_catalogue_attributes_without_the_manager() -> None:
    source = probe_source()
    start = source.index("local function encode_live_identity_hand()")
    end = source.index("\n-- A selected Pnl_Mulligan", start)
    hand_encoder = source[start:end]

    for field in (
        "cost", "power", "legal_positions", "influence", "note_id", "effect_id",
        "effect_text_id", "effect_influence", "effect_constant", "effect_target",
        "effect_type", "effect_int_parameters", "effect_text_parameters",
        "effect_reference_text_id", "effect_card_definitions", "influence_offsets",
        "effect_influence_offsets",
    ):
        assert f"{field} =" in hand_encoder


def test_soft_object_path_is_never_coerced_to_a_string() -> None:
    source = probe_source()

    # FSoftObjectPath is a reflected struct in UE4SS, not a UObject with a
    # bound ToString method. Read its two named fields, and log only its type.
    assert "text(image_path)" not in source
    assert "opaque_token(image_path)" not in source
    assert "image_path.AssetPathName" in source
    assert "image_path.SubPathString" in source


def test_card_catalogue_contains_authoritative_identity_joins() -> None:
    source = CATEGORY_LUA.read_text(encoding="utf-8")
    entries = re.findall(
        r'\["(Card_\d{3})"\]\s*=\s*\{\s*card_id\s*=\s*"(card_id_\d{3})".*?unique_id\s*=\s*(\d+).*?name_id\s*=\s*"([^\"]+)".*?name\s*=\s*"([^\"]+)"',
        source,
    )
    assert len(entries) == 146
    assert ("Card_000", "card_id_000", "1", "$en_SecuritySoldier_Standard", "Security Officer") in entries
    assert ("Card_006", "card_id_006", "7", "$en_Levrikon_Standard", "Levrikon") in entries
    assert ("Card_010", "card_id_010", "11", "$en_Elfadunk_StandardEnd2", "Elphadunk") in entries
    assert ("Card_117", "card_id_117", "111", "$M_SMN_109_atk001", "Magic Pot") in entries


def test_card_catalogue_contains_authoritative_attributes() -> None:
    source = CATEGORY_LUA.read_text(encoding="utf-8")

    assert 'card_id = "card_id_005"' in source
    assert 'cost = 2, power = 2' in source
    assert 'influence = "DownRight4"' in source
    assert 'influence_square_count = 2' in source
    assert 'influence_offsets = { { row = 1, column = 0 }, { row = 1, column = 1 } }' in source
    assert 'effect_id = "debuff_biorat"' in source
    assert 'effect_text_id = "$cardtext_card_Ability_BuffDebuff_OneShot_Down"' in source
    assert 'effect_influence = "DownRight1"' in source
    assert 'effect_influence_square_count = 1' in source
    assert 'effect_influence_offsets = { { row = 1, column = 1 } }' in source
    assert 'effect_constant = 0' in source
    assert 'effect_int_parameters = {-3}' in source
    assert 'effect_target = 3' in source
    assert 'effect_type = 0' in source
    assert 'effect_text = "When played, lower the power of allied and enemy cards on affected tiles by 3."' in source
    assert 'effect_type_name = "immediate_effect"' in source
    assert 'effect_target_name = "allied_and_enemy_cards"' in source
    assert 'effect_heading_types = {1, 2}' in source
    assert 'effect_parameter_references = {0, 5}' in source
    assert 'effect_filter_tags = {1, 9, 9, 9}' in source
    assert 'effect_reference_count0 = 0' in source
    assert 'auxiliary_cards = {' in source
    assert 'card_id = "card_id_Token_015"' in source
    assert 'name = "Mandragora Minion"' in source


def test_card_catalogue_covers_main_and_auxiliary_rows() -> None:
    source = CATEGORY_LUA.read_text(encoding="utf-8")
    main_entries = re.findall(r'^    \["Card_\d{3}"\]', source, re.MULTILINE)
    auxiliary_entries = re.findall(r'^    \["[^\"]+"\]', source, re.MULTILINE)
    assert len(main_entries) == 146
    assert len(auxiliary_entries) == 146 + 89
    assert source.count('effect_text = "') >= 146
    assert source.count("effect_type_name = ") >= 130
